import os
import pty
import signal
import shutil
import asyncio
import subprocess
import traceback
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()
BASE_DIR = Path.cwd()  # Root directory jahan app chalegi

# Pick whichever shell actually exists in this image.
# python:3.10-slim usually does NOT ship /bin/bash, only /bin/sh.
SHELL_BIN = shutil.which("bash") or shutil.which("sh") or "/bin/sh"


class FileData(BaseModel):
    path: str
    content: str


class PathData(BaseModel):
    path: str


# ==========================================
# 1. FRONTEND: Web UI (HTML + Tailwind + Monaco + Xterm)
# ==========================================
@app.get("/")
async def get_ide():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ayuprime Web IDE</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <!-- Monaco Editor (VS Code Engine) -->
        <script src="https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.36.1/min/vs/loader.min.js"></script>
        <!-- Xterm.js (Terminal) -->
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.css" />
        <script src="https://cdn.jsdelivr.net/npm/xterm/lib/xterm.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit/lib/xterm-addon-fit.js"></script>

        <style>
            ::-webkit-scrollbar { width: 8px; height: 8px; }
            ::-webkit-scrollbar-track { background: #1e1e1e; }
            ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 4px; }
            ::-webkit-scrollbar-thumb:hover { background: #6b7280; }
        </style>
    </head>
    <body class="bg-[#1e1e1e] text-gray-300 h-screen flex overflow-hidden font-sans">

        <!-- Sidebar: File Explorer -->
        <div class="w-64 bg-[#252526] border-r border-[#333333] flex flex-col">
            <div class="p-3 bg-[#333333] text-sm font-bold text-gray-100 flex justify-between items-center">
                <span>EXPLORER</span>
                <button onclick="loadFiles()" class="hover:text-emerald-400" title="Refresh">🔄</button>
            </div>

            <div class="flex p-2 gap-2 border-b border-[#333333] text-xs">
                <button onclick="newFile()" class="flex-1 bg-[#3a3d41] hover:bg-[#505357] py-1 rounded">📄 File</button>
                <button onclick="newFolder()" class="flex-1 bg-[#3a3d41] hover:bg-[#505357] py-1 rounded">📁 Folder</button>
                <label class="flex-1 bg-[#3a3d41] hover:bg-[#505357] py-1 rounded text-center cursor-pointer">
                    📤 Upload
                    <input type="file" id="file-upload" class="hidden" onchange="uploadFile(event)">
                </label>
            </div>

            <div id="file-list" class="flex-1 overflow-y-auto p-2 text-sm space-y-1"></div>
        </div>

        <!-- Main Content area -->
        <div class="flex-1 flex flex-col">
            <!-- Topbar -->
            <div class="h-10 bg-[#1e1e1e] flex items-center px-4 justify-between border-b border-[#333333]">
                <div id="current-file" class="text-emerald-400 text-sm font-mono tracking-wide">No file opened</div>
                <div class="flex gap-3">
                    <button onclick="saveFile()" class="bg-blue-600 hover:bg-blue-500 text-white px-3 py-1 rounded text-xs font-bold transition">💾 Save (Ctrl+S)</button>
                    <button onclick="runFile()" class="bg-emerald-600 hover:bg-emerald-500 text-white px-3 py-1 rounded text-xs font-bold transition">▶ Run Code</button>
                    <button onclick="stopFile()" class="bg-red-600 hover:bg-red-500 text-white px-3 py-1 rounded text-xs font-bold transition">■ Stop</button>
                </div>
            </div>

            <!-- Code Editor (Monaco) -->
            <div id="editor-container" class="flex-1"></div>

            <!-- Terminal (Xterm) -->
            <div class="h-64 border-t border-[#333333] flex flex-col bg-[#1e1e1e]">
                <div class="h-8 bg-[#252526] px-4 flex items-center text-xs font-bold text-gray-400 justify-between">
                    <span>TERMINAL</span>
                    <span id="term-status" class="text-yellow-400 text-[10px]">🟡 Connecting...</span>
                </div>
                <div id="terminal" class="flex-1 p-2"></div>
            </div>
        </div>

        <script>
            let currentFilePath = "";
            let editor;

            // 1. Monaco Editor
            require.config({ paths: { 'vs': 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.36.1/min/vs' }});
            require(['vs/editor/editor.main'], function() {
                editor = monaco.editor.create(document.getElementById('editor-container'), {
                    value: '// Ayuprime IDE Ready\\n// Select or create a file to start coding...',
                    language: 'javascript',
                    theme: 'vs-dark',
                    automaticLayout: true,
                    fontSize: 14,
                    autoClosingBrackets: 'always',
                    autoClosingQuotes: 'always',
                    minimap: { enabled: false }
                });
                editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function() {
                    saveFile();
                });
            });

            // 2. Terminal (Xterm.js)
            const term = new Terminal({
                theme: { background: '#1e1e1e' },
                fontFamily: 'monospace',
                fontSize: 13,
                cursorBlink: true
            });
            const fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
            term.open(document.getElementById('terminal'));
            fitAddon.fit();

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            let ws;
            let pingInterval;
            let reconnectDelay = 1000;

            function setStatus(text, cls) {
                const el = document.getElementById('term-status');
                el.innerText = text;
                el.className = `${cls} text-[10px]`;
            }

            function connectTerminal() {
                setStatus('🟡 Connecting...', 'text-yellow-400');
                ws = new WebSocket(`${protocol}//${window.location.host}/ws/term`);

                ws.onopen = () => {
                    term.writeln('\\x1b[32m[Terminal Connected - Ayuprime System]\\x1b[0m');
                    setStatus('🟢 Connected', 'text-emerald-400');
                    reconnectDelay = 1000;
                    // Keep-alive ping so proxies/load-balancers don't idle-kill the socket
                    pingInterval = setInterval(() => {
                        if (ws.readyState === WebSocket.OPEN) ws.send('\\x00__ping__');
                    }, 25000);
                };

                ws.onmessage = (e) => term.write(e.data);

                ws.onerror = () => {
                    setStatus('🔴 Error', 'text-red-400');
                };

                ws.onclose = () => {
                    clearInterval(pingInterval);
                    setStatus('🔴 Disconnected', 'text-red-400');
                    term.writeln('\\n\\x1b[31m[Connection Lost - retrying...]\\x1b[0m');
                    setTimeout(connectTerminal, reconnectDelay);
                    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
                };
            }
            connectTerminal();

            term.onData(data => {
                if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
            });

            window.addEventListener('resize', () => fitAddon.fit());

            // 3. File System Functions
            async function loadFiles(path = ".") {
                const res = await fetch(`/api/fs?path=${path}`);
                const data = await res.json();
                const list = document.getElementById('file-list');
                list.innerHTML = "";

                if (path !== ".") {
                    const parent = path.split('/').slice(0, -1).join('/') || '.';
                    list.innerHTML += `<div onclick="loadFiles('${parent}')" class="cursor-pointer hover:bg-[#37373d] p-1 rounded text-blue-400">📁 .. (Go Back)</div>`;
                }

                data.files.forEach(f => {
                    const icon = f.is_dir ? "📁" : "📄";
                    const fullPath = path === "." ? f.name : `${path}/${f.name}`;
                    const color = f.is_dir ? "text-blue-300" : "text-gray-300";

                    const div = document.createElement('div');
                    div.className = `cursor-pointer hover:bg-[#37373d] p-1 rounded ${color} truncate`;
                    div.innerText = `${icon} ${f.name}`;

                    div.onclick = () => {
                        if (f.is_dir) loadFiles(fullPath);
                        else openFile(fullPath);
                    };
                    list.appendChild(div);
                });
            }

            async function openFile(path) {
                const res = await fetch(`/api/file?path=${path}`);
                const data = await res.json();
                if (data.error) return alert(data.error);

                currentFilePath = path;
                document.getElementById('current-file').innerText = path;
                editor.setValue(data.content);

                const ext = path.split('.').pop();
                const langMap = { 'py': 'python', 'js': 'javascript', 'ts': 'typescript', 'json': 'json', 'html': 'html', 'css': 'css', 'sh': 'shell', 'c': 'c', 'cpp': 'cpp', 'java': 'java', 'go': 'go', 'rb': 'ruby', 'php': 'php', 'rs': 'rust' };
                monaco.editor.setModelLanguage(editor.getModel(), langMap[ext] || 'plaintext');
            }

            async function saveFile() {
                if (!currentFilePath) return alert("No file opened!");
                const content = editor.getValue();
                const res = await fetch('/api/file', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: currentFilePath, content: content })
                });
                if (res.ok) {
                    const btn = document.querySelector('button[onclick="saveFile()"]');
                    const origText = btn.innerText;
                    btn.innerText = "✅ Saved!";
                    btn.classList.replace('bg-blue-600', 'bg-green-600');
                    setTimeout(() => {
                        btn.innerText = origText;
                        btn.classList.replace('bg-green-600', 'bg-blue-600');
                    }, 1000);
                } else alert("Failed to save!");
            }

            async function newFile() {
                const name = prompt("Enter file name (e.g. main.py):");
                if (!name) return;
                await fetch('/api/touch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: name })
                });
                loadFiles();
                openFile(name);
            }

            async function newFolder() {
                const name = prompt("Enter folder name:");
                if (!name) return;
                await fetch('/api/mkdir', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: name })
                });
                loadFiles();
            }

            async function uploadFile(e) {
                const file = e.target.files[0];
                if (!file) return;
                const formData = new FormData();
                formData.append("file", file);
                formData.append("path", file.name);
                await fetch('/api/upload', { method: 'POST', body: formData });
                loadFiles();
            }

            // 4. Extension-based Run Logic
            const RUNNERS = {
                py:   f => `python3 -u "${f}"`,
                js:   f => `node "${f}"`,
                ts:   f => `npx ts-node "${f}"`,
                sh:   f => `bash "${f}"`,
                c:    f => `gcc "${f}" -o /tmp/a.out && /tmp/a.out`,
                cpp:  f => `g++ "${f}" -o /tmp/a.out && /tmp/a.out`,
                java: f => `javac "${f}" && java -cp "$(dirname "${f}")" "$(basename "${f}" .java)"`,
                go:   f => `go run "${f}"`,
                rb:   f => `ruby "${f}"`,
                php:  f => `php "${f}"`,
                rs:   f => `rustc "${f}" -o /tmp/a.out && /tmp/a.out`,
            };

            function runFile() {
                if (!currentFilePath) return alert("No file selected!");
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    alert("❌ Terminal not connected yet. Wait a second and try again.");
                    return;
                }
                saveFile();

                const ext = currentFilePath.split('.').pop().toLowerCase();
                const builder = RUNNERS[ext];
                const cmd = builder ? builder(currentFilePath) : `./"${currentFilePath}"`;

                term.writeln(`\\x1b[33m▶ Running: ${cmd}\\x1b[0m`);
                ws.send(cmd + "\\r");
            }

            function stopFile() {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send('\\x03'); // Ctrl+C into the pty
                    term.writeln('\\x1b[31m[Sent Ctrl+C]\\x1b[0m');
                }
            }

            // Init
            loadFiles();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# Koyeb health check endpoint (lightweight, no disk/pty access)
