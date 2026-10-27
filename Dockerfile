# Hundjäger Media Hub - Container Image
# Zero-external-dependency Python core with ffmpeg and yt-dlp runtime
FROM python:3.12-slim

# Install system dependencies (ffmpeg and curl for yt-dlp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install latest official yt-dlp binary
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

# Copy application files
COPY app/ ./app/

# Create downloads volume target
RUN mkdir -p /downloads

EXPOSE 5050

# Run with non-root user permissions
USER 1000:1000

ENTRYPOINT ["python3", "app/server.py", "--host", "0.0.0.0", "--port", "5050", "--downloads-dir", "/downloads"]
