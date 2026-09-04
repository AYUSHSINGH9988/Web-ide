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

# Hugging Face Spaces (Docker SDK) expects the app on port 7860.
EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
