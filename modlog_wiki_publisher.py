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
import hashlib
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
CURRENT_DB_VERSION = 5

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
    
    # Set default wiki actions if not specified
    if 'wiki_actions' not in config:
        config['wiki_actions'] = ['removelink', 'removecomment', 'addremovalreason', 'spamlink', 'spamcomment']
        logger.info("Using default wiki_actions: removals and removal reasons only")
    
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
        
        # Migration from version 2 to 3: Add removal reason column
        if current_version < 3:
            logger.info("Applying migration: Add removal reason column (v2 -> v3)")
            
            # Check if column already exists
            cursor.execute("PRAGMA table_info(processed_actions)")
            existing_columns = [row[1] for row in cursor.fetchall()]
            
            if 'removal_reason' not in existing_columns:
                try:
                    cursor.execute("ALTER TABLE processed_actions ADD COLUMN removal_reason TEXT")
                    logger.info("Added column: removal_reason")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
                        raise
            
            set_db_version(3)
        
        # Migration from version 3 to 4: Add wiki hash caching table
        if current_version < 4:
            logger.info("Applying migration: Add wiki hash caching table (v3 -> v4)")
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS wiki_hash_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subreddit TEXT NOT NULL,
                    wiki_page TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    last_updated INTEGER DEFAULT (strftime('%s', 'now')),
                    UNIQUE(subreddit, wiki_page)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_subreddit_page ON wiki_hash_cache(subreddit, wiki_page)")
            logger.info("Created wiki_hash_cache table")
            
            set_db_version(4)
        
        # Migration from version 4 to 5: Add subreddit column
        if current_version < 5:
            logger.info("Applying migration: Add subreddit column (v4 -> v5)")
            
            # Check if column already exists
            cursor.execute("PRAGMA table_info(processed_actions)")
            existing_columns = [row[1] for row in cursor.fetchall()]
            
            if 'subreddit' not in existing_columns:
                try:
                    cursor.execute("ALTER TABLE processed_actions ADD COLUMN subreddit TEXT")
                    logger.info("Added column: subreddit")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
                        raise
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_subreddit ON processed_actions(subreddit)")
            
            set_db_version(5)
        
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
        update_missing_subreddits()
        logger.info("Database setup completed successfully")
    except Exception as e:
        logger.error(f"Database setup failed: {e}")
        raise

def get_content_hash(content: str) -> str:
    """Calculate SHA-256 hash of content"""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

