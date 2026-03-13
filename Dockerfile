FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        build-essential \
    && pip install uv \
    && uv pip install --system --no-cache -r requirements.txt \
    && apt-get purge -y gcc build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN mkdir -p tmp logs

EXPOSE 8080
CMD ["python", "app.py"]
