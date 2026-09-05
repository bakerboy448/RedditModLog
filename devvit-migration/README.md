# RedditModLog → Devvit Migration

Re-platforming the Python/PRAW **Reddit Modlog Wiki Publisher** into a **Devvit** app that runs on Reddit's Developer Platform — no self-hosted daemon, no bot-password credential, no SQLite. The published wiki output and its transparency/privacy guarantees stay recognizably identical.

**Feasibility: confirmed.** Devvit provides every primitive the bot needs:

| Python (PRAW) | Devvit equivalent |
| --- | --- |
| `subreddit.mod.log(limit=N)` | `reddit.getModerationLog({ subredditName, type?, limit })` |
| `subreddit.wiki[page].edit()` | `reddit.getWikiPage` / `reddit.updateWikiPage` (512 KB cap unchanged) |
| SQLite dedup + retention + hash-cache | `context.redis` (strings for dedup, sorted-set by timestamp for retention, hash for wiki-hash cache) |
| Continuous daemon (`update_interval`) | `Devvit.addSchedulerJob` + cron (1-min min granularity) + optional `onModAction` trigger |
| `config.json` / env / CLI (19 options) | Devvit app + per-install settings (`anonymize_moderators` hardcoded true) |
| Docker / s6 / systemd | Reddit-hosted (serverless, free); publish via app review |

## Layout

- [`docs/01-requirements.md`](docs/01-requirements.md) — product requirements, feature-parity matrix (MoSCoW), risk register, MVP acceptance criteria
- [`docs/02-research-api-shapes.md`](docs/02-research-api-shapes.md) — exact Devvit Reddit-API signatures (`getModerationLog`, `ModAction`, wiki) with Python→Devvit field mapping
- [`docs/03-research-platform.md`](docs/03-research-platform.md) — Redis data model, scheduler/trigger, settings, webview/UI, publishing & limits
- [`docs/04-architecture.md`](docs/04-architecture.md) — module layout, Redis key schema, execution model, settings schema, phased plan, gap list
- [`docs/STATUS.md`](docs/STATUS.md) — what is scaffolded vs. TODO, mapped to the parity matrix
- [`docs/reddit-api/`](docs/reddit-api/) — stored Devvit API reference snippets
- [`../devvit/`](../devvit/) — the Devvit app scaffold (TypeScript; compiles clean via `tsc --noEmit`)

## Status

Scaffold stage: requirements + research + architecture complete; component modules (`storage`, `modlog`, `render`, `wiki`, `settings`, `menu`, `main`) written and type-checking clean. Not yet `devvit upload`-tested against a live test subreddit. See [`docs/STATUS.md`](docs/STATUS.md).

This scaffold was produced by a multi-agent scrum (requirements → research → architecture → per-component implementation → scaffold). Treat the generated TypeScript as a reviewed starting point, not a shipped app — every Devvit API call should be validated in `devvit playtest` before publishing.
