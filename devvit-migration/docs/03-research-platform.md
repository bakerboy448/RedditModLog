I have everything needed. Writing the report.

---

# Devvit Platform Research: RedditModLog Migration

Sources verified against `reddit/devvit-docs` (latest `docs/`) and `reddit/devvit` (`packages/reddit/src/`) on 2026-06-23. Doc commit pin: `69f35f2`.

## 0. Architecture note — this is "Devvit Web", not classic Devvit

The current platform is **Devvit Web**: a serverless Node HTTP server (Hono or Express) where capabilities are declared in **`devvit.json`** and wired to `/internal/...` POST endpoints. The classic `Devvit.addSchedulerJob` / `Devvit.addSettings` / `Devvit.addTrigger` builder API (from version-0.11 docs) is the *old* model. **Target the Devvit Web model.** Imports come from `@devvit/web/server`, `@devvit/redis`, `@devvit/web/shared`, and `@devvit/reddit`.

The 3 Reddit calls map cleanly:
- `subreddit.mod.log(limit=N)` → `reddit.getModerationLog({ subredditName, limit, pageSize, type?, moderatorUsernames? })` returns `Listing<ModAction>`; call `.all()` or paginate.
- `subreddit.wiki[page].edit()` → `reddit.getWikiPage(sub, page, revisionId?)` + `reddit.updateWikiPage({ subredditName, page, content, reason? })` returns `WikiPage`. (Use `createWikiPage` first-time; `updateWikiPage` fails if page absent — catch and create.)
- OAuth password-grant → **gone**. The app runs as the installed app identity on Reddit infra; no auth code, no client secret, no refresh tokens. This deletes ~all of the Python auth layer.

`ModAction` shape (from `models/ModAction.ts`) gives you everything the Python field-extractor needs natively: `id`, `type` (`ModActionType` union — includes all 7 `wiki_actions` defaults: `removelink`, `removecomment`, `spamlink`, `spamcomment`, `addremovalreason`, `approvelink`, `approvecomment`), `moderatorName`, `moderatorId` (T2), `createdAt: Date`, `subredditName`, `subredditId`, `description`, `details`, and `target?: { id, author?, body?, permalink?, title? }`. The `target.permalink` is relative (no `https://www.reddit.com` prefix) — matches the Python "never link profiles, only post/comment permalinks" invariant since `target` only carries content permalinks. `display_id` P/C/U/A prefixing is derived from `target.id` fullname prefix (`t3_`/`t1_`) client-side as today.

---

## 1. Redis: command set, limits, and the SQLite → Redis data model

### Supported commands (Devvit subset)
- **Simple:** `get`, `set`, `exists`, `del`, `type`, `rename`
- **Batch:** `mGet`, `mSet`
- **Strings:** `getRange`, `setRange`, `strLen`
- **Numbers:** `incrBy`
- **Hash:** `hGet`, `hMGet` (allowlisted — may be disabled), `hSet`, `hSetNX`, `hDel`, `hGetAll`, `hKeys`, `hScan`, `hIncrBy`, `hLen`
- **Sorted set:** `zAdd`, `zCard`, `zRange` (by `score`/`lex`/`rank`), `zRem`, `zScore`, `zRank`, `zIncrBy`, `zScan`, `zRemRangeByLex`, `zRemRangeByRank`, **`zRemRangeByScore`**
- **Expiration:** `expire`, `expireTime` (seconds; per-key TTL supported)
- **Transactions:** `watch` → `multi`/`exec`/`discard`/`unwatch` (optimistic, WATCH-based)
- **Bitfield:** `bitfield`
- **NOT supported:** plain SETs, LISTs, pub/sub, `KEYS`/global scan, pipelining, `SCAN` over all keys.

### Limits (verified, latest)
| Limit | Value |
|---|---|
| Storage per installation | **500 MB** |
| Request size | **5 MB** |
| Command throughput | 40,000 cmds/s per installation |
| Per-setting/value | governed by 5 MB request cap; use `redisCompressed` (gzip proxy) for large values |
| Transactions | 20–30 concurrent blocks, 5 s execution timeout |
| Hash size | ~4.2 B field-value pairs |
| `zRange` BYSCORE/BYLEX | LIMIT capped at 1000/call |
| TTL | per-key via `expire` (seconds), no max documented |

