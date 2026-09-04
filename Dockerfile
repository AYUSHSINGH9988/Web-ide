FROM python:3.11-slim

# ---- System packages: runtimes the "Run Code" button can call ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    git \
    curl \
    nodejs \
    npm \
    ruby \
    php-cli \
    nano \
    htop \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ---- Hugging Face Spaces runs containers as a non-root user (UID 1000) ----
RUN useradd -m -u 1000 coder
WORKDIR /app

# ---- Python deps ----
COPY --chown=coder:coder requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- App code ----
COPY --chown=coder:coder app.py .
COPY --chown=coder:coder static/ ./static/

# ---- Writable workspace where user files & terminal sessions live ----
RUN mkdir -p /app/workspace && chown -R coder:coder /app

ENV WORKSPACE_ROOT=/app/workspace \
    HOME=/home/coder \
    PYTHONUNBUFFERED=1

USER coder

# Koyeb injects the port to listen on via $PORT (defaults to 8000 if unset,
# which also matches Koyeb's health-check default). Works unchanged on
# Hugging Face Spaces too, since HF sets PORT=7860 automatically.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT is expanded at container start (Koyeb sets it dynamically).
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
