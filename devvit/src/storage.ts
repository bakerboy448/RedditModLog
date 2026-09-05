/**
 * storage.ts — Redis data-access layer (Repository pattern).
 *
 * This is the ONLY module that talks to Redis. Every other module depends on
 * the exported async functions here, never on raw Redis commands (patterns.md
 * Repository pattern). It encapsulates four concerns:
 *
 *   1. Dedup of processed ModAction ids                    (INV-5)
 *   2. The render-ready action-record collection           (rebuild source)
 *   3. Retention via a sorted-set keyed by created_at secs  (INV-9 / retention)
 *   4. The wiki content-hash cache                          (INV-6)
 *
 * Per-subreddit isolation (INV-9) is structural: Devvit name-spaces every app
 * INSTALLATION's Redis data by subreddit, so there is NO `subreddit` column and
 * cross-subreddit mixing is impossible. Key prefixes below are therefore scoped
 * only by `wikiPage`, allowing multiple wiki pages per install without collision.
 *
 * No KEYS / global SCAN is used anywhere (Devvit forbids it) — every collection
 * is rooted at an explicit, known key.
 *
 * Devvit Redis API notes (verified against reddit/devvit
 * packages/redis/src/RedisClient.ts):
 *   - `set(key, value, { nx, expiration })` — `expiration` is a Date, and the
 *     return value is the stored string, NOT a NX-success boolean. So we do NOT
 *     use `set` for atomic dedup-newness detection.
 *   - `hSetNX(key, field, value)` returns 1 when the field was newly created and
 *     0 when it already existed — this IS race-free and is our dedup primitive.
 *   - `zAdd(key, ...members)` takes spread `ZMember[]` (not an array).
 *   - `zRange(key, start, stop, { by:'score' })` returns `{member,score}[]` and
 *     internally caps at 1000 members per call (we loop to drain).
 *   - `zRemRangeByScore(key, min, max)` prunes by score window.
 *   - `hGetAll` returns `Record<string,string>`; `hDel(key, fields[])`.
 */

// The prompt mandates importing from '@devvit/public-api', which re-exports the
// Redis types. `RedisClient` is the structural type of the `redis` handle that
// callers obtain from their Devvit context / `@devvit/redis`.
import type { RedisClient } from '@devvit/public-api';

// ---------------------------------------------------------------------------
// Record type — the persisted, render-ready shape of one moderation action.
// ---------------------------------------------------------------------------

/** P/C/U/A display-prefix kind derived from the target fullname. */
export type DisplayKind = 'P' | 'C' | 'U' | 'A';

/**
 * One moderation-log action, stored in render-ready form. Enough is persisted
 * to rebuild the entire wiki page from Redis alone (no re-fetch required).
 *
 * INVARIANTS baked into this shape:
 *   - `moderator` is ALREADY anonymized at ingest (INV-1) — a real moderator
 *     name must NEVER reach Redis. Only `HumanModerator`, `AutoModerator`, or
 *     `Reddit` are valid here.
 *   - `permalink` is present ONLY for posts/comments (INV-2). For user/subreddit
 *     targets it is `undefined` so the render layer cannot emit a profile link.
 *   - `reason` is stored RAW; email-censor + pipe-escape (INV-4) are applied at
 *     render time, keeping storage idempotent and the censor regex evolvable
 *     without a data migration.
 */
export interface ModActionRecord {
  /** ModAction.id — the dedup key (INV-5). Also the hash field + zset member. */
  id: string;
  /** Epoch SECONDS of the action's creation; the sorted-set score (retention). */
  createdAtSec: number;
  /** The tracked action type (e.g. 'removelink'); already filtered to INV-7. */
  actionType: string;
  /** ALREADY-anonymized moderator label (INV-1) — never a real name. */
  moderator: string;
  /** Target fullname (t3_/t1_/t2_/t5_...), if any. */
  targetId?: string;
  /** P (post) | C (comment) | U (user) | A (any/other). */
  displayKind: DisplayKind;
  /** Short, prefixed display id (e.g. 'P:abc123'), if a target exists. */
  displayId?: string;
  /** Post/comment permalink ONLY (INV-2); undefined for profiles/subreddits. */
  permalink?: string;
  /** Target author handle, for the modmail removal-inquiry prefill. */
  targetAuthor?: string;
  /** RAW reason text; sanitized at render (INV-4). */
  reason?: string;
}

