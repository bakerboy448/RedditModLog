#!/usr/bin/env python3
"""
Test script to verify removal reason processing without Reddit API calls
Creates a local markdown file to demonstrate the functionality
"""
import os
import sqlite3
import sys
from datetime import datetime

# Add the current directory to path to import our module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modlog_wiki_publisher import *


# Mock Reddit action objects for testing
class MockRedditAction:
    def __init__(self, action_id, action_type, details, mod_name, target_type="post", target_id="abc123"):
        self.id = action_id
        self.action = action_type
        self.details = details
        self.created_utc = int(datetime.now().timestamp())

        # Mock moderator
        class MockMod:
            def __init__(self, name):
                self.name = name

        self.mod = MockMod(mod_name)

        # Mock targets based on type
        if target_type == "post":
            self.target_submission = target_id
            self.target_comment = None
            self.target_author = "testuser"
            self.target_title = "Test Post Title"
            self.target_permalink = f"/r/test/comments/{target_id}/test_post/"
        elif target_type == "comment":
            self.target_submission = None
            self.target_comment = target_id
            self.target_author = "testuser"
            self.target_title = None
            self.target_permalink = f"/r/test/comments/parent123/test_post/{target_id}/"


def test_removal_reasons():
    """Test removal reason processing and storage"""
    print("Testing Removal Reason Processing")
    print("=" * 50)

    # Clean up any existing test database
    test_db = "test_modlog.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    # Override the global DB_PATH for testing
    global DB_PATH
    original_db_path = DB_PATH
    DB_PATH = test_db

    try:
        # Initialize test database
        print("   Setting up test database...")
        setup_database()

        # Verify table was created
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processed_actions'")
        if not cursor.fetchone():
            print("   Database table not found, creating manually...")
            cursor.execute(
                """
                CREATE TABLE processed_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT UNIQUE NOT NULL,
                    action_type TEXT,
                    moderator TEXT,
                    target_id TEXT,
                    target_type TEXT,
                    display_id TEXT,
                    target_permalink TEXT,
                    removal_reason TEXT,
                    created_at INTEGER NOT NULL,
                    processed_at INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """
            )
            conn.commit()
        conn.close()

        # Test cases with different removal reasons
        test_actions = [
            MockRedditAction("test1", "removelink", "Rule 1: No spam", "HumanMod1", "post", "post123"),
            MockRedditAction("test2", "removecomment", "Rule 2: Be civil", "HumanMod2", "comment", "comment456"),
            MockRedditAction("test3", "spamlink", "Spam detection", "AutoModerator", "post", "post789"),
            MockRedditAction("test4", "addremovalreason", "Adding removal reason for clarity", "HumanMod1", "post", "post999"),
            MockRedditAction("test5", "removelink", None, "HumanMod3", "post", "post111"),  # No removal reason
            MockRedditAction("test6", "removecomment", "   Rule 3: No off-topic   ", "HumanMod2", "comment", "comment222"),  # Test whitespace stripping
        ]

        print("\n1. Storing test actions...")
        for action in test_actions:
            print(f"   Storing: {action.action} - '{action.details}'")
            store_processed_action(action)

        print("\n2. Verifying database storage...")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT action_id, action_type, removal_reason FROM processed_actions ORDER BY action_id")
        results = cursor.fetchall()
        conn.close()

        for action_id, action_type, removal_reason in results:
            print(f"   {action_id}: {action_type} -> '{removal_reason}'")

        print("\n3. Testing wiki content generation...")

        # Create a mock config for testing
        mock_config = {
            "wiki_actions": ["removelink", "removecomment", "addremovalreason", "spamlink"],
            "anonymize_moderators": True,
            "source_subreddit": "test",
            "max_wiki_entries_per_page": 1000,
            "retention_days": 30,
        }

        # Get actions from database (simulating force refresh)
        actions = get_recent_actions_from_db(mock_config)
        print(f"   Retrieved {len(actions)} actions from database")

        # Generate wiki content
        wiki_content = build_wiki_content(actions, mock_config)

        # Write to local markdown file
        output_file = "test_modlog_output.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(wiki_content)

        print(f"\n4. Wiki content written to: {output_file}")
        print("\nFirst few lines of generated content:")
        print("-" * 40)
        lines = wiki_content.split("\n")
        for i, line in enumerate(lines[:15]):
            print(f"{i+1:2d}: {line}")
        if len(lines) > 15:
            print("    ... (truncated)")

        print("\n5. Checking removal reasons in wiki content...")
        if "Rule 1: No spam" in wiki_content:
            print("   ✓ Found 'Rule 1: No spam' in wiki content")
        else:
            print("   ❌ Missing 'Rule 1: No spam' in wiki content")

        if "Rule 2: Be civil" in wiki_content:
            print("   ✓ Found 'Rule 2: Be civil' in wiki content")
        else:
            print("   ❌ Missing 'Rule 2: Be civil' in wiki content")

        if "Rule 3: No off-topic" in wiki_content:
            print("   ✓ Found 'Rule 3: No off-topic' (whitespace stripped)")
        else:
            print("   ❌ Missing 'Rule 3: No off-topic' in wiki content")

        if "No reason" in wiki_content:
            print("   ✓ Found 'No reason' for action without details")
        else:
            print("   ❌ Missing 'No reason' fallback in wiki content")

        print(f"\nTest completed successfully!")
        print(f"Check '{output_file}' to see the full generated wiki content.")

    finally:
        # Restore original DB path
        DB_PATH = original_db_path

        # Clean up test database
        if os.path.exists(test_db):
            os.remove(test_db)


if __name__ == "__main__":
    test_removal_reasons()