**Critical design constraint:** Redis is **namespaced per installation (per-subreddit)** — there is NO shared cross-subreddit store. This *changes the Python "multi-subreddit single store" invariant*: each subreddit install has its own siloed Redis. The app is still multi-subreddit (one app, many installs), but state is naturally partitioned. This is actually simpler — drop the `subreddit` column entirely; it's implicit. No `KEYS` scan means **you must track collection keys explicitly** (use hashes/sorted-sets as known collection roots, never one-key-per-record that you'd later need to enumerate).

### Concrete data model (replaces SQLite schema v5)

All keys are implicitly per-subreddit. Per wiki page (the Python app supports a configurable wiki page name), namespace with the page name.

**(a) Dedup — replaces `processed_actions(action_id UNIQUE)`**
The cheapest correct dedup is a **string key with TTL** (auto-handles 90-day retention for the dedup check itself):
```ts
const key = `seen:${actionId}`;           // ModAction.id
const isNew = await redis.set(key, "1", { nx: true, expiration: ... });
// or: if (!(await redis.exists(key))) { ...process...; await redis.set(key,"1"); await redis.expire(key, 90*86400); }
```
Use `exists`/`set nx` for the UNIQUE-constraint semantics. TTL of `retention_days*86400` makes dedup keys self-expire, so old action IDs can recur after retention (matches Python behavior where retention prunes the table).

**(b) Per-subreddit action records — replaces the row columns**
Store the rendered/extracted record in a **hash keyed by page**, field = action_id:
```ts
// hash: actions:<wikiPage>  field: <actionId>  value: JSON(record)
await redis.hSet(`actions:${page}`, { [actionId]: JSON.stringify(record) });
```
`record` carries the columns you actually render: `created_at, action_type, moderator(anon-id), target_id, target_type, display_id, target_permalink, removal_reason, target_author`. Iterate with `hScan`/`hGetAll` to rebuild the table. For large logs use `redisCompressed.hSet`. This is the "stable collection key" pattern the docs mandate (no global key scan).

**(c) Retention — replaces `DELETE WHERE created_at < now-90d` via sorted set + `zRemRangeByScore`**
Maintain a time index so you can prune both the hash and the index without scanning:
```ts
// zAdd member=actionId, score=createdAt_epoch_seconds
await redis.zAdd(`actions_by_time:${page}`, { member: actionId, score: createdAtSec });

// retention job (scheduler): find + remove old, then hDel from the record hash
const cutoff = nowSec - retentionDays*86400;
const old = await redis.zRange(`actions_by_time:${page}`, 0, cutoff, { by: "score" }); // members < cutoff
if (old.length) {
  await redis.hDel(`actions:${page}`, old.map(o => o.member));
  await redis.zRemRangeByScore(`actions_by_time:${page}`, 0, cutoff);
}
```
This is exactly the FIFO/retention pattern the docs recommend (sorted-set timestamp score → `zRange` oldest → remove). Wrap the hDel+zRemRange in a `watch`/`multi`/`exec` transaction if concurrent writers are a concern, but a scheduled single-writer cleanup avoids that need.

**(d) Wiki-hash cache — replaces `wiki_hash_cache` (skip unchanged writes)**
Single hash, one field per page:
```ts
// hash: wiki_hash  field: <page>  value: <sha256 hex>
const prev = await redis.hGet("wiki_hash", page);
if (prev !== newHash) {
  await reddit.updateWikiPage({ subredditName, page, content, reason: "modlog update" });
  await redis.hSet("wiki_hash", { [page]: newHash });
}
```
SHA-256 in TS: `crypto.subtle.digest` (Web Crypto is available in the Node runtime) or `node:crypto`. The 512 KB / 524288-byte wiki cap still applies — keep the trim-to-512KB logic verbatim.

---

## 2. Scheduler vs. onModAction trigger

### Scheduler (Devvit Web)
Declared in `devvit.json`, handled at a `/internal/scheduler/...` endpoint:
```json
"scheduler": { "tasks": {
  "publish-modlog": { "endpoint": "/internal/scheduler/publish-modlog", "cron": "*/10 * * * *" }
}}
```
- **Cron:** standard 5-field UNIX cron. **Experimental 6-field (seconds) granularity** exists (`*/30 * * * * *`).
- **Min interval:** effectively **1 minute** for standard cron (jobs run once/minute); sub-minute only via the experimental seconds field, and actual cadence depends on job duration/parallelism.
- **One-off jobs** at runtime: `scheduler.runJob({ name, data, runAt })` → returns jobId; `scheduler.cancelJob(jobId)`; `scheduler.listJobs()`.
- **Limits (per installation):** max **10 live recurring actions**; `runJob()` creation rate **60/min**; delivery rate **60/min**. Request execution has a ~30 s wall-clock limit (use daisy-chained one-off jobs for long work — see the Redis migration example pattern).

