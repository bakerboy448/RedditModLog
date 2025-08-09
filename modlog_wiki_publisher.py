#!/usr/bin/env python3
"""
Reddit Modlog Wiki Publisher
Scrapes moderation logs and publishes them to a subreddit wiki page
"""
import os
import sys
import json
import sqlite3
import time
import argparse
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import praw

DB_PATH = "modlog.db"
LOGS_DIR = "logs"
logger = logging.getLogger(__name__)

# Configuration limits and defaults
CONFIG_LIMITS = {
    'retention_days': {'min': 1, 'max': 365, 'default': 90},
    'batch_size': {'min': 10, 'max': 500, 'default': 50},
    'update_interval': {'min': 60, 'max': 3600, 'default': 600},
    'max_wiki_entries_per_page': {'min': 100, 'max': 2000, 'default': 1000},
    'max_continuous_errors': {'min': 1, 'max': 50, 'default': 5},
    'rate_limit_buffer': {'min': 30, 'max': 300, 'default': 60},
    'max_batch_retries': {'min': 1, 'max': 10, 'default': 3},
    'archive_threshold_days': {'min': 1, 'max': 30, 'default': 7}
}

# Database schema version
CURRENT_DB_VERSION = 2

def get_db_version():
    """Get current database schema version"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if version table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='schema_version'
        """)
        
        if not cursor.fetchone():
            conn.close()
            return 0
        
        cursor.execute("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result else 0
    except Exception as e:
        logger.warning(f"Could not determine database version: {e}")
        return 0

def set_db_version(version):
    """Set database schema version"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                applied_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        
        cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
        conn.close()
        logger.info(f"Database schema version set to {version}")
    except Exception as e:
        logger.error(f"Failed to set database version: {e}")
        raise

def validate_config_value(key, value, config_limits):
    """Validate and enforce configuration limits"""
    if key not in config_limits:
        return value
    
    limits = config_limits[key]
    if value < limits['min']:
        logger.warning(f"{key} value {value} below minimum {limits['min']}, using minimum")
        return limits['min']
    elif value > limits['max']:
        logger.warning(f"{key} value {value} above maximum {limits['max']}, using maximum")
        return limits['max']
    
    return value

def apply_config_defaults_and_limits(config):
    """Apply default values and enforce limits on configuration"""
    for key, limits in CONFIG_LIMITS.items():
        if key not in config:
            config[key] = limits['default']
            logger.info(f"Using default value for {key}: {limits['default']}")
        else:
            config[key] = validate_config_value(key, config[key], CONFIG_LIMITS)
    
    # Validate required fields
    required_fields = ['reddit', 'source_subreddit']
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Missing required configuration field: {field}")
    
    # Validate reddit credentials
    reddit_config = config.get('reddit', {})
    required_reddit_fields = ['client_id', 'client_secret', 'username', 'password']
    for field in required_reddit_fields:
        if field not in reddit_config or not reddit_config[field]:
            raise ValueError(f"Missing required reddit configuration field: {field}")
    
    return config

def migrate_database():
    """Run database migrations to current version"""
    current_version = get_db_version()
    target_version = CURRENT_DB_VERSION
    
    if current_version >= target_version:
        logger.info(f"Database already at version {current_version}, no migration needed")
        return
    
    logger.info(f"Migrating database from version {current_version} to {target_version}")
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Migration from version 0 to 1: Initial schema
        if current_version < 1:
            logger.info("Applying migration: Initial schema (v0 -> v1)")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT UNIQUE NOT NULL,
                    created_at INTEGER NOT NULL,
                    processed_at INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_action_id ON processed_actions(action_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON processed_actions(created_at)")
            set_db_version(1)
        
        # Migration from version 1 to 2: Add tracking columns
        if current_version < 2:
            logger.info("Applying migration: Add tracking columns (v1 -> v2)")
            
            # Check if columns already exist to handle partial migrations
            cursor.execute("PRAGMA table_info(processed_actions)")
            existing_columns = [row[1] for row in cursor.fetchall()]
            
            columns_to_add = [
                ('action_type', 'TEXT'),
                ('moderator', 'TEXT'),
                ('target_id', 'TEXT'),
                ('target_type', 'TEXT'),
                ('display_id', 'TEXT'),
                ('target_permalink', 'TEXT')
            ]
            
            for column_name, column_type in columns_to_add:
                if column_name not in existing_columns:
                    try:
                        cursor.execute(f"ALTER TABLE processed_actions ADD COLUMN {column_name} {column_type}")
                        logger.info(f"Added column: {column_name}")
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" not in str(e):
                            raise
            
            # Add new indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_display_id ON processed_actions(display_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_target_id ON processed_actions(target_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_target_type ON processed_actions(target_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_moderator ON processed_actions(moderator)")
            
            set_db_version(2)
        
        conn.commit()
        conn.close()
        logger.info(f"Database migration completed successfully to version {target_version}")
    
    except Exception as e:
        logger.error(f"Database migration failed: {e}")
        raise

def setup_database():
    """Initialize and migrate database"""
    try:
        migrate_database()
        logger.info("Database setup completed successfully")
    except Exception as e:
        logger.error(f"Database setup failed: {e}")
        raise

def extract_target_id(action):
    """Extract Reddit ID from action target"""
    if hasattr(action, 'target_submission') and action.target_submission:
        return action.target_submission.id
    elif hasattr(action, 'target_comment') and action.target_comment:
        return action.target_comment.id
    elif hasattr(action, 'target_author') and action.target_author:
        return action.target_author.name
    else:
        return action.id  # Fallback to action ID

def get_target_type(action):
    """Determine target type for ID prefix"""
    if hasattr(action, 'target_submission') and action.target_submission:
        return 'post'
    elif hasattr(action, 'target_comment') and action.target_comment:
        return 'comment'
    elif hasattr(action, 'target_author'):
        return 'user'
    else:
        return 'action'

def generate_display_id(action):
    """Generate human-readable display ID"""
    target_id = extract_target_id(action)
    target_type = get_target_type(action)
    
    prefixes = {
        'post': 'P',
        'comment': 'C', 
        'user': 'U',
        'action': 'A'
    }
    
    prefix = prefixes.get(target_type, 'X')
    
    # Shorten long IDs for display
    if len(str(target_id)) > 8 and target_type in ['post', 'comment']:
        short_id = str(target_id)[:6]
        return f"{prefix}{short_id}"
    else:
        return f"{prefix}{target_id}"

def get_target_permalink(action):
    """Get permalink for the target content"""
    try:
        if hasattr(action, 'target_submission') and action.target_submission:
            return f"https://reddit.com{action.target_submission.permalink}"
        elif hasattr(action, 'target_comment') and action.target_comment:
            return f"https://reddit.com{action.target_comment.permalink}"
        elif hasattr(action, 'target_author') and action.target_author:
            return f"https://reddit.com/u/{action.target_author.name}"
    except:
        pass
    return None

def is_duplicate_action(action_id: str) -> bool:
    """Check if action has already been processed"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT 1 FROM processed_actions WHERE action_id = ? LIMIT 1",
            (action_id,)
        )
        
        result = cursor.fetchone() is not None
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Error checking duplicate action: {e}")
        return False

