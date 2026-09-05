I have all the grounding I need. All primitives are confirmed: `getModerationLog` (with `type` filter and `subredditName`), `updateWikiPage`, `ModAction` fields (`createdAt`, `moderatorName`, `moderatorId`, `description`, `details`, `target`, `type`), `onModAction` trigger, scheduler with cron (`/internal/.+` endpoints, min 1-min cron), Redis (500MB/5MB limits, sorted sets, hashes, transactions), and settings (global/subreddit scopes, secrets via `isSecret`). Here is the requirements document.

---

# RedditModLog → Devvit Migration: Requirements Specification

| Field | Value |
|---|---|
| Document type | Product Requirements (PO + BA) |
| Author role | Product Owner / Business Analyst |
| Status | Draft for review |
| Date | 2026-06-23 |
| Source of truth (current) | `/mnt/data/_development/RedditModLog/modlog_wiki_publisher.py` (1725 LOC, PRAW) |
| Target platform | Reddit Developer Platform (Devvit, TypeScript/Node, serverless) |
| Target code location | `/mnt/data/_development/RedditModLog/devvit/` |
| Branch | `feat/devvit-migration` |
| Devvit API version referenced | `@devvit/public-api` v0.13.x (verified via `reddit/devvit` + `reddit/devvit-docs`) |

---

## 1. Product Vision

**Today.** RedditModLog is a self-hosted Python/PRAW daemon. A subreddit's moderators run it on their own infrastructure (Docker + SQLite), supply a Reddit bot account's password credentials, and the daemon polls each subreddit's moderation log every ~10 minutes and republishes a transparency view of removals/approvals to a subreddit **wiki** page as dated markdown tables. Each table row carries a prefilled "removal inquiry" modmail link so affected users can contact mods. Moderator identities are always anonymized (enforced); user profiles are never linked.

**Tomorrow.** The same transparency product, re-platformed as a **Devvit app** that runs *on Reddit's own infrastructure*. A moderator installs the app on their subreddit from the Apps directory, configures a few settings in the install UI, and the app maintains the wiki page automatically on a schedule — with **no servers, no bot account, no password credential, no Docker, no SQLite** to operate. The app inherits the installing moderator's permissions, persists state in Devvit Redis, runs work via the Devvit scheduler, and is distributed/updated through Reddit's app review pipeline.

**Why migrate.** Eliminate operator burden (hosting, secrets, DB migrations, uptime), eliminate the password-grant security liability, gain first-class Reddit auth/permissions, and make the product installable by any moderator without engineering skills. The migration is a **re-platform, not a redesign**: the published wiki output and its transparency/privacy guarantees must remain recognizably identical.

**Primary persona.** A subreddit moderator who wants public, auditable transparency of removal actions with a low-friction appeal path, and who does not want to run infrastructure.

**Out of scope (non-goals).** No new analytics/dashboards; no cross-subreddit aggregation across installs (the Python "multi-subreddit single store" becomes "one isolated install per subreddit" — see FR-15 and Risk R-7); no migration/import of historical SQLite data into Devvit Redis (greenfield per install); no change to the wiki table schema or modmail link format beyond what platform constraints force.

---

## 2. Feature Parity Matrix

MoSCoW: **M**ust / **S**hould / **C**ould / **W**on't (this release). "Devvit can do" verified against the cited primitives.

