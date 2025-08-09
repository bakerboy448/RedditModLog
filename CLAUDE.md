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
# View recent processed actions with removal reasons
sqlite3 modlog.db "SELECT action_id, action_type, moderator, removal_reason, subreddit, created_at FROM processed_actions ORDER BY created_at DESC LIMIT 10;"

# View actions by subreddit
sqlite3 modlog.db "SELECT action_type, moderator, target_author, removal_reason FROM processed_actions WHERE subreddit = 'usenet' ORDER BY created_at DESC LIMIT 5;"

# Track content lifecycle by target ID  
sqlite3 modlog.db "SELECT target_id, action_type, moderator, removal_reason, datetime(created_at, 'unixepoch') FROM processed_actions WHERE target_id LIKE '%1mkz4jm%' ORDER BY created_at;"

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
- `anonymize_moderators`: Whether to show "HumanModerator" for human mods (default: true)

### Configuration Options

**Moderator Display (`anonymize_moderators`)**:
- `true` (default): Shows "AutoMod", "Reddit", or "HumanModerator"
- `false`: Shows actual moderator usernames

**Database Storage**:
- All moderator names are stored as actual usernames in the database regardless of display setting
- Removal reasons from the Reddit API are stored in the `removal_reason` column with intelligent text/number handling
- Target authors are stored in the `target_author` column for proper content attribution
- Multi-subreddit support with `subreddit` column for data separation
- Content IDs are extracted from permalinks and stored for tracking

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

## Content Link Guidelines

**CRITICAL**: Content links in the modlog should NEVER point to user profiles (`/u/username`). Links should only point to:
- Actual removed posts (`/comments/postid/`)  
- Actual removed comments (`/comments/postid/_/commentid/`)
- No link at all if no actual content is available

User profile links are a privacy concern and not useful for modlog purposes.

## Recent Improvements (v2.1)

**Multi-Subreddit Database Support**:
- ✅ Fixed critical error that prevented multi-subreddit databases from working
- ✅ Single database now safely handles multiple subreddits with proper data separation
- ✅ Per-subreddit wiki updates without cross-contamination
- ✅ Subreddit-specific logging and error handling

**Removal Reason Transparency**:
- ✅ Fixed "Removal reason applied" showing instead of actual text
- ✅ Full transparency - shows ALL available removal reason data including template numbers
- ✅ Consistent handling between storage and display logic using correct Reddit API fields
- ✅ Displays actual removal reasons like "Invites - No asking", "This comment has been filtered due to crowd control"

**Unique Content ID Tracking**:
- ✅ Fixed duplicate IDs in markdown tables where all comments showed same post ID
- ✅ Comments now show unique comment IDs (e.g., "n7ravg2") for precise tracking
- ✅ Posts show post IDs for clear content identification
- ✅ Each modlog entry has a unique identifier for easy reference

**Content Linking and Display**:
- ✅ Content links point to actual Reddit posts/comments, never user profiles for privacy
- ✅ Fixed target authors showing as [deleted] - now displays actual usernames  
- ✅ Proper content titles extracted from Reddit API data
- ✅ AutoModerator displays as "AutoModerator" (not anonymized)
- ✅ Configurable anonymization for human moderators

**Data Integrity**:
- ✅ Pipe character escaping for markdown table compatibility
- ✅ Robust error handling for mixed subreddit scenarios  
- ✅ Database schema at version 5 with all required columns
- ✅ Consistent Reddit API field usage (action.details vs action.description)

## Common Issues

- 401 errors: Check app type is "script" and verify client_id/client_secret
- Wiki permission denied: Ensure bot has moderator or wiki contributor access
- Rate limiting: Increase `--interval` and/or reduce `--batch-size`
- always update CLAUDE.md and README.md
- if branch is not main then you may commit and push if a PR is draft or not open
- always update commands and flags
- if an .venv has been told to use, remember it
- always remove CHANGELOG from CLAUDE.md
- always create and update a changelog. this should be scripted and based on a git tag?
- always use conventional commits
- use multiple commits if needed. you may patch if easier, do this automatically
- rewrite this file to be more organized
- always escape markdown table values like removal reasons for pipes; just do this in the database, no pipes
- confirm the cache file of wiki page and ensure warn if the the same interfactively ask to force refresh
