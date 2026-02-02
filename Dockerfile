FROM python:3.12-slim

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir yt-dlp python-telegram-bot

# Copy application code
COPY bot.py .
# config.json will be mounted via volume to preserve secrets

# Wrapper script to handle config if needed, or just run python
CMD ["python", "bot.py"]