// ---------------------------------------------------------------------------
// Key layout. All keys are scoped by wiki page; the install is already scoped
// by subreddit (INV-9), so no subreddit segment is needed.
// ---------------------------------------------------------------------------

/** Hash of seen action ids — field=actionId, value='1'. Powers atomic dedup. */
const seenKey = (page: string): string => `seen:${page}`;
/** Hash of full records — field=actionId, value=JSON(ModActionRecord). */
const actionsKey = (page: string): string => `actions:${page}`;
/** Sorted set: member=actionId, score=createdAtSec. Drives retention pruning. */
const timeIndexKey = (page: string): string => `actions_by_time:${page}`;
/** Hash of wiki content hashes — field=page, value=sha256hex. */
const WIKI_HASH_KEY = 'wiki_hash';

/** zRange returns at most this many members per call; drain loops honor it. */
const ZRANGE_PAGE_LIMIT = 1000;

// ---------------------------------------------------------------------------
// 1. Dedup (INV-5)
// ---------------------------------------------------------------------------

/**
 * Atomically test-and-mark an action id as processed.
 *
 * Returns `true` if this id had NOT been seen before (caller SHOULD process and
 * persist it), or `false` if it was already processed (caller skips). This is
 * race-free: `hSetNX` only sets — and returns truthy — when the field is new,
 * so two concurrent invocations (cron + onModAction trigger) can never both
 * believe they "won" the same id.
 *
 * Naming: exported as `isProcessed` per the module contract. Note the inverted
 * sense — it returns whether the action is NEW (not-yet-processed). It also
 * has the side effect of MARKING the id, by design, so the dedup decision and
 * the claim are a single atomic step.
 *
 * @param redis    Devvit redis handle (subreddit-scoped by the platform).
 * @param page     Wiki page these actions belong to.
 * @param actionId The ModAction.id to test-and-claim.
 * @returns        `true` when newly claimed (process it); `false` when duplicate.
 */
export async function isProcessed(
  redis: RedisClient,
  page: string,
  actionId: string,
): Promise<boolean> {
  // hSetNX => 1 (truthy) when newly created, 0 when the field already existed.
  const created = await redis.hSetNX(seenKey(page), actionId, '1');
  return !created;
}

// ---------------------------------------------------------------------------
// 2. Record collection (rebuild source)
// ---------------------------------------------------------------------------

/**
 * Persist one render-ready record. Writes the full record into the `actions`
 * hash AND indexes it in the time-ordered sorted set so retention pruning and
 * "newest N" reads both work. Re-recording the same id is idempotent (hash
 * field + zset member are overwritten in place), which lets force-rebuild
 * re-ingest without creating duplicates.
 *
 * NOTE: the two writes are not wrapped in a transaction. With the cron job as
 * the normal single writer the worst-case interleave (an entry in the hash but
 * not yet the zset, or vice versa) is self-healing on the next run, and a
 * partially-indexed record still renders. If the onModAction trigger and cron
 * are later allowed to write concurrently, wrap these in `watch/multi/exec`.
 *
 * @param redis  Devvit redis handle.
 * @param page   Wiki page scope.
 * @param record The fully-extracted, ALREADY-anonymized record (INV-1).
 */
export async function recordAction(
  redis: RedisClient,
  page: string,
  record: ModActionRecord,
): Promise<void> {
  await redis.hSet(actionsKey(page), { [record.id]: JSON.stringify(record) });
  await redis.zAdd(timeIndexKey(page), {
    member: record.id,
    score: record.createdAtSec,
  });
}

/**
 * Return the most-recent records, newest first, capped to `maxEntries`.
 *
 * Reads ids newest-first from the time index (reverse score order), then
 * batch-resolves their full records from the `actions` hash. Records that are
 * missing or fail to parse are skipped defensively (never throws on bad data).
 *
 * @param redis      Devvit redis handle.
 * @param page       Wiki page scope.
 * @param maxEntries Upper bound on returned records (e.g. AppConfig.maxWikiEntries).
 * @returns          Up to `maxEntries` records, sorted createdAtSec descending.
 */
