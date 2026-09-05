I have all the API shapes needed. Writing the reference now.

---

# Devvit API Reference — RedditModLog Migration

Source-of-truth: `reddit/devvit` @ `packages/reddit/src/` (verified 2026-06-23). Doc cross-refs from `reddit/devvit-docs`. All types below are quoted from the actual TypeScript sources, not docs prose.

## 1. `getModerationLog` — replaces `subreddit.mod.log(limit=N)`

```ts
// reddit.getModerationLog(options): Listing<ModAction>
getModerationLog(options: GetModerationLogOptions): Listing<ModAction>

export type GetModerationLogOptions = ListingFetchOptions & {
  subredditName: string;            // REQUIRED
  moderatorUsernames?: string[];    // optional mod filter (joined with ',')
  type?: ModActionType;             // SINGLE action type only (see note)
};

export type ListingFetchOptions = {
  after?: string;
  before?: string;
  limit?: number;      // default = Infinity  (NOT a small default!)
  pageSize?: number;   // default = 100
  more?: MoreObject;
};
```

Usage (from RedditClient.ts JSDoc):

```ts
import { reddit } from '@devvit/reddit';

const modActions = await reddit.getModerationLog({
  subredditName: 'memes',
  limit: 1000,
  pageSize: 100,
}).all();          // .all() drains the Listing into ModAction[]
```

`Listing<ModAction>` is async-iterable and exposes `.all(): Promise<T[]>`. `DEFAULT_PAGE_SIZE = 100`, `DEFAULT_LIMIT = Infinity`. **Always set an explicit `limit`** to mirror the Python `limit=N` — an unset limit will page the entire log.

### CRITICAL DIFFERENCE — `type` is single-valued

The Python app filters to a **list** of 7 `wiki_actions` (`removelink`, `removecomment`, `spamlink`, `spamcomment`, `addremovalreason`, `approvelink`, `approvecomment`). Devvit's `type?` accepts **one** `ModActionType`, not an array (it maps to the upstream `type=` query param). Two options for the migration:

- **Fetch unfiltered** (`type` omitted), pull a page batch, and filter client-side against the `wiki_actions` set — matches current multi-type behavior in one call. **Recommended.**
- Or issue 7 separate `getModerationLog` calls (one per type) and merge — more network calls, redundant against the free serverless budget; not recommended.

## 2. `ModAction` interface — field mapping

```ts
export interface ModAction {
  id: string;              // "ModAction_1b1af634-..." — the modlog entry id
  type: ModActionType;
  moderatorName: string;
  moderatorId: T2;         // "t2_..."
  createdAt: Date;         // native Date (Python had to parse epoch)
  subredditName: string;
  subredditId: T5;         // "t5_..."
  description?: string;    // e.g. "Page X edited"
  details?: string;        // e.g. removal-reason details / context
  target?: ModActionTarget;
}

export type ModActionTarget = {
  id: string;              // fullname: t3_/t1_/t2_/t5_...
  author?: string;         // username the action was taken upon
  body?: string;           // bodytext of the targeted item
  permalink?: string;      // relative permalink (no scheme/host)
  title?: string;          // title of the targeted item
};
```

### Python-read field → Devvit field

| Python (PRAW `ModAction` / extraction) | Devvit field | Notes |
|---|---|---|
| `action.id` (dedup `action_id`) | `ModAction.id` | Format `ModAction_<uuid>`; PRAW used `ModAction_<uuid>` too. Use as Redis dedup key. |
| `action.action` (`action_type`) | `ModAction.type` | Enum, see §3. |
| `action.mod` (moderator name) | `ModAction.moderatorName` | Anonymize before render (INVARIANT). |
| `action.created_utc` (`created_at`) | `ModAction.createdAt` | Native `Date`; Python parsed epoch float. `createdAt.getTime()/1000` for Redis sorted-set score. |
| `action.subreddit` | `ModAction.subredditName` | |
| `action.target_fullname` → `target_id` | `ModAction.target?.id` | `t3_=link/post (P)`, `t1_=comment (C)`, `t2_=user (U)`, else (A/other). Derive `display_id` P/C/U/A prefix from the fullname prefix. |
| `target_type` | derived from `target.id` prefix | No dedicated field — compute it. |
| `action.target_permalink` (`target_permalink`) | `ModAction.target?.permalink` | Relative (no `https://www.reddit.com`); prepend host for links. INVARIANT: only post/comment permalinks, never user profiles. |
| `action.target_author` (`target_author`) | `ModAction.target?.author` | Used for modmail prefill. |
| removal reason text (`removal_reason`) | `ModAction.details` (+ maybe `description`) | **FLAG — see below.** PRAW exposed `action.details` / `action.description`; same here. Apply email-censor + pipe-escape. |

### MISSING / FLAGGED fields (verify against live data before relying)

- **`removal_reason`**: There is **no dedicated `removalReason` field**. The Python code extracted the reason from `details` / `description`. Devvit exposes exactly `description?` and `details?` — same raw strings PRAW surfaced. For `addremovalreason` actions the human-readable reason lives in `details`. **Action: port the existing string-extraction logic verbatim onto `ModAction.details` (fallback `description`).** Confirm shape with one real `addremovalreason` entry on the target sub before shipping.
- **`target_title` / `target_body`**: `target.title` / `target.body` exist (not read by the Python app, but available if you want richer rows).
- **`target_type`**: not a field — must be derived from `target.id`'s fullname prefix (Python did the same from `target_fullname`).
- **No raw OAuth / API-call surface**: auth, rate-limit, and HTTP are handled by Devvit's serverless reddit client. The Python password-grant flow has **no equivalent and must be dropped** (the app runs as the install's mod context).

