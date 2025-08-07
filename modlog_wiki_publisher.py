#!/usr/bin/env python3
"""
Reddit Modlog Wiki Publisher
Scrapes moderation logs and publishes them to a subreddit wiki page
"""
import argparse
import json
import logging
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
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS processed_actions (
                action_id TEXT PRIMARY KEY,
                action_type TEXT,
                timestamp INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON processed_actions(timestamp)
        ''')
        self.conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_created_at
            ON processed_actions(created_at)
        ''')
        self.conn.commit()

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
            (cutoff_date,)
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
        'removepost', 'removecontent','addremovalreason'
    }

    # Actions to ignore
    IGNORED_ACTIONS = {
        'addnote', 'adjust_post_crowd_control_level', 'approvecomment', 'approvelink',
        'banuser', 'community_welcome_page', 'community_widgets', 'deleterule',
        'distinguish', 'edit_comment_requirements', 'edit_post_requirements',
        'edit_saved_response', 'edited_widget', 'editrule', 'editsettings',
        'ignorereports', 'lock', 'marknsfw', 'reorderrules', 'setflair', 'spoiler',
        'sticky', 'unlock', 'unmarknsfw', 'unspoiler', 'unsticky', 'wikirevise',
        'wikipermlevel', 'wikipagelisted', 'wikipageunlisted', 'createrule','editflair'
    }

    def __init__(self, config_path: str = "config.json"):
        """Initialize with configuration"""
        self.config = self._load_config(config_path)
        self.reddit = self._init_reddit()
        self.db = ModlogDatabase(
            retention_days=self.config.get('retention_days', 30)
        )
        self.wiki_char_limit = 524288  # Reddit wiki character limit
        self.batch_size = self.config.get('batch_size', 100)

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from JSON file"""
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config: {e}")
            sys.exit(1)

    def _init_reddit(self) -> praw.Reddit:
        """Initialize Reddit API connection"""
        reddit_config = self.config['reddit']

        # Add debug logging
        logger.debug(f"Attempting login with username: {reddit_config['username']}")
        logger.debug(f"Client ID: {reddit_config['client_id'][:4]}...")  # Show first 4 chars

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
            logger.info(f"Successfully authenticated as: {me.name}")
            return reddit

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            if hasattr(e, 'response'):
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
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
            f"**Link: **:** {url}\n\n"
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
            logger.info(f"Ignoring action: [{action_type}] for entry {entry.id} by {entry.mod.name}")
            return None
        # Skip ignored moderators
        ignored_mods = self.config.get('ignored_moderators', [])
        if entry.mod.name in ignored_mods:
            logger.info(f"Ignoring action by ignored moderator: [{entry.mod.name}] for entry {entry.id}")
            return None

        # Check if already processed
        action_id = f"{entry.id}_{entry.created_utc}"
        if self.db.is_processed(action_id):
            return None
        if action_type not in self.REMOVAL_ACTIONS:
            logger.info(f'==== Processing non-removal action: [{action_type}] for entry {entry.id} by {entry.mod.name}')
            logger.info(f"Entry details: {entry.details}")
            logger.info(f"Entry target author: {entry.target_author}")
            logger.info(f"Entry target title: {entry.target_title}")
            logger.info(f"Entry target permalink: {entry.target_permalink}")
            logger.info(f"Entry created at: {entry.created_utc}")
            logger.info(f"Entry mod note: {getattr(entry, 'mod_note', None)}")
            logger.info(f"Entry description: {getattr(entry, 'description', None)}")
        # Get Mod Note
        entry.parsed_mod_note = ''
        if hasattr(entry, 'mod_note') and entry.mod_note:
            entry.parsed_mod_note = entry.mod_note.strip()
        elif hasattr(entry, 'description') and entry.description:
            entry.parsed_mod_note = entry.description.strip()
        entry.p_mod_name = ''
        entry_mod = ''
        if hasattr(entry, 'mod') and entry.mod:
            entry_mod = entry.mod.name.strip()
        if entry_mod:
            if entry_mod == '[deleted]':
                entry.p_mod_name = '[deletedHumanModerator]'
            if entry_mod == 'AutoModerator':
                entry.p_mod_name = 'AutoModerator'
            else:
                entry.p_mod_name = 'HumanModerator'
        if entry.details:
            entry.p_details = entry.details.strip()
            if action_type in ['addremovalreason']:
                entry.p_details = entry.parsed_mod_note.strip()
        else:
            entry.p_details = ''
        # Extract details
        result = {
            'id': action_id,
            'timestamp': entry.created_utc,
            'action_type': action_type,
            'moderator': entry.p_mod_name,
            'target_author': entry.target_author if entry.target_author else '[deleted]',
            'removal_reason': entry.p_details,
            'note': entry.parsed_mod_note
        }
        # Get title and URL based on action type
        # if target_permalink contents /comments/ then is comment
        if entry.target_permalink and '/comments/' in entry.target_permalink:
            is_comment = True
        else:
            is_comment = False
        # Determine Title for Wiki
        formatted_title = ''
        if is_comment and entry.target_title:
            formatted_title = entry.target_title
        elif is_comment and not entry.target_title:
            formatted_title = f"Comment by u/{result['target_author']}"
        elif not is_comment and entry.target_title:
            formatted_title = entry.target_title
        elif not is_comment and not entry.target_title:
            formatted_title = f"Post by u/{result['target_author']}"
        else:
            formatted_title = 'UnknownTitle'
        formatted_link = ''
        if entry.target_permalink:
            formatted_link = f"https://www.reddit.com{entry.target_permalink}"
        result['title'] = formatted_title
        result['url'] = formatted_link
        # Generate modmail URL for removals
        if action_type in self.REMOVAL_ACTIONS:
            result['modmail_url'] = self._generate_modmail_url(
                self.config['target_subreddit'],
                action_type,
                result['title'],
                result['url']
            )
        else:
            result['modmail_url'] = ''
        # Remove Pipes from Result Values
        for key in result:
            if isinstance(result[key], str):
                result[key] = result[key].replace('|', ' ')
        return result

    def fetch_modlog_entries(self, limit: int = 100) -> List[Dict]:
        """Fetch and process modlog entries"""
        subreddit = self.reddit.subreddit(self.config['source_subreddit'])
        entries = []

        try:
            for entry in subreddit.mod.log(limit=limit):
                processed = self._process_modlog_entry(entry)
                if processed:
                    entries.append(processed)
                    # Mark as processed
                    self.db.mark_processed(
                        processed['id'],
                        processed['action_type'],
                        processed['timestamp']
                    )

            # Sort by timestamp (newest first)
            entries.sort(key=lambda x: x['timestamp'], reverse=True)

        except Exception as e:
            logger.error(f"Error fetching modlog: {e}")

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
        print(f"Processing removal reason: {entry['removal_reason']}")
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
        """Generate wiki page content from entries"""
        if not entries:
            return "# Moderation Log\n\nNo moderation actions to display.\n\n*Last updated: {} UTC*".format(
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )

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
            ""
        ]

        # Add tables for each date
        for date in sorted(grouped.keys(), reverse=True):
            lines.append(f"## {date}")
            lines.append("")
            lines.append("| Time | Action | Moderator | Content | Reason | Inquire |")
            lines.append("|------|--------|--------|---------|--------|---------|")

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
                    "|------|--------|--------|---------|--------|---------|"
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

    def update_wiki(self, content: str) -> bool:
        """Update the wiki page with new content"""
        try:
            subreddit = self.reddit.subreddit(self.config['target_subreddit'])
            wiki_page = self.config.get('wiki_page', 'modlog')

            # Update wiki page
            subreddit.wiki[wiki_page].edit(
                content=content,
                reason="Automated modlog update"
            )

            logger.info(f"Successfully updated wiki page: /r/{self.config['target_subreddit']}/wiki/{wiki_page}")
            return True

        except Exception as e:
            logger.error(f"Error updating wiki: {e}")
            return False

    def run_once(self):
        """Run a single update cycle"""
        logger.info("Starting modlog update cycle...")

        # Cleanup old database entries
        self.db.cleanup_old_entries()

        # Fetch recent modlog entries
        entries = self.fetch_modlog_entries(limit=self.batch_size)

        if entries:
            logger.info(f"Processing {len(entries)} new modlog entries")

            # Generate wiki content
            content = self.generate_wiki_content(entries)

            # Update wiki
            self.update_wiki(content)
        else:
            logger.info("No new modlog entries to process")

    def run_continuous(self):
        """Run continuously with interval"""
        interval = self.config.get('update_interval', 300)
        logger.info(f"Starting continuous mode, updating every {interval} seconds")

        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in update cycle: {e}")

            logger.info(f"Sleeping for {interval} seconds...")
            time.sleep(interval)

    def cleanup(self):
        """Cleanup resources"""
        self.db.close()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Reddit Modlog Wiki Publisher')
    parser.add_argument(
        '--config',
        default='config.json',
        help='Path to configuration file (default: config.json)'
    )
    parser.add_argument(
        '--continuous',
        action='store_true',
        help='Run continuously (default: run once and exit)'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Test configuration and exit'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Create and run publisher
    publisher = ModlogWikiPublisher(args.config)

    try:
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
    finally:
        publisher.cleanup()


if __name__ == "__main__":
    main()