| # | Current Python behavior | Devvit requirement | MoSCoW | Devvit-can-do note |
|---|---|---|---|---|
| P-1 | Auth via OAuth password grant (`client_id/secret/username/password`) | Drop entirely. App acts under install permissions via `reddit.*` client; no credentials stored | **M** | Yes — Devvit injects auth context; password grant impossible/unneeded |
| P-2 | `subreddit.mod.log(limit=N)` poll, filtered to `wiki_actions` | `reddit.getModerationLog({ subredditName, type?, limit })` → `Listing<ModAction>` | **M** | Verified: `getModerationLog(GetModerationLogOptions)`; supports `type` (single `ModActionType`) + `ListingFetchOptions` (limit/paging) |
| P-3 | Filter to action set `[removelink, removecomment, spamlink, spamcomment, addremovalreason, approvelink, approvecomment]` | Same default set; iterate listing and filter by `action.type` (and/or per-type fetch) | **M** | Yes — all 7 strings exist in `ModActionType` enum |
| P-4 | Action-field extraction: target id/type, moderator, datetime, removal reason | Map from `ModAction`: `id`, `type`, `moderatorName`, `moderatorId`, `createdAt: Date`, `description`/`details`, `target?: ModActionTarget` | **M** | Yes — fields confirmed in `ModAction` interface |
| P-5 | `display_id` with `P`/`C`/`U`/`A` prefix + short id | Reproduce from `target` kind + id | **S** | Yes — derivable; exact short-id rule is internal logic |
| P-6 | Moderator anonymization → `HumanModerator`, keep `AutoModerator`/`Reddit` literal | Same mapping from `moderatorName`; **anonymize always on** | **M** | Yes — string mapping; see SEC-1 |
| P-7 | Removal-reason extraction priority (`description` for addremovalreason → `mod_note` → `details`) | Same priority over `ModAction.description`/`details` | **M** | Yes — `description`+`details` present; `mod_note` maps to details/description text |
| P-8 | Email censoring → `[EMAIL]` regex over reason text | Identical regex applied to reason before render | **M** | Yes — pure string transform; see SEC-3 |
| P-9 | Pipe-escape (`\|` → space) for markdown table safety | Identical sanitizer | **M** | Yes — pure string transform |
| P-10 | Markdown table build, grouped by date desc, columns `Time/Action/ID/Moderator/Content/Reason/Inquire` | Identical builder producing identical table layout | **M** | Yes — pure string assembly |
| P-11 | Content link → post/comment permalink only; **never** user profile | Build permalink from `target`; reject `/u/` profiles | **M** | Yes — `target` exposes content permalink/id; see SEC-2 |
| P-12 | Prefilled modmail "removal inquiry" link (`/message/compose?to=/r/sub&subject=&message=`) | Identical URL builder | **M** | Yes — plain URL string |
| P-13 | Approval rows shown only if prior Reddit/AutoMod removal exists; combined removal+reason rows | Same correlation logic using Redis-stored prior actions | **S** | Yes — Redis lookup replaces SQLite `SELECT ... LIKE`; see Risk R-3 |
| P-14 | Wiki publish via `subreddit.wiki[page].edit()` | `reddit.updateWikiPage({ subredditName, page, content, reason })` | **M** | Verified: `updateWikiPage(UpdateWikiPageOptions{page,content,reason?})` + `getWikiPage` |
| P-15 | 512 KB (524288-byte) wiki cap with 90% trim-oldest-days logic | Identical byte cap + trim | **M** | Yes — pure size logic; cap unchanged by platform (NFR-2) |
| P-16 | SHA-256 wiki-hash cache to skip unchanged writes | Store last content hash in Redis hash; compare before write | **M** | Yes — `redis.hSet/hGet`; idempotency now mandatory (SEC-6) |
| P-17 | Dedup via `processed_actions(action_id UNIQUE)` | Redis string/set membership keyed by `ModAction.id` | **M** | Yes — `redis.get/set` or sorted-set membership |
| P-18 | Retention: delete rows older than `retention_days` (default 90) | Redis sorted set scored by `createdAt` epoch; `zRemRangeByScore` on schedule | **M** | Yes — sorted set + scheduled cleanup is the documented Devvit pattern |
| P-19 | Continuous daemon loop, `update_interval` 600s | `Devvit.addSchedulerJob` + cron (`/internal/...` endpoint) | **M** | Verified scheduler; **min cron granularity 1 minute** → 600s expressed as `*/10 * * * *` |
| P-20 | Near-real-time freshness only at poll interval | **Add** `onModAction` trigger for low-latency incremental updates | **S** | Verified `onModAction` trigger exists; complements (not replaces) scheduler |
| P-21 | 19 config options via CLI > env > JSON precedence | App settings: `subreddit` scope (mod-editable) + `global` scope; no CLI/env/JSON | **M** | Yes — `devvit.json` settings, scopes `global`/`subreddit`, types string/boolean/select/multiSelect |
| P-22 | `wiki_actions` configurable + validated against known action list | `multiSelect` setting constrained to valid `ModActionType` values | **S** | Yes — `multiSelect` with fixed option list |
| P-23 | `ignored_moderators` (default `[AutoModerator]`) | `string`/`multiSelect` setting; filter by `moderatorName` | **S** | Yes — settings + in-code filter |
| P-24 | Config validation/limits (min/max clamping for 8 numeric keys) | `onValidate` setting validators + in-code clamps | **S** | Yes — settings support validation; clamps are code |
| P-25 | Multi-subreddit in one store; strict per-sub filtering; mixed-data guard | One install = one subreddit; Redis namespaced per install; cross-sub mixing structurally impossible | **M** | Yes — install isolation; see FR-15 / Risk R-7 |
| P-26 | `--test` connection check | "Run now / Test" UI action (menu/button) that does one fetch+report | **C** | Yes — menu action / blocks button |
| P-27 | `--force-modlog` / `--force-wiki` / `--force-all` rebuild | "Force rebuild" + "Force wiki write (bypass hash)" mod-only actions | **C** | Yes — menu actions invoking the same job code |
| P-28 | DB schema migrations v0→v5 | Redis key-schema `version` marker + forward migration on boot | **C** | Yes — store `schema_version` in Redis; greenfield = v1 |
| P-29 | Exponential backoff on continuous errors; `max_continuous_errors` | Rely on scheduler retry semantics + bounded per-run try/catch | **S** | Partial — no long-lived loop; per-invocation error handling + next-tick retry |
| P-30 | stdout/stderr logging, `logs/` dir, debug level | `console.*` logs (platform-captured); no filesystem | **M** | Yes — serverless: no FS; structured logging only |
| P-31 | Footer crediting GitHub repo; "Last Updated" header | Identical header/footer in generated content | **S** | Yes — pure string |
| P-32 | Docker/s6/PUID/Dockerfile/systemd deployment | Removed; replaced by Devvit publish + app review | **M** | Yes — platform-native distribution |
| P-33 | Config auto-update + `.backup` file write | Removed (no FS; settings are platform-managed) | **W** | N/A on platform |
| P-34 | Arbitrary outbound network / filesystem | Not available; not needed | **W** (out of scope) | Devvit sandbox forbids both by design |

