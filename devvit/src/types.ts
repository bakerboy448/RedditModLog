/**
 * types.ts — Shared contracts and frozen constants (single source of truth).
 *
 * Every other module imports its cross-cutting types and constants from here so
 * the record/config shapes are declared exactly once (coding-style: no
 * duplication). This module has NO runtime behavior and NO `@devvit/*` imports,
 * which keeps the pure render layer free of platform dependencies.
 *
 * The invariants referenced below (INV-1..INV-9) are the binding rules carried
 * verbatim from the legacy Python publisher (`modlog_wiki_publisher.py`):
 *
 *   INV-1  Anonymize moderators ALWAYS (no toggle).
 *   INV-2  Never link user profiles (only post/comment permalinks).
 *   INV-3  512 KB wiki cap.
 *   INV-4  Email censor + pipe-escape on free text.
 *   INV-5  Dedup by ModAction.id.
 *   INV-6  Wiki hash-skip on unchanged content.
 *   INV-7  Default tracked action types.
 *   INV-8  Default ignored moderators ([AutoModerator]).
 *   INV-9  One install == one subreddit (Redis is install-scoped).
 */

// ---------------------------------------------------------------------------
// Action-type model
// ---------------------------------------------------------------------------

/**
 * A moderation-log action type.
 *
 * Typed as `string` rather than a closed union so it stays assignment-compatible
 * with Devvit's own `ModActionType` union (which `modlog.ts` reads off
 * `ModAction.type`) WITHOUT pulling a `@devvit/*` import into the pure layers.
 * The canonical tracked set is constrained at the edges by `VALID_MODLOG_ACTIONS`
 * + the settings validators, not by the type system.
 */
export type ModActionType = string;

/** P (post) | C (comment) | U (user) | A (any/other) display-prefix kind. */
export type DisplayKind = 'P' | 'C' | 'U' | 'A';

// ---------------------------------------------------------------------------
// Wiki byte cap (INV-3)
// ---------------------------------------------------------------------------

/** Hard wiki content cap in UTF-8 bytes (Reddit limit). */
export const WIKI_BYTE_CAP = 524_288;

/** Trim target: day-blocks are dropped until content fits in <= 90% of the cap. */
export const WIKI_TRIM_TARGET = Math.floor(WIKI_BYTE_CAP * 0.9);

// ---------------------------------------------------------------------------
// Anonymization (INV-1)
// ---------------------------------------------------------------------------

/**
 * Anonymization is mandatory and not user-configurable (the Python app refused
 * to start when it was false). Exposed read-only on AppConfig for clarity.
 */
export const ANONYMIZE_MODERATORS = true as const;

/** Single shared label every human moderator collapses to (INV-1). */
export const ANON_LABEL = 'HumanModerator';

/**
 * Actors kept literal (NOT anonymized): platform automation. Everything else is
 * a human moderator and collapses to ANON_LABEL.
 */
export const LITERAL_MODS: ReadonlySet<string> = new Set(['AutoModerator', 'Reddit']);

// ---------------------------------------------------------------------------
// Tracked actions (INV-7) + valid set
// ---------------------------------------------------------------------------

/**
 * The full set of moderation-log action types the app understands. The settings
 * multiSelect and validators are derived from this list so the form and the
 * runtime filter never drift. Every value here exists in Devvit's ModActionType
 * union (verified against reddit/devvit ModAction.ts).
 */
export const VALID_MODLOG_ACTIONS: readonly ModActionType[] = Object.freeze([
  'removelink',
  'removecomment',
  'spamlink',
  'spamcomment',
  'addremovalreason',
  'approvelink',
  'approvecomment',
]);

/** Default tracked action types (INV-7) — the 7 removal/spam/approval/reason actions. */
export const DEFAULT_WIKI_ACTIONS: readonly ModActionType[] = Object.freeze([
  'removelink',
  'removecomment',
  'spamlink',
  'spamcomment',
  'addremovalreason',
  'approvelink',
  'approvecomment',
]);

// ---------------------------------------------------------------------------
// Ignored moderators (INV-8)
// ---------------------------------------------------------------------------

