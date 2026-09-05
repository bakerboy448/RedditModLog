/**
 * menu.ts — Moderator-only menu actions for RedditModLog (Devvit).
 *
 * Provides two subreddit menu items, both restricted to moderators
 * (`forUserType: 'moderator'`):
 *
 *   1. "Mod Log: Publish now"  — manually triggers the full ingest +
 *      wiki-publish pipeline (the same path the scheduler runs), then
 *      reports the result via a toast.
 *
 *   2. "Mod Log: Show status"  — a tiny read-only status view (last run
 *      time, last published time, stored entry count) read straight from
 *      Redis storage. Rendered as a toast to keep the UI minimal, per the
 *      architecture spec ("Keep UI minimal (blocks or a toast)").
 *
 * This module owns NO business logic of its own — it is a thin adapter that
 * wires Devvit menu events to the shared pipeline modules:
 *   settings.loadConfig() -> modlog.ingest() -> wiki.publishFromStore()
 * and to the storage status accessors. That keeps the manual "Publish now"
 * action behaviourally identical to the scheduled job (single code path,
 * per the architecture's design note for `publishFromStore`).
 *
 * Dependency direction (downward only, no cycles):
 *   menu -> { settings, modlog, wiki, storage }
 *
 * Registration: import this file from `main.ts`; calling `registerMenuItems()`
 * (or simply importing for its side effects) installs the menu items via
 * `Devvit.addMenuItem`. We expose an explicit `registerMenuItems()` function
 * rather than registering at import time so `main.ts` controls ordering
 * (e.g. after `Devvit.configure`).
 */

import { Devvit } from '@devvit/public-api';
import type { Context, MenuItemOnPressEvent } from '@devvit/public-api';

import { loadConfig } from './settings.js';
import { ingest } from './modlog.js';
import { publishFromStore } from './wiki.js';
import {
  getStatus,
  recordRunStarted,
  recordPublished,
} from './storage.js';

/**
 * Shape of the lightweight status snapshot read from Redis.
 *
 * Implemented by `storage.getStatus()`. Fields are optional because a
 * freshly-installed app has never run or published yet.
 *
 * NOTE (storage contract): the §1.2 storage spec enumerates dedup / record /
 * time-index / wiki-hash / dirty / schema keys. The "last run" and "last
 * published" status fields are status metadata that `getStatus` is expected to
 * surface. If `storage.ts` names these accessors differently, only the three
 * imports above need to change — the handlers below are otherwise decoupled.
 */
interface AppStatus {
  /** Epoch ms of the last time the ingest+publish pipeline started. */
  lastRunAtMs?: number;
  /** Epoch ms of the last time the wiki page was actually written. */
  lastPublishedAtMs?: number;
  /** Number of mod-action records currently retained in storage. */
  entryCount: number;
}

// --------------------------------------------------------------------------
// Formatting helpers (pure)
// --------------------------------------------------------------------------

/**
 * Render an epoch-ms timestamp as a compact, human-readable UTC string, or a
 * placeholder when the event has never occurred. Toast text is short-lived
 * and space-constrained, so we keep this terse.
 */
function formatTimestamp(epochMs?: number): string {
  if (!epochMs || !Number.isFinite(epochMs)) {
    return 'never';
  }
  // ISO 8601 trimmed to minute precision in UTC, e.g. "2026-06-23 14:05Z".
  return new Date(epochMs).toISOString().replace('T', ' ').slice(0, 16) + 'Z';
}

/**
 * Build the single-line status string shown in the "Show status" toast.
 * Kept as a pure function so it is trivially unit-testable.
 */
function formatStatusText(status: AppStatus): string {
  const entries = `${status.entryCount} entr${status.entryCount === 1 ? 'y' : 'ies'}`;
  return (
    `Mod Log status — ${entries} stored. ` +
    `Last run: ${formatTimestamp(status.lastRunAtMs)}. ` +
    `Last published: ${formatTimestamp(status.lastPublishedAtMs)}.`
  );
}

// --------------------------------------------------------------------------
// Handlers
// --------------------------------------------------------------------------

