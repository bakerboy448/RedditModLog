FROM python:3.11-slim

# OCI Image Spec Annotations
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.created=$BUILD_DATE \
      org.opencontainers.image.url="https://github.com/$GITHUB_REPOSITORY" \
      org.opencontainers.image.source="https://github.com/$GITHUB_REPOSITORY" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.vendor="RedditModLog" \
      org.opencontainers.image.title="Reddit Moderation Log Publisher" \
      org.opencontainers.image.description="Automated Reddit moderation log scraper and wiki publisher" \
      org.opencontainers.image.licenses="MIT"

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY modlog_wiki_publisher.py .
COPY config_template.json .

# Create directories for data persistence
RUN mkdir -p /app/data /app/logs

# Create non-root user for security
RUN groupadd -r modlogbot && useradd -r -g modlogbot modlogbot
RUN chown -R modlogbot:modlogbot /app
USER modlogbot

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/app/data/modlog.db
ENV LOGS_DIR=/app/logs

# Expose health check port (if we add one)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sqlite3; conn = sqlite3.connect('${DB_PATH}'); conn.close()" || exit 1

# Default command - can be overridden
CMD ["python", "modlog_wiki_publisher.py", "--continuous"]
