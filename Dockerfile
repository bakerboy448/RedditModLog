# Build stage
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Runtime stage
FROM python:3.11-slim

# OCI Labels
LABEL org.opencontainers.image.title="Reddit ModLog Wiki Publisher" \
      org.opencontainers.image.description="Automated Reddit moderation log publisher to wiki pages" \
      org.opencontainers.image.authors="bakerboy448" \
      org.opencontainers.image.source="https://github.com/bakerboy448/RedditModLog" \
      org.opencontainers.image.documentation="https://github.com/bakerboy448/RedditModLog/blob/main/README.md" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.vendor="bakerboy448" \
      org.opencontainers.image.base.name="python:3.11-slim"

# Install runtime dependencies and s6-overlay for user management
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install s6-overlay for proper init and user management
ARG S6_OVERLAY_VERSION=3.1.6.2
ARG TARGETARCH
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp
RUN case ${TARGETARCH} in \
        "amd64") S6_ARCH=x86_64 ;; \
        "arm64") S6_ARCH=aarch64 ;; \
        "arm/v7") S6_ARCH=arm ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    curl -L "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" -o /tmp/s6-overlay-arch.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz && \
    rm /tmp/s6-overlay-*.tar.xz

# Create default user and group
RUN groupadd -g 1000 modlogbot && \
    useradd -u 1000 -g modlogbot -d /config -s /bin/bash modlogbot

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUID=1000 \
    PGID=1000 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    DATABASE_PATH=/config/data/modlog.db \
    LOGS_DIR=/config/logs

# Create application directories
RUN mkdir -p /config /config/data /config/logs /app /etc/s6-overlay/s6-rc.d/modlog-bot /etc/s6-overlay/s6-rc.d/init-modlogbot /etc/s6-overlay/scripts

# Create s6 init script for user/group management
RUN echo '#!/command/with-contenv bash\n\
set -e\n\
\n\
# Validate critical environment variables\n\
echo "Validating required environment variables..."\n\
\n\
missing_vars=()\n\
\n\
[ -z "$REDDIT_CLIENT_ID" ] && missing_vars+=("REDDIT_CLIENT_ID")\n\
[ -z "$REDDIT_CLIENT_SECRET" ] && missing_vars+=("REDDIT_CLIENT_SECRET")\n\
[ -z "$REDDIT_USERNAME" ] && missing_vars+=("REDDIT_USERNAME")\n\
[ -z "$REDDIT_PASSWORD" ] && missing_vars+=("REDDIT_PASSWORD")\n\
[ -z "$SOURCE_SUBREDDIT" ] && missing_vars+=("SOURCE_SUBREDDIT")\n\
\n\
if [ ${#missing_vars[@]} -ne 0 ]; then\n\
    echo "ERROR: Missing required environment variables:" >&2\n\
    printf "  - %s\n" "${missing_vars[@]}" >&2\n\
    echo "" >&2\n\
    echo "Please set all required environment variables and restart the container." >&2\n\
    exit 1\n\
fi\n\
\n\
echo "All required environment variables are set."\n\
\n\
PUID=${PUID:-1000}\n\
PGID=${PGID:-1000}\n\
\n\
echo "Setting UID:GID to ${PUID}:${PGID}"\n\
\n\
# Update user and group IDs\n\
groupmod -o -g "$PGID" modlogbot\n\
usermod -o -u "$PUID" modlogbot\n\
\n\
# Fix ownership\n\
echo "Fixing ownership of /config and /app"\n\
chown -R modlogbot:modlogbot /config /app\n\
\n\
# Ensure data directory has correct permissions\n\
if [ ! -f /config/data/modlog.db ]; then\n\
    echo "Initializing database directory"\n\
    touch /config/data/modlog.db\n\
    chown modlogbot:modlogbot /config/data/modlog.db\n\
fi' > /etc/s6-overlay/scripts/init-modlogbot-run && \
    chmod +x /etc/s6-overlay/scripts/init-modlogbot-run

# Create s6 service run script
RUN echo '#!/command/with-contenv bash\n\
cd /app\n\
exec s6-setuidgid modlogbot python modlog_wiki_publisher.py --continuous' > /etc/s6-overlay/scripts/modlog-bot-run && \
    chmod +x /etc/s6-overlay/scripts/modlog-bot-run

# Setup s6 service definitions
RUN echo 'oneshot' > /etc/s6-overlay/s6-rc.d/init-modlogbot/type && \
    echo '/etc/s6-overlay/scripts/init-modlogbot-run' > /etc/s6-overlay/s6-rc.d/init-modlogbot/up && \
    echo 'longrun' > /etc/s6-overlay/s6-rc.d/modlog-bot/type && \
    echo '/etc/s6-overlay/scripts/modlog-bot-run' > /etc/s6-overlay/s6-rc.d/modlog-bot/run && \
    echo 'init-modlogbot' > /etc/s6-overlay/s6-rc.d/modlog-bot/dependencies && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/init-modlogbot && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/modlog-bot

# Set working directory
WORKDIR /app

# Copy application files
COPY --chown=modlogbot:modlogbot modlog_wiki_publisher.py /app/
COPY --chown=modlogbot:modlogbot config_template.json /app/

# Health check
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, sys; sys.exit(0 if os.path.exists(os.getenv('DATABASE_PATH', '/config/data/modlog.db')) else 1)"

# Use s6-overlay as entrypoint
ENTRYPOINT ["/init"]
