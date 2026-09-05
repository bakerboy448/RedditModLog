# RedditModLog → Devvit: Migration Status

| Field | Value |
|---|---|
| Status | Scaffolded — **type-checks clean** against `@devvit/public-api@0.13.5` (`npm run type-check` passes, `dist/` emits); not yet playtested on a live subreddit |
| Date | 2026-06-23 |
| Branch | `feat/devvit-migration` |
| Code root | `devvit/` (classic `@devvit/public-api` 0.13.5 model) |
| Legacy source | `modlog_wiki_publisher.py` (read-only reference) |

This document tracks what is implemented vs. outstanding, mapped to the parity
matrix and the binding invariants (INV-1..INV-9).

---

## 1. Build artifacts (this pass)

| File | State | Notes |
|---|---|---|
| `devvit/package.json` | DONE | `redditmodlog-devvit`, dep `@devvit/public-api@0.13.5`, scripts: `deploy`/`dev`/`playtest`/`login`/`launch`/`type-check`/`test`. |
| `devvit/devvit.yaml` | DONE | `name: redditmodlog`. Unique-name claim happens on first `devvit upload` — rename here first if taken. |
| `devvit/tsconfig.json` | DONE | Extends `@devvit/public-api/devvit.tsconfig.json` (module/moduleResolution = **NodeNext**). |
| `devvit/.gitignore` | DONE | `node_modules`, `dist`, `.devvit`, `.env*`. |
| `devvit/src/main.ts` | DONE | Entrypoint: `Devvit.configure` + scheduler job + triggers + settings/menu registration + the shared publish cycle. |
| `devvit/README.md` | DONE | Upload/playtest, settings table, parity/invariant matrix. |
| `devvit/src/types.ts` | DONE (NEW) | The shared contract every module imported but which did not exist — see §3. |

---

## 2. Component modules (authored separately; wired this pass)

| Module | State | Owner-of (invariants) |
|---|---|---|
| `storage.ts` | DONE + adapter layer added (§3) | INV-5, INV-6, INV-9, retention |
| `modlog.ts` | DONE | INV-1, INV-2 (gating), INV-5, INV-7, INV-8 |
| `render.ts` | DONE (1 import fixed, §3) | INV-2 (emit), INV-3, INV-4, INV-6 (hash) |
| `wiki.ts` | DONE | INV-3 (guard), INV-6 |
| `settings.ts` | DONE | INV-1, INV-7, INV-8, INV-9 |
| `menu.ts` | DONE (signature drift fixed, §3) | — (thin adapter) |

---

## 3. Contract reconciliation performed this pass

The component modules were authored in parallel against the architecture spec,
but the spec's idealized names and `storage.ts`'s actual implementation had
**drifted**. The whole project would not have compiled or linked. The following
minimal, non-logic reconciliations were made so it builds:

1. **`types.ts` created** — `modlog.ts`, `render.ts`, and `settings.ts` all
   `import` from `./types.js`, but the file did not exist. Created it as the
   single source of truth for `ModRecord`, `AppConfig`, `ModActionType`,
   `DisplayKind`, and all constants (`WIKI_BYTE_CAP`, `WIKI_TRIM_TARGET`,
   `ANON_LABEL`, `LITERAL_MODS`, `ANONYMIZE_MODERATORS`, `DEFAULT_*`,
   `*_MIN/_MAX`, `VALID_MODLOG_ACTIONS`, `SCHEMA_VERSION`). No `@devvit/*` import
   so the pure render layer stays platform-free.

2. **Storage spec-name adapter layer** (appended to `storage.ts`) — the spec /
   sibling modules call `markSeen` / `putRecord` / `getAllRecords` /
   `getStatus` / `recordRunStarted` / `recordPublished`, but the implementation
   defined `isProcessed` / `recordAction` / `getRecentActions` and **no** status
   accessors. Added thin adapters:
   - `markSeen` = `!isProcessed` (inverse sense: returns `true` when NEW).
   - `putRecord` → `recordAction`.
   - `getAllRecords` → `getRecentActions` with a default cap.
   - `getStatus` / `recordRunStarted` / `recordPublished` → new `status` hash.
   No existing logic was rewritten.

3. **`render.ts` import extension** — `from './types'` → `from './types.js'`.
   Under the Devvit base tsconfig's **NodeNext** resolution, relative specifiers
   MUST carry the `.js` extension; the bare specifier was a compile error.

### Reconciliations performed this pass (compiler-verified)

4. **`modlog.ts` package + client threading fixed** — it imported
   `{ reddit }` and `ModAction` from `@devvit/reddit` (the split-package /
   Devvit-Web name), which does NOT exist in the classic
   `@devvit/public-api@0.13.5` model and failed with `TS2307`. The classic model
   has **no `reddit` singleton** — the Reddit client is `context.reddit`
   (`RedditAPIClient`), and `ModAction`/`ModActionType` are re-exported from
   `@devvit/public-api`. Fixed by importing types from `@devvit/public-api` and
   threading a `RedditAPIClient` argument through `fetchActions(reddit, cfg)` and
   `ingest(reddit, redis, cfg)` (mirrors `wiki.ts`). Callers updated:
   `main.ts` (`runPublishCycle` + `ModAction` trigger now destructure `reddit`
   and pass it), `menu.ts` (`handlePublishNow` passes `reddit`).

