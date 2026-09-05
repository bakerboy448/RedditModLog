/**
 * modlog.ts — Moderation-log ingestion.
 *
 * Turns raw `reddit.getModerationLog` output into deduped, anonymized,
 * render-ready `ModRecord`s and persists them via the storage layer.
 *
 * This module owns several binding invariants from the legacy Python app
 * (see devvit architecture spec / modlog_wiki_publisher.py — read-only ref):
 *
 *   INV-1  Anonymize moderators ALWAYS. Real names never leave this module;
 *          `ModRecord.moderator` always holds an anonymized label.
 *   INV-2  NEVER link user profiles. Only post (t3_) and comment (t1_)
 *          targets get a permalink; user (t2_) / subreddit (t5_) / other
 *          targets get `permalink: undefined`.
 *   INV-5  Dedup by ModAction.id — each action processed at most once.
 *   INV-7  Default tracked action types (client-side filter).
 *   INV-8  Default ignored moderators (filtered by moderatorName).
 *
 * Dependency direction (downward only): modlog -> { reddit, storage, types }.
 * `extractRecord`, `anonymizeMod`, and `deriveDisplay` are pure and exported
 * for unit testing.
 */

import type { ModAction, RedditAPIClient, RedisClient } from '@devvit/public-api';

import type { AppConfig, DisplayKind, ModRecord } from './types.js';
import { ANON_LABEL, LITERAL_MODS } from './types.js';
import * as storage from './storage.js';

/**
 * INV-1 — Map a raw moderator username to its render-safe label.
 *
 * `AutoModerator` and `Reddit` (the platform's own automated actor) are kept
 * literal so their actions remain attributable to automation; every human
 * moderator collapses to a single shared `HumanModerator` label so individual
 * mods can never be singled out from the published log.
 *
 * A missing/empty name is treated as the platform actor and labeled literally
 * as `Reddit` (matches legacy behavior where unattributed actions are system
 * actions, never a human).
 */
export function anonymizeMod(name: string | undefined | null): string {
  const raw = (name ?? '').trim();
  if (raw.length === 0) {
    return 'Reddit';
  }
  if (LITERAL_MODS.has(raw)) {
    return raw;
  }
  return ANON_LABEL;
}

/**
 * Derive the display kind + short display id from a target fullname.
 *
 * Reddit fullnames are prefixed by type:
 *   t1_ -> comment (C)   t3_ -> post (P)   t2_ -> user (U)   t5_ -> subreddit
 * Anything else (or a missing target) is a non-content "action" target (A).
 *
 * Ported from the legacy `generate_display_id`: post/comment ids are shortened
 * to the first 6 chars of the bare id when the bare id exceeds 8 chars; user
 * and action ids are passed through (with their prefix stripped) verbatim.
 *
 * Returns the *display* kind/id only — link gating (INV-2) is decided in
 * `extractRecord`, which has the full target context.
 */
export function deriveDisplay(targetId: string | undefined | null): {
  kind: DisplayKind;
  displayId?: string;
} {
  const fullname = (targetId ?? '').trim();
  if (fullname.length === 0) {
    return { kind: 'A' };
  }

  // Split the t#_ prefix from the bare id, if present.
  const match = /^t(\d)_(.+)$/.exec(fullname);
  const typeNum = match ? match[1] : undefined;
  const bareId = match ? match[2] : fullname;

  let kind: DisplayKind;
  switch (typeNum) {
    case '3':
      kind = 'P'; // post
      break;
    case '1':
      kind = 'C'; // comment
      break;
    case '2':
      kind = 'U'; // user
      break;
    default:
      kind = 'A'; // subreddit / award / unknown — treated as a generic action
      break;
  }

  // Shorten long content ids for display (post/comment only), matching the
  // legacy 8-char threshold -> 6-char truncation.
  let shortBare = bareId;
  if ((kind === 'P' || kind === 'C') && bareId.length > 8) {
    shortBare = bareId.slice(0, 6);
  }

  return { kind, displayId: `${kind}${shortBare}` };
}

/**
 * Extract the human-readable removal/mod-reason text from a ModAction.
 *
 * Priority mirrors the legacy publisher:
 *   1. `details`     — for `addremovalreason` the operator-typed reason lands
 *                      here; for most other actions this is the richest field.
 *   2. `description` — fallback summary string.
 *
 * The text is stored RAW. Email censoring and pipe-escaping (INV-4) are
 * applied later in the pure render layer so the censor regex can evolve
 * without a data migration.
 */
