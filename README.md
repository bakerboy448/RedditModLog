# Reddit Modlog Wiki Publisher

Automatically publishes Reddit moderation logs to a subreddit wiki page with modmail inquiry links.

## Features

- 📊 Publishes modlogs as organized markdown tables
- 📧 Pre-populated modmail links for removal inquiries
- 🗄️ SQLite database for efficient deduplication
- ⏰ Configurable update intervals
- 🔒 Automatic cleanup of old entries
- ⚡ Handles Reddit's 524KB wiki size limit

## Quick Start

1. **Install dependencies**
```bash
pip install praw
```

2. **Create Reddit App**
   - Go to https://www.reddit.com/prefs/apps
   - Click "Create App" → Select "script"
   - Note your `client_id` and `client_secret`

3. **Configure**
```bash
cp config.template.json config.json
# Edit config.json with your credentials
```

4. **Test connection**
```bash
python modlog_wiki_publisher.py --test
```

5. **Run**
```bash
# Default: Run once and exit
python modlog_wiki_publisher.py

# Continuous mode (daemon)
python modlog_wiki_publisher.py --continuous
```

## Configuration

Create `config.json`:

```json
{
  "reddit": {
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "username": "YOUR_BOT_USERNAME",
    "password": "YOUR_BOT_PASSWORD"
  },
  "source_subreddit": "YourSubreddit",
  "target_subreddit": "YourSubreddit",
  "wiki_page": "modlog",
  "ignored_moderators": ["AutoModerator"],
  "update_interval": 300,
  "batch_size": 100,
  "retention_days": 30
}
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `source_subreddit` | Subreddit to read modlogs from | Required |
| `target_subreddit` | Subreddit to publish wiki to | Required |
| `wiki_page` | Wiki page name | `modlog` |
| `ignored_moderators` | Moderators to ignore | `["AutoModerator"]` |
| `update_interval` | Seconds between updates (continuous mode) | `300` |
| `batch_size` | Modlog entries per fetch | `100` |
| `retention_days` | Days to track processed entries | `30` |

## Wiki Output

The script creates tables organized by date:

```markdown
## 2025-01-15

| Time | Action | Content | Reason | Inquire |
|------|--------|---------|--------|---------|
| 14:25:33 UTC | removepost by **ModName** | [Post Title](url) by u/user | spam | [Contact Mods](modmail) |
```

## Command Line Options

```bash
python modlog_wiki_publisher.py [options]

Options:
  --config FILE      Config file path (default: config.json)
  --continuous       Run continuously (default: run once)
  --test            Test configuration and exit
  --debug           Enable debug logging
```

## Requirements

- Python 3.6+
- PRAW (Reddit API wrapper)
- Moderator access to source subreddit
- Wiki edit permissions on target subreddit

## Systemd Service (Optional)

For continuous operation on Linux:

```bash
# Create service file
sudo nano /etc/systemd/system/modlog-wiki.service
```

```ini
[Unit]
Description=Reddit Modlog Wiki Publisher
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/script
ExecStart=/usr/bin/python3 /path/to/modlog_wiki_publisher.py --continuous
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl enable modlog-wiki
sudo systemctl start modlog-wiki
```

## Permissions Required

Your bot account needs:
- **Moderator access** to read modlogs
- **Wiki permissions** to edit wiki pages

Add your bot as a moderator with "Wiki" permissions, or add to wiki contributors at:
`/r/YourSubreddit/wiki/settings/modlog`

## Database

The script uses SQLite (`modlog.db`) to track processed entries:

```bash
# View recent entries
sqlite3 modlog.db "SELECT * FROM processed_actions ORDER BY created_at DESC LIMIT 10;"

# Check database size
ls -lh modlog.db

# Manual cleanup (if needed)
sqlite3 modlog.db "DELETE FROM processed_actions WHERE created_at < date('now', '-30 days');"
```

## Troubleshooting

**Authentication Failed**
- Verify credentials in config.json
- Use app-specific password if 2FA is enabled

**Wiki Permission Denied**
- Ensure bot has moderator or wiki contributor access
- Check wiki page permissions

**Rate Limiting**
- Increase `update_interval` (300+ seconds recommended)
- Reduce `batch_size` if needed

**Database Growing**
- Reduce `retention_days` in config
- Database auto-vacuums daily

## License

GNU General Public License v3.0

## Contributing

Pull requests welcome! Please test changes thoroughly before submitting.

## Support

For issues or questions, create an issue on GitHub.