---

## 3. Functional Requirements (numbered mod stories)

Format: **As a moderator, I want … so that …**, with acceptance criteria.

**FR-1 — Install & configure.** As a mod, I want to install RedditModLog on my subreddit and set the wiki page name (default `modlog`), schedule cadence, retention days, and tracked action types in the install settings, so that the app publishes to the right place with my chosen behavior without editing code or files.
- AC: Settings UI exposes, at minimum: `wikiPage` (string, default `modlog`), `wikiActions` (multiSelect, default = the 7-action set), `ignoredModerators` (default `AutoModerator`), `retentionDays` (default 90, clamp 1–365), `updateCadence` (select/cron-backed, default ~10 min), `maxWikiEntriesPerPage` (default 1000, clamp 100–2000).
- AC: Invalid values are rejected at save (`onValidate`) or clamped at runtime with a logged warning.

**FR-2 — Scheduled publish.** As a mod, I want the app to fetch new mod actions and rewrite the wiki page on a recurring schedule, so that the public modlog stays current without my intervention.
- AC: A scheduler cron job fetches via `getModerationLog`, builds content, and calls `updateWikiPage`.
- AC: Default cadence ≈10 minutes (`*/10 * * * *`); cadence is bounded ≥1 minute (platform minimum).

**FR-3 — Incremental near-real-time update (Should).** As a mod, I want removals to appear on the wiki shortly after they happen, so that transparency is timely.
- AC: An `onModAction` trigger handler ingests qualifying actions into Redis and (debounced/coalesced) refreshes the wiki, guarded against acting on the app's own events.