@app.get("/health")
async def health():
    return {"status": "ok"}


# ==========================================
# 2. BACKEND: File System APIs
# ==========================================
@app.get("/api/fs")
async def list_files(path: str = "."):
    target = BASE_DIR / path
    if not target.exists() or not target.is_dir():
        return {"files": []}

    files = []
    for item in target.iterdir():
        if item.name.startswith("."):
            continue
        files.append({"name": item.name, "is_dir": item.is_dir()})
    files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"files": files}


@app.get("/api/file")
async def read_file(path: str):
    try:
        content = (BASE_DIR / path).read_text(encoding="utf-8")
        return {"content": content}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/file")
async def save_file(data: FileData):
    try:
        (BASE_DIR / data.path).write_text(data.content, encoding="utf-8")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/touch")
async def create_file(data: PathData):
    (BASE_DIR / data.path).touch(exist_ok=True)
    return {"status": "ok"}


@app.post("/api/mkdir")
async def create_folder(data: PathData):
    (BASE_DIR / data.path).mkdir(parents=True, exist_ok=True)
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_file(path: str = Form(...), file: UploadFile = File(...)):
    target = BASE_DIR / path
    with open(target, "wb") as f:
        f.write(await file.read())
    return {"status": "ok"}


# ==========================================
# 3. BACKEND: Pseudo-Terminal (WebSocket)
# ==========================================
@app.websocket("/ws/term")
async def terminal_endpoint(websocket: WebSocket):
    await websocket.accept()

    master = slave = None
    process = None
    loop = asyncio.get_running_loop()

    try:
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [SHELL_BIN],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=str(BASE_DIR),
            env={**os.environ, "TERM": "xterm-256color"},
            preexec_fn=os.setsid,  # own process group -> Ctrl+C only kills the child tree
        )
        # Parent doesn't need the slave end once the child has it
        os.close(slave)
        slave = None

        def read_from_pty():
            try:
                data = os.read(master, 4096)
                if data:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_text(data.decode("utf-8", "replace")), loop
                    )
                else:
                    loop.remove_reader(master)
            except OSError:
                try:
                    loop.remove_reader(master)
                except Exception:
                    pass

        loop.add_reader(master, read_from_pty)

        while True:
            text = await websocket.receive_text()
            if text == "\x00__ping__":
                continue  # keep-alive, no-op
            if text == "\x03":
                # Send Ctrl+C to the foreground process group in the pty
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except Exception:
                    pass
                continue
            os.write(master, text.encode("utf-8"))

    except WebSocketDisconnect:
        pass
    except Exception:
        # Surface the real error into the terminal instead of failing silently
        err = traceback.format_exc()
        try:
            await websocket.send_text(f"\r\n\x1b[31m[Server Error]\x1b[0m\r\n{err}\r\n")
        except Exception:
            pass
    finally:
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except Exception:
                pass
        for fd in (master, slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    # Koyeb injects PORT; keep a sane local default for other hosts.
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        workers=1,          # free tier = 1 vCPU / 512MB, extra workers just waste RAM
        log_level="info",
    )
