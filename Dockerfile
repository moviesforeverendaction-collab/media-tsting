FROM python:3.12-slim

# Install system deps (gcc needed for TgCrypto, ffmpeg for media processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade yt-dlp

# Copy source
COPY . .
RUN mkdir -p tmp logs

EXPOSE 8080
CMD ["python", "app.py"]
