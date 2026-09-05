/**
 * settings.ts — Devvit settings schema + typed config resolver.
 *
 * Maps the 19 Python config options (CLI / env / JSON in the legacy
 * `modlog_wiki_publisher.py`) onto Devvit install-scope (per-subreddit) and
 * app-scope settings, then resolves them into a single frozen `AppConfig`.
 *
 * Binding invariants carried verbatim from the Python app:
 *   - INV-1  anonymize_moderators is ALWAYS true. It is NOT a user-facing
 *            setting here (the Python app refused to start when false). It is
 *            hardcoded below and surfaced read-only on AppConfig.
 *   - INV-7  default tracked actions = the 7 removal/approval/reason actions.
 *   - INV-8  default ignored moderators = [AutoModerator].
 *   - INV-9  one install == one subreddit. The subreddit name comes from the
 *            install context, never from a user setting, so cross-subreddit
 *            mixing is structurally impossible.
 *
 * Settings API shape verified against reddit/devvit reference apps
 * (devvit-sandbox/modlog-archive, devvit-docs app-configurations.md):
 *   - Devvit.addSettings([ ...fields ])  with field.type in
 *     'string' | 'boolean' | 'number' | 'select' | 'paragraph' | 'group'
 *   - per-field `scope: SettingScope.Installation | SettingScope.App`
 *   - per-field `onValidate: async ({ value }) => string | void`
 *     (return a string to REJECT at save time, void/undefined to accept)
 *   - read at runtime via `context.settings.get<T>(name)` /
 *     `settings.get<T>(name)`.
 *
 * @module settings
 */

import { Devvit, SettingScope } from '@devvit/public-api';

import {
  ANONYMIZE_MODERATORS,
  DEFAULT_FETCH_LIMIT,
  DEFAULT_IGNORED_MODS,
  DEFAULT_MAX_WIKI_ENTRIES,
  DEFAULT_RETENTION_DAYS,
  DEFAULT_WIKI_ACTIONS,
  DEFAULT_WIKI_PAGE,
  FETCH_LIMIT_MAX,
  FETCH_LIMIT_MIN,
  MAX_WIKI_ENTRIES_MAX,
  MAX_WIKI_ENTRIES_MIN,
  RETENTION_DAYS_MAX,
  RETENTION_DAYS_MIN,
  VALID_MODLOG_ACTIONS,
  type AppConfig,
  type ModActionType,
} from './types.js';

// ---------------------------------------------------------------------------
// Setting keys (single source of truth — used by both the schema and loader)
// ---------------------------------------------------------------------------

/**
 * Stable setting names. Changing a value here is a data migration (Devvit keys
 * the stored value by name), so treat these as frozen identifiers.
 */
export const SETTING_KEYS = {
  /** Wiki page slug to publish to. (Python: `wiki_page`, env WIKI_PAGE) */
  wikiPage: 'wikiPage',
  /** multiSelect of tracked modlog action types. (Python: `wiki_actions`) */
  wikiActions: 'wikiActions',
  /** Comma-separated extra mods to ignore. (Python: `ignored_moderators`) */
  ignoredModerators: 'ignoredModerators',
  /** Retention window in days. (Python: `retention_days`, 1..365) */
  retentionDays: 'retentionDays',
  /** Max entries rendered per wiki page. (Python: `max_wiki_entries_per_page`) */
  maxWikiEntries: 'maxWikiEntries',
  /** Explicit modlog fetch limit per run. (Python: `batch_size`) */
  fetchLimit: 'fetchLimit',
} as const;