`update_interval=600s` → `cron: "*/10 * * * *"`. This is the direct, simple replacement for the daemon loop.

### onModAction trigger — it EXISTS
`onModAction` is a supported trigger (full list includes `onModAction`, `onModMail`, plus per-post/comment create/delete/report/update). Declared in `devvit.json`:
```json
"triggers": { "onModAction": "/internal/on-mod-action" }
```
Payload is a `ModAction` (see `ModActions` model). **Caveat (from docs):** *triggers are NOT exactly-once* — "Triggers are not guaranteed to deliver only once... checking if content has been recently actioned before taking action again." Your Redis dedup (`seen:<actionId>`) already handles this.

### Recommendation: **cron-primary, trigger-optional (hybrid)**
- **Use cron (`*/10`) as the system of record** for publishing. It preserves the existing batched-publish semantics, the SHA-256 hash-skip, and the 512KB-trim in one place; it's resilient to missed/duplicate trigger deliveries; and it respects the wiki-write rate naturally (one write per interval, only if changed). This is the lowest-risk 1:1 port.
- **Optionally add `onModAction`** purely as an **ingest fast-path**: on each event, dedup + write the record to the `actions:<page>` hash + `actions_by_time` zset, but **do NOT publish from the trigger** (publishing on every mod action would hammer the wiki and risk the write rate). Let cron own the wiki publish.
- If you want lower latency than 10 min without trigger complexity, just lower the cron to `*/5` or `*/2`. Given the 10-recurring-jobs cap and 60/min rate, cron alone is sufficient; the trigger is an optimization, not a requirement. **Start cron-only; add the trigger later if latency matters.**

---

## 3. App settings — mapping the 19 Python config options

Two scopes (declared in `devvit.json` under `settings`):
- **`global`** — set by the developer, shared across all installs; **secrets** live here (`isSecret: true`, CLI-only, encrypted). At least one install must exist before secrets can be set.
- **`subreddit`** — per-install, editable by moderators in the Install Settings UI.

Types: `string`, `boolean`, `number`, `select` (single), `multiSelect`. Optional `validationEndpoint` (`/internal/settings/validate-*`). Read via `import { settings } from "@devvit/web/server"; await settings.get("key")`.

**Limits:** max **2 KB per setting value**; secrets are **global-only**; secret values are not fully surfaced in CLI; `.env` only works during playtest.

### Mapping (illustrative; the Python app has 19 opts across auth/behavior/lists)
| Python config | Devvit scope | Type | Notes |
|---|---|---|---|
| client_id / client_secret / username / password / user_agent (OAuth) | **DROP** | — | No auth needed; app runs as installed identity |
| subreddits (list) | **DROP** | — | One install per subreddit; implicit. Multi-sub = install in each sub |
| wiki_page (name) | `subreddit` | `string` | Per-install page name |
| update_interval (600) | (app) | — | Express as `cron` in `devvit.json`, not a runtime setting |
| retention_days (90) | `subreddit` | `number` | + `validationEndpoint` for bounds |
| wiki_actions (list, 7 defaults) | `subreddit` | `multiSelect` | options = ModActionType values |
| ignored_moderators (`[AutoModerator]`) | `subreddit` | `string` (CSV) or `multiSelect` | parse client-side |
| anonymize_moderators (**enforced true**) | **DROP / hardcode true** | — | Don't expose as a setting; keep the invariant in code so it can't be disabled |
| censor emails / pipe-escape toggles | hardcode | — | invariants, not user-facing |
| max wiki bytes (512KB) | hardcode const | — | platform cap, not configurable |
| db path / log level / daemon flags | **DROP** | — | serverless; use `console.log` + `devvit logs` |

**Key decision:** the `anonymize_moderators=true` "refuse start if false" invariant is best preserved by **not making it a setting at all** — bake it in. Settings are mod-editable; a security invariant must not be toggleable. Same for the never-link-profiles rule.

---

## 4. UI options, publish/review, resource limits

