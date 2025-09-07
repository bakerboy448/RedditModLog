# GitHub Copilot Instructions for RedditModLog

## Project Context
This is a Python-based Reddit moderation log publisher that scrapes mod actions and publishes them to wiki pages. Focus on security, reliability, and maintainability.

## Code Style & Standards
- **No comments on self-documenting code** - avoid obvious comments
- **Use conventional commits** for all changes
- **Environment variables** for all configuration (Docker-ready)
- **Security first**: Never expose moderator identities, always anonymize
- **Error handling**: Graceful degradation, comprehensive logging

## Key Architecture Patterns
- **Single-file application**: Keep core logic in `modlog_wiki_publisher.py`
- **SQLite database**: Multi-subreddit support with proper data separation
- **Configuration hierarchy**: CLI args → env vars → config file
- **Action validation**: Use `VALID_MODLOG_ACTIONS` constant for all validation

## Critical Security Rules
- **NEVER** allow `anonymize_moderators=false` in production
- **NEVER** expose actual moderator usernames in public wikis
- **ALWAYS** validate Reddit API inputs before processing
- **ALWAYS** escape markdown content (pipe characters) for table safety

## Docker & Deployment
- **Non-root user**: All containers must run as non-root for security
- **Multi-platform**: Support linux/amd64 and linux/arm64 architectures
- **Health checks**: Include SQLite connectivity validation
- **Volume mounts**: Persist data and logs outside containers
- **Environment variables**: Full configuration via env vars

## Reddit API Best Practices
- **Rate limiting**: Respect Reddit's API limits with exponential backoff
- **Authentication**: Use script-type apps, not web apps
- **Error handling**: Graceful handling of 401, 403, 429 errors
- **Content linking**: Link to actual posts/comments, never user profiles

## Database Schema
- **Multi-subreddit**: Include subreddit column in all relevant tables
- **Action deduplication**: Use action_id as primary key
- **Content IDs**: Extract from permalinks for accurate tracking
- **Schema versioning**: Implement migrations for database changes

## Testing & Quality
- **Pre-commit hooks**: Black, flake8, isort, mypy, security scanning
- **Validation**: Strict input validation with clear error messages
- **Environment testing**: `--test` flag for configuration validation
- **CI/CD**: Multi-platform builds with manual approval for main branch

## Documentation Standards
- **CLAUDE.md**: Primary developer documentation with examples
- **README.md**: User-facing documentation with setup instructions
- **Environment variables**: Document all config options with examples
- **Security warnings**: Highlight privacy and security considerations

## Forbidden Patterns
- ❌ Hardcoded credentials or configuration
- ❌ User profile links in public wikis
- ❌ Unescaped markdown content in tables
- ❌ Missing error handling for Reddit API calls
- ❌ Exposing real moderator names in production

## Preferred Libraries
- **PRAW**: Reddit API wrapper (already in use)
- **SQLite3**: Database (already in use)
- **Apprise**: Future notification support
- **Standard library**: Prefer built-in modules when possible

## Git Workflow
- **Feature branches**: Use descriptive branch names
- **Pull requests**: Required for main branch changes
- **Manual approval**: Never auto-merge to main branch
- **Conventional commits**: Use standard commit message format

When suggesting code improvements, prioritize security, reliability, and maintainability over clever optimizations.