## 3. `ModActionType` enum (the action "type" values)

String-literal union (full list in `ModAction.ts`). The 7 the app cares about (`wiki_actions`) are all present:

```
'removelink' | 'removecomment' | 'spamlink' | 'spamcomment'
| 'addremovalreason' | 'approvelink' | 'approvecomment'
```

Other notable members: `banuser`, `unbanuser`, `addmoderator`, `distinguish`, `lock`, `unlock`, `sticky`, `editflair`, `createremovalreason`, `updateremovalreason`, `deleteremovalreason`, `wikirevise`, plus `dev_platform_app_*`. The Python `ignored_moderators` default (`[AutoModerator]`) is filtered on `moderatorName`, not on `type` — unchanged logic.

## 4. Wiki — replaces `subreddit.wiki[page].edit()`

```ts
// READ
async getWikiPage(
  subredditName: string,
  page: string,
  revisionId?: WikiPageRevisionId
): Promise<WikiPage>

// WRITE (update existing)
async updateWikiPage(options: UpdateWikiPageOptions): Promise<WikiPage>
export type UpdateWikiPageOptions = {
  subredditName: string;
  page: string;
  content: string;       // markdown
  reason?: string;
};

// CREATE (first-time; getWikiPage throws if page absent)
async createWikiPage(options: CreateWikiPageOptions): Promise<WikiPage>
export type CreateWikiPageOptions = {
  subredditName: string; page: string; content: string; reason?: string;
};
```

`WikiPage` getters: `name`, `subredditName`, `content` (markdown), `contentHtml`, `revisionId`, `revisionDate: Date`, `revisionReason`, `revisionAuthor`. Instance helpers: `page.update(content, reason)`, `page.getRevisions(...)`, `page.revertTo(revisionId)`, `page.getSettings()`, `page.updateSettings(...)`.

### Read / write snippet

```ts
import { reddit } from '@devvit/reddit';

const sub = 'mysub';
const pageName = 'modlog';
const markdown = buildTables(actions);   // enforce 512KB / 524288-byte cap first

// CREATE-or-UPDATE pattern (getWikiPage throws if the page doesn't exist):
let existing: WikiPage | undefined;
try {
  existing = await reddit.getWikiPage(sub, pageName);
} catch {
  existing = undefined;
}

if (!existing) {
  await reddit.createWikiPage({
    subredditName: sub, page: pageName, content: markdown,
    reason: 'RedditModLog initial publish',
  });
} else if (existing.content !== markdown) {     // hash-skip equivalent
  await reddit.updateWikiPage({
    subredditName: sub, page: pageName, content: markdown,
    reason: 'RedditModLog update',
  });
}
```

Notes for migration:
- **512KB cap still applies** — Devlatform does not lift Reddit's 524288-byte wiki limit. Keep the trim logic.
- **Hash-skip**: `WikiPage.content` returns the current markdown, so you can compare directly (or keep the SHA-256 cache in Redis hash). The `wiki_hash_cache` table maps to a Redis hash keyed by `subreddit:wiki_page → content_hash`.
- `getWikiPage` for a non-existent page throws — guard with try/catch and fall back to `createWikiPage` (Python's `wiki[page].edit()` auto-created). To restrict visibility use `WikiPagePermissionLevel` (`SUBREDDIT_PERMISSIONS=0`, `APPROVED_CONTRIBUTORS_ONLY=1`, `MODS_ONLY=2`) via `updateWikiPageSettings`.

## 5. Trigger / scheduling notes (for the orchestration layer)

- **Scheduler**: `reddit/devvit-docs/docs/capabilities/server/scheduler.mdx` covers cron jobs — replaces the `update_interval` (600s) daemon loop. Declare a cron-scheduled endpoint in `devvit.json` (the modern config-file model; the older `Devvit.addSchedulerJob` API also exists).
- **`onModAction` trigger** exists (`devvit.json` `"triggers": { "onModAction": "/internal/on-mod-action" }`; proto `OnModActionDefinition`). It fires per mod action and could drive event-driven publishing instead of/alongside polling. **However**, the wiki write is the expensive, rate-limited op and the table is a full re-render — a debounced cron (every N min) is the cleaner mapping of the 600s loop. Recommend: **cron-driven publish**, optionally with `onModAction` only to set a "dirty" Redis flag the cron checks (avoids rewriting unchanged pages). Confirm the `ModAction` trigger payload shape (it mirrors the `ModAction` interface above) against a live event before wiring dedup off it.

### Net migration deltas to flag for the implementer
1. `type` filter is single-valued → filter the 7 `wiki_actions` **client-side**.
2. No `removalReason` field → reuse Python's `details`/`description` extraction.
3. No OAuth/password-grant → drop entirely (serverless mod context).
4. `target_type` is derived, not a field.
5. `limit` defaults to `Infinity` → always pass an explicit limit.
6. `getWikiPage` throws on missing page → create/update branch required.
