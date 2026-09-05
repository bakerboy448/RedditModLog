I have all the grounding I need from the requirements and research docs. Writing the architecture markdown now.

# RedditModLog → Devvit: Architecture Specification

| Field | Value |
|---|---|
| Document type | Technical Architecture (Lead Architect) |
| Status | Draft for build |
| Date | 2026-06-23 |
| Target | Devvit Web (`@devvit/web/server`, `@devvit/reddit`, `@devvit/redis`), TypeScript/Node serverless |
| Code root | `/mnt/data/_development/RedditModLog/devvit/` |
| Branch | `feat/devvit-migration` |
| Source of truth (legacy) | `modlog_wiki_publisher.py` (read-only reference; DO NOT modify) |

---

## 0. Invariants (binding — carried verbatim from the Python app)

These are non-negotiable and every module below is constrained by them:

- **INV-1 — Anonymize moderators ALWAYS.** Real moderator names are mapped to `HumanModerator` before render. `AutoModerator` and `Reddit` are kept literal. There is NO config toggle (Python refused start if `anonymize_moderators=false`; here it is hardcoded `true`, the flag is dropped). Render layer must NEVER receive a real mod name in an output column.
- **INV-2 — NEVER link user profiles.** Only post (`t3_`) and comment (`t1_`) permalinks are emitted as content links. Any `target` whose fullname is `t2_` (user) or `t5_` (subreddit) produces NO hyperlink. Profile URLs (`/u/`, `/user/`) must never appear in output.
- **INV-3 — 512 KB wiki cap.** Content is hard-capped at 524288 bytes (UTF-8). Over-cap content is trimmed oldest-day-first to ≤90% of cap before write.
- **INV-4 — Email censor + pipe-escape.** Reason text passes the email-censor regex (`→ [EMAIL]`) and pipe-escape (`|` → space) before entering any markdown cell.
- **INV-5 — Dedup by action id.** Every `ModAction.id` is processed at most once into a record.
- **INV-6 — Wiki hash-skip.** Never write the wiki if the SHA-256 of the rendered content equals the last written hash.
- **INV-7 — Default tracked actions** = `[removelink, removecomment, spamlink, spamcomment, addremovalreason, approvelink, approvecomment]`.
- **INV-8 — Default ignored moderators** = `[AutoModerator]` (filtered by `moderatorName`).
- **INV-9 — Per-subreddit isolation.** One install = one subreddit. Redis is implicitly namespaced per install; cross-subreddit data mixing is structurally impossible (drop the `subreddit` column entirely).

---

## 1. Module Layout (`devvit/src/`)

Seven implementation modules plus the `devvit.json` manifest and HTTP entrypoint. Dependency direction is strictly **downward** (no cycles): `main` → {`menu`, settings, modlog, render, wiki, storage}; `modlog`/`wiki` → `storage`; `render` is pure (no I/O). `storage` depends only on `@devvit/redis`.

```
devvit/
├── devvit.json                 # manifest: settings, scheduler, triggers, menu, permissions
├── package.json
├── tsconfig.json
└── src/
    ├── types.ts                # shared interfaces/enums (ModRecord, AppConfig, constants)
    ├── storage.ts              # Redis data-access layer (Repository pattern)
    ├── modlog.ts               # fetch + filter + field-extract ModAction → ModRecord
    ├── render.ts               # PURE: ModRecord[] → markdown (tables, links, censor, cap)
    ├── wiki.ts                 # read/create/update wiki page + hash-skip + trim orchestration
    ├── settings.ts             # settings read + validation + defaults/clamps → AppConfig
    ├── menu.ts                 # mod-only menu actions (run-now, force-rebuild, force-wiki, test)
    └── main.ts                 # HTTP entrypoint: /internal/* handlers (scheduler, trigger, menu, validate)
```

