# Base image
FROM python:3.14.2-alpine3.23

WORKDIR /app

# Install system dependencies
RUN apk update && apk add --no-cache ffmpeg unzip

# Install deno to non-standard path (only used when cookies are present)
RUN DENO_VERSION=$(wget -qO- https://dl.deno.land/release-latest.txt) && \
    ARCH=$(uname -m) && \
    wget -qO /tmp/deno.zip "https://dl.deno.land/release/${DENO_VERSION}/deno-${ARCH}-unknown-linux-gnu.zip" && \
    mkdir -p /opt/deno/bin && unzip -o /tmp/deno.zip -d /opt/deno/bin && \
    chmod +x /opt/deno/bin/deno && rm /tmp/deno.zip

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

ENV PYTHONMALLOC=malloc

CMD ["python", "bot.py"]