**FR-4 — Removal rows.** As a reader, I want each tracked removal/spam action rendered as a dated table row with time, action, short content id, anonymized moderator, content link, reason, and an inquiry link.
- AC: Output columns and ordering are byte-comparable to current Python output for the same input (date desc, time desc within date).

**FR-5 — AutoModerator filter labeling.** As a mod, I want AutoModerator removals labeled `filter-<action>`, so that automated filtering is distinguishable from human removals.
- AC: Removal actions whose moderator resolves to AutoModerator render `filter-removelink` etc.

**FR-6 — Removal-reason resolution.** As a reader, I want the most meaningful reason text shown (added removal reason → mod note → details), so rows are informative.
- AC: Priority order matches P-7; missing reason renders `-`.

**FR-7 — Combined removal+reason rows.** As a mod, I want a removal and its subsequent `addremovalreason` for the same content merged into one row, so the table isn't duplicated.
- AC: Per-content correlation merges reason into the removal row (P-13 logic) using Redis-stored context.

**FR-8 — Conditional approval rows.** As a mod, I want an approval shown only when it reverses a prior Reddit/AutoMod removal, annotated "Approved <mod> removal[: reason]", so the log reflects meaningful reversals only.
- AC: Approval with no matching prior auto/Reddit removal in retention window is excluded.

**FR-9 — Inquiry (modmail) link.** As an affected user, I want a one-click prefilled modmail link per row including content id, title, action type, and link, so I can appeal easily.
- AC: URL is `https://www.reddit.com/message/compose?to=/r/<sub>&subject=<enc>&message=<enc>`; subject carries `[ID: <8-char>]`; rendered as `[Contact Mods](...)`.

**FR-10 — Dedup / idempotency.** As a mod, I want each mod action published at most once even when polling and the trigger overlap or a run retries, so rows aren't duplicated.
- AC: Action id present in Redis dedup store is skipped; concurrent/duplicate invocations converge to the same wiki content (SEC-6).

**FR-11 — Unchanged-write skip.** As a mod, I want the wiki untouched when content hasn't changed, so I don't spam wiki revisions/rate limits.
- AC: SHA-256 of candidate content compared to Redis-cached hash; equal → no `updateWikiPage` call (unless force).

**FR-12 — Size cap & trim.** As a mod, I want the page to stay under Reddit's 512 KB wiki limit by trimming oldest days first, with an in-page notice, so writes never fail on size.
- AC: Candidate content >90% of 524288 bytes triggers oldest-day trimming + "N older day(s) trimmed" notice; final content ≤524288 bytes or the write is refused with a clear log.

**FR-13 — Retention cleanup.** As a mod, I want stored actions older than `retentionDays` purged, so storage stays bounded.
- AC: Scheduled job removes Redis sorted-set members scored older than cutoff; default 90 days.

**FR-14 — Manual actions (Could).** As a mod, I want menu/button actions to (a) run a publish now, (b) force a full rebuild, (c) force a wiki write bypassing the hash, so I can recover or test on demand.
- AC: Each action is mod-gated and reuses the scheduled job's code path.

**FR-15 — Per-subreddit isolation.** As a mod, I want my install's data and output confined to my subreddit, so no other subreddit's actions can leak into my wiki.
- AC: All Redis keys are install/subreddit-namespaced; `getModerationLog` is called only with this install's `subredditName`; mixed-subreddit data is structurally impossible (replaces the Python runtime mixed-data guard).

**FR-16 — Empty state.** As a reader, I want a clear "No recent moderation actions found." page when nothing qualifies, so the page is never broken/blank.
- AC: Header + empty message rendered; still hash-skippable.

