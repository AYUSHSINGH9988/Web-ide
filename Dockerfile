# ---- Ayuprime Web IDE — Koyeb free-tier Dockerfile ----
FROM python:3.10-slim

# bash is NOT included in slim by default — required by the pty terminal.
# build-essential + gcc/g++ let the IDE compile/run C & C++ files.
# Kept minimal on purpose: Koyeb's free instance has 512MB RAM / shared vCPU.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        gcc \
        g++ \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (Koyeb, like HF Spaces, runs containers as non-root by default)
RUN useradd -m -u 1000 appuser
WORKDIR /home/appuser/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

USER appuser

# Koyeb sets PORT at runtime; app.py reads it via os.environ.get("PORT", 8000)
EXPOSE 8000

# Lightweight healthcheck hitting the /health route added in app.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

CMD ["python3", "app.py"]