### UI options
- **Menu actions** (recommended primary UI): three-dot menu items declared in `devvit.json` `menu.items[]`, with `location: comment|post|subreddit`, `forUserType: moderator`, `endpoint: /internal/menu/...`. Respond with a `UiResponse` (`showToast`, `showForm`, navigation). This is the right fit: a moderator-only **subreddit** menu item like "Publish modlog now" (force a cron-out-of-band run) and "Configure". Note the **10-minute completion window** when a mod opens a form from a `forUserType: moderator` menu action.
- **Forms** — declared under `forms`, opened via `showForm` from a menu endpoint; fields `string`/`number`/`boolean`/`select`. Good for an ad-hoc "republish with options" action.
- **Toasts** — lightweight feedback (`showToast: { text, appearance: success|neutral }`).
- **Webview / interactive post (blocks)** — supported but **overkill** for this app. RedditModLog has no per-post UI; its output is a wiki page. Recommend **no custom post / no webview** — just menu actions + a status toast. (A webview would only make sense if you wanted an in-app dashboard; the wiki page already is the UI.)

### Publish / app-review process
- Submit via CLI: **`npx devvit publish`** (`--bump major|minor|patch`, default patch; or `--version 1.0.1`). Must add a user-facing `README.md` first.
- **Playtest** before publish: hot-reload via `npm run dev`; unpublished apps can only install on subreddits with **< 200 members**.
- Enters Reddit's **review queue**: team reviews code, example posts, and docs. Email on approval; Modmail/chat if more info needed. **Review time 1–2 business days** typically; longer for higher-risk features (payments, fetch) — RedditModLog uses neither, so it's low-risk. Reviews pause during certain holiday periods.
- **By default published apps are unlisted** (community-specific). For a general-purpose mod tool installable by any subreddit, run **`npx devvit publish --public`** to request App Directory listing — requires a detailed `README.md` (overview, installer instructions, changelog). RedditModLog is a general mod tool → `--public` is appropriate.
- Compliance with **Devvit Rules** streamlines review. One rule is directly relevant: *"if your app stores user content from Reddit, remove it when deleted from Reddit."* RedditModLog stores `target_author`, `body`-derived removal reasons, and permalinks — **respect `onCommentDelete`/`onPostDelete` triggers** (or rely on the 90-day retention + the fact that mod-log records are mod-metadata) to stay compliant. Worth confirming during review; the anonymize-moderators invariant already aligns with privacy expectations.

### Resource limits (consolidated)
| Resource | Limit |
|---|---|
| Redis storage / install | 500 MB |
| Redis request size | 5 MB |
| Redis throughput | 40k cmd/s |
| Redis transactions | 20–30 concurrent, 5 s timeout |
| Setting value | 2 KB |
| Recurring scheduler jobs / install | 10 |
| `runJob` create / deliver rate | 60/min each |
| Request execution wall-clock | ~30 s (daisy-chain for longer) |
| Wiki page content | 512 KB / 524288 bytes (unchanged) |
| Runtime | Node/TS serverless, no filesystem, no arbitrary outbound network except declared HTTP Fetch domains |

---

## Migration summary (what changes vs. stays)

- **Deleted entirely:** OAuth/PRAW auth layer, SQLite/DB-path config, daemon loop, multi-subreddit single-store (now per-install silos), ~5 auth config options.
- **Direct 1:1 ports:** mod-log poll → `getModerationLog`; wiki edit → `getWikiPage`/`updateWikiPage`; SHA-256 hash-skip; 512KB trim; dedup; 90-day retention; removal-reason extraction + email-censor + pipe-escape; modmail prefill link (build the same `/message/compose` URL string).
- **New idioms:** `devvit.json` declares scheduler/triggers/settings/menu; `/internal/...` POST handlers (Hono or Express); Redis hash+sorted-set+string+TTL model; settings via `settings.get()`.
- **Recommended stack:** cron `*/10` publish job (primary) + optional `onModAction` ingest fast-path; menu actions (moderator) for "publish now" / config; no webview/custom post; `--public` publish for App Directory listing.
- **Invariants to bake into code (not settings):** `anonymize_moderators=true`, never-link-profiles, 512KB cap, email-censor.

Relevant local files: existing reference at `/mnt/data/_development/RedditModLog/modlog_wiki_publisher.py` (do not modify); new code target `/mnt/data/_development/RedditModLog/devvit/`.

Sources:
- [Devvit launch guide (publish/review)](https://github.com/reddit/devvit-docs/blob/main/docs/guides/launch/launch-guide.md)
- [Developer Platform & Accessing Reddit Data – Reddit Help](https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data)
- reddit/devvit-docs: `docs/capabilities/server/{redis,scheduler,triggers,settings-and-secrets}.mdx`, `docs/capabilities/client/menu-actions.mdx`
- reddit/devvit: `packages/reddit/src/RedditClient.ts`, `packages/reddit/src/models/ModAction.ts`