def store_processed_action(action):
    """Store processed action to prevent duplicates"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO processed_actions 
            (action_id, action_type, moderator, target_id, target_type, 
             display_id, target_permalink, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            action.id,
            action.action,
            action.mod.name if action.mod else None,
            extract_target_id(action),
            get_target_type(action),
            generate_display_id(action),
            get_target_permalink(action),
            int(action.created_utc.timestamp())
        ))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error storing processed action: {e}")
        raise

def cleanup_old_entries(retention_days: int):
    """Remove entries older than retention_days"""
    if retention_days <= 0:
        retention_days = CONFIG_LIMITS['retention_days']['default']
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cutoff_timestamp = int((datetime.now() - datetime.fromtimestamp(0)).total_seconds()) - (retention_days * 86400)
        
        cursor.execute(
            "DELETE FROM processed_actions WHERE created_at < ?",
            (cutoff_timestamp,)
        )
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old entries")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

def format_content_link(action) -> str:
    """Format content link for wiki table"""
    if hasattr(action, 'target_title') and action.target_title:
        title = action.target_title
    elif hasattr(action, 'target_author') and action.target_author:
        title = f"Content by u/{action.target_author}"
    else:
        title = "Unknown content"
    
    if hasattr(action, 'target_permalink') and action.target_permalink:
        return f"[{title}](https://reddit.com{action.target_permalink})"
    else:
        return title

def format_modlog_entry(action, config: Dict[str, Any]) -> Dict[str, str]:
    """Format modlog entry with unique ID for tracking"""
    
    display_id = generate_display_id(action)
    
    return {
        'time': action.created_utc.strftime('%H:%M:%S UTC'),
        'action': action.action,
        'id': display_id,
        'moderator': action.mod.name if action.mod else 'Unknown',
        'content': format_content_link(action),
        'reason': action.details or 'No reason',
        'inquire': generate_modmail_link(config['source_subreddit'], action)
    }

def generate_modmail_link(subreddit: str, action) -> str:
    """Generate modmail link for user inquiries"""
    subject = f"Inquiry about moderation action"
    
    if hasattr(action, 'target_title') and action.target_title:
        content_desc = action.target_title[:50]
    else:
        content_desc = "your content"
    
    body = f"I would like to inquire about the {action.action} action on {content_desc}"
    
    from urllib.parse import quote
    return f"https://reddit.com/message/compose?to=/r/{subreddit}&subject={quote(subject)}&message={quote(body)}"

def build_wiki_content(actions: List, config: Dict[str, Any]) -> str:
    """Build wiki page content from actions"""
    if not actions:
        return "No recent moderation actions found."
    
    # Enforce wiki entry limits
    max_entries = config.get('max_wiki_entries_per_page', CONFIG_LIMITS['max_wiki_entries_per_page']['default'])
    if len(actions) > max_entries:
        logger.warning(f"Truncating wiki content to {max_entries} entries (was {len(actions)})")
        actions = actions[:max_entries]
    
    # Group actions by date
    actions_by_date = {}
    for action in actions:
        date_str = action.created_utc.strftime('%Y-%m-%d')
        if date_str not in actions_by_date:
            actions_by_date[date_str] = []
        actions_by_date[date_str].append(action)
    
    # Build content
    content_parts = []
    for date_str in sorted(actions_by_date.keys(), reverse=True):
        content_parts.append(f"## {date_str}")
        content_parts.append("| Time | Action | ID | Moderator | Content | Reason | Inquire |")
        content_parts.append("|------|--------|----|-----------|---------|--------|---------|")
        
        for action in sorted(actions_by_date[date_str], key=lambda x: x.created_utc, reverse=True):
            entry = format_modlog_entry(action, config)
            content_parts.append(f"| {entry['time']} | {entry['action']} | `{entry['id']}` | {entry['moderator']} | {entry['content']} | {entry['reason']} | {entry['inquire']} |")
        
        content_parts.append("")  # Empty line between dates
    
    return "\n".join(content_parts)

def setup_reddit_client(config: Dict[str, Any]):
    """Initialize Reddit API client"""
    try:
        reddit = praw.Reddit(
            client_id=config['reddit']['client_id'],
            client_secret=config['reddit']['client_secret'],
            username=config['reddit']['username'],
            password=config['reddit']['password'],
            user_agent=f"ModlogWikiPublisher/2.0 by /u/{config['reddit']['username']}"
        )
        
        # Test authentication
        me = reddit.user.me()
        logger.info(f"Successfully authenticated as: /u/{me.name}")
        return reddit
    except Exception as e:
        logger.error(f"Failed to authenticate with Reddit: {e}")
        raise

def update_wiki_page(reddit, subreddit_name: str, wiki_page: str, content: str):
    """Update wiki page with content"""
    try:
        subreddit = reddit.subreddit(subreddit_name)
        subreddit.wiki[wiki_page].edit(
            content=content,
            reason="Automated modlog update"
        )
        logger.info(f"Updated wiki page: /r/{subreddit_name}/wiki/{wiki_page}")
    except Exception as e:
        logger.error(f"Failed to update wiki page: {e}")
        raise

def process_modlog_actions(reddit, config: Dict[str, Any]) -> List:
    """Fetch and process new modlog actions"""
    try:
        # Validate batch size
        batch_size = validate_config_value('batch_size', config.get('batch_size', 50), CONFIG_LIMITS)
        if batch_size != config.get('batch_size'):
            config['batch_size'] = batch_size
        
        subreddit = reddit.subreddit(config['source_subreddit'])
        ignored_mods = set(config.get('ignored_moderators', []))
        
        new_actions = []
        processed_count = 0
        
        logger.info(f"Fetching modlog entries from /r/{config['source_subreddit']}")
        
        for action in subreddit.mod.log(limit=batch_size):
            if action.mod and action.mod.name in ignored_mods:
                continue
            
            if is_duplicate_action(action.id):
                continue
            
            new_actions.append(action)
            store_processed_action(action)
            processed_count += 1
            
            if processed_count >= batch_size:
                break
        
        logger.info(f"Processed {processed_count} new modlog actions")
        return new_actions
    except Exception as e:
        logger.error(f"Error processing modlog actions: {e}")
        raise

def load_config(config_path: str) -> Dict[str, Any]:
    """Load and validate configuration"""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # Apply defaults and validate limits
        config = apply_config_defaults_and_limits(config)
        
        logger.info("Configuration loaded and validated successfully")
        return config
    except FileNotFoundError:
        logger.error(f"Config file not found: {config_path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file: {e}")
        raise
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        logger.error("Please check your configuration file format and required fields")
        raise

def create_argument_parser():
    """Create command line argument parser"""
    parser = argparse.ArgumentParser(
        description='Reddit Modlog Wiki Publisher',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        '--config', default='config.json',
        help='Path to configuration file'
    )
    parser.add_argument(
        '--source-subreddit',
        help='Source subreddit name'
    )
    parser.add_argument(
        '--wiki-page', default='modlog',
        help='Wiki page name'
    )
    parser.add_argument(
        '--retention-days', type=int,
        help='Database retention period in days'
    )
    parser.add_argument(
        '--batch-size', type=int,
        help='Number of entries to fetch per run'
    )
    parser.add_argument(
        '--interval', type=int,
        help='Update interval in seconds for continuous mode'
    )
    parser.add_argument(
        '--continuous', action='store_true',
        help='Run continuously with interval updates'
    )
    parser.add_argument(
        '--test', action='store_true',
        help='Test configuration and Reddit API access'
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='Enable debug logging'
    )
    parser.add_argument(
        '--show-config-limits', action='store_true',
        help='Show configuration limits and defaults'
    )
    parser.add_argument(
        '--force-migrate', action='store_true',
        help='Force database migration (use with caution)'
    )
    
    return parser

def setup_logging(debug: bool = False):
    """Setup logging configuration"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

