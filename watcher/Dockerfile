FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
# yt-dlp is upgraded on every build: YouTube changes break old releases often.
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --upgrade yt-dlp

COPY watcher.py .

# Non-root. /data exists in the image with this owner, so the named volume
# mounted there inherits a writable directory for the state file.
RUN useradd -r -u 10001 watcher && mkdir -p /data && chown watcher /data
USER watcher

CMD ["python", "-u", "watcher.py"]