export async function getRecentActions(
  redis: RedisClient,
  page: string,
  maxEntries: number,
): Promise<ModActionRecord[]> {
  if (maxEntries <= 0) return [];

  // Pull the newest ids from the sorted set. `reverse: true` gives high scores
  // (most recent) first; we only need the newest `maxEntries`.
  // Full score window (-inf..+inf) expressed as ±Infinity bounds.
  const indexed = await redis.zRange(timeIndexKey(page), -Infinity, +Infinity, {
    by: 'score',
    reverse: true,
    limit: { offset: 0, count: maxEntries },
  });
  if (indexed.length === 0) return [];

  // Resolve full records. hGetAll is a single round-trip; we then look up by id
  // preserving the time-index ordering (which is authoritative for "newest").
  const all = await redis.hGetAll(actionsKey(page));
  if (!all) return [];

  const records: ModActionRecord[] = [];
  for (const { member } of indexed) {
    const raw = all[member];
    if (!raw) continue; // record evicted/missing — skip rather than fail.
    try {
      records.push(JSON.parse(raw) as ModActionRecord);
    } catch {
      // Corrupt JSON for one record must not break the whole rebuild.
      continue;
    }
  }
  return records;
}

// ---------------------------------------------------------------------------
// 3. Retention (INV-9 / time-based pruning)
// ---------------------------------------------------------------------------

/**
 * Delete every record older than `retentionDays` relative to `nowSec`.
 *
 * Works off the sorted-set time index: it pages out members whose score is at
 * or below the cutoff (honoring the 1000-member-per-zRange limit by looping),
 * removes them from the `actions` hash, the `seen` dedup hash, and finally
 * trims the time index by score. Returns the total number of records removed.
 *
 * The `seen` dedup field is also pruned so an old, since-deleted action can be
 * re-recorded if it somehow reappears; this bounds the dedup hash to roughly
 * the retention window rather than growing unbounded.
 *
 * @param redis         Devvit redis handle.
 * @param page          Wiki page scope.
 * @param retentionDays Days to keep (e.g. AppConfig.retentionDays, clamped 1..365).
 * @param nowSec        Current epoch seconds (injected for testability).
 * @returns             Count of records removed.
 */
export async function cleanupOld(
  redis: RedisClient,
  page: string,
  retentionDays: number,
  nowSec: number,
): Promise<number> {
  const cutoffSec = nowSec - retentionDays * 86_400;
  if (cutoffSec <= 0) return 0;

  let totalRemoved = 0;

  // Drain expired members in pages of up to ZRANGE_PAGE_LIMIT. We re-query the
  // bottom of the index each iteration because zRemRangeByScore shifts it.
  for (;;) {
    const expired = await redis.zRange(timeIndexKey(page), -Infinity, cutoffSec, {
      by: 'score',
      limit: { offset: 0, count: ZRANGE_PAGE_LIMIT },
    });
    if (expired.length === 0) break;

    const ids = expired.map((m) => m.member);
    // Remove the full records and their dedup markers.
    await redis.hDel(actionsKey(page), ids);
    await redis.hDel(seenKey(page), ids);
    totalRemoved += ids.length;

    if (expired.length < ZRANGE_PAGE_LIMIT) break; // last (partial) page handled.
  }

  // Finally trim the time index itself in one shot (idempotent if already gone).
  if (totalRemoved > 0) {
    await redis.zRemRangeByScore(timeIndexKey(page), -Infinity, cutoffSec);
  }

  return totalRemoved;
}

// ---------------------------------------------------------------------------
// 4. Wiki content-hash cache (INV-6)
// ---------------------------------------------------------------------------

/**
 * Get the last-written content hash for a wiki page, or `undefined` if the page
 * has never been published (or its hash is not yet cached).
 *
 * @param redis Devvit redis handle.
 * @param page  Wiki page name.
 */
export async function getWikiHash(
  redis: RedisClient,
  page: string,
): Promise<string | undefined> {
  const all = await redis.hGetAll(WIKI_HASH_KEY);
  if (!all) return undefined;
  return all[page] ?? undefined;
}

/**
 * Store the content hash that was just written to a wiki page, so the next
 * publish can skip an unchanged write (INV-6).
 *
 * @param redis Devvit redis handle.
 * @param page  Wiki page name.
 * @param hash  SHA-256 hex of the content that was published.
 */