function extractReason(action: ModAction): string | undefined {
  const details = action.details?.trim();
  if (details) {
    return details;
  }
  const description = action.description?.trim();
  if (description) {
    return description;
  }
  return undefined;
}

/**
 * PURE mapper: ModAction -> ModRecord, or `null` if the action is filtered
 * out (untracked type per INV-7, or ignored moderator per INV-8).
 *
 * No I/O. Dedup (INV-5) and persistence happen in `ingest`.
 */
export function extractRecord(action: ModAction, cfg: AppConfig): ModRecord | null {
  // INV-7 — only configured action types are tracked.
  if (!cfg.wikiActions.includes(action.type)) {
    return null;
  }

  // INV-8 — drop actions performed by ignored moderators (e.g. AutoModerator).
  // Case-insensitive: Reddit usernames match case-insensitively, and the ignore
  // list (default + per-install) may carry mixed case. Comparing raw case would
  // silently leak ignored mods into the public wiki.
  const rawModerator = (action.moderatorName ?? '').trim().toLowerCase();
  if (rawModerator.length > 0 && cfg.ignoredModerators.some((m) => m.toLowerCase() === rawModerator)) {
    return null;
  }

  const targetId = action.target?.id;
  const { kind, displayId } = deriveDisplay(targetId);

  // INV-2 — only post/comment targets are ever linkable. A user (U) or
  // generic-action (A) target must never produce a profile/permalink.
  const permalink = kind === 'P' || kind === 'C' ? action.target?.permalink : undefined;

  const record: ModRecord = {
    id: action.id,
    createdAtSec: Math.floor(action.createdAt.getTime() / 1000),
    actionType: action.type,
    moderator: anonymizeMod(action.moderatorName), // INV-1 — anonymized at ingest
    targetId: targetId,
    displayKind: kind,
    displayId: displayId,
    permalink: permalink,
    targetAuthor: action.target?.author,
    reason: extractReason(action),
  };

  return record;
}

/**
 * Fetch raw moderation-log actions for the configured subreddit.
 *
 * Notes on the API shape (verified against @devvit/reddit ModAction model):
 *   - `type` is a SINGLE-valued filter on GetModerationLogOptions, so it
 *     cannot express the 7-type tracked set. We omit it and filter
 *     client-side in `extractRecord`.
 *   - `limit` must be passed explicitly; the listing default is unbounded
 *     (Infinity), which we never want. `pageSize` caps per-request fan-out.
 *
 * Returns the fully-resolved array (newest first, as Reddit returns it).
 *
 * In the classic `@devvit/public-api` model there is no `reddit` singleton; the
 * client is `context.reddit` (a `RedditAPIClient`) threaded in by the caller —
 * mirrors the pattern in `wiki.ts`.
 */
export async function fetchActions(
  reddit: RedditAPIClient,
  cfg: AppConfig,
): Promise<ModAction[]> {
  const listing = reddit.getModerationLog({
    subredditName: cfg.subredditName,
    limit: cfg.fetchLimit,
    pageSize: 100,
  });

  return listing.all();
}

/**
 * Orchestrate one ingestion pass — the single shared path used by both the
 * scheduler cron and the onModAction trigger.
 *
 *   fetch -> extract (filter/anonymize/normalize) -> dedup gate -> persist
 *
 * `markSeen` is an atomic NX set (INV-5): it returns `true` only the first
 * time a given action id is seen, so concurrent cron/trigger invocations can
 * never double-insert. Records that fail extraction (untracked type / ignored
 * mod) are still marked seen so we don't re-evaluate them every pass.
 *
 * Returns the number of NEW records persisted this pass. On a partial failure
 * mid-batch we surface the error to the caller (main.ts), which is responsible
 * for logging and letting the scheduler retry — there is no daemon loop here.
 */
export async function ingest(
  reddit: RedditAPIClient,
  redis: RedisClient,
  cfg: AppConfig,
): Promise<{ added: number; scanned: number }> {
  const actions = await fetchActions(reddit, cfg);

  let added = 0;
  for (const action of actions) {
    // INV-5 — atomic dedup. Skip anything we've already accounted for.
    const isNew = await storage.markSeen(redis, cfg.wikiPage, action.id, cfg.retentionDays);
    if (!isNew) {
      continue;
    }

    const record = extractRecord(action, cfg);
    if (record === null) {
      // Filtered out (untracked type or ignored mod). It is now marked seen,
      // so we won't reconsider it on the next pass.
      continue;
    }

    await storage.putRecord(redis, cfg.wikiPage, record);
    added += 1;
  }

  return { added, scanned: actions.length };
}
