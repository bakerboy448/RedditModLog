#!/usr/bin/env python3
"""
Recovery script to rebuild database from historical wiki revisions.
This recovers modlog data that was lost when the database was accidentally deleted.
"""

import json
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any, List, Tuple

import praw


def parse_wiki_content(content: str, subreddit_name: str) -> List[Tuple[Any, ...]]:
    """Parse wiki markdown content and extract modlog entries."""
    entries: list[tuple[Any, ...]] = []
    current_date = None

    lines = content.split("\n")

    for line in lines:
        # Check for date header
        date_match = re.match(r"^## (\d{4}-\d{2}-\d{2})$", line.strip())
        if date_match:
            current_date = date_match.group(1)
            continue

        # Skip table headers and dividers
        if line.startswith("|---") or line.startswith("| Time | Action"):
            continue

        # Parse table rows
        if line.startswith("|") and current_date and "|" in line[1:]:
            parts = [p.strip() for p in line.split("|")[1:-1]]  # Remove empty first/last
            if len(parts) < 6:
                continue

            time_str, action, entry_id, moderator, content, reason = parts[:6]

            # Skip empty or header rows
            if not time_str or time_str == "Time":
                continue

            # Parse timestamp
            try:
                timestamp_str = f"{current_date} {time_str}"
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S %Z")
                created_at = int(dt.timestamp())
            except ValueError:
                print(f"Warning: Could not parse timestamp: {timestamp_str}", file=sys.stderr)
                continue

            # Extract target info from content markdown
            target_author = None
            target_permalink = None
            target_type = None

            # Extract permalink
            permalink_match = re.search(r"\[.*?\]\((https://[^)]+)\)", content)
            if permalink_match:
                target_permalink = permalink_match.group(1)

            # Extract author
            author_match = re.search(r"u/([A-Za-z0-9_-]+)", content)
            if author_match:
                target_author = author_match.group(1)

            # Determine target type from action
            if "comment" in action.lower():
                target_type = "comment"
            elif "link" in action.lower() or "post" in action.lower():
                target_type = "submission"
            else:
                target_type = "unknown"

            # Clean up action type (remove filter- prefix if present)
            action_clean = action.replace("filter-", "")

            # Create entry tuple matching database schema
            entry = (
                entry_id,  # action_id
                created_at,  # created_at
                action_clean,  # action_type
                moderator,  # moderator
                entry_id,  # target_id (same as action_id for display)
                target_type,  # target_type
                entry_id,  # display_id
                target_permalink,  # target_permalink
                reason if reason and reason != "-" else None,  # removal_reason
                subreddit_name,  # subreddit
                target_author,  # target_author
            )
            entries.append(entry)

    return entries


def insert_entries(db_path: str, entries: List[Tuple]) -> Tuple[int, int]:
    """Insert entries into database, returning (inserted, skipped) counts."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    inserted = 0
    skipped = 0

    for entry in entries:
        try:
            cursor.execute(
                """
                INSERT INTO processed_actions
                (action_id, created_at, action_type, moderator, target_id,
                 target_type, display_id, target_permalink, removal_reason,
                 subreddit, target_author)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                entry,
            )
            inserted += 1
        except sqlite3.IntegrityError:
            # Already exists (UNIQUE constraint on action_id)
            skipped += 1

    conn.commit()
    conn.close()

    return inserted, skipped


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 wiki_recovery.py <config_path> [revision_offset]")
        print("  revision_offset: How many revisions back from latest (default: 10)")
        sys.exit(1)

    config_path = sys.argv[1]
    revision_offset = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    # Load config
    with open(config_path) as f:
        config = json.load(f)

    print(f"Connecting to Reddit as {config['reddit']['username']}...")
    reddit = praw.Reddit(
        client_id=config["reddit"]["client_id"],
        client_secret=config["reddit"]["client_secret"],
        username=config["reddit"]["username"],
        password=config["reddit"]["password"],
        user_agent="RedditModLog Wiki Recovery/1.0",
    )

    subreddit_name = config["source_subreddit"]
    wiki_page_name = config.get("wiki_page", "modlog")
    db_path = config.get("database_path", "/config/data/modlog.db")

    print(f"Fetching wiki revisions for /r/{subreddit_name}/wiki/{wiki_page_name}...")
    subreddit = reddit.subreddit(subreddit_name)
    wiki_page = subreddit.wiki[wiki_page_name]

    revisions_list = list(wiki_page.revisions(limit=100))
    print(f"Found {len(revisions_list)} revisions")

    if len(revisions_list) < revision_offset:
        print(f"Warning: Only {len(revisions_list)} revisions available, using oldest")
        revision_offset = len(revisions_list)

    target_rev = revisions_list[-revision_offset]
    print(f"Recovering from revision -{revision_offset} (timestamp: {target_rev['timestamp']})")

    old_page = subreddit.wiki[wiki_page_name].revision(target_rev["id"])
    old_content = old_page.content_md

    print(f"Wiki content: {len(old_content)} chars, {old_content.count(chr(10))} lines")

    print("Parsing wiki content...")
    entries = parse_wiki_content(old_content, subreddit_name)
    print(f"Parsed {len(entries)} entries from wiki")

    if not entries:
        print("No entries found! Check wiki format.")
        sys.exit(1)

    print(f"Inserting into database: {db_path}")
    inserted, skipped = insert_entries(db_path, entries)

    print(f"\nRecovery complete!")
    print(f"  Inserted: {inserted}")
    print(f"  Skipped (already exist): {skipped}")
    print(f"  Total processed: {inserted + skipped}")


if __name__ == "__main__":
    main()
