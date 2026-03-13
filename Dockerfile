FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────────────────────
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
RUN git clone --depth 1 --single-branch \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /bgutil \
    && cd /bgutil/server \
    && npm ci --omit=dev \
    && echo "✓ bgutil server installed"

# ── App ───────────────────────────────────────────────────────────────────────
COPY . .

RUN mkdir -p tmp logs

EXPOSE 8080

CMD ["python", "app.py"]