5. **`menu.ts` signature drift fixed** — it called `loadConfig()` (0 args) and
   `ingest(reddit, redis, cfg)` against a then-2-arg `ingest`. Now calls
   `loadConfig(context.settings, context.subredditName)` (with an INV-9
   no-subreddit guard) and `ingest(reddit, redis, cfg)` against the corrected
   3-arg signature.

After (4)+(5): `npm run type-check` passes with zero errors and `dist/` emits
for all 8 modules (forced clean rebuild verified).

### Known residual drift (cosmetic — non-blocking)

- **`ModRecord` (types) vs `ModActionRecord` (storage)** are structurally
  identical, so cross-passing type-checks today. Consider collapsing to one
  named type to avoid future drift.
- **`wiki.ts` header comments** still reference the `@devvit/reddit` /
  `@devvit/redis` split packages as a Devvit-Web TODO; the actual imports
  correctly use `@devvit/public-api`. Documentation-only; harmless.

---

## 4. Parity matrix (Python → Devvit)

| Capability | Python | Devvit | State |
|---|---|---|---|
| Auth | password-grant OAuth | platform-managed | DONE (no code) |
| Mod-log fetch | `subreddit.mod.log(limit)` | `reddit.getModerationLog({limit,pageSize})` | DONE |
| Action filter (7 types) | client-side | client-side (`type` filter is single-valued) | DONE |
| Anonymize (INV-1) | enforced | hardcoded, no toggle | DONE |
| Profile-link ban (INV-2) | enforced | permalink only for t1/t3 | DONE |
| Reason censor/escape (INV-4) | regex | ported regex (`render`) | DONE |
| Markdown tables | per-day | per-day (`render.buildContent`) | DONE |
| Modmail prefill link | yes | `render.modmailLink` | DONE |
| 512 KB cap + trim (INV-3) | yes | `render.enforceByteCap` + `wiki` guard | DONE |
| Dedup (INV-5) | SQLite UNIQUE | Redis atomic NX (`markSeen`) | DONE |
| Wiki hash-skip (INV-6) | SHA-256 cache | SHA-256 cache (`wiki`/`storage`) | DONE |
| Retention (90d) | row delete | zset prune by score (`cleanupOld`) | DONE |
| Daemon loop | `update_interval` 600s | scheduler cron `*/10 * * * *` | DONE |
| Prompt-fast on action | n/a | `ModAction` trigger (ingest only) | DONE |
| Config (19 opts) | CLI/env/JSON | 6 install settings + 1 hardcoded | DONE |
| Multi-subreddit | single store | one install per sub (isolation) | DONE (by design) |
| CLI `--test` / `--force-*` | yes | menu "Publish now" (force/test variants partial) | PARTIAL |

---

## 5. Outstanding TODO before a real deploy

1. ~~Fix `menu.ts` signature drift~~ — **DONE** (§3.4/§3.5); project type-checks
   end-to-end.
2. ~~Install deps + type-check~~ — **DONE**: `npm install` (457 pkgs) +
   `npm run type-check` pass clean against `@devvit/public-api@0.13.5`; `dist/`
   emits. The import surface and the `getModerationLog` / `getWikiPage` /
   `createWikiPage` / `updateWikiPage` / scheduler / settings call shapes are now
   compiler-validated against the installed SDK types.
3. **Verify remaining runtime call shapes against a live install** — types
   compile, but these need playtest confirmation (behavior, not just types):
   - `reddit.getModerationLog({ subredditName, limit, pageSize })` + `.all()`
     (Listing drain — some versions use `for await` instead).
   - `reddit.getWikiPage(sub, page)` throwing on absence; `createWikiPage` /
     `updateWikiPage` option shapes (`{ subredditName, page, content, reason }`).
   - `context.scheduler.listJobs()` / `cancelJob(id)` / `runJob({name, cron})`.
   - `Devvit.addTrigger({ events: ['AppInstall','AppUpgrade'] })` and
     `event: 'ModAction'` payload fields.
   - `context.settings.get`, `context.subredditName` on scheduler/trigger ctx.
4. **Cron cadence**: confirm `*/10 * * * *` is permitted for the app tier;
   tighten/loosen as policy allows (Python used 600s).
5. **Playtest** on a test subreddit (`npm run playtest`) — exercise: install →
   settings save (validators) → menu "Publish now" → wiki page created →
   second run hash-skips → mod action triggers ingest → retention prune.
6. **Unit tests** for the pure layers (`render.*`, `modlog.anonymizeMod` /
   `deriveDisplay` / `extractRecord`, `settings` validators). `vitest` is wired
   in `package.json`; no test files written yet.
7. **App review** before public listing (`devvit publish`).

---

## 6. Dropped legacy options (intentional, no parity needed)

`client_id`, `client_secret`, `username`, `password` (platform auth);
`source_subreddit` (install context, INV-9); `update_interval` (scheduler);
`wiki_display_days` (folded into `retention_days`); `max_continuous_errors`,
`rate_limit_buffer`, `max_batch_retries` (no daemon; platform-managed retry/
rate-limit); `archive_threshold_days`, `database_path` (Redis, no SQLite);
`display_format` (fixed render). `anonymize_moderators` is hardcoded `true`
(INV-1), not a setting.