def get_cached_wiki_hash(subreddit: str, wiki_page: str) -> Optional[str]:
    """Get cached wiki content hash for subreddit/page"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT content_hash FROM wiki_hash_cache WHERE subreddit = ? AND wiki_page = ?",
            (subreddit, wiki_page)
        )
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        logger.warning(f"Failed to get cached wiki hash: {e}")
        return None

def update_cached_wiki_hash(subreddit: str, wiki_page: str, content_hash: str):
    """Update cached wiki content hash for subreddit/page"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO wiki_hash_cache (subreddit, wiki_page, content_hash, last_updated)
            VALUES (?, ?, ?, strftime('%s', 'now'))
        """, (subreddit, wiki_page, content_hash))
        conn.commit()
        conn.close()
        logger.debug(f"Updated cached hash for /r/{subreddit}/wiki/{wiki_page}")
    except Exception as e:
        logger.warning(f"Failed to update cached wiki hash: {e}")

def censor_email_addresses(text):
    """Censor email addresses in removal reasons"""
    if not text:
        return text
    import re
    # Replace email addresses with [EMAIL]
    return re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]', text)

def get_action_datetime(action):
    """Convert action.created_utc to datetime object regardless of input type"""
    if isinstance(action.created_utc, (int, float)):
        return datetime.fromtimestamp(action.created_utc, tz=timezone.utc)
    else:
        return action.created_utc

def get_moderator_name(action, anonymize=True):
    """Get moderator name with optional anonymization for human moderators"""
    if not action.mod:
        return None
    
    # Extract the actual moderator name
    if isinstance(action.mod, str):
        mod_name = action.mod
    else:
        mod_name = action.mod.name
    
    # Handle special cases - don't censor these, match main branch exactly
    if mod_name.lower() in ['automoderator', 'reddit']:
        if mod_name.lower() == 'automoderator':
            return 'AutoModerator'  # Match main branch exactly
        else:
            return 'Reddit'
    
    # For human moderators, show generic label or actual name based on config
    if anonymize:
        return 'HumanModerator'
    else:
        return mod_name

def extract_target_id(action):
    """Extract Reddit ID from action target - NEVER return user ID"""
    # Priority order: get actual post/comment ID first
    if hasattr(action, 'target_submission') and action.target_submission:
        if hasattr(action.target_submission, 'id'):
            return action.target_submission.id
        else:
            # Extract ID from submission object string representation
            target_str = str(action.target_submission)
            if target_str.startswith('t3_'):
                return target_str[3:]  # Remove t3_ prefix
            return target_str
    elif hasattr(action, 'target_comment') and action.target_comment:
        if hasattr(action.target_comment, 'id'):
            return action.target_comment.id
        else:
            # Extract ID from comment object string representation
            target_str = str(action.target_comment)
            if target_str.startswith('t1_'):
                return target_str[3:]  # Remove t1_ prefix
            return target_str
    else:
        # For user-related actions, use action ID instead of user ID
        return action.id

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
    """Generate human-readable display ID - NEVER use user ID"""
    target_id = extract_target_id(action)
    target_type = get_target_type(action)
    
    prefixes = {
        'post': 'P',
        'comment': 'C', 
        'user': 'U',  # Use 'A' for action ID when dealing with user actions
        'action': 'A'
    }
    
    prefix = prefixes.get(target_type, 'ZZU')
    
    # Shorten long IDs for display
    if len(str(target_id)) > 8 and target_type in ['post', 'comment']:
        short_id = str(target_id)[:6]
        return f"{prefix}{short_id}"
    else:
        return f"{prefix}{target_id}"

def get_target_permalink(action):
    """Get permalink for the target content - prioritize actual content over user profiles"""
    # Check if we have a cached permalink from database
    if hasattr(action, 'target_permalink_cached') and action.target_permalink_cached:
        return action.target_permalink_cached
    
    try:
        # Priority 1: get actual post/comment permalinks from Reddit API
        if hasattr(action, 'target_submission') and action.target_submission:
            if hasattr(action.target_submission, 'permalink'):
                return f"https://reddit.com{action.target_submission.permalink}"
            elif hasattr(action.target_submission, 'id'):
                # Construct permalink from submission ID
                return f"https://reddit.com/comments/{action.target_submission.id}/"
        elif hasattr(action, 'target_comment') and action.target_comment:
            if hasattr(action.target_comment, 'permalink'):
                return f"https://reddit.com{action.target_comment.permalink}"
            elif hasattr(action.target_comment, 'id') and hasattr(action.target_comment, 'submission'):
                # For comments, construct proper permalink with submission ID
                return f"https://reddit.com/comments/{action.target_comment.submission.id}/_/{action.target_comment.id}/"
            elif hasattr(action.target_comment, 'id'):
                # Fallback for comment without submission info
                return f"https://reddit.com/comments/{action.target_comment.id}/"
        
        # Priority 2: Try to get content permalink from action.target_permalink if it's not a user profile
        if hasattr(action, 'target_permalink') and action.target_permalink:
            permalink = action.target_permalink
            # Only use if it's actual content (contains /comments/) not user profile (/u/)
            if '/comments/' in permalink and '/u/' not in permalink:
                return f"https://reddit.com{permalink}" if not permalink.startswith('http') else permalink
        
        # NEVER fall back to user profiles - only link to actual content
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

def extract_subreddit_from_permalink(permalink):
    """Extract subreddit name from Reddit permalink URL"""
    if not permalink:
        return None
    
    import re
    # Match patterns like /r/subreddit/ or https://reddit.com/r/subreddit/
    match = re.search(r'/r/([^/]+)/', permalink)
    return match.group(1) if match else None

def store_processed_action(action, subreddit_name=None):
    """Store processed action to prevent duplicates"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Process removal reason properly - ALWAYS prefer mod_note over numeric details
        removal_reason = None
        
        # First priority: mod_note (actual removal reason text)
        if hasattr(action, 'mod_note') and action.mod_note:
            removal_reason = censor_email_addresses(str(action.mod_note).strip())
        # Second priority: details (accept ALL details text, including numbers)
        elif hasattr(action, 'details') and action.details:
            details_str = str(action.details).strip()
            removal_reason = censor_email_addresses(details_str)
        
        # Extract subreddit from URL if not provided
        target_permalink = get_target_permalink(action)
        if not subreddit_name and target_permalink:
            subreddit_name = extract_subreddit_from_permalink(target_permalink)
        
        # Add subreddit column if it doesn't exist
        cursor.execute("PRAGMA table_info(processed_actions)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subreddit' not in columns:
            cursor.execute("ALTER TABLE processed_actions ADD COLUMN subreddit TEXT")
        
        # Add target_author column if it doesn't exist
        if 'target_author' not in columns:
            cursor.execute("ALTER TABLE processed_actions ADD COLUMN target_author TEXT")
        
        # Extract target author
        target_author = None
        if hasattr(action, 'target_author') and action.target_author:
            if hasattr(action.target_author, 'name'):
                target_author = action.target_author.name
            else:
                target_author = str(action.target_author)
        
        cursor.execute("""
            INSERT OR REPLACE INTO processed_actions 
            (action_id, action_type, moderator, target_id, target_type, 
             display_id, target_permalink, removal_reason, target_author, created_at, subreddit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            action.id,
            action.action,
            get_moderator_name(action, False),  # Store actual name in database
            extract_target_id(action),
            get_target_type(action),
            generate_display_id(action),
            target_permalink,
            removal_reason.replace("|"," ") if removal_reason is not None else None,  # Store properly processed removal reason
            target_author,
            int(action.created_utc) if isinstance(action.created_utc, (int, float)) else int(action.created_utc.timestamp()),
            subreddit_name or 'unknown'
        ))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error storing processed action: {e}")
        raise

def update_missing_subreddits():
    """Update NULL subreddit entries by extracting from permalinks"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get entries with NULL subreddit but valid permalink
        cursor.execute("""
            SELECT id, target_permalink FROM processed_actions 
            WHERE subreddit IS NULL AND target_permalink IS NOT NULL
        """)
        
        updates = []
        for row_id, permalink in cursor.fetchall():
            subreddit = extract_subreddit_from_permalink(permalink)
            if subreddit:
                updates.append((subreddit, row_id))
        
        # Update entries in batches
        if updates:
            cursor.executemany(
                "UPDATE processed_actions SET subreddit = ? WHERE id = ?",
                updates
            )
            logger.info(f"Updated {len(updates)} entries with extracted subreddit names")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Error updating missing subreddits: {e}")

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

def get_recent_actions_from_db(config: Dict[str, Any], force_all_actions: bool = False, show_only_removals: bool = True) -> List:
    """Fetch recent actions from database for force refresh"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # For force refresh, get ALL actions, not just wiki_actions filter
        if force_all_actions:
            # Get all unique action types in database
            cursor.execute("SELECT DISTINCT action_type FROM processed_actions WHERE action_type IS NOT NULL")
            wiki_actions = set(row[0] for row in cursor.fetchall())
            logger.info(f"Force refresh: including all action types: {wiki_actions}")
        elif show_only_removals:
            wiki_actions = set([
                'removelink', 'removecomment', 'addremovalreason', 'spamlink', 'spamcomment'
            ])
        else:
            # Get configurable list of actions to show in wiki
            wiki_actions = set(config.get('wiki_actions', [
                'removelink', 'removecomment', 'addremovalreason', 'spamlink', 'spamcomment'
            ]))
        
        # Get recent actions within retention period
        retention_days = config.get('retention_days', CONFIG_LIMITS['retention_days']['default'])
        cutoff_timestamp = int((datetime.now() - datetime.fromtimestamp(0)).total_seconds()) - (retention_days * 86400)
        
        # Limit to max wiki entries
        max_entries = config.get('max_wiki_entries_per_page', CONFIG_LIMITS['max_wiki_entries_per_page']['default'])
        
        placeholders = ','.join(['?'] * len(wiki_actions))
        # STRICT subreddit filtering - only exact matches, no nulls
        subreddit_name = config.get('source_subreddit', '')
        
        logger.debug(f"Query parameters - cutoff: {cutoff_timestamp}, wiki_actions: {wiki_actions}, subreddit: '{subreddit_name}', max_entries: {max_entries}")
        
        # Check if actions exist for the requested subreddit
        cursor.execute("""
            SELECT COUNT(*) FROM processed_actions 
            WHERE created_at >= ? AND action_type IN ({}) 
            AND LOWER(subreddit) = LOWER(?)
        """.format(placeholders), [cutoff_timestamp] + list(wiki_actions) + [subreddit_name])
        
        action_count = cursor.fetchone()[0]
        
        # If no actions exist for this subreddit, return empty list
        if action_count == 0:
            logger.info(f"No actions found for subreddit '{subreddit_name}' in the specified time range")
            conn.close()
            return []
        
        logger.debug(f"Found {action_count} actions for subreddit '{subreddit_name}'")
        
        # Get list of all subreddits for informational purposes
        cursor.execute("""
            SELECT DISTINCT LOWER(subreddit) FROM processed_actions 
            WHERE created_at >= ? AND subreddit IS NOT NULL
        """, [cutoff_timestamp])
        
        all_subreddits = [row[0] for row in cursor.fetchall() if row[0]]
        if len(all_subreddits) > 1:
            logger.info(f"Multi-subreddit database contains data for: {sorted(all_subreddits)}")
        logger.info(f"Retrieving actions for subreddit: '{subreddit_name}'")
        
        query = f"""
            SELECT action_id, action_type, moderator, target_id, target_type, 
                   display_id, target_permalink, removal_reason, target_author, created_at 
            FROM processed_actions 
            WHERE created_at >= ? AND action_type IN ({placeholders})
            AND LOWER(subreddit) = LOWER(?)
            ORDER BY created_at DESC 
            LIMIT ?
        """
        
        cursor.execute(query, [cutoff_timestamp] + list(wiki_actions) + [subreddit_name, max_entries])
        rows = cursor.fetchall()
        conn.close()
        
        logger.debug(f"Database query returned {len(rows)} rows")
        
        # Convert database rows to mock action objects for compatibility with existing functions
        mock_actions = []
        for row in rows:
            action_id, action_type, moderator, target_id, target_type, display_id, target_permalink, removal_reason, target_author, timestamp = row
            logger.debug(f"Processing cached action: {action_type} by {moderator} at {timestamp}")
            
            # Create a mock action object with the data we have
            class MockAction:
                def __init__(self, action_id, action_type, moderator, target_id, target_type, display_id, target_permalink, removal_reason, target_author, timestamp):
                    self.id = action_id
                    self.action = action_type
                    self.mod = moderator
                    # Use the timestamp directly
                    self.created_utc = timestamp
                    self.details = removal_reason or "No removal reason found."
                    self.display_id = display_id
                    self.target_permalink = target_permalink.replace('https://reddit.com', '') if target_permalink and target_permalink.startswith('https://reddit.com') else target_permalink
                    self.target_permalink_cached = target_permalink
                    
                    # Use actual target_author from database
                    self.target_title = None
                    self.target_author = target_author  # Use actual target_author from database
                    
            mock_actions.append(MockAction(action_id, action_type, moderator, target_id, target_type, display_id, target_permalink, removal_reason, target_author, timestamp))
        
        logger.info(f"Retrieved {len(mock_actions)} actions from database for force refresh")
        return mock_actions
        
    except Exception as e:
        logger.error(f"Error fetching actions from database: {e}")
        return []

def format_content_link(action) -> str:
    """Format content link for wiki table - matches main branch approach exactly"""
    
    # Use actual Reddit API data like main branch does
    formatted_link = ''
    if hasattr(action, 'target_permalink') and action.target_permalink:
        formatted_link = f"https://www.reddit.com{action.target_permalink}"
    elif hasattr(action, 'target_permalink_cached') and action.target_permalink_cached:
        formatted_link = action.target_permalink_cached
    
    # Check if comment using main branch logic
    is_comment = bool(hasattr(action, 'target_permalink') and action.target_permalink 
                     and '/comments/' in action.target_permalink and action.target_permalink.count('/') > 6)
    
    # Determine title using main branch approach
    formatted_title = ''
    if is_comment and hasattr(action, 'target_title') and action.target_title:
        formatted_title = action.target_title
    elif is_comment and (not hasattr(action, 'target_title') or not action.target_title):
        target_author = action.target_author if hasattr(action, 'target_author') and action.target_author else '[deleted]'
        formatted_title = f"Comment by u/{target_author}"
    elif not is_comment and hasattr(action, 'target_title') and action.target_title:
        formatted_title = action.target_title
    elif not is_comment and (not hasattr(action, 'target_title') or not action.target_title):
        target_author = action.target_author if hasattr(action, 'target_author') and action.target_author else '[deleted]'
        formatted_title = f"Post by u/{target_author}"
    else:
        formatted_title = 'Unknown content'
    
    # Format with link like main branch
    if formatted_link:
        formatted_title = f"[{formatted_title}]({formatted_link})"
    return formatted_title.replace("|"," ")

def extract_content_id_from_permalink(permalink):
    """Extract the actual post/comment ID from Reddit permalink URL"""
    if not permalink:
        return None
    
    import re
    # Check for comment ID first - URLs like /comments/abc123/title/def456/
    comment_match = re.search(r'/comments/[a-zA-Z0-9]+/[^/]*/([a-zA-Z0-9]+)/?', permalink)
    if comment_match:
        return f"t1_{comment_match.group(1)}"
    
    # Extract post ID from URLs like /comments/abc123/ (only if no comment ID found)
    post_match = re.search(r'/comments/([a-zA-Z0-9]+)/', permalink)
    if post_match:
        return f"t3_{post_match.group(1)}"
    
    return None

def format_modlog_entry(action, config: Dict[str, Any]) -> Dict[str, str]:
    """Format modlog entry - matches main branch approach exactly"""
    
    # Handle removal reasons like main branch - match exact logic
    reason_text = "-"
    
    # Get mod note first (like main branch parsed_mod_note)
    parsed_mod_note = ''
    if hasattr(action, 'mod_note') and action.mod_note:
        parsed_mod_note = str(action.mod_note).strip()
    elif hasattr(action, 'details') and action.details:
        parsed_mod_note = str(action.details).strip()
    
    # Process details like main branch
    if hasattr(action, 'details') and action.details:
        reason_text = str(action.details).strip()
        # For addremovalreason, use mod_note instead of details (main branch logic)
        if action.action in ['addremovalreason']:
            reason_text = parsed_mod_note if parsed_mod_note else reason_text
    elif parsed_mod_note:
        reason_text = parsed_mod_note
    
    # Extract content ID for tracking
    content_id = "-"
    if hasattr(action, 'target_permalink') and action.target_permalink:
        extracted_id = extract_content_id_from_permalink(action.target_permalink)
        if extracted_id:
            content_id = extracted_id.replace('t3_', '').replace('t1_', '')[:8]  # Short ID for table
    
    return {
        'time': get_action_datetime(action).strftime('%H:%M:%S UTC'),
        'action': action.action,
        'id': content_id,
        'moderator': get_moderator_name(action, config.get('anonymize_moderators', True)) or 'Unknown',
        'content': format_content_link(action),
        'reason': str(reason_text).replace("|"," "),
        'inquire': generate_modmail_link(config['source_subreddit'], action)
    }

def generate_modmail_link(subreddit: str, action) -> str:
    """Generate modmail link for user inquiries with content ID for tracking"""
    from urllib.parse import quote
    
    # Determine removal type like main branch
    type_map = {
        'removelink': 'Post',
        'removepost': 'Post', 
        'removecomment': 'Comment',
        'spamlink': 'Spam Post',
        'spamcomment': 'Spam Comment',
        'removecontent': 'Content',
        'addremovalreason': 'Removal Reason',
    }
    removal_type = type_map.get(action.action, 'Content')
    
    # Get content ID for tracking
    content_id = "-"
    if hasattr(action, 'target_permalink') and action.target_permalink:
        extracted_id = extract_content_id_from_permalink(action.target_permalink)
        if extracted_id:
            content_id = extracted_id.replace('t3_', '').replace('t1_', '')[:8]
    
    # Get title and truncate if needed
    if hasattr(action, 'target_title') and action.target_title:
        title = action.target_title
    else:
        title = f"Content by u/{action.target_author}" if hasattr(action, 'target_author') and action.target_author else "Unknown content"
    
    # Truncate title if too long
    max_title_length = 50
    if len(title) > max_title_length:
        title = title[:max_title_length-3] + "..."
    
    # Get URL
    url = ""
    if hasattr(action, 'target_permalink_cached') and action.target_permalink_cached:
        url = action.target_permalink_cached
    elif hasattr(action, 'target_permalink') and action.target_permalink:
        url = f"https://www.reddit.com{action.target_permalink}" if not action.target_permalink.startswith('http') else action.target_permalink
    
    # Create subject line with content ID for tracking
    subject = f"{removal_type} Removal Inquiry - {title} [ID: {content_id}]"
    
    # Create body with content ID for easier modmail tracking
    body = (
        f"Hello Moderators of /r/{subreddit},\n\n"
        f"I would like to inquire about the recent removal of the following {removal_type.lower()}:\n\n"
        f"**Content ID:** {content_id}\n\n"
        f"**Title:** {title}\n\n"
        f"**Action Type:** {action.action}\n\n"
        f"**Link:** {url}\n\n"
        "Please provide details regarding this action.\n\n"
        "Thank you!"
    )
    
    modmail_url = f"https://www.reddit.com/message/compose?to=/r/{subreddit}&subject={quote(subject)}&message={quote(body)}"
    return f"[Contact Mods]({modmail_url})"

def build_wiki_content(actions: List, config: Dict[str, Any]) -> str:
    """Build wiki page content from actions"""
    if not actions:
        return "No recent moderation actions found."
    
    # CRITICAL: Validate all actions belong to the same subreddit before building content
    target_subreddit = config.get('source_subreddit', '')
    mixed_subreddits = set()
    
    for action in actions:
        # Check if action has subreddit info and if it matches (case-insensitive)
        if hasattr(action, 'subreddit') and action.subreddit:
            if action.subreddit.lower() != target_subreddit.lower():
                mixed_subreddits.add(action.subreddit)
    
    if mixed_subreddits:
        logger.error(f"CRITICAL: Mixed subreddit data in actions for {target_subreddit}: {mixed_subreddits}")
        raise ValueError(f"Cannot build wiki content - mixed subreddit data detected: {mixed_subreddits}")
    
    # Enforce wiki entry limits
    max_entries = config.get('max_wiki_entries_per_page', CONFIG_LIMITS['max_wiki_entries_per_page']['default'])
    if len(actions) > max_entries:
        logger.warning(f"Truncating wiki content to {max_entries} entries (was {len(actions)})")
        actions = actions[:max_entries]
    
    # Group actions by date
    actions_by_date = {}
    for action in actions:
        date_str = get_action_datetime(action).strftime('%Y-%m-%d')
        if date_str not in actions_by_date:
            actions_by_date[date_str] = []
        actions_by_date[date_str].append(action)
    
    # Build content - include ID column for tracking actions across the table
    content_parts = []
    for date_str in sorted(actions_by_date.keys(), reverse=True):
        content_parts.append(f"## {date_str}")
        content_parts.append("| Time | Action | ID | Moderator | Content | Reason | Inquire |")
        content_parts.append("|------|--------|----|-----------|---------|--------|---------|")
        
        for action in sorted(actions_by_date[date_str], key=lambda x: x.created_utc, reverse=True):
            entry = format_modlog_entry(action, config)
            content_parts.append(f"| {entry['time']} | {entry['action']} | {entry['id']} | {entry['moderator']} | {entry['content']} | {entry['reason']} | {entry['inquire']} |")
        
        content_parts.append("")  # Empty line between dates
    
    # Add bot attribution footer after all content
    content_parts.append("---")
    content_parts.append("")
    content_parts.append("*This modlog is automatically maintained by [RedditModLog](https://github.com/bakerboy448/RedditModLog) bot.*")
    
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

def update_wiki_page(reddit, subreddit_name: str, wiki_page: str, content: str, force: bool = False):
    """Update wiki page with content, using hash caching to avoid unnecessary updates"""
    try:
        # Calculate content hash
        content_hash = get_content_hash(content)
        
        # Check if content has changed (unless forced)
        cached_hash = get_cached_wiki_hash(subreddit_name, wiki_page)
        if cached_hash == content_hash:
            if force:
                logger.info(f"Wiki content unchanged, but you selected force for /r/{subreddit_name}/wiki/{wiki_page}, forcing update")
            else:
                logger.info(f"Wiki content unchanged for /r/{subreddit_name}/wiki/{wiki_page}, skipping update")
                return False
        
        # Update the wiki page
        subreddit = reddit.subreddit(subreddit_name)
        subreddit.wiki[wiki_page].edit(
            content=content,
            reason="Automated modlog update"
        )
        
        # Update the cached hash
        update_cached_wiki_hash(subreddit_name, wiki_page, content_hash)
        
        action_type = "force updated" if force else "updated"
        logger.info(f"Successfully {action_type} wiki page: /r/{subreddit_name}/wiki/{wiki_page}")
        return True
        
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
        
        # Get configurable list of actions to show in wiki
        wiki_actions = set(config.get('wiki_actions', [
            'removelink', 'removecomment', 'addremovalreason', 'spamlink', 'spamcomment'
        ]))
        
        for action in subreddit.mod.log(limit=batch_size):
            mod_name = get_moderator_name(action, False)  # Use actual name for ignore check
            if mod_name and mod_name in ignored_mods:
                continue
            
            if is_duplicate_action(action.id):
                continue
            
            # Store ALL actions to database to prevent duplicates
            store_processed_action(action, config['source_subreddit'])
            processed_count += 1
            
            # Only include specific action types in the wiki display
            if action.action in wiki_actions:
                new_actions.append(action)
            
            if processed_count >= batch_size:
                break
        
        logger.info(f"Processed {processed_count} new modlog actions")
        return new_actions
    except Exception as e:
        logger.error(f"Error processing modlog actions: {e}")
        raise

def load_config(config_path: str, auto_update: bool = True) -> Dict[str, Any]:
    """Load and validate configuration"""
    try:
        # Load existing config
        original_config = {}
        config_updated = False
        
        try:
            with open(config_path, 'r') as f:
                original_config = json.load(f)
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            raise
        
        # Store original config for comparison
        config_before = original_config.copy()
        
        # Apply defaults and validate limits
        config = apply_config_defaults_and_limits(original_config)
        
        # Check if any new defaults were added
        for key, limits in CONFIG_LIMITS.items():
            if key not in config_before:
                config_updated = True
                logger.info(f"Added new configuration field '{key}' with default value: {limits['default']}")
        
        # Auto-update config file if new defaults were added and auto_update is enabled
        if config_updated and auto_update:
            try:
                # Create backup of original config
                backup_path = f"{config_path}.backup"
                import shutil
                shutil.copy2(config_path, backup_path)
                logger.info(f"Created backup of original config: {backup_path}")
                
                # Write updated config
                with open(config_path, 'w') as f:
                    json.dump(config, f, indent=2)
                logger.info(f"Auto-updated config file '{config_path}' with new defaults")
                
            except Exception as e:
                logger.warning(f"Could not auto-update config file: {e}")
                logger.info("Configuration will still work with in-memory defaults")
        elif config_updated and not auto_update:
            logger.info("Config file updates available but auto-update disabled. Run without --no-auto-update-config to update.")
        
        logger.info("Configuration loaded and validated successfully")
        return config
        
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
    parser.add_argument(
        '--no-auto-update-config', action='store_true',
        help='Disable automatic config file updates'
    )
    parser.add_argument(
        '--force-modlog', action='store_true',
        help='Fetch ALL modlog actions from Reddit API and completely rebuild wiki from database'
    )
    parser.add_argument(
        '--force-wiki', action='store_true', 
        help='Force wiki page update even if content appears unchanged (bypasses hash check)'
    )
    parser.add_argument(
        '--force-all', action='store_true',
        help='Equivalent to --force-modlog + --force-wiki (complete rebuild and force update)'
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

def run_continuous_mode(reddit, config: Dict[str, Any], force: bool = False):
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
                update_wiki_page(reddit, config['source_subreddit'], wiki_page, content, force=first_run_force)
                first_run_force = False
            
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
            wait_time = min(BASE_BACKOFF_WAIT * (2 ** (error_count - 1)), MAX_BACKOFF_WAIT)  # Max 5 minutes
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
        
        config = load_config(args.config, auto_update=not args.no_auto_update_config)
        
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
        
        # Handle force commands
        if args.force_all:
            args.force_modlog = True
            args.force_wiki = True
            logger.info("Force all requested - will fetch from Reddit AND force wiki update")
        
        if args.force_modlog:
            logger.info("Force modlog requested - fetching ALL modlog actions from Reddit and rebuilding wiki...")
            # First, fetch all recent modlog actions to populate database
            logger.info("Fetching all modlog actions from Reddit...")
            process_modlog_actions(reddit, config)
            
            # Then rebuild wiki from database (showing only removal actions)
            logger.info("Rebuilding wiki from database...")
            actions = get_recent_actions_from_db(config, force_all_actions=False,show_only_removals=True)
            if actions:
                logger.info(f"Found {len(actions)} removal actions in database for wiki")
                content = build_wiki_content(actions, config)
                wiki_page = config.get('wiki_page', 'modlog')
                update_wiki_page(reddit, config['source_subreddit'], wiki_page, content, force=args.force_wiki)
            else:
                logger.warning("No removal actions found in database for wiki refresh")
            return
        
        # Process modlog actions
        actions = process_modlog_actions(reddit, config)
        
        if actions or args.force_wiki:
            logger.info(f"Found {len(actions)} new actions to process")
            if args.force_wiki:
                logger.info("Force Wiki Selected")
            content = build_wiki_content(actions, config)
            wiki_page = config.get('wiki_page', 'modlog')
            update_wiki_page(reddit, config['source_subreddit'], wiki_page, content, force=args.force_wiki)
        
        cleanup_old_entries(config.get('retention_days', CONFIG_LIMITS['retention_days']['default']))
        
        if args.continuous:
            run_continuous_mode(reddit, config, force=args.force_wiki)
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
