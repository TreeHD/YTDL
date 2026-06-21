# Base image (Debian-based for glibc compatibility with deno)
FROM python:3.14-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg unzip wget && \
    rm -rf /var/lib/apt/lists/*

# Install deno
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
ENV PATH="/opt/deno/bin:${PATH}"

CMD ["python", "bot.py"]
