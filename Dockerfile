# Base image
FROM python:3.12.12-alpine3.23

WORKDIR /app

# Install system dependencies
RUN apk update && apk add --no-cache ffmpeg

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application modules
COPY bot.py .
COPY config.py .
COPY database.py .
COPY downloader.py .
COPY uploader.py .
COPY handlers.py .
COPY queue_processor.py .
COPY subscription.py .

# Create data directory for SQLite
RUN mkdir -p /app/data /app/downloads

CMD ["python", "bot.py"]
