# Feature Request: Apprise Notification Support

## Overview
Add optional Apprise notification support to send real-time alerts when new moderation actions are processed and added to the database.

## Motivation
- **Real-time awareness**: Moderators get immediate notifications of new actions
- **Multi-platform support**: Apprise supports 80+ notification services (Discord, Slack, Email, etc.)
- **Flexible configuration**: Each subreddit can have different notification preferences
- **Action filtering**: Only notify for specific action types or moderators

## Proposed Implementation

### 1. Configuration Options
Add new configuration fields:
```json
{
  "notifications": {
    "enabled": false,
    "apprise_urls": [
      "discord://webhook_id/webhook_token",
      "mailto://user:password@domain.com"
    ],
    "notify_actions": ["removelink", "removecomment", "spamlink", "spamcomment"],
    "ignore_moderators": ["AutoModerator"],
    "rate_limit": {
      "max_per_minute": 10,
      "burst_limit": 5
    }
  }
}
```

### 2. Environment Variable Support
- `APPRISE_ENABLED`: Enable/disable notifications
- `APPRISE_URLS`: Comma-separated notification URLs
- `NOTIFY_ACTIONS`: Comma-separated action types to notify
- `NOTIFY_RATE_LIMIT`: Rate limiting configuration

### 3. Notification Content
**Subject**: `[{subreddit}] Moderation Action: {action_type}`

**Body**:
```
Action: {action_type}
Moderator: {moderator_name}
Target: {target_author}
Content: {content_title}
Reason: {removal_reason}
Link: {content_url}
Time: {timestamp}
```

### 4. Features
- **Rate limiting**: Prevent notification spam
- **Retry logic**: Handle temporary service outages
- **Async notifications**: Don't block main processing
- **Error handling**: Graceful degradation if notifications fail
- **Testing**: `--test-notifications` flag to verify configuration

### 5. Integration Points
- **After action processing**: Send notification when new action is stored in database
- **Batch notifications**: Optional digest mode for high-volume subreddits
- **Filtering**: Skip notifications for ignored moderators or action types

## Technical Requirements

### Dependencies
- Add `apprise` to requirements.txt
- Optional dependency with graceful fallback if not installed

### Database Schema
Consider adding notification tracking table:
```sql
CREATE TABLE notification_log (
    id INTEGER PRIMARY KEY,
    action_id TEXT,
    subreddit TEXT,
    notification_urls TEXT,
    status TEXT, -- 'sent', 'failed', 'skipped'
    created_at INTEGER,
    error_message TEXT
);
```

### Configuration Validation
- Validate Apprise URLs on startup
- Test notification connectivity with `--test` flag
- Clear error messages for invalid configurations

## Example Usage

### Basic Setup
```bash
# Environment variables
export APPRISE_ENABLED=true
export APPRISE_URLS="discord://webhook_id/webhook_token"
export NOTIFY_ACTIONS="removelink,removecomment"

# Run with notifications
python modlog_wiki_publisher.py --source-subreddit usenet --continuous
```

### Docker Deployment
```yaml
environment:
  - APPRISE_ENABLED=true
  - APPRISE_URLS=discord://webhook_id/webhook_token,slack://token/channel
  - NOTIFY_ACTIONS=removelink,removecomment,spamlink,spamcomment
  - NOTIFY_RATE_LIMIT=10
```

## Security Considerations
- Store notification URLs securely (environment variables recommended)
- Rate limiting to prevent abuse
- Option to anonymize moderator names in notifications
- Validate notification URLs to prevent injection attacks

## Testing Strategy
- Unit tests for notification formatting and rate limiting
- Integration tests with mock Apprise services
- `--test-notifications` flag to verify configuration
- Documentation with example configurations for popular services

## Documentation Updates
- Add notification configuration section to CLAUDE.md
- Update README.md with notification examples
- Add troubleshooting guide for common notification issues
- Document supported Apprise URL formats

## Breaking Changes
None - this is an optional feature that defaults to disabled.

## Future Enhancements
- Web dashboard for notification management
- Custom notification templates
- Notification scheduling (e.g., daily digest)
- Integration with Reddit's real-time API for instant notifications

---

**Priority**: Medium
**Complexity**: Medium
**Estimated effort**: 1-2 days development + testing
**Dependencies**: None (Apprise is optional)