`types.ts` is added (not in the prompt's file list) because every module shares `ModRecord`/`AppConfig`/constant definitions; per the coding-style rule (many small focused files, no duplication) the shared contract must live in one place rather than being re-declared. It exports types and constants only — no logic, no I/O.

### 1.1 `types.ts` — shared contracts and constants

**Responsibility:** Single source of truth for cross-module types and frozen constants. No runtime behavior.

**Exports:**
- `const WIKI_BYTE_CAP = 524288;` and `const WIKI_TRIM_TARGET = Math.floor(WIKI_BYTE_CAP * 0.9);`
- `const DEFAULT_WIKI_ACTIONS: ModActionType[]` (the 7 from INV-7)
- `const DEFAULT_IGNORED_MODS = ['AutoModerator'];`
- `const ANON_LABEL = 'HumanModerator';` `const LITERAL_MODS = new Set(['AutoModerator', 'Reddit']);`
- `const SCHEMA_VERSION = 1;`
- `type DisplayKind = 'P' | 'C' | 'U' | 'A';`
- `interface ModRecord` — the persisted, render-ready record:
  ```ts
  interface ModRecord {
    id: string;             // ModAction.id (dedup key)
    createdAtSec: number;   // epoch seconds (sorted-set score)
    actionType: ModActionType;
    moderator: string;      // ALREADY anonymized (INV-1) — never a real name
    targetId?: string;      // fullname (t3_/t1_/...)
    displayKind: DisplayKind;
    displayId?: string;     // short id w/ P/C/U/A prefix
    permalink?: string;     // post/comment only (INV-2); undefined for profiles
    targetAuthor?: string;  // for modmail prefill
    reason?: string;        // RAW reason; censor+escape applied at render time
  }
  ```
- `interface AppConfig` — resolved settings:
  ```ts
  interface AppConfig {
    wikiPage: string;
    wikiActions: ModActionType[];
    ignoredModerators: string[];
    retentionDays: number;       // clamped 1..365
    maxWikiEntries: number;      // clamped 100..2000
    fetchLimit: number;          // explicit getModerationLog limit
    subredditName: string;       // from context
  }
  ```

> Design note: `ModRecord.moderator` stores the **already-anonymized** label, not the raw name. INV-1 is enforced at ingest (`modlog.ts`), so a real name can never reach Redis or render. `reason` is stored raw and sanitized at render to keep storage idempotent and let the censor regex evolve without a data migration.

### 1.2 `storage.ts` — Redis Repository

**Responsibility:** All Redis I/O. The ONLY module that imports `@devvit/redis`. Encapsulates dedup, the record collection, the time index, the wiki-hash cache, the dirty flag, and the schema-version marker. Repository pattern (per `patterns.md`): callers depend on these functions, not on Redis commands. No `KEYS`/global scan anywhere (Devvit forbids it) — all collections are rooted at explicit known keys.

**Exported functions (all `async`, take a `redis` handle + `AppConfig`/`page` as needed):**

- `markSeen(redis, page, actionId, retentionDays): Promise<boolean>` — atomic dedup (INV-5). Uses `set(`seen:${page}:${actionId}`, "1", { nx: true, expiration: retentionDays*86400 })`; returns `true` if newly inserted (caller should process), `false` if already seen. TTL self-prunes dedup keys at retention.
- `putRecord(redis, page, rec: ModRecord): Promise<void>` — `hSet(`actions:${page}`, { [rec.id]: JSON.stringify(rec) })` AND `zAdd(`actions_by_time:${page}`, { member: rec.id, score: rec.createdAtSec })`. (Wrap the two writes in `watch/multi/exec` only if the trigger + cron can interleave; with single-writer cron a plain pair is acceptable — see §3.)
- `getAllRecords(redis, page): Promise<ModRecord[]>` — `hGetAll(`actions:${page}`)`, JSON-parse values, sort by `createdAtSec` desc. (Use `hScan` paging if field count is large; cap to `maxWikiEntries` newest.)
- `pruneRetention(redis, page, retentionDays, nowSec): Promise<number>` — INV-9/P-18. `zRange(by score, 0..cutoff)` → `hDel(actions, members)` → `zRemRangeByScore(actions_by_time, 0, cutoff)`; returns count removed. Honor the `zRange` 1000-member LIMIT by looping until empty.
- `getWikiHash(redis, page): Promise<string | undefined>` / `setWikiHash(redis, page, hash): Promise<void>` — INV-6, single hash `wiki_hash` field=page.
- `setDirty(redis, page): Promise<void>` / `isDirty(redis, page): Promise<boolean>` / `clearDirty(redis, page): Promise<void>` — `dirty:${page}` flag the trigger sets and cron consumes (§3).
- `getSchemaVersion(redis): Promise<number>` / `setSchemaVersion(redis, v): Promise<void>` / `migrate(redis): Promise<void>` — P-28; greenfield = write `SCHEMA_VERSION` if absent; forward-migration hook for future versions.

**Inputs:** `redis` handle, page name, `ModRecord`, retention/limits.
**Outputs:** booleans/records/counts. Never throws on "not found" — returns `undefined`/empty.

### 1.3 `modlog.ts` — fetch, filter, extract

**Responsibility:** Turn raw `getModerationLog` output into deduped, anonymized `ModRecord[]`. Owns INV-1, INV-2 (permalink gating), INV-5, INV-7, INV-8, and `display_id` derivation. Imports `@devvit/reddit` (read) and `storage` (dedup + persist).

**Exported functions:**

- `fetchActions(reddit, cfg: AppConfig): Promise<ModAction[]>` — `reddit.getModerationLog({ subredditName: cfg.subredditName, limit: cfg.fetchLimit, pageSize: 100 }).all()`. **`type` is omitted** — single-valued filter can't express 7 types; filter client-side (research delta #1). Always passes an explicit `limit` (research delta #5; default is `Infinity`).
- `extractRecord(action: ModAction, cfg: AppConfig): ModRecord | null` — PURE mapper:
  - Returns `null` if `action.type ∉ cfg.wikiActions` (INV-7) OR `action.moderatorName ∈ cfg.ignoredModerators` (INV-8).
  - `moderator` = `anonymizeMod(action.moderatorName)` (INV-1): literal if in `LITERAL_MODS`, else `ANON_LABEL`.
  - `createdAtSec` = `Math.floor(action.createdAt.getTime()/1000)`.
  - `displayKind`/`displayId` derived from `action.target?.id` fullname prefix (`t3_`→P, `t1_`→C, `t2_`→U, else A).
  - `permalink` = `action.target?.permalink` ONLY when `displayKind ∈ {P, C}` (INV-2); otherwise `undefined`.
  - `reason` = `extractReason(action)` (research delta #2): priority `details` → `description` (for `addremovalreason` the human reason is in `details`). Stored raw.
  - `targetAuthor` = `action.target?.author`.
- `ingest(reddit, redis, cfg): Promise<{ added: number }>` — orchestrates: `fetchActions` → for each, `extractRecord`; skip nulls; `markSeen` gate; `putRecord` for new ones. Returns count added. This is the shared ingest path used by both cron and the trigger.
- `anonymizeMod(name): string` and `deriveDisplay(targetId?): {kind, displayId?}` exported for unit testing.

**Inputs:** `reddit` client, `redis`, `AppConfig`.
**Outputs:** `ModRecord[]` persisted via storage; counts.

### 1.4 `render.ts` — PURE markdown builder

**Responsibility:** `ModRecord[] → string` (markdown). **Zero I/O, zero Reddit/Redis imports** — fully unit-testable, deterministic. Owns INV-3 (cap/trim), INV-4 (censor/escape), INV-2 (link emission), the table layout (P-10), modmail link (P-12), header/footer (P-31), and approval-correlation render (P-13).

**Exported functions:**

- `buildContent(records: ModRecord[], cfg: AppConfig, nowIso: string): string` — top-level. Groups by date desc, renders one table per day with columns `Time | Action | ID | Moderator | Content | Reason | Inquire`, prepends "Last Updated" header, appends GitHub-credit footer, then applies `enforceByteCap`.
- `renderRow(rec, cfg): string` — one table row. Calls `censorEmail` + `escapePipes` on reason; `contentLink(rec)` (emits `[displayId](https://www.reddit.com{permalink})` only when permalink present per INV-2, else plain `displayId`); `modmailLink(cfg.subredditName, rec)`.
- `censorEmail(text): string` (INV-4), `escapePipes(text): string` (INV-4) — pure string transforms, ported verbatim from Python regex.
- `modmailLink(sub, rec): string` — `https://www.reddit.com/message/compose?to=/r/${sub}&subject=...&message=...` URL-encoded (P-12).
- `enforceByteCap(markdown, dayBlocks): string` — INV-3. UTF-8 byte length check; if over `WIKI_BYTE_CAP`, drop oldest day-blocks until ≤ `WIKI_TRIM_TARGET`. Operates on pre-assembled day blocks so trimming is oldest-day-first.
- `contentHash(markdown): Promise<string>` — SHA-256 hex via Web Crypto (`crypto.subtle.digest`) for INV-6. (Async because `subtle.digest` is async; lives here so render owns "what was rendered → its hash".)

**Inputs:** `ModRecord[]`, `AppConfig`, timestamp string.
**Outputs:** capped markdown string; hash.

> Immutability (coding-style rule): `render.ts` never mutates input records; it maps to new strings/arrays.

### 1.5 `wiki.ts` — wiki publish orchestration

**Responsibility:** The create-or-update + hash-skip publish flow (P-14, P-16, INV-6). Imports `@devvit/reddit` and `storage` + `render` (`contentHash`).

**Exported functions:**

- `publish(reddit, redis, cfg, content: string, opts?: { bypassHash?: boolean }): Promise<{ wrote: boolean; reason: string }>`:
  1. `hash = await contentHash(content)`.
  2. Unless `bypassHash`: `prev = getWikiHash`; if `prev === hash` return `{ wrote:false, reason:'unchanged' }` (INV-6).
  3. `getWikiPage(sub, page)` in try/catch (research delta #6: throws if absent).
     - absent → `createWikiPage({...})`.
     - present & `existing.content !== content` → `updateWikiPage({...})`.
     - present & equal → skip (defensive double-check of INV-6).
  4. `setWikiHash(page, hash)`; return `{ wrote:true, reason:'created'|'updated' }`.
- `publishFromStore(reddit, redis, cfg, opts?): Promise<{ wrote, reason }>` — convenience: `getAllRecords` → `render.buildContent` → `publish`. This is the single code path cron, trigger-coalesced refresh, and menu force-write all call.

**Inputs:** clients, `AppConfig`, content (or store-derived).
**Outputs:** `{ wrote, reason }` for logging/menu feedback.

### 1.6 `settings.ts` — config resolution + validation

**Responsibility:** Read Devvit settings, apply defaults, clamp numerics, validate, and produce a frozen `AppConfig` (P-21..P-24). Imports `@devvit/web/server` (`settings`, `context`).

**Exported functions:**

- `loadConfig(): Promise<AppConfig>` — reads each setting via `settings.get`, applies defaults from `types.ts`, clamps `retentionDays` to `[1,365]` and `maxWikiEntries` to `[100,2000]` (log a warning on clamp, P-24), parses `ignoredModerators` (CSV/multiSelect), validates `wikiActions` ⊆ `ModActionType` (drop unknowns, P-22), reads `subredditName` from `context.subredditName`. Returns `Object.freeze(cfg)` (immutability).
- `validateRetention(value): string | void`, `validateMaxEntries(value): string | void`, `validateWikiPage(value): string | void` — handlers for the settings `validationEndpoint` (P-24); return an error string to reject at save, `void` to accept.

**Inputs:** Devvit settings store + context.
**Outputs:** validated, frozen `AppConfig`; validation verdicts.

### 1.7 `menu.ts` — moderator actions

**Responsibility:** Mod-only menu/button actions (P-26, P-27), and their handler bodies. Imports clients + `modlog`/`wiki`/`settings`.

**Exported functions:**

- `handleRunNow(reddit, redis): Promise<UiResponse>` — P-26/P-27 "Run now": `loadConfig` → `ingest` → `publishFromStore` → toast with `{ added, wrote, reason }`.
- `handleForceRebuild(reddit, redis): Promise<UiResponse>` — P-27 `--force-modlog`/`--force-all`: re-ingest (records are idempotent by id) → `publishFromStore`.
- `handleForceWiki(reddit, redis): Promise<UiResponse>` — P-27 `--force-wiki`: `publishFromStore(..., { bypassHash: true })` (INV-6 deliberately bypassed).
- `handleTest(reddit): Promise<UiResponse>` — P-26 `--test`: one `getModerationLog` fetch of a small limit; report count + sample types; performs NO write.

All return a Devvit `UiResponse` (toast). Each handler is restricted to moderators via the menu item's manifest config (mod-only context).

### 1.8 `main.ts` — HTTP entrypoint

**Responsibility:** The serverless HTTP server wiring `/internal/*` POST endpoints declared in `devvit.json` to handler bodies. No business logic — thin adapters that build `reddit`/`redis`/`context`, call into the modules, and shape responses. Owns per-invocation try/catch (P-29: no daemon loop; rely on scheduler retry + bounded error handling) and structured `console.*` logging (P-30).

**Endpoints (each a small handler):**
- `POST /internal/scheduler/publish-modlog` → `migrate` (once) → `loadConfig` → `pruneRetention` → `ingest` → `if isDirty || added>0` → `publishFromStore` → `clearDirty`. (§3)
- `POST /internal/on-mod-action` → `loadConfig` → `extractRecord`+`markSeen`+`putRecord` for the single event payload → `setDirty`. **No wiki write here** (§3).
- `POST /internal/menu/run-now` | `/force-rebuild` | `/force-wiki` | `/test` → corresponding `menu.ts` handler.
- `POST /internal/settings/validate-retention` | `validate-max-entries` | `validate-wiki-page` → `settings.ts` validators.

**Inputs:** HTTP request (Devvit-injected context).
**Outputs:** HTTP responses / `UiResponse`.

---

## 2. Redis Key Schema (exact)

All keys are **implicitly per-installation (per-subreddit)** — no `subreddit` segment (INV-9). `${page}` = configured wiki page name (default `modlog`) so a future multi-page install never collides. No key is ever enumerated via `KEYS`/`SCAN`-all (forbidden); every collection is rooted at a fixed key below.

| Purpose | Type | Key | Member/Field → Value | Lifecycle |
|---|---|---|---|---|
| Dedup (INV-5, P-17) | string | `seen:${page}:${actionId}` | `"1"` | TTL = `retentionDays*86400`; self-expires |
| Record store (P-4) | hash | `actions:${page}` | field=`actionId` → `JSON(ModRecord)` | field removed by retention prune |
| Time index (P-18) | sorted set | `actions_by_time:${page}` | member=`actionId`, score=`createdAtSec` | `zRemRangeByScore` on prune |
| Wiki hash cache (INV-6, P-16) | hash | `wiki_hash` | field=`${page}` → SHA-256 hex | overwritten each successful write |
| Dirty flag (§3) | string | `dirty:${page}` | `"1"` | set by trigger; `del` after cron publish |
| Schema marker (P-28) | string | `schema_version` | `"1"` | written on first boot; bumped on migration |

**Rationale for the dual record-store + time-index:** `hGetAll(actions:${page})` rebuilds the full render set without any global scan; the parallel sorted set gives O(log n) retention pruning by `createdAtSec` via `zRemRangeByScore` (the documented Devvit FIFO/retention pattern). The hash field key (`actionId`) and zset member (`actionId`) are identical, so prune removes from both with the same id list. Dedup is a **separate** TTL string (not derived from the hash) so that the UNIQUE-constraint check is a single O(1) `set nx` and old ids can legitimately recur after retention — matching Python's "retention prunes the table, then ids can reappear" behavior.

**Size budget:** 500 MB/install. A `ModRecord` JSON is ~300–500 bytes; at `maxWikiEntries`=2000 cap the `actions` hash is <1 MB. Dedup TTL keys at 90-day retention with heavy mod volume stay well under budget. If a record ever approaches the 5 MB request cap (it won't), `redisCompressed.hSet` is the escape hatch — not needed at MVP.

---

## 3. Execution Model Decision — Cron-primary, trigger as dirty-flag ingest

**Decision: cron is the system of record for publishing; `onModAction` is an OPTIONAL ingest fast-path that only sets a dirty flag and never writes the wiki.**

### Rationale

| Factor | Cron-only | Trigger-publishes | **Chosen: cron-primary + trigger-ingest** |
|---|---|---|---|
| 1:1 map of Python 600s loop | ✅ `*/10 * * * *` | ❌ event-driven, different semantics | ✅ cron owns publish cadence |
| Wiki write pressure / rate limit | ✅ one write/interval, only if changed | ❌ a write per mod action — hammers wiki, risks write-rate cap | ✅ trigger never writes; cron coalesces |
| Exactly-once correctness | ✅ batch dedup | ⚠️ triggers are NOT exactly-once (docs) | ✅ Redis `markSeen` dedup covers trigger redelivery |
| Full re-render cost (table is whole-page rebuild) | ✅ amortized per interval | ❌ re-render on every action | ✅ render only when cron sees `dirty` or new records |
| Latency | ⚠️ up to interval | ✅ near-real-time | ✅ records ingested at event time; publish at next tick (P-3 "shortly after", P-20) |
| Self-loop guard (app's own `dev_platform_app_*`/wiki edits) | n/a | needs explicit guard | ✅ trigger filters to `wikiActions` only; app wiki edits aren't in the set |

**Mechanics:**
1. **`onModAction` (`/internal/on-mod-action`)** — per event: `extractRecord` (drops non-tracked types, drops ignored mods, INV-1/2 applied), `markSeen` gate (handles non-exactly-once redelivery), `putRecord`, then `setDirty(page)`. It does **NOT** call `getModerationLog` and does **NOT** publish. Cheap, idempotent, self-loop-safe (the app's own wiki edits aren't in `wikiActions`).
2. **Cron (`/internal/scheduler/publish-modlog`, `*/10 * * * *`)** — the authoritative path: `pruneRetention` → `ingest` (catches anything the trigger missed/dropped) → if `added>0 || isDirty(page)` then `publishFromStore` (hash-skip still applies, INV-6) → `clearDirty`. Default `*/10` mirrors `update_interval=600`; bounded ≥1 min (platform minimum). Lowering to `*/5`/`*/2` is a config change, not code.

**Single-writer note:** publishing happens ONLY in cron. The trigger only writes per-record Redis entries (different keys, idempotent by id). Therefore `putRecord`'s two-key write does not require a transaction at MVP; a `watch/multi/exec` wrapper is added only if a future design lets two writers publish concurrently. Retention prune runs single-writer in cron — no transaction needed.

**MVP simplification:** ship **cron-only** first (trigger omitted) for the lowest-risk 1:1 port; add the trigger in the parity phase purely as a latency optimization. The cron path is correct and complete without it.

---

## 4. Devvit Settings Schema (`devvit.json` → `settings`)

Scope `subreddit` = moderator-editable in Install Settings UI; values resolved by `settings.ts` into `AppConfig`. Auth/`subreddits`/`update_interval`/`anonymize_moderators` are **dropped** (P-1, P-25, P-19, INV-1). All numeric bounds enforced both by `validationEndpoint` (reject at save) and runtime clamp (defense in depth, P-24).

| Setting key | Scope | Type | Default | Constraint / validation | Maps to (Python) |
|---|---|---|---|---|---|
| `wikiPage` | subreddit | `string` | `modlog` | non-empty, no spaces/slashes; `validate-wiki-page` | `wiki_page` |
| `wikiActions` | subreddit | `multiSelect` | the 7 (INV-7) | options = the 7 `ModActionType` literals only | `wiki_actions` |
| `ignoredModerators` | subreddit | `string` (CSV) | `AutoModerator` | parsed/trimmed client-side (INV-8) | `ignored_moderators` |
| `retentionDays` | subreddit | `number` | `90` | clamp/validate `1..365`; `validate-retention` | `retention_days` |
| `maxWikiEntries` | subreddit | `number` | `1000` | clamp/validate `100..2000`; `validate-max-entries` | `max_wiki_entries_per_page` |
| `fetchLimit` | subreddit | `number` | `1000` | clamp `100..5000`; explicit `getModerationLog` limit (delta #5) | `modlog_limit` |

**Cadence** is NOT a runtime setting — it is the `cron` string in `devvit.json` `scheduler.tasks.publish-modlog` (P-19). Changing cadence is a manifest change + redeploy, not a mod-editable field (platform model). If mod-editable cadence is later required, expose a `select` of allowed crons mapped to `scheduler.runJob` — deferred (GAP-3).

**Dropped settings (explicit):** `client_id`, `client_secret`, `username`, `password`, `user_agent` (P-1, no auth); `subreddits` (P-25, implicit per install); `anonymize_moderators` (INV-1, hardcoded true); `update_interval` (→ cron); `log_level`/`logs_dir`/config-file paths (P-30/P-33, serverless no-FS); `max_continuous_errors`/backoff (P-29, scheduler retry semantics).

---

## 5. Phased Plan

### Phase 1 — MVP (cron-only, core transparency loop)
**Goal:** Installable app that publishes the modlog to the wiki on schedule, with all privacy invariants intact.
- `types.ts`, `storage.ts` (dedup/record/time-index/wiki-hash/schema), `modlog.ts` (fetch/filter/extract/ingest), `render.ts` (tables/links/censor/cap/hash), `wiki.ts` (create-or-update + hash-skip), `settings.ts` (load+clamp+validate), `main.ts` (scheduler + validate endpoints only).
- `devvit.json`: `settings`, `scheduler.tasks.publish-modlog` (`*/10 * * * *`), permissions (read modlog, manage wiki).
- **Enforced invariants:** INV-1..INV-9 ALL active at MVP (they are privacy/correctness, not features).
- **Done when:** install on a test sub → cron writes a wiki page matching the legacy table layout; hash-skip prevents redundant writes; retention prune runs; no real mod names, no profile links in output.
- **Verification:** unit tests on `render.ts` (pure) and `modlog.extractRecord`/`anonymizeMod`/`deriveDisplay`/`extractReason` (pure); golden-file compare of rendered markdown against legacy Python output for a fixed `ModRecord[]` fixture; live playtest fetch+publish on a sandbox sub; confirm one real `addremovalreason` entry's reason lands in `details` (research delta #2).

### Phase 2 — Parity (UX + latency + completeness)
**Goal:** Close remaining MoSCoW Should/Could parity items.
- `menu.ts` + menu endpoints (P-26 test, P-27 force-rebuild/force-wiki/run-now).
- `onModAction` trigger (`/internal/on-mod-action`) + dirty-flag coalescing (§3, P-20/P-3).
- Approval-correlation render (P-13): cron looks back in the record store for a prior Reddit/AutoMod removal before emitting approval rows (Redis lookup replaces SQLite `LIKE`).
- Settings `validationEndpoint`s fully wired (P-24); schema-version migration path exercised (P-28).
- **Done when:** mods can force a rebuild/test from the UI; removals appear shortly after action via trigger ingest; approval rows obey the correlation rule.
- **Verification:** trigger redelivery dedup test (fire same action twice → one record); force-wiki bypasses hash; menu actions mod-gated.

### Phase 3 — Publish (review + distribution)
**Goal:** Public listing in the Apps directory.
- App metadata, icon, description, privacy statement (transparency/anonymization guarantees), required-permissions justification.
- Pre-submission audit against INV-1/INV-2 (reviewer-visible privacy posture), 512KB handling, error logging.
- Submit to Reddit app review; address feedback; publish; document install/upgrade.
- **Done when:** app is listed and installable by any moderator; upgrade path documented.
- **Verification:** review passes; fresh install on an unrelated sub reproduces MVP behavior greenfield (schema v1).

---

## 6. GAP List (explicit — unknowns/risks to resolve in build)

- **GAP-1 — `addremovalreason` reason field (delta #2).** No dedicated `removalReason` field; reason assumed in `details` (fallback `description`). **MUST confirm against one live `addremovalreason` event** before Phase 1 sign-off. If shape differs, `extractReason` priority order changes — localized to `modlog.ts`.
- **GAP-2 — `onModAction` payload shape.** Assumed to mirror `ModAction`. Confirm field availability (esp. `target.permalink`/`target.author`) on a live trigger payload before relying on trigger-ingest in Phase 2; cron path is unaffected.
- **GAP-3 — Mod-editable cadence.** Cron lives in the manifest, not settings; mods cannot change cadence without redeploy. Deferred. If required, expose a `select`→`scheduler.runJob` mapping (≥1 min floor).
- **GAP-4 — Approval correlation cost (P-13).** SQLite `LIKE` becomes a Redis record-store lookup. With `hGetAll` already loaded in cron, correlation is in-memory over the loaded set — but only sees records within retention. Behavior at the retention boundary may differ from Python (which queried the full table up to 90d). Document the bound; acceptable since retention is the same 90d.
- **GAP-5 — `getModerationLog` paging vs ~30s wall clock.** `fetchLimit` default 1000 / pageSize 100 = ~10 pages. Large/backfilled logs could approach the per-invocation wall-clock limit. Mitigation: cap `fetchLimit`, rely on dedup so successive ticks make progress; for first-run backfill on a high-volume sub, daisy-chain one-off jobs (`scheduler.runJob`). Confirm timing in playtest.
- **GAP-6 — `hMGet`/hash command allowlisting.** Research notes `hMGet` may be disabled in the Devvit subset. `storage.ts` uses `hGetAll`/`hSet`/`hDel`/`hGet` only (all in the confirmed set); avoid `hMGet`. Verify the full hash command set at build start.
- **GAP-7 — SHA-256 availability.** `crypto.subtle.digest` (Web Crypto) assumed present in the Devvit Node runtime; `node:crypto` is the fallback. Confirm one is importable in the sandbox at Phase 1 start (INV-6 depends on it).
- **GAP-8 — Initial wiki page permissions.** Python's `wiki[page].edit()` auto-created; here `createWikiPage` is explicit. Decide default visibility (`SUBREDDIT_PERMISSIONS` vs `MODS_ONLY`) — transparency intent implies public/SUBREDDIT_PERMISSIONS. Set via `updateWikiPageSettings` post-create if needed.
- **GAP-9 — No historical SQLite import (non-goal, restated).** Each install starts greenfield at schema v1; legacy data is not migrated. Operators of the Python app accept a fresh start.
