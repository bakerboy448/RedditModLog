/**
 * main.ts — Devvit app entrypoint and wiring.
 *
 * This is the ONLY module that registers Devvit capabilities. It wires the
 * shared pipeline modules together; it contains no business logic of its own
 * beyond orchestration and per-invocation error handling (P-29: no daemon loop —
 * rely on the scheduler's retry semantics + bounded try/catch).
 *
 * Capabilities registered here:
 *   - Devvit.configure          — enable Reddit API + Redis.
 *   - settings.registerSettings — the per-install configuration form.
 *   - menu.registerMenuItems    — moderator-only "Publish now" / "Show status".
 *   - Devvit.addSchedulerJob    — the recurring ingest+publish+retention job.
 *   - Devvit.addTrigger         — AppInstall/AppUpgrade (schedule the cron) and
 *                                 ModAction (cheap incremental ingest).
 *
 * Pipeline (the single shared path, run by both the cron job and the menu):
 *   loadConfig -> ingest (fetch/filter/anonymize/dedup/persist)
 *              -> publishFromStore (render -> hash-skip -> create/update wiki)
 *              -> cleanupOld (retention prune)
 *
 * INVARIANTS are enforced in the modules this file calls, NOT here:
 *   INV-1 anonymize (modlog), INV-2 link-gating (modlog/render),
 *   INV-3 byte cap (render/wiki), INV-4 censor/escape (render),
 *   INV-5 dedup (storage/modlog), INV-6 hash-skip (wiki),
 *   INV-7/8 filters (modlog/settings), INV-9 per-install isolation (platform).
 *
 * Dependency direction (downward only): main -> { settings, menu, modlog, wiki,
 * storage }. No module imports main.
 */

import { Devvit } from '@devvit/public-api';
import type { ScheduledJobEvent, TriggerContext } from '@devvit/public-api';

import { registerSettings, loadConfig } from './settings.js';
import { registerMenuItems } from './menu.js';
import { ingest } from './modlog.js';
import { publishFromStore } from './wiki.js';
import {
  cleanupOld,
  recordRunStarted,
  recordPublished,
} from './storage.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Scheduler job name. Stable identifier — referenced by runJob/cancelJob. */
const PUBLISH_JOB_NAME = 'publish-modlog';

/**
 * Cron schedule for the recurring publish job. The legacy Python daemon polled
 * every 600s (10 min); the closest sane cron that respects Devvit's scheduler
 * cadence is every 10 minutes. Adjust here if the platform imposes a minimum
 * interval for a given app tier.
 */
const PUBLISH_CRON = '*/10 * * * *';

// ---------------------------------------------------------------------------
// Devvit configuration
// ---------------------------------------------------------------------------

Devvit.configure({
  redditAPI: true,
  redis: true,
});

// ---------------------------------------------------------------------------
// Core pipeline — shared by the scheduler job and (manually) the menu action.
// ---------------------------------------------------------------------------

/**
 * Run one full publish cycle for the install's subreddit.
 *
 * Resolves config, ingests new mod-log actions, publishes the rendered wiki
 * page (hash-skipped if unchanged), then prunes records past the retention
 * window. Returns a small summary for logging. Never throws — callers in a
 * serverless context must not propagate (the scheduler will retry on its own
 * cadence).
 */
async function runPublishCycle(context: TriggerContext): Promise<void> {
  const { reddit, redis, settings, subredditName } = context;

  if (!subredditName) {
    console.error('[main] runPublishCycle: no subredditName in context; skipping.');
    return;
  }

  try {
    await recordRunStarted(redis, Date.now());

    // 1. Resolve config (per-install settings + context subreddit, INV-9).
    const cfg = await loadConfig(settings, subredditName);

    // 2. Ingest new actions (fetch -> filter/anonymize -> dedup -> persist).
    const { added, scanned } = await ingest(reddit, redis, cfg);

    // 3. Render the store and publish (hash-skip honored unless content changed).
    const { wrote, reason } = await publishFromStore(reddit, redis, cfg);
    if (wrote) {
      await recordPublished(redis, Date.now());
    }

    // 4. Retention prune (INV-9 / time-based). nowSec injected for determinism.
    const nowSec = Math.floor(Date.now() / 1000);
    const removed = await cleanupOld(redis, cfg.wikiPage, cfg.retentionDays, nowSec);

    console.info(
      `[main] cycle complete sub="${subredditName}" page="${cfg.wikiPage}" ` +
        `scanned=${scanned} added=${added} wrote=${wrote} reason=${reason} pruned=${removed}`,
    );
  } catch (err) {
    // P-29/P-30: log rich context, swallow so the platform retries on schedule.
    console.error(`[main] runPublishCycle failed for sub="${subredditName}":`, err);
  }
}

// ---------------------------------------------------------------------------
// Scheduler job
// ---------------------------------------------------------------------------

Devvit.addSchedulerJob({
  name: PUBLISH_JOB_NAME,
  onRun: async (_event: ScheduledJobEvent<undefined>, context) => {
    await runPublishCycle(context);
  },
});

// ---------------------------------------------------------------------------
// Lifecycle triggers — (re)schedule the recurring job on install/upgrade.
// ---------------------------------------------------------------------------

/**
 * Cancel any pre-existing instances of the publish job, then schedule a fresh
 * cron instance. Running on both AppInstall and AppUpgrade keeps exactly one
 * scheduled job alive and lets a cron-schedule change take effect on upgrade.
 */
async function schedulePublishJob(context: TriggerContext): Promise<void> {
  const { scheduler } = context;
  try {
    // Remove stale instances so an upgrade doesn't stack duplicate jobs.
    const existing = await scheduler.listJobs();
    for (const job of existing) {
      if (job.name === PUBLISH_JOB_NAME) {
        await scheduler.cancelJob(job.id);
      }
    }

    await scheduler.runJob({ name: PUBLISH_JOB_NAME, cron: PUBLISH_CRON });
    console.info(`[main] scheduled "${PUBLISH_JOB_NAME}" cron="${PUBLISH_CRON}".`);
  } catch (err) {
    console.error('[main] failed to schedule publish job:', err);
  }
}

Devvit.addTrigger({
  events: ['AppInstall', 'AppUpgrade'],
  onEvent: async (_event, context) => {
    await schedulePublishJob(context);
  },
});

// ---------------------------------------------------------------------------
// ModAction trigger — cheap incremental ingest.
// ---------------------------------------------------------------------------
//
// On each moderation action we run an ingest pass so newly-removed content is
// captured promptly without waiting up to a full cron interval. We deliberately
// DO NOT publish from the trigger: publishing is coalesced into the next cron
// run to avoid a wiki write per individual mod action (rate + churn). Ingest is
// idempotent by ModAction.id (INV-5), so trigger + cron overlap is safe.

Devvit.addTrigger({
  event: 'ModAction',
  onEvent: async (_event, context) => {
    const { reddit, redis, settings, subredditName } = context;
    if (!subredditName) {
      return;
    }
    try {
      const cfg = await loadConfig(settings, subredditName);
      const { added } = await ingest(reddit, redis, cfg);
      if (added > 0) {
        console.info(`[main] ModAction trigger ingested ${added} new action(s) for r/${subredditName}.`);
      }
    } catch (err) {
      console.error(`[main] ModAction trigger failed for sub="${subredditName}":`, err);
    }
  },
});

// ---------------------------------------------------------------------------
// Settings + menu registration (side-effecting; order after configure).
// ---------------------------------------------------------------------------

registerSettings();
registerMenuItems();

export default Devvit;