/** Always-ignored moderators (INV-8). AutoModerator is non-removable. */
export const DEFAULT_IGNORED_MODS: readonly string[] = Object.freeze(['AutoModerator']);

// ---------------------------------------------------------------------------
// Numeric settings: defaults + clamp bands
// ---------------------------------------------------------------------------

/** Default wiki page slug to publish to (Python default: "modlog"). */
export const DEFAULT_WIKI_PAGE = 'modlog';

/** Retention window in days (Python default 90; band 1..365). */
export const DEFAULT_RETENTION_DAYS = 90;
export const RETENTION_DAYS_MIN = 1;
export const RETENTION_DAYS_MAX = 365;

/** Max entries rendered per wiki page (band 100..2000). */
export const DEFAULT_MAX_WIKI_ENTRIES = 1000;
export const MAX_WIKI_ENTRIES_MIN = 100;
export const MAX_WIKI_ENTRIES_MAX = 2000;

/** Mod-log entries fetched per run (band 50..1000). */
export const DEFAULT_FETCH_LIMIT = 500;
export const FETCH_LIMIT_MIN = 50;
export const FETCH_LIMIT_MAX = 1000;

/** Storage schema version marker (forward-migration hook). */
export const SCHEMA_VERSION = 1;

// ---------------------------------------------------------------------------
// Record + config shapes
// ---------------------------------------------------------------------------

/**
 * One moderation-log action in persisted, render-ready form. Enough is stored to
 * rebuild the entire wiki page from Redis alone (no re-fetch required).
 *
 * INVARIANTS baked into this shape:
 *   - `moderator` is ALREADY anonymized at ingest (INV-1) — a real moderator
 *     name must NEVER reach Redis or the render layer.
 *   - `permalink` is present ONLY for posts/comments (INV-2); user/subreddit
 *     targets carry `undefined`, so render can never emit a profile link.
 *   - `reason` is stored RAW; censor + pipe-escape (INV-4) are applied at
 *     render time so the censor regex can evolve without a data migration.
 */
export interface ModRecord {
  /** ModAction.id — the dedup key (INV-5); also the hash field + zset member. */
  id: string;
  /** Epoch SECONDS of the action's creation; the sorted-set score (retention). */
  createdAtSec: number;
  /** Tracked action type (e.g. 'removelink'); already filtered to INV-7. */
  actionType: ModActionType;
  /** ALREADY-anonymized moderator label (INV-1) — never a real name. */
  moderator: string;
  /** Target fullname (t3_/t1_/t2_/t5_...), if any. */
  targetId?: string;
  /** P (post) | C (comment) | U (user) | A (any/other). */
  displayKind: DisplayKind;
  /** Short, prefixed display id (e.g. 'Pabc123'), if a target exists. */
  displayId?: string;
  /** Post/comment permalink ONLY (INV-2); undefined for profiles/subreddits. */
  permalink?: string;
  /** Target author handle, for the modmail removal-inquiry prefill. */
  targetAuthor?: string;
  /** RAW reason text; sanitized at render (INV-4). */
  reason?: string;
}

/**
 * Resolved, validated application configuration for one install (INV-9).
 * Produced by `settings.loadConfig`; consumed (read-only) everywhere else.
 */
export interface AppConfig {
  /** Subreddit name from the install context (INV-9) — never a user setting. */
  subredditName: string;
  /** Wiki page slug to publish to. */
  wikiPage: string;
  /** Tracked action types (INV-7), validated against VALID_MODLOG_ACTIONS. */
  wikiActions: ModActionType[];
  /** Ignored moderators (INV-8 default unioned in), lowercased + deduped. */
  ignoredModerators: string[];
  /** Retention window in days, clamped to [RETENTION_DAYS_MIN, MAX]. */
  retentionDays: number;
  /** Max entries rendered, clamped to [MAX_WIKI_ENTRIES_MIN, MAX]. */
  maxWikiEntries: number;
  /** Mod-log fetch limit per run, clamped to [FETCH_LIMIT_MIN, MAX]. */
  fetchLimit: number;
  /** Always true (INV-1); surfaced read-only, never derived from a setting. */
  anonymizeModerators: boolean;
}
