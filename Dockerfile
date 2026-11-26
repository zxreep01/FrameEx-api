FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    MAX_CONCURRENT_JOBS=2 \
    FFMPEG_THREADS=2

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# System ffmpeg is already installed above, so skip the bundled binary wheel to keep the image lean
RUN grep -v '^imageio-ffmpeg' requirements.txt | pip install --no-cache-dir -r /dev/stdin

COPY app ./app

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