export async function setWikiHash(
  redis: RedisClient,
  page: string,
  hash: string,
): Promise<void> {
  await redis.hSet(WIKI_HASH_KEY, { [page]: hash });
}

// ---------------------------------------------------------------------------
// 5. Spec-name compatibility layer + status accessors
// ---------------------------------------------------------------------------
//
// The architecture spec (and the sibling modlog/wiki/menu modules) reference
// these function names; the implementations above were authored under slightly
// different names. The thin adapters below bind the spec names to the
// implementations so the whole project links without duplicating logic.
//
// Drift map (spec name -> implementation):
//   markSeen      -> inverse of isProcessed (markSeen returns TRUE when NEW)
//   putRecord     -> recordAction
//   getAllRecords -> getRecentActions (newest-first, default cap)
//   getStatus / recordRunStarted / recordPublished -> status hash (added below)

/** Default cap for getAllRecords when no explicit limit is supplied. */
const DEFAULT_RECORD_FETCH_CAP = 2000;

/** Status hash: field=metric, value=epoch-ms string. */
const STATUS_KEY = 'status';
const STATUS_FIELD_LAST_RUN = 'lastRunAtMs';
const STATUS_FIELD_LAST_PUBLISHED = 'lastPublishedAtMs';

/**
 * The status accessors are not page-scoped in the menu contract, so they read
 * the default wiki page for the entry count. Multi-page installs still publish
 * correctly; the status entry count just reflects the primary page.
 */
const DEFAULT_STATUS_PAGE = 'modlog';

/**
 * Atomic test-and-mark for dedup (INV-5), spec spelling.
 *
 * Returns `true` when the action id was NOT previously seen (caller should
 * process it), `false` when it was already processed. This is the inverse sense
 * of `isProcessed` (which returns `true` for duplicates), so we negate.
 *
 * `retentionDays` is accepted for signature compatibility with the spec; the
 * underlying dedup hash is pruned by `cleanupOld` rather than per-key TTL.
 */
export async function markSeen(
  redis: RedisClient,
  page: string,
  actionId: string,
  _retentionDays?: number,
): Promise<boolean> {
  const duplicate = await isProcessed(redis, page, actionId);
  return !duplicate;
}

/** Persist one render-ready record (spec spelling for `recordAction`). */
export async function putRecord(
  redis: RedisClient,
  page: string,
  record: ModActionRecord,
): Promise<void> {
  return recordAction(redis, page, record);
}

/**
 * Return all retained records newest-first (spec spelling). The wiki layer caps
 * to maxWikiEntries during render, so a generous default cap here is safe.
 */
export async function getAllRecords(
  redis: RedisClient,
  page: string,
  maxEntries: number = DEFAULT_RECORD_FETCH_CAP,
): Promise<ModActionRecord[]> {
  return getRecentActions(redis, page, maxEntries);
}

/** Read the lightweight status snapshot used by the "Show status" menu item. */
export async function getStatus(
  redis: RedisClient,
): Promise<{ lastRunAtMs?: number; lastPublishedAtMs?: number; entryCount: number }> {
  const status = await redis.hGetAll(STATUS_KEY);
  const lastRun = status?.[STATUS_FIELD_LAST_RUN];
  const lastPub = status?.[STATUS_FIELD_LAST_PUBLISHED];
  // entryCount is best-effort: count of fields in the actions hash.
  const actions = await redis.hGetAll(actionsKey(DEFAULT_STATUS_PAGE));
  const entryCount = actions ? Object.keys(actions).length : 0;
  return {
    lastRunAtMs: lastRun ? Number(lastRun) : undefined,
    lastPublishedAtMs: lastPub ? Number(lastPub) : undefined,
    entryCount,
  };
}

/** Stamp the last-run-started time (epoch ms). */
export async function recordRunStarted(redis: RedisClient, epochMs: number): Promise<void> {
  await redis.hSet(STATUS_KEY, { [STATUS_FIELD_LAST_RUN]: String(epochMs) });
}

/** Stamp the last-published time (epoch ms). */
export async function recordPublished(redis: RedisClient, epochMs: number): Promise<void> {
  await redis.hSet(STATUS_KEY, { [STATUS_FIELD_LAST_PUBLISHED]: String(epochMs) });
}