/**
 * Handle the "Publish now" menu action.
 *
 * Runs the same pipeline as the scheduled job:
 *   1. Resolve config from app/install settings.
 *   2. Ingest new mod-log actions into storage (deduped, anonymized).
 *   3. Render the store to markdown and publish to the wiki, honouring the
 *      SHA-256 hash-skip (INV-6) so an unchanged page is not rewritten.
 *
 * All failures are caught and surfaced to the moderator as a neutral toast;
 * a serverless menu invocation must never throw unhandled (P-29). Detailed
 * context is logged server-side via `console.error` (P-30, coding-style:
 * "Log detailed error context on the server side").
 */
async function handlePublishNow(
  _event: MenuItemOnPressEvent,
  context: Context,
): Promise<void> {
  const { reddit, redis, settings, subredditName, ui } = context;

  // INV-9: the subreddit comes from the install context, never a setting.
  if (!subredditName) {
    console.error('[menu] Publish now: no subredditName in context; skipping.');
    ui.showToast({
      text: 'Mod Log: could not determine subreddit — see app logs.',
      appearance: 'neutral',
    });
    return;
  }

  try {
    // Mark the run start so "Show status" reflects manual runs too.
    await recordRunStarted(redis, Date.now());

    const cfg = await loadConfig(settings, subredditName);

    // Ingest is idempotent by ModAction.id (INV-5), so a manual run that
    // overlaps the scheduler cannot double-insert records. The Reddit client
    // comes from context (classic model has no `reddit` singleton).
    const { added } = await ingest(reddit, redis, cfg);

    // Single shared publish path: render store -> hash-skip -> create/update.
    const { wrote, reason } = await publishFromStore(reddit, redis, cfg);

    if (wrote) {
      // Stamp the published time only when the wiki was actually written.
      await recordPublished(redis, Date.now());
    }

    const summary = wrote
      ? `Published (${reason}). ${added} new action${added === 1 ? '' : 's'} ingested.`
      : `Wiki unchanged (${reason}). ${added} new action${added === 1 ? '' : 's'} ingested.`;

    ui.showToast({ text: `Mod Log: ${summary}`, appearance: 'success' });
  } catch (err) {
    console.error('[menu] Publish now failed:', err);
    ui.showToast({
      text: 'Mod Log: publish failed — see app logs for details.',
      appearance: 'neutral',
    });
  }
}

/**
 * Handle the "Show status" menu action.
 *
 * Read-only: pulls the status snapshot from Redis and renders it as a toast.
 * Performs no Reddit calls and no writes, so it is safe to invoke freely.
 */
async function handleShowStatus(
  _event: MenuItemOnPressEvent,
  context: Context,
): Promise<void> {
  const { redis, ui } = context;

  try {
    const status = (await getStatus(redis)) as AppStatus;
    ui.showToast({ text: formatStatusText(status), appearance: 'neutral' });
  } catch (err) {
    console.error('[menu] Show status failed:', err);
    ui.showToast({
      text: 'Mod Log: could not read status — see app logs for details.',
      appearance: 'neutral',
    });
  }
}

// --------------------------------------------------------------------------
// Registration
// --------------------------------------------------------------------------

/**
 * Register all moderator menu items. Call once from `main.ts`, after
 * `Devvit.configure(...)`.
 *
 * Both items are scoped to `location: 'subreddit'` (the subreddit "..." menu)
 * and `forUserType: 'moderator'` so only mods see/trigger them — this is the
 * Devvit-native authorization gate for menu actions (no manual mod-check
 * needed in the handler).
 */
export function registerMenuItems(): void {
  Devvit.addMenuItem({
    label: 'Mod Log: Publish now',
    description: 'Manually ingest the mod log and publish it to the wiki page.',
    location: 'subreddit',
    forUserType: 'moderator',
    onPress: handlePublishNow,
  });

  Devvit.addMenuItem({
    label: 'Mod Log: Show status',
    description: 'Show last run time, last published time, and stored entry count.',
    location: 'subreddit',
    forUserType: 'moderator',
    onPress: handleShowStatus,
  });
}

// Exported for unit testing of the pure formatting layer.
export { formatStatusText, formatTimestamp };
export type { AppStatus };
