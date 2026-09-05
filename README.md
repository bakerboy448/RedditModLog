# Reddit Modlog Wiki Publisher

[![Docker Build](https://github.com/baker-scripts/RedditModLog/actions/workflows/docker-build.yml/badge.svg)](https://github.com/baker-scripts/RedditModLog/actions/workflows/docker-build.yml) [![Pre-commit](https://github.com/baker-scripts/RedditModLog/actions/workflows/pre-commit.yml/badge.svg)](https://github.com/baker-scripts/RedditModLog/actions/workflows/pre-commit.yml)

Automatically publishes Reddit moderation logs to a subreddit wiki page with modmail inquiry links.

> **🧪 Devvit migration in progress.** A re-platform of this bot to a Reddit-hosted [Devvit](https://developers.reddit.com/) app (no self-hosting, no bot password, no SQLite) is being scaffolded under [`devvit/`](devvit/) with requirements, research, and architecture docs in [`devvit-migration/docs/`](devvit-migration/). The Python app documented below remains the supported version. See [`devvit-migration/README.md`](devvit-migration/README.md) for status.

## Features

* 📊 Publishes modlogs as organized markdown tables with unique content tracking IDs
* 📧 Pre-populated modmail links for removal inquiries (formatted as clickable markdown links)
* 🗄️ SQLite database for deduplication and retention with **multi-subreddit support**
* ⏰ Configurable update intervals with continuous daemon mode
* 🔒 Automatic cleanup of old entries with configurable retention
* ⚡ Handles Reddit's 524KB wiki size limit automatically
* 🧩 Fully CLI-configurable (no need to edit `config.json`)
* 📁 Per-subreddit log files for debugging and monitoring
* 🔒 Configurable moderator anonymization (AutoModerator/HumanModerator)
* 📝 **Complete removal reason transparency** - AutoModerator rule text, addremovalreason descriptions, all actual removal text (never generic messages or template numbers)
* 🔗 Links directly to actual content (posts/comments), never user profiles for privacy
* 🆔 **Unique content IDs** - comments show comment IDs, posts show post IDs for precise tracking
* ✅ **Multi-subreddit database support** - single database handles multiple subreddits safely

## Deployment Options

Choose your preferred deployment method:

- **🐳 Docker** (Recommended) - Containerized deployment with s6-overlay init system. See [Docker Deployment](#docker-deployment)
- **⚙️ Systemd** (Production) - Native Linux service with automatic restart and log rotation. See [Systemd Service](#systemd-service-production)
- **🐍 Python Native** (Development/Testing) - Direct Python execution. See [Quick Start](#quick-start) below

## Quick Start (Python Native)

1. **Install dependencies**

```bash
pip install praw
```

2. **Create Reddit App**

   * Visit: [https://www.reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)
   * Click "Create App" → Choose "script"
   * Note `client_id` and `client_secret`

3. **Copy and edit config**

```bash
cp config.template.json config.json
# Edit your credentials and subreddit info
```

4. **Test connection**

```bash
python modlog_wiki_publisher.py --test
```

5. **Run**

```bash
# Run once and exit
python modlog_wiki_publisher.py

# Run continuously
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
  "wiki_page": "modlog",
  "ignored_moderators": ["AutoModerator"],
  "update_interval": 300,
  "batch_size": 100,
  "retention_days": 30,
  "anonymize_moderators": true
}
```

### Configurable via CLI

| CLI Option | JSON Key | Description | Default | Min | Max |
|------------|----------|-------------|---------|-----|-----|
| `--source-subreddit` | `source_subreddit` | Subreddit to read and write logs | required | - | - |
| `--wiki-page` | `wiki_page` | Wiki page name | modlog | - | - |
| `--retention-days` | `retention_days` | Keep entries this many days | 90 | 1 | 365 |
| `--batch-size` | `batch_size` | Entries to fetch per run | 50 | 10 | 500 |
| `--interval` | `update_interval` | Seconds between updates in daemon mode | 600 | 60 | 3600 |
| `--config` | – | Path to config file | config.json | - | - |
| `--debug` | – | Enable verbose output | false | - | - |
| `--show-config-limits` | – | Show configuration limits and defaults | false | - | - |
| `--force-migrate` | – | Force database migration | false | - | - |
| `--no-auto-update-config` | – | Disable automatic config file updates | false | - | - |

CLI values override config file values.

## Configuration Limits

All configuration values are automatically validated and enforced within safe limits. Use `--show-config-limits` to see current limits and defaults.

## Automatic Config Updates

The application automatically updates your config file when new configuration options are added, while preserving your existing settings. A backup is created before any changes. Use `--no-auto-update-config` to disable this behavior.

## Database Migration

The database will automatically migrate to the latest schema version on startup. Use `--force-migrate` to manually trigger migration.

## Wiki Output

Sample wiki table output:

```markdown
## 2025-08-09

| Time | Action | ID | Moderator | Content | Reason | Inquire |
|------|--------|----|-----------|---------|--------|---------|
| 08:15:42 UTC | removecomment | n7ravg2 | AutoModerator | [Comment by u/user123](https://www.reddit.com/r/opensignups/comments/1ab2cd3/title/n7ravg2/) | Possibly requesting an invite - [invited] Offers must be [O] 3x Invites to MyAwesomeTracker | [Contact Mods](https://www.reddit.com/message/compose?to=/r/opensignups&subject=Comment%20Removal%20Inquiry...) |
| 07:45:18 UTC | addremovalreason | 1ab2cd3 | Bakerboy448 | [Post title here](https://www.reddit.com/r/opensignups/comments/1ab2cd3/title/) | Invites - No asking | [Contact Mods](https://www.reddit.com/message/compose?to=/r/opensignups&subject=Removal%20Reason%20Inquiry...) |
| 06:32:15 UTC | removelink | 1xy9def | AutoModerator | [Another post](https://www.reddit.com/r/opensignups/comments/1xy9def/another/) | No standalone URL in post body | [Contact Mods](https://www.reddit.com/message/compose?to=/r/opensignups&subject=Post%20Removal%20Inquiry...) |
```

## Logging

Each subreddit gets its own log file under `logs/`:

```
logs/
└── yoursubreddit.log
```

Use `--debug` to enable verbose output.

## Command Line Options

```bash
python modlog_wiki_publisher.py [options]

Options:
  --config FILE            Path to config file (default: config.json)
  --source-subreddit NAME  Subreddit to read from and publish to
  --wiki-page NAME         Wiki page to update (default: modlog)
  --retention-days N       Days to keep processed entries
  --batch-size N           Number of modlog entries to fetch
  --interval N             Seconds between updates (daemon)
  --debug                  Enable debug logging
  --test                   Run a test and exit
  --continuous             Run continuously
  --force-modlog           Fetch ALL actions from Reddit API and rebuild wiki
  --force-wiki             Update wiki even if content appears unchanged
  --force-all              Do both --force-modlog and --force-wiki
```

### Force Commands Explained

**--force-modlog**: Complete rebuild from Reddit
- Fetches ALL recent modlog actions from Reddit API
- Stores them in database
- Rebuilds entire wiki page from database
- Use when: Starting fresh, major updates, or troubleshooting

**--force-wiki**: Force wiki update only
- Uses existing database data
- Forces wiki update even if content hash matches
- Use when: Format changes, modmail updates, or cache issues

**--force-all**: Complete refresh (replaces old --force)
- Combines both --force-modlog and --force-wiki
- Fetches from Reddit AND forces wiki update
- Use when: Major changes, troubleshooting, or unsure which force to use

```bash
# Complete rebuild from Reddit API
python modlog_wiki_publisher.py --source-subreddit usenet --force-modlog

# Update wiki with current database data (bypass cache)
python modlog_wiki_publisher.py --source-subreddit usenet --force-wiki

# Do both (equivalent to old --force)
python modlog_wiki_publisher.py --source-subreddit usenet --force-all
```

## Database

Uses `modlog.db` (SQLite) for deduplication and history:

```bash
# View recent actions with removal reasons
sqlite3 modlog.db "SELECT action_id, action_type, moderator, removal_reason, subreddit, created_at FROM processed_actions ORDER BY created_at DESC LIMIT 10;"

# View all columns including removal reasons and target author
sqlite3 modlog.db "SELECT * FROM processed_actions ORDER BY created_at DESC LIMIT 10;"

# View actions by subreddit
sqlite3 modlog.db "SELECT action_type, moderator, target_author, removal_reason FROM processed_actions WHERE subreddit = 'usenet' ORDER BY created_at DESC LIMIT 5;"

# Track content lifecycle by target ID
sqlite3 modlog.db "SELECT target_id, action_type, moderator, removal_reason, datetime(created_at, 'unixepoch') FROM processed_actions WHERE target_id LIKE '%1mkz4jm%' ORDER BY created_at;"

# View removal reasons that are text (not numbers)
sqlite3 modlog.db "SELECT action_type, removal_reason FROM processed_actions WHERE removal_reason NOT LIKE '%[0-9]%' AND removal_reason != 'remove' LIMIT 5;"

# Clean manually
sqlite3 modlog.db "DELETE FROM processed_actions WHERE created_at < date('now', '-30 days');"
```

### Database Schema

The database includes comprehensive moderation data with full transparency:

- **`removal_reason` column**: Stores actual removal reason text from Reddit's API
  - AutoModerator actions: Full rule text (e.g., "Possibly requesting an invite - [invited] Offers must be [O]")
  - addremovalreason actions: Readable removal reason (e.g., "Invites - No asking") instead of template numbers
  - Manual removals: Moderator-provided text or rule details
- **`target_author` column**: Actual usernames of content authors (never shows [deleted])
- **`subreddit` column**: Multi-subreddit support with proper data separation
- **Unique content IDs**: Comments show comment IDs (e.g., n7ravg2), posts show post IDs

## Docker Deployment

### Quick Start with Docker

The recommended approach is to use a config file for all settings:

```bash
# 1. Create config directory structure
mkdir -p ./config/data ./config/logs

# 2. Create config.json from template
cp config_template.json ./config/config.json
# Edit ./config/config.json with your credentials

# 3. Run with config file (recommended)
docker run -d \
  --name reddit-modlog \
  -e PUID=1000 \
  -e PGID=1000 \
  -v ./config:/config \
  ghcr.io/baker-scripts/redditmodlog:1

# 4. Using Docker Compose (recommended)
docker compose up -d
```

**What gets mounted to `/config`:**
- `config.json` - Your configuration file (auto-updated with new defaults on upgrades)
- `data/modlog.db` - SQLite database (persistent)
- `logs/` - Per-subreddit log files

### Alternative: Environment Variables

You can use environment variables instead of a config file:

```bash
docker run -d \
  --name reddit-modlog \
  -e PUID=1000 \
  -e PGID=1000 \
  -e REDDIT_CLIENT_ID=your_client_id \
  -e REDDIT_CLIENT_SECRET=your_client_secret \
  -e REDDIT_USERNAME=your_username \
  -e REDDIT_PASSWORD=your_password \
  -e SOURCE_SUBREDDIT=yoursubreddit \
  -v ./config:/config \
  ghcr.io/baker-scripts/redditmodlog:1
```

### Docker Compose Example

```yaml
version: '3.8'

services:
  redditmodlog-opensignups:
    image: ghcr.io/baker-scripts/redditmodlog:1
    container_name: redditmodlog-opensignups
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
    volumes:
      - ./opensignups:/config
    mem_limit: 256m
    cpus: 0.5
```

### Environment Variables

**User/Group IDs** (for file permissions):
- `PUID` - User ID (default: 1000)
- `PGID` - Group ID (default: 1000)

**Reddit API Credentials** (required if not using config file):
- `REDDIT_CLIENT_ID` - Reddit app client ID
- `REDDIT_CLIENT_SECRET` - Reddit app client secret
- `REDDIT_USERNAME` - Bot username
- `REDDIT_PASSWORD` - Bot password
- `SOURCE_SUBREDDIT` - Subreddit name

**Optional Settings** (override config.json if set):
- `WIKI_PAGE` - Wiki page name (default: modlog)
- `RETENTION_DAYS` - Database retention in days (default: 90, max: 365)
- `BATCH_SIZE` - Entries per fetch (default: 50, max: 500)
- `UPDATE_INTERVAL` - Seconds between updates (default: 600, max: 3600)
- `ANONYMIZE_MODERATORS` - true/false (default: true)

**Internal Paths** (don't modify):
- `DATABASE_PATH=/config/data/modlog.db`
- `LOGS_DIR=/config/logs`

### Docker Image

Pre-built images available at GitHub Container Registry:

**Recommended Tags:**
- `ghcr.io/baker-scripts/redditmodlog:1` - Major version (gets v1.x.x updates automatically)
- `ghcr.io/baker-scripts/redditmodlog:1.4` - Minor version (gets v1.4.x patches only)
- `ghcr.io/baker-scripts/redditmodlog:1.4.3` - Specific version (pinned, no updates)

**Other Tags:**
- `ghcr.io/baker-scripts/redditmodlog:latest` - Always latest build (use with caution)
- `ghcr.io/baker-scripts/redditmodlog:sha-<commit>` - Specific commit SHA

**Architectures:** `linux/amd64`, `linux/arm64`

**Recommendation:** Use `:1` for production to get automatic updates within v1 while avoiding breaking changes from v2.

### Docker Features

- ✅ s6-overlay v3 init system for proper process management
- ✅ PUID/PGID support for file permission management
- ✅ Automatic config file updates on version upgrades
- ✅ Single `/config` mount for all persistent data
- ✅ Supports both config file and environment variable configuration
- ✅ Built-in troubleshooting tools (htop, vim, sqlite3)
- ✅ Health checks for monitoring
- ✅ Proper log routing (INFO→stdout, errors→stderr)

## Systemd Service (Production)

### Installation

```bash
# Run the installation script
cd systemd
sudo ./install.sh

# Copy and edit configs for your subreddits
sudo cp /etc/redditmodlog/opensignups.json.example /etc/redditmodlog/opensignups.json
sudo nano /etc/redditmodlog/opensignups.json

# Start services
sudo systemctl start modlog@opensignups
sudo systemctl enable modlog@opensignups

# Check logs
tail -f /var/log/redditmodlog/opensignups.log
```

### Service Template

The systemd template (`modlog@.service`) supports multiple instances:

```bash
# Start multiple subreddit services
sudo systemctl start modlog@subreddit1
sudo systemctl start modlog@subreddit2

# Each service uses its own config file
# /etc/redditmodlog/subreddit1.json
# /etc/redditmodlog/subreddit2.json

# Logs go to separate files
# /var/log/redditmodlog/subreddit1.log
# /var/log/redditmodlog/subreddit2.log
```

### Features

- ✅ Per-subreddit configuration files
- ✅ Automatic log rotation (30 days retention, 100MB max size)
- ✅ Security hardening (read-only filesystem, private /tmp)
- ✅ Resource limits (256MB RAM, 25% CPU)
- ✅ Automatic restart on failure
- ✅ Proper user/group management

## Permissions Required

Your bot account needs:

* **Moderator** on the subreddit
* **Wiki edit permissions**

Add the bot as a moderator or approved wiki contributor:

```
/r/<yoursubreddit>/wiki/settings/modlog
```

## Troubleshooting

| Issue         | Fix                                              |
| ------------- | ------------------------------------------------ |
| Auth failed   | Check credentials, 2FA, use app password         |
| Wiki denied   | Bot needs wiki mod or contributor access         |
| Rate limiting | Increase `--interval` and reduce `--batch-size`  |
| Growing DB    | Lower `--retention-days` or run cleanup manually |

## Contributing

Issues and pull requests welcome. See the [contributing guidelines](https://github.com/baker-scripts/.github/blob/main/CONTRIBUTING.md); [open an issue](https://github.com/baker-scripts/RedditModLog/issues) to discuss larger changes. Include test runs and changes to CLI/help output.

## Contributors

<a href="https://github.com/baker-scripts/RedditModLog/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=baker-scripts/RedditModLog" alt="Contributors" />
</a>

## Disclaimer

This software is provided as-is with no warranty. Always review your Reddit API credentials and bot permissions before deployment. The authors are not responsible for any moderation issues or account actions resulting from its use. This project is not affiliated with or endorsed by Reddit.

## License

[GPL-3.0](LICENSE)
