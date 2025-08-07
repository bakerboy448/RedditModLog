#!/usr/bin/env python3
"""
Reddit Modlog Wiki Publisher
Scrapes moderation logs and publishes them to a subreddit wiki page
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote

import praw

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ModlogDatabase:
    """SQLite database for tracking processed actions"""

    def __init__(self, db_path: str = "modlog.db", retention_days: int = 30):
        self.db_path = db_path
        self.retention_days = retention_days
        self.conn = None
        self._init_db()

    def _init_db(self):
        """Initialize database and create tables if needed"""
        self.conn = sqlite3.connect(self.db_path)

        # Create migrations table first
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Check if migration 0 is already applied
        cursor = self.conn.execute("SELECT 1 FROM schema_migrations WHERE id = 0")
        if not cursor.fetchone():
            logger.info("Applying Migration 0: Initial schema")
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS processed_actions (
                    action_id TEXT PRIMARY KEY,
                    action_type TEXT,
                    timestamp INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS modlog_entries (
                    action_id TEXT PRIMARY KEY,
                    timestamp INTEGER,
                    action_type TEXT,
                    moderator TEXT,
                    target_author TEXT,
                    title TEXT,
                    url TEXT,
                    removal_reason TEXT,
                    note TEXT,
                    modmail_url TEXT,
                    subreddit TEXT
                )
            ''')
            self.conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_modlog_timestamp
                ON modlog_entries(timestamp)
            ''')
            self.conn.execute("INSERT INTO schema_migrations (id, name) VALUES (0, 'initial schema')")
        self.conn.commit()

        # Apply migration 1 if not already applied
        cursor = self.conn.execute("SELECT 1 FROM schema_migrations WHERE id = 1")
        if not cursor.fetchone():
            logger.info("Applying Migration 1: Add subreddit column to modlog_entries")
            try:
                self.conn.execute("ALTER TABLE modlog_entries ADD COLUMN subreddit TEXT")
            except sqlite3.OperationalError:
                pass  # Already exists or failed silently
            self.conn.execute("INSERT INTO schema_migrations (id, name) VALUES (1, 'add subreddit column')")
        self.conn.commit()

        logger.info("Database initialized at %s", self.db_path)

    def store_entry(self, entry: Dict):
        """Insert or replace a modlog entry record"""
        self.conn.execute('''
            INSERT OR REPLACE INTO modlog_entries (
                action_id, timestamp, action_type, moderator, target_author,
                title, url, removal_reason, note, modmail_url, subreddit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            entry['id'],
            entry['timestamp'],
            entry['action_type'],
            entry['moderator'],
            entry['target_author'],
            entry['title'],
            entry['url'],
            entry['removal_reason'],
            entry['note'],
            entry['modmail_url'],
            entry['subreddit']
        ))
        self.conn.commit()

    def get_recent_entries(self, cutoff_timestamp: float, subreddit: Optional[str] = None) -> List[Dict]:
        """Return all modlog entries newer than the cutoff, optionally filtered by subreddit"""
        query = '''
            SELECT action_id, timestamp, action_type, moderator, target_author,
                   title, url, removal_reason, note, modmail_url
            FROM modlog_entries
            WHERE timestamp >= ?
        '''
        params = [cutoff_timestamp]

        if subreddit:
            query += ' AND subreddit = ?'
            params.append(subreddit)

        query += ' ORDER BY timestamp DESC'

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()
        return [
            {
                'id': r[0], 'timestamp': r[1], 'action_type': r[2], 'moderator': r[3],
                'target_author': r[4], 'title': r[5], 'url': r[6],
                'removal_reason': r[7], 'note': r[8], 'modmail_url': r[9]
            } for r in rows
        ]

    def is_processed(self, action_id: str) -> bool:
        """Check if an action has been processed"""
        cursor = self.conn.execute(
            "SELECT 1 FROM processed_actions WHERE action_id = ?",
            (action_id,)
        )
        return cursor.fetchone() is not None

    def mark_processed(self, action_id: str, action_type: str, timestamp: int):
        """Mark an action as processed"""
        try:
            self.conn.execute(
                "INSERT INTO processed_actions (action_id, action_type, timestamp) VALUES (?, ?, ?)",
                (action_id, action_type, timestamp)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # Already exists, ignore
            pass

    def cleanup_old_entries(self):
        """Remove entries older than retention period"""
        cutoff_date = datetime.now() - timedelta(days=self.retention_days)
        self.conn.execute(
            "DELETE FROM processed_actions WHERE created_at < ?",
            (cutoff_date.isoformat(),)
        )
        self.conn.execute(
            "DELETE FROM modlog_entries WHERE timestamp < ?",
            (cutoff_date.timestamp(),)
        )
        self.conn.commit()
        # Vacuum occasionally to reclaim space
        if time.time() % 86400 < 300:  # Once per day approximately
            self.conn.execute("VACUUM")

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()


class ModlogWikiPublisher:
    """Main class for publishing modlogs to wiki"""

    # Actions that result in content removal
    REMOVAL_ACTIONS = {
        'removelink', 'removecomment', 'spamlink', 'spamcomment',
        'removepost', 'removecontent', 'addremovalreason'
    }

    # Actions to ignore
    IGNORED_ACTIONS = {
        'addnote', 'adjust_post_crowd_control_level', 'approvecomment', 'approvelink',
        'banuser', 'community_welcome_page', 'community_widgets', 'deleterule',
        'distinguish', 'edit_comment_requirements', 'edit_post_requirements',
        'edit_saved_response', 'edited_widget', 'editrule', 'editsettings',
        'ignorereports', 'lock', 'marknsfw', 'reorderrules', 'setflair', 'spoiler',
        'sticky', 'unlock', 'unmarknsfw', 'unspoiler', 'unsticky', 'wikirevise',
        'wikipermlevel', 'wikipagelisted', 'wikipageunlisted', 'createrule', 'editflair',
        'invitemoderator', 'acceptmoderatorinvite', 'removemoderator', 'rejectmoderatorinvite',
        'unbanuser', 'setsuggestedsort', 'muteuser', 'submit_scheduled_post'
    }

    # Action groupings for statistics
    ACTION_GROUPS = {
        'spam': ['spamlink', 'spamcomment'],
        'remove': ['removelink', 'removecomment', 'removepost', 'removecontent'],
        'reason': ['addremovalreason'],
    }

    def __init__(self, config_path: str = "config.json", cli_args: Optional[argparse.Namespace] = None):
        self.config = self._load_config(config_path, cli_args or argparse.Namespace())
        self._validate_config(self.config)
        self.reddit = self._init_reddit()
        self.db = ModlogDatabase(retention_days=self.config.get('retention_days', 30))
        self.wiki_char_limit = 524288
        self.batch_size = self.config.get('batch_size', 100)

    def _load_config(self, config_path: str, cli_args: argparse.Namespace) -> dict:
        """Load JSON config, then override with CLI args"""
        config = {}
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            logger.warning("No config file found at %s, using CLI only", config_path)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in config: %s", e)
            sys.exit(1)
        
        # CLI overrides
        if hasattr(cli_args, 'source_subreddit') and cli_args.source_subreddit:
            config['source_subreddit'] = cli_args.source_subreddit
        if hasattr(cli_args, 'wiki_page') and cli_args.wiki_page:
            config['wiki_page'] = cli_args.wiki_page
        if hasattr(cli_args, 'retention_days') and cli_args.retention_days is not None:
            config['retention_days'] = cli_args.retention_days
        if hasattr(cli_args, 'batch_size') and cli_args.batch_size is not None:
            config['batch_size'] = cli_args.batch_size
        if hasattr(cli_args, 'interval') and cli_args.interval is not None:
            config['update_interval'] = cli_args.interval
        if 'target_subreddit' not in config:
            config['target_subreddit'] = config.get('source_subreddit')
        return config

    def _validate_config(self, config: dict) -> None:
        """Validate configuration has required fields"""
        required = ['reddit', 'source_subreddit']
        reddit_required = ['client_id', 'client_secret', 'username', 'password']
        
        for field in required:
            if field not in config:
                raise ValueError(f"Missing required config field: {field}")
        
        if 'reddit' in config:
            for field in reddit_required:
                if field not in config['reddit']:
                    raise ValueError(f"Missing required reddit config: {field}")
        
        # Validate retention_days is reasonable
        retention = config.get('retention_days', 30)
        if not 1 <= retention <= 365:
            logger.warning("Unusual retention_days: %s, using 30", retention)
            config['retention_days'] = 30

    def _init_reddit(self) -> praw.Reddit:
        """Initialize Reddit API connection"""
        reddit_config = self.config['reddit']

        # Add debug logging
        logger.debug("Attempting login with username: %s", reddit_config['username'])
        logger.debug("Client ID: %s...", reddit_config['client_id'][:4])  # Show first 4 chars

        try:
            reddit = praw.Reddit(
                client_id=reddit_config['client_id'],
                client_secret=reddit_config['client_secret'],
                username=reddit_config['username'],
                password=reddit_config['password'],
                user_agent=f"ModlogWikiPublisher/1.0 by /u/{reddit_config['username']}"
            )

            # Force authentication test
            me = reddit.user.me()
            logger.info("Successfully authenticated as: %s", me.name)
            return reddit

        except Exception as e:
            logger.error("Authentication failed: %s", e)
            logger.error("Error type: %s", type(e).__name__)
            if hasattr(e, 'response'):
                logger.error("Response status: %s", e.response.status_code)
                logger.error("Response body: %s", e.response.text)
            raise

    def test_connection(self) -> bool:
        """Test Reddit connection and permissions"""
        print("\n" + "="*50)
        print("Testing Reddit API Connection")
        print("="*50)

        try:
            # Test authentication with detailed error catching
            try:
                me = self.reddit.user.me()
                print(f"✓ Authenticated as: /u/{me.name}")
            except Exception as auth_error:
                print(f"❌ Authentication failed: {auth_error}")
                if hasattr(auth_error, 'response'):
                    print(f"   Status Code: {auth_error.response.status_code}")
                    print(f"   Response: {auth_error.response.text}")
                if '401' in str(auth_error):
                    print("\nCommon 401 causes:")
                    print("  - Incorrect client_id or client_secret")
                    print("  - Wrong username or password")
                    print("  - 2FA enabled (need app-specific password)")
                    print("  - Spaces/quotes in credentials")
                return False

            # Test subreddit access
            source_sub = self.reddit.subreddit(self.config['source_subreddit'])
            _ = source_sub.created_utc
            print(f"✓ Source subreddit exists: /r/{self.config['source_subreddit']}")

            # Check moderator status
            is_mod = False
            try:
                for mod in source_sub.moderator():
                    if mod.name.lower() == self.config['reddit']['username'].lower():
                        is_mod = True
                        break
            except:
                pass

            if is_mod:
                print(f"✓ User is moderator of /r/{self.config['source_subreddit']}")
            else:
                print(f"⚠ User is NOT moderator of /r/{self.config['source_subreddit']}")
                print("  You need moderator access to read modlogs")
                return False

            # Test modlog access
            try:
                log_entry = next(source_sub.mod.log(limit=1), None)
                if log_entry:
                    print(f"✓ Can read modlog (latest action: {log_entry.action})")
                else:
                    print("⚠ No modlog entries found (might be empty)")
            except Exception as e:
                print(f"❌ Cannot read modlog: {e}")
                return False

            # Test wiki access
            target_sub = self.reddit.subreddit(self.config['target_subreddit'])
            wiki_page = self.config['wiki_page']

            try:
                page = target_sub.wiki[wiki_page]
                content = page.content_md
                print(f"✓ Wiki page exists: /r/{self.config['target_subreddit']}/wiki/{wiki_page}")
                print(f"  Current size: {len(content)} characters")
            except:
                print(f"⚠ Wiki page doesn't exist yet: /r/{self.config['target_subreddit']}/wiki/{wiki_page}")
                print("  It will be created on first run")

            print("\n✓ All tests passed!")
            return True

        except Exception as e:
            print(f"❌ Connection test failed: {e}")
            return False

    def sanitize_for_table(self, text: str) -> str:
        """Sanitize text for markdown table display"""
        if not text:
            return ''
        # Replace pipes with similar Unicode character and clean whitespace
        return text.replace('|', '┃').strip()

    def get_action_group(self, action_type: str) -> str:
        """Get the group name for an action type"""
        for group, actions in self.ACTION_GROUPS.items():
            if action_type in actions:
                return group
        return 'other'

    def _format_timestamp(self, timestamp: float) -> str:
        """Format timestamp as HH:MM:SS UTC"""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt.strftime("%H:%M:%S UTC")

    def _format_date(self, timestamp: float) -> str:
        """Format timestamp as YYYY-MM-DD"""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")

    def _generate_modmail_url(self, subreddit: str, action_type: str, title: str, url: str) -> str:
        """Generate pre-populated modmail URL"""
        # Determine removal type
        type_map = {
            'removelink': 'Post',
            'removepost': 'Post',
            'removecomment': 'Comment',
            'spamlink': 'Spam Post',
            'spamcomment': 'Spam Comment',
            'removecontent': 'Content',
            'addremovalreason': 'Removal Reason',
        }
        removal_type = type_map.get(action_type, 'Content')

        # Truncate title if too long
        max_title_length = 50
        if len(title) > max_title_length:
            title = title[:max_title_length-3] + "..."

        # Create subject line
        subject = f"{removal_type} Removal Inquiry - {title}"
        body = (
            f"Hello Moderators of /r/{subreddit},\n\n"
            f"I would like to inquire about the recent removal of the following {removal_type.lower()}:\n\n"
            f"**Title:** {title}\n\n"
            f"**Action Type:** {action_type}\n\n"
            f"**Link:** {url}\n\n"
            "Please provide details regarding this action.\n\n"
            "Thank you!"
        )

        # Generate modmail URL
        url = f"https://www.reddit.com/message/compose?to=/r/{subreddit}&subject={quote(subject)}&message={quote(body)}"
        return url

    def _process_modlog_entry(self, entry) -> Optional[Dict]:
        """Process a single modlog entry"""
        action_type = entry.action

        # Skip ignored actions
        if action_type in self.IGNORED_ACTIONS:
            logger.debug("Ignoring action: [%s] for entry %s by %s", action_type, entry.id, entry.mod.name)
            return None
        
        # Skip ignored moderators
        ignored_mods = self.config.get('ignored_moderators', [])
        if entry.mod.name in ignored_mods:
            logger.debug("Ignoring action by ignored moderator: [%s] for entry %s", entry.mod.name, entry.id)
            return None

        # Check if already processed
        action_id = f"{entry.id}_{entry.created_utc}"
        if self.db.is_processed(action_id):
            return None
        
        # Debug logging for non-removal actions
        if action_type not in self.REMOVAL_ACTIONS:
            logger.debug('Processing non-removal action: [%s] for entry %s by %s', action_type, entry.id, entry.mod.name)
            logger.debug("Entry details: %s", entry.details)
            logger.debug("Entry target author: %s", entry.target_author)
            logger.debug("Entry target title: %s", entry.target_title)
            logger.debug("Entry target permalink: %s", entry.target_permalink)
        
        # Get Mod Note
        parsed_mod_note = ''
        if hasattr(entry, 'mod_note') and entry.mod_note:
            parsed_mod_note = entry.mod_note.strip()
        elif hasattr(entry, 'description') and entry.description:
            parsed_mod_note = entry.description.strip()
        
        # Process moderator name (FIXED BUG: using elif)
        p_mod_name = ''
        entry_mod = ''
        if hasattr(entry, 'mod') and entry.mod:
            entry_mod = entry.mod.name.strip()
        
        if entry_mod:
            if entry_mod == '[deleted]':
                p_mod_name = '[deletedHumanModerator]'
            elif entry_mod == 'AutoModerator':
                p_mod_name = 'AutoModerator'
            elif entry_mod == 'reddit':
                p_mod_name = 'reddit'
            else:
                p_mod_name = 'HumanModerator'
        
        # Process details
        p_details = ''
        if entry.details:
            p_details = entry.details.strip()
            if action_type in ['addremovalreason']:
                p_details = parsed_mod_note.strip()
        
        # Check if comment (improved detection)
        is_comment = bool(entry.target_permalink and '/comments/' in entry.target_permalink 
                         and entry.target_permalink.count('/') > 6)
        
        # Determine Title for Wiki
        formatted_title = ''
        if is_comment and entry.target_title:
            formatted_title = entry.target_title
        elif is_comment and not entry.target_title:
            formatted_title = f"Comment by u/{entry.target_author if entry.target_author else '[deleted]'}"
        elif not is_comment and entry.target_title:
            formatted_title = entry.target_title
        elif not is_comment and not entry.target_title:
            formatted_title = f"Post by u/{entry.target_author if entry.target_author else '[deleted]'}"
        else:
            formatted_title = 'UnknownTitle'
        
        formatted_link = ''
        if entry.target_permalink:
            formatted_link = f"https://www.reddit.com{entry.target_permalink}"
        
        # Build result with sanitization
        result = {
            'id': action_id,
            'timestamp': entry.created_utc,
            'action_type': action_type,
            'moderator': self.sanitize_for_table(p_mod_name),
            'target_author': self.sanitize_for_table(entry.target_author or '[deleted]'),
            'removal_reason': self.sanitize_for_table(p_details),
            'note': self.sanitize_for_table(parsed_mod_note),
            'title': self.sanitize_for_table(formatted_title),
            'url': formatted_link  # URLs don't need sanitization
        }
        
        # Generate modmail URL for removals
        if action_type in self.REMOVAL_ACTIONS:
            result['modmail_url'] = self._generate_modmail_url(
                self.config['target_subreddit'],
                action_type,
                result['title'],
                result['url']
            )
        else:
            logger.debug("Non-removal action, skipping modmail URL generation")
            result['modmail_url'] = ''
        
        return result

    def fetch_modlog_entries(self, limit: int = 100) -> List[Dict]:
        """Fetch and process modlog entries with rate limit handling"""
        subreddit = self.reddit.subreddit(self.config['source_subreddit'])
        entries = []

        try:
            for entry in subreddit.mod.log(limit=limit):
                try:
                    processed = self._process_modlog_entry(entry)
                    if processed:
                        processed['subreddit'] = subreddit.display_name
                        entries.append(processed)
                        # Mark as processed
                        self.db.mark_processed(
                            processed['id'],
                            processed['action_type'],
                            processed['timestamp']
                        )
                        self.db.store_entry(processed)
                except praw.exceptions.APIException as e:
                    if e.error_type == "RATELIMIT":
                        # Extract wait time from message
                        import re
                        match = re.search(r'(\d+) minute', str(e))
                        wait_time = int(match.group(1)) * 60 if match else 60
                        logger.warning("Rate limited, waiting %s seconds", wait_time)
                        time.sleep(wait_time)
                    else:
                        raise
            
            # Sort by timestamp (newest first)
            entries.sort(key=lambda x: x['timestamp'], reverse=True)

        except Exception as e:
            logger.error("Error fetching modlog: %s", e)

        return entries

    def _format_table_row(self, entry: Dict) -> str:
        """Format a single entry as a table row"""
        # Format action with moderator
        action = f"{entry['action_type']}"
        moderator = entry['moderator']
        
        # Format title with URL
        if entry['url']:
            title = f"[{entry['title']}]({entry['url']})"
        else:
            title = f"{entry['title']}"

        # Format removal reason
        reason = entry['removal_reason'] or entry['note'] or '-'
        
        # Format inquire link
        if entry['modmail_url']:
            inquire = f"[Contact Mods]({entry['modmail_url']})"
        else:
            inquire = '-'

        # Format time
        time_str = self._format_timestamp(entry['timestamp'])
        return f"| {time_str} | {action} | {moderator} | {title} | {reason} | {inquire} |"

    def generate_wiki_content(self, entries: List[Dict]) -> str:
        """Generate wiki page content with statistics"""
        if not entries:
            return "# Moderation Log\n\nNo moderation actions to display.\n\n*Last updated: {} UTC*".format(
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )

        # Calculate statistics
        total_actions = len(entries)
        action_counts = {}
        for entry in entries:
            action = entry['action_type']
            action_counts[action] = action_counts.get(action, 0) + 1

        # Group entries by date
        grouped = {}
        for entry in entries:
            date = self._format_date(entry['timestamp'])
            if date not in grouped:
                grouped[date] = []
            grouped[date].append(entry)

        # Build content
        lines = [
            "# Moderation Log",
            "",
            f"*Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC*",
            f"*Total actions in period: {total_actions}*",
            ""
        ]

        # Add summary if there are actions
        if action_counts and len(action_counts) > 1:  # Only show if there's variety
            lines.append("## Summary")
            lines.append("")
            # Sort by count descending, show top 5
            for action, count in sorted(action_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                lines.append(f"- **{action}**: {count}")
            if len(action_counts) > 5:
                lines.append(f"- *...and {len(action_counts) - 5} other action types*")
            lines.append("")

        # Add tables for each date
        for date in sorted(grouped.keys(), reverse=True):
            lines.append(f"## {date}")
            lines.append("")
            lines.append("| Time | Action | Moderator | Content | Reason | Inquire |")
            lines.append("|------|--------|-----------|---------|--------|---------|")

            for entry in grouped[date]:
                row = self._format_table_row(entry)
                lines.append(row)

            lines.append("")

        content = "\n".join(lines)

        # Check size limit
        if len(content) > self.wiki_char_limit:
            logger.warning("Wiki content exceeds character limit, truncating...")
            # Keep header and as many recent entries as possible
            lines = lines[:4]  # Keep header
            lines.append("\n**Note: Content truncated due to size limits**\n")
            # Add dates/entries until we approach the limit
            for date in sorted(grouped.keys(), reverse=True):
                date_section = [
                    f"## {date}",
                    "",
                    "| Time | Action | Moderator | Content | Reason | Inquire |",
                    "|------|--------|-----------|---------|--------|---------|"
                ]
                for entry in grouped[date]:
                    row = self._format_table_row(entry)
                    date_section.append(row)
                date_section.append("")

                section_text = "\n".join(date_section)
                if len("\n".join(lines)) + len(section_text) < self.wiki_char_limit - 1000:
                    lines.extend(date_section)
                else:
                    break

            content = "\n".join(lines)

        return content

    def update_wiki(self, new_entries: List[Dict]) -> bool:
        """Merge with existing wiki content and update"""
        try:
            subreddit = self.reddit.subreddit(self.config['target_subreddit'])
            wiki_page = self.config.get('wiki_page', 'modlog')

            # Get current wiki content (for logging purposes)
            try:
                existing_content = subreddit.wiki[wiki_page].content_md
                logger.debug("Existing wiki content size: %s characters", len(existing_content))
            except Exception:
                logger.info("Wiki page doesn't exist yet, will create new")

            # Only use DB entries; wiki parsing no longer needed
            cutoff = time.time() - self.config.get('retention_days', 30) * 86400
            retained = self.db.get_recent_entries(cutoff, subreddit=self.config['source_subreddit'])

            # Sort newest first
            retained.sort(key=lambda x: x['timestamp'], reverse=True)

            # Render content
            content = self.generate_wiki_content(retained)

            # Update the wiki
            subreddit.wiki[wiki_page].edit(
                content=content,
                reason="Rolling modlog update with retention"
            )
            logger.info("Wiki page updated with %s entries.", len(retained))
            return True

        except praw.exceptions.APIException as e:
            if e.error_type == "RATELIMIT":
                logger.error("Rate limited when updating wiki: %s", e)
                return False
            else:
                raise
        except Exception as e:
            logger.error("Failed to update wiki: %s", e)
            return False

    def run_once(self):
        """Run a single update cycle"""
        logger.info("Starting modlog update cycle...")

        # Cleanup old database entries
        self.db.cleanup_old_entries()

        # Fetch recent modlog entries
        entries = self.fetch_modlog_entries(limit=self.batch_size)

        if entries:
            logger.info("Processing %s new modlog entries", len(entries))
            # Update wiki with current database content
            self.update_wiki(entries)
        else:
            logger.info("No new modlog entries to process")

    def run_continuous(self):
        """Run continuously with interval"""
        interval = self.config.get('update_interval', 300)
        logger.info("Starting continuous mode, updating every %s seconds", interval)

        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.error("Error in update cycle: %s", e)

            logger.info("Sleeping for %s seconds...", interval)
            time.sleep(interval)

    def cleanup(self):
        """Cleanup resources"""
        self.db.close()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Reddit Modlog Wiki Publisher')
    parser.add_argument('--config', default='config.json', help='Path to configuration file')
    parser.add_argument('--source-subreddit', help='Source subreddit (modlog source)')
    parser.add_argument('--wiki-page', help='Wiki page name (default: modlog)')
    parser.add_argument('--retention-days', type=int, help='Retention window in days')
    parser.add_argument('--batch-size', type=int, help='Batch size to fetch per run')
    parser.add_argument('--interval', type=int, help='Interval (seconds) for continuous mode')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--continuous', action='store_true', help='Run continuously')
    parser.add_argument('--test', action='store_true', help='Test configuration and exit')
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # Create and run publisher
        publisher = ModlogWikiPublisher(args.config, args)

        if args.test:
            # Test mode - just validate connection
            success = publisher.test_connection()
            sys.exit(0 if success else 1)
        elif args.continuous:
            # Continuous mode
            publisher.run_continuous()
        else:
            # Default: run once
            publisher.run_once()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        sys.exit(1)
    finally:
        if 'publisher' in locals():
            publisher.cleanup()


if __name__ == "__main__":
    main()