**FR-17 — Schema/version bootstrap.** As a maintainer, I want the app to record and forward-migrate its Redis key schema version, so future releases can evolve storage safely.
- AC: On first run, `schema_version` set; later versions run idempotent forward migrations.

---

## 4. Non-Functional Requirements

**NFR-1 — Platform constraints (hard).** No filesystem, no long-lived daemon, no arbitrary outbound network. All persistence via Devvit Redis; all recurring work via scheduler; all Reddit I/O via the `reddit` client. (Verified: serverless sandbox.)

**NFR-2 — Wiki size limit.** 524288-byte (512 KB) cap preserved exactly; trimming threshold at 90%. This is a Reddit wiki constraint, independent of platform.

**NFR-3 — Redis budget.** Each install has **500 MB** storage and **5 MB** request-size limits; transactions limited to **30 concurrent blocks / 5 s timeout**. Design dedup/retention to stay well under these; cap sorted sets to entries actually rendered; never load unbounded ranges in one request. (Verified from redis.mdx.)

**NFR-4 — Scheduler granularity.** Minimum cron granularity is 1 minute; default cadence 10 min. Per-invocation work must complete within platform request limits — batch + cursor if a rebuild is large (documented Devvit pattern).

**NFR-5 — Determinism / parity.** For an identical sequence of mod actions, generated wiki markdown must be byte-comparable to the Python output (golden-file tested), except where platform field availability forces a documented, reviewed deviation.

**NFR-6 — Performance.** A normal incremental run (≤ batch size new actions) must complete within a single scheduler invocation and issue at most one `updateWikiPage` write (skipped when unchanged).

**NFR-7 — Observability.** Structured `console` logging at info/warn/error; no secrets or raw moderator names in logs at default level (see SEC-1). No filesystem log dir.

**NFR-8 — Maintainability / code org.** Per coding-style: many small TS modules (≤400 LOC typical, 800 max), immutable data flow, errors handled at every boundary, input from `ModAction`/settings validated before use. No single 1725-LOC file.

**NFR-9 — Distribution.** Publishing requires Reddit app review; app declares only the capabilities it uses (reddit api, redis, scheduler, triggers, settings). Plan review lead time into the release.

---

## 5. Security & Privacy Invariants (MUST preserve — non-negotiable)

These are the product's safety contract. Any build that violates one is a release blocker.

**SEC-1 — Moderator anonymization enforced.** Human moderators MUST render as `HumanModerator`; only `AutoModerator` and `Reddit` may render literally. In Python this is enforced by *refusing to start* if `anonymize_moderators=false`. **Devvit requirement:** anonymization is hard-coded (no setting to disable). Real `moderatorName`/`moderatorId` may be used internally for filtering/correlation but MUST NEVER be written to the wiki or to default-level logs. (`ModAction` exposes `moderatorName`/`moderatorId`; the guard is ours.)

**SEC-2 — Never link user profiles.** Content links MUST point only to posts/comments (`/comments/...`). Any `/u/<user>` or `/user/...` permalink MUST be rejected/suppressed. Derive links from `ModAction.target` content permalink only.

**SEC-3 — Email censoring.** Removal-reason text MUST have email addresses replaced with `[EMAIL]` (existing regex) before rendering to the public wiki.

**SEC-4 — Markdown injection / table safety.** All user/mod-derived text (reasons, titles) MUST be pipe-escaped and treated as untrusted before insertion into markdown tables. Validate all `ModAction`-sourced strings at the boundary.

**SEC-5 — 512 KB cap as a safety limit.** The byte cap is also a guard against runaway writes; it MUST be enforced before every `updateWikiPage`.

**SEC-6 — Idempotency without a daemon (NEW, elevated).** Because there is no single long-lived loop, the scheduler job AND the `onModAction` trigger can run concurrently/overlapping and runs can be retried by the platform. Dedup (per-action-id) and unchanged-hash-skip MUST make publishing idempotent: replays and overlaps converge to identical wiki content and never duplicate rows. Where correctness depends on read-check-write (e.g., "first writer wins" dedup, hash compare), use Redis transactions or single-command atomic ops per the Devvit transaction guidance.