def show_config_limits():
    """Display configuration limits and defaults"""
    print("Configuration Limits and Defaults:")
    print("=" * 50)
    for key, limits in CONFIG_LIMITS.items():
        print(f"{key}:")
        print(f"  Default: {limits['default']}")
        print(f"  Minimum: {limits['min']}")
        print(f"  Maximum: {limits['max']}")
        print()
    
    print("Required Configuration Fields:")
    print("- reddit.client_id")
    print("- reddit.client_secret")
    print("- reddit.username")
    print("- reddit.password")
    print("- source_subreddit")

def run_continuous_mode(reddit, config: Dict[str, Any]):
    """Run in continuous monitoring mode"""
    logger.info("Starting continuous mode...")
    
    error_count = 0
    max_errors = config.get('max_continuous_errors', CONFIG_LIMITS['max_continuous_errors']['default'])
    
    while True:
        try:
            error_count = 0  # Reset on successful run
            actions = process_modlog_actions(reddit, config)
            
            if actions:
                content = build_wiki_content(actions, config)
                wiki_page = config.get('wiki_page', 'modlog')
                update_wiki_page(reddit, config['source_subreddit'], wiki_page, content)
            
            cleanup_old_entries(config.get('retention_days', CONFIG_LIMITS['retention_days']['default']))
            
            interval = validate_config_value('update_interval', 
                                           config.get('update_interval', CONFIG_LIMITS['update_interval']['default']), 
                                           CONFIG_LIMITS)
            logger.info(f"Waiting {interval} seconds until next update...")
            time.sleep(interval)
            
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
            break
        except Exception as e:
            error_count += 1
            logger.error(f"Error in continuous mode (attempt {error_count}/{max_errors}): {e}")
            
            if error_count >= max_errors:
                logger.error(f"Maximum error count ({max_errors}) reached, shutting down")
                break
            
            # Exponential backoff for errors
            wait_time = min(60 * (2 ** (error_count - 1)), 300)  # Max 5 minutes
            logger.info(f"Waiting {wait_time} seconds before retry...")
            time.sleep(wait_time)

