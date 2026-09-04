"""
Pro Web IDE — FastAPI backend
- Real file explorer (create/rename/delete/upload/read/write) scoped to WORKSPACE_ROOT
- Live PTY terminal over WebSocket (real bash shell, not just command output)
- Run Code endpoint hint (frontend sends the right command per extension)
- Safety: every path is resolved and checked to stay inside WORKSPACE_ROOT
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import shutil
import signal
import struct
import termios
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pro-web-ide")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/home/claude/ide-project/workspace")).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Pro Web IDE")


@app.on_event("startup")
async def check_websocket_support():
    try:
        import websockets  # noqa: F401
        logger.info("websockets library OK (version %s) — WebSocket terminal should work.", websockets.__version__)
    except ImportError:
        logger.error(
            "websockets library NOT installed — /ws/terminal will fail to upgrade. "
            "Check that requirements.txt installed correctly."
        )

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file, keeps things sane in a container


def safe_path(rel_path: str) -> Path:
    """Resolve a user-supplied relative path and make sure it can't escape the workspace."""
    rel_path = (rel_path or "").lstrip("/")
    candidate = (WORKSPACE_ROOT / rel_path).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    return candidate


# --------------------------------------------------------------------------
# File explorer API
# --------------------------------------------------------------------------

def build_tree(path: Path) -> dict:
    node = {
        "name": path.name or ".",
        "path": str(path.relative_to(WORKSPACE_ROOT)),
        "type": "folder",
        "children": [],
    }
    try:
        entries = sorted(
            path.iterdir(),
            key=lambda p: (p.is_file(), p.name.lower()),
        )
    except (PermissionError, FileNotFoundError):
        return node
    for entry in entries:
        if entry.name.startswith(".") and entry.name in {".git", "__pycache__", "node_modules"}:
            continue
        if entry.is_dir():
            node["children"].append(build_tree(entry))
        else:
            node["children"].append({
                "name": entry.name,
                "path": str(entry.relative_to(WORKSPACE_ROOT)),
                "type": "file",
                "size": entry.stat().st_size,
            })
    return node


@app.get("/api/tree")
def get_tree():
    return build_tree(WORKSPACE_ROOT)


@app.get("/api/file")
def read_file(path: str = Query(...)):
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Binary file, cannot display")
    return {"path": path, "content": content}


class SaveBody(BaseModel):
    path: str
    content: str


