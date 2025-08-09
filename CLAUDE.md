# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python-based Reddit moderation log publisher that automatically scrapes moderation actions from a subreddit and publishes them to a wiki page. The application uses PRAW (Python Reddit API Wrapper) and SQLite for data persistence.

## Core Architecture

- **Main application**: `modlog_wiki_publisher.py` - Single-file application containing all core functionality
- **Database layer**: `ModlogDatabase` class handles SQLite operations for deduplication and retention
- **Configuration**: JSON-based config with CLI override support
- **Logging**: Per-subreddit log files in `logs/` directory with rotating handlers
- **Authentication**: Reddit OAuth2 script-type app authentication

## Development Commands

### Setup and Dependencies
```bash
# Install dependencies
pip install praw

# Copy template config (required for first run)
cp config_template.json config.json
```

### Running the Application
```bash
# Test connection and configuration
python modlog_wiki_publisher.py --test

# Single run
python modlog_wiki_publisher.py --source-subreddit SUBREDDIT_NAME

# Continuous daemon mode
python modlog_wiki_publisher.py --source-subreddit SUBREDDIT_NAME --continuous

# Debug authentication issues
python debug_auth.py
```

### Database Operations
```bash
# View recent processed actions
sqlite3 modlog.db "SELECT * FROM processed_actions ORDER BY created_at DESC LIMIT 10;"

# Manual cleanup of old entries
sqlite3 modlog.db "DELETE FROM processed_actions WHERE created_at < date('now', '-30 days');"
```

## Key Configuration

The application supports both JSON config files and CLI arguments (CLI overrides JSON):

- `--source-subreddit`: Target subreddit for reading/writing logs
- `--wiki-page`: Wiki page name (default: "modlog")
- `--retention-days`: Database cleanup period (default: 30)
- `--batch-size`: Entries fetched per run (default: 100)
- `--interval`: Seconds between updates in daemon mode (default: 300)
- `--debug`: Enable verbose logging

## Authentication Requirements

The bot account needs:
- Moderator status on the target subreddit
- Wiki edit permissions for the specified wiki page
- Reddit app credentials (script type, not web app)

## File Structure

- `modlog_wiki_publisher.py`: Main application
- `debug_auth.py`: Authentication debugging utility
- `config.json`: Runtime configuration (created from template)
- `modlog.db`: SQLite database for processed actions
- `logs/`: Per-subreddit log files
- `requirements.txt`: Python dependencies

## Testing

Use `--test` flag to verify configuration and Reddit API connectivity without making changes.

## Common Issues

- 401 errors: Check app type is "script" and verify client_id/client_secret
- Wiki permission denied: Ensure bot has moderator or wiki contributor access
- Rate limiting: Increase `--interval` and/or reduce `--batch-size`