// ---------------------------------------------------------------------------
// Mapping of the 19 legacy Python options
// ---------------------------------------------------------------------------
//
// Legacy option                  -> Devvit disposition
// --------------------------------------------------------------------------
//  1 reddit.client_id            -> DROP (Devvit owns OAuth; no app credential)
//  2 reddit.client_secret        -> DROP (same)
//  3 reddit.username             -> DROP (runs as the app account)
//  4 reddit.password             -> DROP (no password grant on Devvit)
//  5 source_subreddit            -> DROP as a setting; from install context (INV-9)
//  6 wiki_page                   -> Installation setting `wikiPage`
//  7 retention_days              -> Installation setting `retentionDays` (clamped)
//  8 batch_size                  -> Installation setting `fetchLimit` (clamped)
//  9 update_interval             -> DROP (Scheduler cron in devvit.json, not a setting)
// 10 max_wiki_entries_per_page   -> Installation setting `maxWikiEntries` (clamped)
// 11 wiki_display_days           -> DROP (collapsed into retentionDays; legacy
//                                   constraint was display_days <= retention_days,
//                                   so a single window is sufficient)
// 12 max_continuous_errors       -> DROP (no daemon loop; serverless per-invoke)
// 13 rate_limit_buffer           -> DROP (platform-managed rate limiting)
// 14 max_batch_retries           -> DROP (Scheduler provides retry semantics)
// 15 archive_threshold_days      -> DROP (no SQLite archive table; Redis TTL/ZSET)
// 16 anonymize_moderators        -> HARDCODED true (INV-1); NOT a setting
// 17 ignored_moderators          -> Installation setting `ignoredModerators`
// 18 wiki_actions                -> Installation setting `wikiActions` (multiSelect)
// 19 database_path / display_format -> DROP (Redis storage; fixed render format)
//
// Net: 6 of 19 surface as Devvit settings; 1 is hardcoded; 12 are obsolete on
// the Devvit platform. All preserved options keep their Python defaults/ranges.

// ---------------------------------------------------------------------------
// Settings schema
// ---------------------------------------------------------------------------

/**
 * Build the multiSelect option list for tracked action types from the canonical
 * VALID_MODLOG_ACTIONS list so the form and validation never drift.
 */
const WIKI_ACTION_OPTIONS = VALID_MODLOG_ACTIONS.map((action) => ({
  label: action,
  value: action,
}));

/**
 * Register the app's configuration form. Moderators edit these on the per-install
 * Settings page; all are `SettingScope.Installation` so each subreddit is
 * configured independently (reinforces INV-9). There is intentionally NO
 * anonymize-moderators toggle (INV-1) and NO Reddit-credential fields (Devvit
 * owns auth).
 *
 * Call once at module load from `main.ts`.
 */
export function registerSettings(): void {
  Devvit.addSettings([
    {
      type: 'string',
      name: SETTING_KEYS.wikiPage,
      label: 'Wiki page name to publish the mod log to (e.g. "modlog")',
      scope: SettingScope.Installation,
      defaultValue: DEFAULT_WIKI_PAGE,
      onValidate: ({ value }) => validateWikiPage(value),
    },
    {
      type: 'select',
      name: SETTING_KEYS.wikiActions,
      label: 'Mod-log action types to publish',
      scope: SettingScope.Installation,
      multiSelect: true,
      options: WIKI_ACTION_OPTIONS,
      // Devvit `select` defaults are an array of option values.
      defaultValue: [...DEFAULT_WIKI_ACTIONS],
      onValidate: ({ value }) => validateWikiActions(value),
    },
    {
      type: 'string',
      name: SETTING_KEYS.ignoredModerators,
      label:
        'Additional moderators to exclude (comma-separated usernames). ' +
        'AutoModerator is always excluded.',
      scope: SettingScope.Installation,
      // Stored as a CSV string; AutoModerator is merged in by the loader.
      defaultValue: '',
    },
    {
      type: 'number',
      name: SETTING_KEYS.retentionDays,
      label: `Days of history to keep (${RETENTION_DAYS_MIN}-${RETENTION_DAYS_MAX})`,
      scope: SettingScope.Installation,
      defaultValue: DEFAULT_RETENTION_DAYS,
      onValidate: ({ value }) =>
        validateNumberRange(value, RETENTION_DAYS_MIN, RETENTION_DAYS_MAX, 'Retention days'),
    },
    {
      type: 'number',
      name: SETTING_KEYS.maxWikiEntries,
      label: `Maximum entries to render on the wiki page (${MAX_WIKI_ENTRIES_MIN}-${MAX_WIKI_ENTRIES_MAX})`,
      scope: SettingScope.Installation,
      defaultValue: DEFAULT_MAX_WIKI_ENTRIES,
      onValidate: ({ value }) =>
        validateNumberRange(value, MAX_WIKI_ENTRIES_MIN, MAX_WIKI_ENTRIES_MAX, 'Max wiki entries'),
    },
    {
      type: 'number',
      name: SETTING_KEYS.fetchLimit,
      label: `Mod-log entries to fetch per run (${FETCH_LIMIT_MIN}-${FETCH_LIMIT_MAX})`,
      scope: SettingScope.Installation,
      defaultValue: DEFAULT_FETCH_LIMIT,
      onValidate: ({ value }) =>
        validateNumberRange(value, FETCH_LIMIT_MIN, FETCH_LIMIT_MAX, 'Fetch limit'),
    },
  ]);
}