**SEC-7 — Least privilege / no stored credentials.** No password grant, no stored Reddit credentials. The app operates under install permissions. Any future secret (none required for core function) MUST use Devvit `isSecret` global settings, never plaintext settings or code.

**SEC-8 — Per-install data isolation.** Redis keys MUST be namespaced per install/subreddit; no code path may read or write another subreddit's data (replaces SQLite "multi-subreddit single store" + mixed-data guard with structural isolation).

---

## 6. Risk Register

| ID | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-1 | `ModAction` lacks a field the Python relied on (e.g., exact `target_permalink`, comment-vs-post discrimination, AutoMod "filter" detection) → output drift | Med | Med | Spike: dump real `ModAction` objects early; map every Python field to a `ModAction`/`target` field; document any deviation; golden-file diff (NFR-5) | Eng |
| R-2 | `getModerationLog` `type` filter is single-valued (`ModActionType`), but Python filters a 7-action set | High | Low | Fetch unfiltered listing and filter in code, OR issue one call per type and merge; verify paging/limit behavior | Eng |
| R-3 | Approval-correlation + combined-row logic depended on SQL `LIKE`/`SELECT`; Redis has no ad-hoc query | Med | Med | Maintain per-content secondary index in Redis (hash/sorted-set keyed by content id) capturing prior removal mod+reason; bound by retention | Eng |
| R-4 | Redis 500 MB / 5 MB / 30-txn limits hit on busy subreddits or large rebuilds | Low | High | Cap sorted sets to rendered entries; batch+cursor rebuilds via scheduler; TTL temp keys; user-facing fallback on write-full (NFR-3) | Eng |
| R-5 | Scheduler min cadence (1 min) + per-invocation time limit insufficient for force-full-rebuild | Med | Med | Incremental design; force-rebuild processes bounded batches with a saved cursor across ticks | Eng |
| R-6 | Trigger + scheduler concurrency causes duplicate rows / racey wiki writes | Med | High | SEC-6 idempotency (dedup store + hash skip + transaction on read-check-write); coalesce trigger-driven writes | Eng |
| R-7 | Loss of cross-subreddit single-store capability changes product semantics for multi-sub operators | Low | Low | Reframe as feature (isolation, SEC-8); document that each subreddit installs independently; no shared store | PO |
| R-8 | App review rejects/ delays publish (privacy of modlog data, wiki writes) | Med | High | Lead with privacy invariants (SEC-1/2/3) in review notes; request only needed capabilities; budget review time (NFR-9) | PO |
| R-9 | Wiki write permission/visibility differs under app identity vs bot account (page must be mod-readable/public per sub policy) | Med | Med | Verify `updateWikiPage` + page permission/listing behavior under install perms in a test sub before GA | Eng |
| R-10 | No historical SQLite import → existing operators start with an empty wiki history | High | Low | Documented as greenfield-per-install; optional future backfill from live modlog on first run within retention window | PO |
| R-11 | Devvit API surface churn (pre-1.0, v0.13.x) breaks build | Med | Med | Pin `@devvit/public-api`; CI build; track changelog | Eng |
| R-12 | Email/PII regex or pipe-escape regression silently leaks data to a public page | Low | High | Port regexes verbatim; unit tests as guard; security review gate before publish | Eng |

---

## 7. MVP Acceptance Criteria

The MVP is shippable when **all** of the following hold:

1. **Install & settings.** App installs on a test subreddit; settings (FR-1) are editable by a mod and persisted; invalid values rejected/clamped.
2. **Scheduled publish (FR-2).** A cron job fetches via `getModerationLog`, builds content, and writes via `updateWikiPage` to the configured page on the default ~10-min cadence.
3. **Output parity (NFR-5, FR-4..FR-9).** For a fixed fixture of mod actions, the generated markdown is byte-comparable to the Python output (golden file), including: 7-action default filter, date-desc/time-desc grouping, 7-column table, `filter-<action>` AutoMod labeling, removal-reason priority, combined removal+reason rows, conditional approval rows, and prefilled modmail links.
4. **Security invariants (SEC-1..SEC-8) verified by tests:**
   - No human moderator name appears in wiki output or default logs.
   - No `/u/` or `/user/` link appears in any content cell.
   - Emails in reasons render as `[EMAIL]`.
   - All reason/title cells are pipe-safe.
   - Every write is ≤524288 bytes.
   - Redis keys are subreddit-namespaced; no cross-sub read/write path exists.
   - No Reddit credentials are stored anywhere; no password grant.
5. **Idempotency (SEC-6, FR-10/FR-11).** Running the job twice back-to-back, and simulating an `onModAction` overlapping a scheduled run, produces no duplicate rows and at most one wiki write when content is unchanged (hash-skip proven).
6. **Size cap & trim (FR-12).** A fixture exceeding 90% of 512 KB trims oldest days, emits the trim notice, and the final write is ≤512 KB.
7. **Retention (FR-13).** A scheduled cleanup removes entries older than `retentionDays` from Redis (verified via store inspection).
8. **Empty state (FR-16).** Zero qualifying actions renders the "No recent moderation actions found." page without error.
9. **No-deviation log.** Any field that could not be reproduced from `ModAction`/`target` (R-1) is documented with the chosen fallback and signed off by PO.
10. **Buildable & reviewable.** App builds against pinned `@devvit/public-api`, declares only the capabilities it uses (reddit api, redis, scheduler, triggers, settings), and is packaged for app-review submission.

**Deferred beyond MVP (Should/Could):** `onModAction` near-real-time trigger (FR-3) if not ready at MVP may ship in fast-follow provided the scheduler path already satisfies idempotency; manual menu actions (FR-14); SQLite history backfill (R-10).

---

### Appendix A — Verified Devvit primitive mapping (evidence)

| Python primitive | Devvit replacement | Evidence (repo) |
|---|---|---|
| `subreddit.mod.log(limit=N)` | `reddit.getModerationLog({subredditName, type?, ...ListingFetchOptions}) : Listing<ModAction>` | `reddit/devvit` `models/ModAction.ts`, `RedditClient.ts`; docs `GetModerationLogOptions.md` |
| `subreddit.wiki[p].edit(content,reason)` | `reddit.updateWikiPage({page,content,reason?})`; `getWikiPage(sub,page)` | `RedditClient.ts`, `RedditAPIClient.ts`; docs `UpdateWikiPageOptions.md` |
| action fields (`mod`, `created_utc`, `details`, target) | `ModAction.{moderatorName,moderatorId,createdAt:Date,description,details,target,type,id}` | docs `ModAction.md` |
| action type filter set | `ModActionType` enum incl. all 7 default actions | docs `ModActionType.md` |
| SQLite dedup/retention/hash-cache | Devvit Redis: strings (dedup/hash), sorted sets scored by time (retention via `zRemRangeByScore`), hashes | docs `capabilities/server/redis.mdx` (500 MB/5 MB/30-txn limits) |
| daemon loop / `update_interval` | `addSchedulerJob` + cron (`/internal/...`, min 1-min) | docs `capabilities/server/scheduler.mdx` |
| (new) low-latency updates | `onModAction` trigger | docs `capabilities/server/triggers.mdx` |
| CLI/env/JSON config | `devvit.json` settings, scopes `global`/`subreddit`, types string/boolean/select/multiSelect, secrets via `isSecret` | docs `capabilities/server/settings-and-secrets.mdx` |

Relevant file paths: existing app `/mnt/data/_development/RedditModLog/modlog_wiki_publisher.py` (read-only, do not modify); existing config template `/mnt/data/_development/RedditModLog/config_template.json`; target Devvit code root `/mnt/data/_development/RedditModLog/devvit/` (to be created; a `devvit-migration/` directory currently exists at repo root).