def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    
    setup_logging(args.debug)
    
    try:
        # Show configuration limits if requested
        if args.show_config_limits:
            show_config_limits()
            return
        
        # Force migration if requested
        if args.force_migrate:
            logger.info("Forcing database migration...")
            migrate_database()
            logger.info("Database migration completed")
            return
        
        setup_database()
        
        config = load_config(args.config)
        
        # Override config with CLI args
        if args.source_subreddit:
            config['source_subreddit'] = args.source_subreddit
        if args.wiki_page:
            config['wiki_page'] = args.wiki_page
        if args.retention_days is not None:
            config['retention_days'] = args.retention_days
        if args.batch_size is not None:
            config['batch_size'] = args.batch_size
        if args.interval is not None:
            config['update_interval'] = args.interval
        
        reddit = setup_reddit_client(config)
        
        if args.test:
            logger.info("Running connection test...")
            # Basic test - try to fetch one modlog entry
            subreddit = reddit.subreddit(config['source_subreddit'])
            test_entry = next(subreddit.mod.log(limit=1), None)
            if test_entry:
                logger.info("✓ Successfully connected and can read modlog")
            else:
                logger.warning("⚠ Connected but no modlog entries found")
            return
        
        # Process modlog actions
        actions = process_modlog_actions(reddit, config)
        
        if actions:
            logger.info(f"Found {len(actions)} new actions to process")
            content = build_wiki_content(actions, config)
            wiki_page = config.get('wiki_page', 'modlog')
            update_wiki_page(reddit, config['source_subreddit'], wiki_page, content)
        
        cleanup_old_entries(config.get('retention_days', CONFIG_LIMITS['retention_days']['default']))
        
        if args.continuous:
            run_continuous_mode(reddit, config)
        else:
            logger.info("Single run completed")
    
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()