// ---------------------------------------------------------------------------
// Validation handlers (return a string to REJECT, void to accept)
// ---------------------------------------------------------------------------

/** Reddit wiki slugs: lowercase letters, digits, and `/_-` separators. */
const WIKI_PAGE_RE = /^[a-z0-9][a-z0-9/_-]*$/;

/**
 * Reject empty/malformed wiki page slugs. Returns an error string on failure,
 * void on success (Devvit `onValidate` contract).
 */
export function validateWikiPage(value: string | undefined): string | void {
  const slug = (value ?? '').trim();
  if (slug.length === 0) {
    return 'Wiki page name is required.';
  }
  if (slug.length > 256) {
    return 'Wiki page name is too long (max 256 characters).';
  }
  if (!WIKI_PAGE_RE.test(slug)) {
    return 'Use lowercase letters, numbers, and / _ - only (e.g. "modlog" or "logs/modlog").';
  }
}

/**
 * Reject unknown / empty action selections. The multiSelect already constrains
 * choices to VALID_MODLOG_ACTIONS, but we defend against an empty selection
 * (which would publish nothing) and any out-of-set value.
 */
export function validateWikiActions(value: string[] | undefined): string | void {
  const selected = value ?? [];
  if (selected.length === 0) {
    return 'Select at least one action type to publish.';
  }
  const unknown = selected.filter(
    (action) => !VALID_MODLOG_ACTIONS.includes(action as ModActionType),
  );
  if (unknown.length > 0) {
    return `Unknown action type(s): ${unknown.join(', ')}.`;
  }
}

/**
 * Inclusive numeric range validator shared by retention/maxEntries/fetchLimit.
 * Rejects non-finite, non-integer, and out-of-range values.
 */
export function validateNumberRange(
  value: number | undefined,
  min: number,
  max: number,
  label: string,
): string | void {
  if (value === undefined || value === null || !Number.isFinite(value)) {
    return `${label} is required and must be a number.`;
  }
  if (!Number.isInteger(value)) {
    return `${label} must be a whole number.`;
  }
  if (value < min || value > max) {
    return `${label} must be between ${min} and ${max}.`;
  }
}

// ---------------------------------------------------------------------------
// Config resolution
// ---------------------------------------------------------------------------

/**
 * Minimal structural shape of the Devvit settings reader. Both
 * `context.settings` and the destructured `settings` handler arg satisfy this,
 * so the loader is decoupled from how the caller obtained the handle.
 */
export interface SettingsReader {
  get<T>(name: string): Promise<T | undefined>;
}

/**
 * Clamp a value into [min, max]. Validation already rejects out-of-range input
 * at save time, but app-scope defaults or legacy stored values could still fall
 * outside the band — clamp defensively and log when we do (parity with the
 * Python `validate_config_value` warn-and-clamp behavior).
 */
function clamp(value: number, min: number, max: number, label: string): number {
  if (value < min) {
    console.warn(`[settings] ${label}=${value} below min ${min}; clamping to ${min}`);
    return min;
  }
  if (value > max) {
    console.warn(`[settings] ${label}=${value} above max ${max}; clamping to ${max}`);
    return max;
  }
  return value;
}

/**
 * Coerce a possibly-undefined number setting to a finite integer, falling back
 * to `fallback` when absent or invalid.
 */
function asInt(value: number | undefined, fallback: number): number {
  if (value === undefined || value === null || !Number.isFinite(value)) {
    return fallback;
  }
  return Math.trunc(value);
}