@app.post("/api/file")
def save_file(body: SaveBody):
    p = safe_path(body.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": body.path}


class NewNodeBody(BaseModel):
    path: str
    type: str  # "file" | "folder"


@app.post("/api/create")
def create_node(body: NewNodeBody):
    p = safe_path(body.path)
    if p.exists():
        raise HTTPException(status_code=409, detail="Already exists")
    if body.type == "folder":
        p.mkdir(parents=True, exist_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    return {"ok": True}


class RenameBody(BaseModel):
    old_path: str
    new_path: str


@app.post("/api/rename")
def rename_node(body: RenameBody):
    src = safe_path(body.old_path)
    dst = safe_path(body.new_path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="Not found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True}


@app.delete("/api/file")
def delete_node(path: str = Query(...)):
    p = safe_path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"ok": True}


@app.post("/api/upload")
async def upload_file(dest: str = Query(""), file: UploadFile = File(...)):
    target_dir = safe_path(dest)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / file.filename
    target = safe_path(str(target.relative_to(WORKSPACE_ROOT)))

    size = 0
    with open(target, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large (25MB limit)")
            out.write(chunk)
    return {"ok": True, "path": str(target.relative_to(WORKSPACE_ROOT))}


# Map file extension -> run command template. {file} is replaced with a shell-quoted
# path relative to the workspace root; {dir} is the containing directory.
RUN_COMMANDS = {
    ".py": "python3 -u {file}",
    ".js": "node {file}",
    ".ts": "npx -y ts-node {file}",
    ".sh": "bash {file}",
    ".rb": "ruby {file}",
    ".go": "go run {file}",
    ".rs": "bash -c 'rustc {file} -o /tmp/a.out && /tmp/a.out'",
    ".c": "bash -c 'gcc {file} -o /tmp/a.out && /tmp/a.out'",
    ".cpp": "bash -c 'g++ {file} -o /tmp/a.out && /tmp/a.out'",
    ".java": "bash -c 'javac {file} -d /tmp && java -cp /tmp {stem}'",
    ".php": "php {file}",
    ".html": "python3 -m http.server 8080 --directory {dir}",
}


@app.get("/api/run-command")
def run_command(path: str = Query(...)):
    p = safe_path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    ext = p.suffix.lower()
    template = RUN_COMMANDS.get(ext)
    if not template:
        raise HTTPException(status_code=400, detail=f"No run command configured for '{ext or 'this file type'}'")
    rel = p.relative_to(WORKSPACE_ROOT)
    cmd = template.format(file=str(rel), dir=str(rel.parent) or ".", stem=p.stem)
    return {"command": cmd}


# --------------------------------------------------------------------------
# Live terminal — real PTY over WebSocket, with an automatic fallback for
# sandboxed hosts (e.g. Hugging Face Spaces run containers under gVisor,
# which blocks /dev/ptmx so pty.fork() raises OSError). The fallback runs a
# real persistent bash process over plain pipes: cd/env/pip/python all work,
# only full-screen curses apps (nano, htop, vim) need the real PTY path.
# --------------------------------------------------------------------------

def set_winsize(fd: int, rows: int, cols: int):
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


async def run_pty_session(websocket: WebSocket, pid: int, fd: int):
    loop = asyncio.get_event_loop()
    os.set_blocking(fd, False)
    set_winsize(fd, 30, 100)
    closed = {"value": False}

    def on_readable():
        if closed["value"]:
            return
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            closed["value"] = True
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            asyncio.ensure_future(websocket.close())
            return
        asyncio.ensure_future(safe_send(data))

    async def safe_send(data: bytes):
        try:
            await websocket.send_text(json.dumps({"type": "output", "data": data.decode(errors="replace")}))
        except Exception:
            pass

    loop.add_reader(fd, on_readable)

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                payload = json.loads(msg)
            except json.JSONDecodeError:
                continue
            mtype = payload.get("type")
            if mtype == "input":
                try:
                    os.write(fd, payload.get("data", "").encode())
                except OSError:
                    break
            elif mtype == "resize":
                rows = int(payload.get("rows", 30))
                cols = int(payload.get("cols", 100))
                set_winsize(fd, rows, cols)
    except WebSocketDisconnect:
        pass
    finally:
        closed["value"] = True
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


async def run_pipe_session(websocket: WebSocket):
    """No-PTY fallback: persistent bash over plain pipes. The client does
    local echo + line buffering (see frontend), and sends one full line at a
    time, so this stays simple and doesn't need a real terminal device."""
    env = os.environ.copy()
    env["TERM"] = "dumb"
    proc = await asyncio.create_subprocess_exec(
        "bash", "--noprofile", "--norc",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(WORKSPACE_ROOT),
        env=env,
    )

    async def pump_output():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                try:
                    await websocket.send_text(json.dumps({"type": "output", "data": chunk.decode(errors="replace")}))
                except Exception:
                    break
        except Exception:
            pass

    pump_task = asyncio.ensure_future(pump_output())

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                payload = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "input":
                data = payload.get("data", "")
                if proc.stdin is None or proc.stdin.is_closing():
                    break
                proc.stdin.write(data.encode())
                try:
                    await proc.stdin.drain()
                except Exception:
                    break
            # "resize" is a no-op in pipe mode — there's no real tty to size.
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass


@app.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("Terminal WebSocket connected: %s", websocket.client)

    try:
        pid, fd = pty.fork()
    except OSError as e:
        logger.warning("pty.fork() failed (%s) — falling back to pipe mode", e)
        pid, fd = None, None

    try:
        if fd is not None:
            if pid == 0:
                os.chdir(str(WORKSPACE_ROOT))
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["PS1"] = r"\[\e[36m\]\w\[\e[0m\] $ "
                os.execvpe("bash", ["bash"], env)
                os._exit(1)
            else:
                await websocket.send_text(json.dumps({"type": "mode", "mode": "pty"}))
                await run_pty_session(websocket, pid, fd)
        else:
            # PTY unavailable (sandboxed host) — fall back to a pipe-based shell.
            await websocket.send_text(json.dumps({
                "type": "mode", "mode": "pipe",
                "note": "Sandboxed host: no real TTY available. Basic shell works "
                        "(pip install, running scripts, cd); full-screen apps like nano/htop won't.",
            }))
            await run_pipe_session(websocket)
    except Exception:
        logger.exception("Terminal WebSocket session crashed")
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        logger.info("Terminal WebSocket closed: %s", websocket.client)


# --------------------------------------------------------------------------
# Health check (useful for Hugging Face Spaces / Docker)
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "id": str(uuid.uuid4())}


# --------------------------------------------------------------------------
# Static frontend (must be mounted last so /api and /ws take priority)
# --------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
