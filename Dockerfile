# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

# FFmpeg + Go (needed to build apple-music-downloader)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        golang \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Build apple-music-downloader from source ──────────────────────────────────
# https://github.com/zhaarey/apple-music-downloader
RUN git clone --depth=1 https://github.com/zhaarey/apple-music-downloader.git /tmp/am-dl \
    && cd /tmp/am-dl \
    && go build -o /usr/local/bin/apple-music-downloader . \
    && rm -rf /tmp/am-dl /root/go /root/.cache

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY . .

# Create runtime directories
RUN mkdir -p tmp logs

# Expose HTTP port (for Render/Heroku/Railway health checks)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/ping')" || exit 1

CMD ["python", "app.py"]