/**
 * Parse the comma-separated `ignoredModerators` CSV into a normalized list and
 * ALWAYS union in the default ignored mods (INV-8: AutoModerator is
 * non-removable). Usernames are lowercased for case-insensitive comparison at
 * the filter site, deduped, and stripped of an optional `u/` prefix.
 */
function parseIgnoredModerators(csv: string | undefined): string[] {
  const fromUser = (csv ?? '')
    .split(',')
    .map((name) => name.trim().replace(/^\/?u\//i, ''))
    .filter((name) => name.length > 0);

  const merged = [...DEFAULT_IGNORED_MODS, ...fromUser].map((name) => name.toLowerCase());
  return Array.from(new Set(merged));
}

/**
 * Validate the stored action selection against the canonical list, dropping any
 * unknown values (P-22). Falls back to the INV-7 defaults if nothing valid
 * remains, so the publisher never silently produces an empty page from a bad
 * stored value.
 */
function parseWikiActions(value: string[] | undefined): ModActionType[] {
  const selected = value ?? [];
  const valid = selected.filter((action): action is ModActionType =>
    VALID_MODLOG_ACTIONS.includes(action as ModActionType),
  );
  const dropped = selected.length - valid.length;
  if (dropped > 0) {
    console.warn(`[settings] dropped ${dropped} unknown wikiActions value(s)`);
  }
  return valid.length > 0 ? valid : [...DEFAULT_WIKI_ACTIONS];
}

/**
 * Resolve all settings into a single validated, clamped, frozen `AppConfig`.
 *
 * @param settings   A Devvit settings reader (`context.settings` or the
 *                   destructured handler `settings` arg).
 * @param subredditName  The install's subreddit, from context (INV-9) — never
 *                   a user setting.
 * @returns A deeply-frozen `AppConfig`. `anonymizeModerators` is always true
 *          (INV-1) and is not derived from any setting.
 */
export async function loadConfig(
  settings: SettingsReader,
  subredditName: string,
): Promise<AppConfig> {
  // Read every setting up front; unrelated reads have no dependencies.
  const [wikiPageRaw, wikiActionsRaw, ignoredRaw, retentionRaw, maxEntriesRaw, fetchLimitRaw] =
    await Promise.all([
      settings.get<string>(SETTING_KEYS.wikiPage),
      settings.get<string[]>(SETTING_KEYS.wikiActions),
      settings.get<string>(SETTING_KEYS.ignoredModerators),
      settings.get<number>(SETTING_KEYS.retentionDays),
      settings.get<number>(SETTING_KEYS.maxWikiEntries),
      settings.get<number>(SETTING_KEYS.fetchLimit),
    ]);

  const wikiPage = (wikiPageRaw ?? '').trim() || DEFAULT_WIKI_PAGE;

  const config: AppConfig = {
    subredditName,
    wikiPage,
    wikiActions: parseWikiActions(wikiActionsRaw),
    ignoredModerators: parseIgnoredModerators(ignoredRaw),
    retentionDays: clamp(
      asInt(retentionRaw, DEFAULT_RETENTION_DAYS),
      RETENTION_DAYS_MIN,
      RETENTION_DAYS_MAX,
      'retentionDays',
    ),
    maxWikiEntries: clamp(
      asInt(maxEntriesRaw, DEFAULT_MAX_WIKI_ENTRIES),
      MAX_WIKI_ENTRIES_MIN,
      MAX_WIKI_ENTRIES_MAX,
      'maxWikiEntries',
    ),
    fetchLimit: clamp(
      asInt(fetchLimitRaw, DEFAULT_FETCH_LIMIT),
      FETCH_LIMIT_MIN,
      FETCH_LIMIT_MAX,
      'fetchLimit',
    ),
    // INV-1: anonymization is mandatory and not user-configurable.
    anonymizeModerators: ANONYMIZE_MODERATORS,
  };

  // Immutability (coding-style rule): hand callers a frozen snapshot. The
  // nested arrays are frozen too so no consumer can mutate shared config state.
  Object.freeze(config.wikiActions);
  Object.freeze(config.ignoredModerators);
  return Object.freeze(config);
}
