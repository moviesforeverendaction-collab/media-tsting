FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────────────────────
# nodejs + npm  : bgutil PO-token server (YouTube bot-detection bypass)
# ffmpeg        : audio/video processing
# gcc + build-essential : compile TgCrypto C extension
# git           : clone bgutil repo during build
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        build-essential \
        nodejs \
        npm \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps ───────────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade yt-dlp \
    && pip install --no-cache-dir bgutil-ytdlp-pot-provider

# ── bgutil PO-token server ────────────────────────────────────────────────────
# Solves Google's BotGuard JS challenge → generates real PO tokens for yt-dlp.
# No cookies needed. Server runs on 127.0.0.1:4416 started at bot boot.
RUN git clone --depth 1 --single-branch \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /bgutil \
    && cd /bgutil/server \
    && npm ci --omit=dev \
    && npm run build \
    && echo "✓ bgutil server built"

# ── App ───────────────────────────────────────────────────────────────────────
COPY . .
RUN mkdir -p tmp logs

EXPOSE 8080
CMD ["python", "app.py"]
