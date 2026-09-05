# RedditModLog (Devvit)

A [Reddit Devvit](https://developers.reddit.com) app that publishes a
subreddit's **moderation log** to a subreddit **wiki page** as markdown tables,
each row carrying a prefilled "removal inquiry" modmail link.

This is the Devvit port of the legacy self-hosted Python/PRAW daemon
(`../modlog_wiki_publisher.py`). It runs entirely on Reddit's Developer Platform
(serverless TypeScript/Node) — **no server, no database, no credentials to
manage**. Reddit OAuth, scheduling, and storage are provided by the platform.

> Anonymization is mandatory and non-configurable. Real moderator names are
> never published; user profiles are never linked. These are hard invariants
> (see [Parity & invariants](#parity--invariants)).

---

## How it works

```
 Reddit mod log ──getModerationLog──▶ modlog.ingest ──▶ Redis (dedup + records)
                                                              │
 scheduler cron (every 10 min) ──▶ wiki.publishFromStore ◀────┘
                                          │
                                   render.buildContent (markdown, capped)
                                          │
                                   reddit.create/updateWikiPage (hash-skipped)
```

- **Scheduler job** (`publish-modlog`, cron `*/10 * * * *`) runs the full cycle:
  ingest new actions → render → publish (skipped if unchanged) → prune old
  records past the retention window.
- **`ModAction` trigger** does a cheap incremental ingest on each moderation
  action so removals are captured promptly; the actual wiki write is coalesced
  into the next cron run (no write-per-action churn).
- **Menu items** (moderator-only): *Mod Log: Publish now* and *Mod Log: Show
  status*.

### Module map (`src/`)

| File | Responsibility |
|---|---|
| `types.ts` | Shared types + frozen constants (single source of truth). No I/O. |
| `storage.ts` | Redis data-access (Repository pattern). The only Redis module. |
| `modlog.ts` | Fetch + filter + extract `ModAction` → `ModRecord` (anonymize, dedup). |
| `render.ts` | **Pure** markdown builder: tables, links, censor, byte-cap, hash. |
| `wiki.ts` | Read/create/update wiki page + hash-skip orchestration. |
| `settings.ts` | Settings schema + validation → frozen `AppConfig`. |
| `menu.ts` | Moderator menu actions (publish now / show status). |
| `main.ts` | Entrypoint: `Devvit.configure` + all capability registrations. |

---

## Develop & deploy

Prerequisites: Node 18+, a Reddit account enrolled in the Developer Platform,
and the Devvit CLI (installed as a dev dependency here, or globally via
`npm i -g devvit`).

```bash
cd devvit
npm install

# One-time: authenticate the CLI with your Reddit account.
npm run login          # devvit login

# Type-check.
npm run type-check     # tsc --build

# Playtest on a TEST subreddit you moderate (live install, hot-reload).
npm run playtest       # devvit playtest <your-test-subreddit>

# Upload a new version to Reddit (private, installable on subs you mod).
npm run deploy         # devvit upload

# Submit for review to make it publicly installable (requires app review).
npm run launch         # devvit publish
```

> `devvit playtest` and `devvit upload` install/run the app on a subreddit you
> moderate. Use a throwaway test subreddit first. Publishing to the public app
> directory requires Reddit's app-review process.

### App name

`devvit.yaml` sets `name: redditmodlog`. This name is **globally unique** and is
claimed on first `devvit upload`. If it's taken, change it in `devvit.yaml`
before the first upload (renaming after publish is disruptive).

---

## Configuration (per-install settings)

Configured per subreddit on the app's install **Settings** page. The 19 legacy
Python options collapse to **6 settings** (Devvit owns OAuth, scheduling, and
storage; one option is hardcoded; the rest are obsolete on-platform).

| Setting | Default | Range | Legacy option |
|---|---|---|---|
| Wiki page name | `modlog` | slug | `wiki_page` |
| Action types to publish | the 7 below | multi-select | `wiki_actions` |
| Additional ignored moderators | *(none)* | CSV usernames | `ignored_moderators` |
| Retention (days) | `90` | 1–365 | `retention_days` |
| Max entries on page | `1000` | 100–2000 | `max_wiki_entries_per_page` |
| Mod-log fetch per run | `500` | 50–1000 | `batch_size` |

Default tracked actions (INV-7): `removelink`, `removecomment`, `spamlink`,
`spamcomment`, `addremovalreason`, `approvelink`, `approvecomment`.

`AutoModerator` is **always** excluded (INV-8); the CSV setting only *adds* to
that list.

---

## Parity & invariants

These rules are carried verbatim from the Python app and enforced in code:

| # | Invariant | Where enforced |
|---|---|---|
| INV-1 | **Anonymize moderators ALWAYS** — human mods → `HumanModerator`; `AutoModerator`/`Reddit` kept literal. No toggle. | `modlog.anonymizeMod` (at ingest) |
| INV-2 | **Never link user profiles** — only post/comment permalinks become hyperlinks. | `modlog.extractRecord` + `render.contentLink` |
| INV-3 | **512 KB wiki cap** — content trimmed oldest-day-first to ≤90% of cap. | `render.enforceByteCap` (+ `wiki` guard) |
| INV-4 | **Email censor + pipe-escape** on all free text. | `render.censorEmail` / `escapePipes` |
| INV-5 | **Dedup by `ModAction.id`** — each action processed once. | `storage.markSeen` (atomic NX) |
| INV-6 | **Wiki hash-skip** — never write unchanged content (SHA-256). | `wiki.publish` + `storage.getWikiHash` |
| INV-7 | **Default tracked actions** (7 types). | `types.DEFAULT_WIKI_ACTIONS` |
| INV-8 | **Default ignored mods** = `[AutoModerator]`. | `types.DEFAULT_IGNORED_MODS` + `settings` |
| INV-9 | **One install == one subreddit** — Redis is install-scoped; no cross-sub mixing. | platform + `storage` key layout |

### What changed from the Python app

- **Auth**: password-grant OAuth → platform-managed (no credentials).
- **Storage**: SQLite (schema v5) → Redis KV (dedup hash, records hash,
  time-sorted set, wiki-hash cache).
- **Loop**: continuous daemon (`update_interval`) → scheduler cron +
  `ModAction` trigger.
- **Config**: CLI/env/JSON (19 opts) → 6 install settings + 1 hardcoded.
- **Multi-subreddit**: one shared store → one install per subreddit (isolation
  is structural).

See `../devvit-migration/docs/STATUS.md` for the full scaffolded-vs-TODO matrix.
