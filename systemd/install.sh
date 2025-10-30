#!/bin/bash
# Installation script for RedditModLog systemd services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}RedditModLog Systemd Installation Script${NC}"
echo "========================================="

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}"
   exit 1
fi

# Create user if it doesn't exist
if ! id "modlogbot" &>/dev/null; then
    echo "Creating modlogbot user..."
    useradd -r -s /bin/false -d /opt/RedditModLog -m modlogbot
else
    echo "User modlogbot already exists"
fi

# Create necessary directories
echo "Creating directories..."
mkdir -p /var/log/redditmodlog
mkdir -p /etc/redditmodlog
mkdir -p /opt/RedditModLog/data

# Set permissions
echo "Setting permissions..."
chown -R modlogbot:modlogbot /var/log/redditmodlog
chown -R modlogbot:modlogbot /opt/RedditModLog/data
chown modlogbot:modlogbot /etc/redditmodlog
chmod 755 /var/log/redditmodlog
chmod 755 /etc/redditmodlog

# Install systemd service template
echo "Installing systemd service template..."
cp modlog@.service /etc/systemd/system/

# Install logrotate configuration
echo "Installing logrotate configuration..."
cp redditmodlog.logrotate /etc/logrotate.d/redditmodlog

# Create example configurations
echo "Creating example configurations..."

# OpenSignups config
cat > /etc/redditmodlog/opensignups.json.example <<EOF
{
  "reddit": {
    "client_id": "your_client_id",
    "client_secret": "your_client_secret",
    "username": "your_bot_username",
    "password": "your_bot_password"
  },
  "source_subreddit": "OpenSignups",
  "wiki_page": "modlog",
  "retention_days": 180,
  "batch_size": 500,
  "update_interval": 600,
  "anonymize_moderators": true,
  "max_wiki_entries_per_page": 1000
}
EOF

# Usenet config
cat > /etc/redditmodlog/usenet.json.example <<EOF
{
  "reddit": {
    "client_id": "your_client_id",
    "client_secret": "your_client_secret",
    "username": "your_bot_username",
    "password": "your_bot_password"
  },
  "source_subreddit": "usenet",
  "wiki_page": "modlog",
  "retention_days": 30,
  "batch_size": 100,
  "update_interval": 300,
  "anonymize_moderators": true,
  "max_wiki_entries_per_page": 1000
}
EOF

# Example environment file
cat > /etc/redditmodlog/example.env <<EOF
# Optional environment variables to override config file
# Uncomment and set as needed

# REDDIT_CLIENT_ID=your_client_id
# REDDIT_CLIENT_SECRET=your_client_secret
# REDDIT_USERNAME=your_bot_username
# REDDIT_PASSWORD=your_bot_password
# BATCH_SIZE=100
# UPDATE_INTERVAL=300
EOF

chmod 640 /etc/redditmodlog/*.example
chmod 640 /etc/redditmodlog/*.env
chown -R modlogbot:modlogbot /etc/redditmodlog/

# Reload systemd
echo "Reloading systemd daemon..."
systemctl daemon-reload

echo ""
echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Copy and edit the config files:"
echo "   cp /etc/redditmodlog/opensignups.json.example /etc/redditmodlog/opensignups.json"
echo "   cp /etc/redditmodlog/usenet.json.example /etc/redditmodlog/usenet.json"
echo "   nano /etc/redditmodlog/opensignups.json"
echo "   nano /etc/redditmodlog/usenet.json"
echo ""
echo "2. Start the services:"
echo "   systemctl start modlog@opensignups.service"
echo "   systemctl start modlog@usenet.service"
echo ""
echo "3. Enable auto-start on boot:"
echo "   systemctl enable modlog@opensignups.service"
echo "   systemctl enable modlog@usenet.service"
echo ""
echo "4. Check logs:"
echo "   tail -f /var/log/redditmodlog/opensignups.log"
echo "   tail -f /var/log/redditmodlog/usenet.log"
echo ""
echo "5. Check service status:"
echo "   systemctl status modlog@opensignups.service"
echo "   systemctl status modlog@usenet.service"
