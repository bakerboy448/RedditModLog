/**
 * wiki.ts — Wiki publish orchestration.
 *
 * Responsibility (per architecture §1.5):
 *   The create-or-update + hash-skip publish flow that writes the rendered
 *   moderation-log markdown to a subreddit wiki page.
 *
 * Invariants owned here:
 *   - INV-3  512 KB wiki cap — content arriving here is ALREADY capped by
 *            render.enforceByteCap; we defensively re-check the byte length and
 *            refuse to write over-cap content (fail closed rather than let
 *            Reddit reject a >524288-byte body).
 *   - INV-6  Wiki hash-skip — never write the wiki when the SHA-256 of the
 *            rendered content equals the last written hash. Bypassable via
 *            `opts.bypassHash` for the menu "force wiki" action.
 *
 * This is the single publish code path shared by the scheduler cron, the
 * trigger-coalesced refresh, and the menu force-write handlers.
 *
 * Dependency direction (strictly downward): wiki -> { storage, render } and
 * the Reddit client. No cycles; render is pure, storage is the only Redis
 * module.
 *
 * NOTE ON IMPORTS: per the build instruction this module imports from
 * '@devvit/public-api' (the classic single-package entrypoint). The
 * architecture spec references the newer split packages
 * ('@devvit/reddit' / '@devvit/redis'); if this project is built on Devvit Web
 * the imports below should be swapped accordingly — see the TODOs.
 */

// TODO(devvit-imports): On Devvit Web this becomes
//   import type { RedditAPIClient } from '@devvit/reddit';
//   import type { RedisClient } from '@devvit/redis';
// Using '@devvit/public-api' here per the build instruction for this file.
import type { RedditAPIClient, RedisClient } from '@devvit/public-api';

import type { AppConfig } from './types.js';
import { WIKI_BYTE_CAP } from './types.js';
import { getAllRecords, getWikiHash, setWikiHash } from './storage.js';
import { buildContent, contentHash } from './render.js';

/** Reason string attached to wiki revisions so the audit trail is legible. */
const WIKI_REVISION_REASON = 'RedditModLog: moderation log update';

/** Outcome of a publish attempt — surfaced to logs and menu toasts. */
export interface PublishResult {
  /** True if a wiki write (create or update) actually happened. */
  wrote: boolean;
  /**
   * Why we did / did not write:
   *   'created'    — page did not exist, was created
   *   'updated'    — page existed and content changed
   *   'unchanged'  — hash matched (INV-6) OR existing content already equal
   *   'over-cap'   — content exceeded the 512 KB cap; refused to write
   */
  reason: 'created' | 'updated' | 'unchanged' | 'over-cap';
}

/**
 * UTF-8 byte length of a string. The wiki cap (INV-3) is defined in BYTES, not
 * code units, so we must measure the encoded length — multi-byte characters
 * (emoji, non-Latin reasons) count for more than one byte.
 */
function utf8ByteLength(value: string): number {
  // TextEncoder is available in the Devvit serverless runtime (Web Crypto /
  // standard globals). Avoids pulling in Node's Buffer.
  return new TextEncoder().encode(value).length;
}

/**
 * Publish pre-rendered markdown to the wiki page with hash-skip + cap guard.
 *
 * Flow (architecture §1.5):
 *   1. Defensive INV-3 byte-cap check — refuse over-cap content.
 *   2. Compute SHA-256 of content (INV-6).
 *   3. Unless bypassHash: compare to stored hash; skip write if equal.
 *   4. Read the page; create if absent, update if present-and-different,
 *      skip if present-and-equal (defensive double-check of INV-6).
 *   5. Persist the new hash so the next run can short-circuit.
 *
 * Idempotent: calling twice with identical content writes at most once.
 *
 * @param reddit  Reddit API client (provides get/create/updateWikiPage).
 * @param redis   Redis handle (hash cache only — via storage layer).
 * @param cfg     Resolved app config (subreddit + wiki page name).
 * @param content Fully rendered, already-capped markdown (from render.buildContent).
 * @param opts.bypassHash  Skip the INV-6 hash short-circuit (menu force-write).
 */
export async function publish(
  reddit: RedditAPIClient,
  redis: RedisClient,
  cfg: AppConfig,
  content: string,
  opts?: { bypassHash?: boolean },
): Promise<PublishResult> {
  const bypassHash = opts?.bypassHash ?? false;

  // --- Step 1: INV-3 defensive byte-cap guard --------------------------------
  // render.enforceByteCap should already keep us under the cap; if something
  // upstream produced over-cap content we fail closed rather than send a body
  // Reddit will reject. This is a guard, not the primary trim mechanism.
  const byteLength = utf8ByteLength(content);
  if (byteLength > WIKI_BYTE_CAP) {
    console.error(
      `[wiki] refusing to publish: content is ${byteLength} bytes, exceeds cap ${WIKI_BYTE_CAP} ` +
        `(page="${cfg.wikiPage}", sub="${cfg.subredditName}"). render.enforceByteCap should have trimmed this.`,
    );
    return { wrote: false, reason: 'over-cap' };
  }

  // --- Step 2: hash of the content we intend to write (INV-6) ----------------
  const hash = await contentHash(content);

  // --- Step 3: hash-skip short-circuit (INV-6) -------------------------------
  if (!bypassHash) {
    const prevHash = await getWikiHash(redis, cfg.wikiPage);
    if (prevHash !== undefined && prevHash === hash) {
      console.info(`[wiki] skip write: content hash unchanged (page="${cfg.wikiPage}").`);
      return { wrote: false, reason: 'unchanged' };
    }
  }

  // --- Step 4: read-or-create, then update if changed ------------------------
  // getWikiPage throws when the page does not exist (research delta #6), so the
  // "does it exist?" probe is a try/catch around the read, not a return code.
  let existingContent: string | undefined;
  try {
    const page = await reddit.getWikiPage(cfg.subredditName, cfg.wikiPage);
    existingContent = page.content;
  } catch (err) {
    // Treat any read failure as "page absent" and attempt creation below.
    // If creation also fails for a non-absence reason, that error propagates.
    existingContent = undefined;
    console.info(
      `[wiki] getWikiPage("${cfg.subredditName}", "${cfg.wikiPage}") failed/absent; will create. ` +
        `(${err instanceof Error ? err.message : String(err)})`,
    );
  }

  if (existingContent === undefined) {
    // Page does not exist — create it.
    // TODO(devvit-api): confirm createWikiPage options shape
    //   { subredditName, page, content, reason } — verified against
    //   CreateWikiPageOptions in devvit-docs (RedditAPIClient.createWikiPage).
    await reddit.createWikiPage({
      subredditName: cfg.subredditName,
      page: cfg.wikiPage,
      content,
      reason: WIKI_REVISION_REASON,
    });
    await setWikiHash(redis, cfg.wikiPage, hash);
    console.info(`[wiki] created page "${cfg.wikiPage}" (${byteLength} bytes).`);
    return { wrote: true, reason: 'created' };
  }

  if (existingContent === content) {
    // Defensive INV-6 double-check: page already byte-for-byte equal. This can
    // happen if the stored hash was lost/reset but the wiki itself is current.
    // Re-persist the hash so the next run short-circuits via step 3.
    await setWikiHash(redis, cfg.wikiPage, hash);
    console.info(`[wiki] skip write: existing page content already equal (page="${cfg.wikiPage}").`);
    return { wrote: false, reason: 'unchanged' };
  }

  // Page exists and differs — update it.
  // TODO(devvit-api): confirm updateWikiPage options shape
  //   { subredditName, page, content, reason } — verified against
  //   UpdateWikiPageOptions in devvit-docs (RedditAPIClient.updateWikiPage).
  await reddit.updateWikiPage({
    subredditName: cfg.subredditName,
    page: cfg.wikiPage,
    content,
    reason: WIKI_REVISION_REASON,
  });
  await setWikiHash(redis, cfg.wikiPage, hash);
  console.info(`[wiki] updated page "${cfg.wikiPage}" (${byteLength} bytes).`);
  return { wrote: true, reason: 'updated' };
}

/**
 * Convenience publish path: read all stored records, render them, and publish.
 *
 * This is the single entrypoint invoked by:
 *   - the scheduler cron job,
 *   - the trigger-coalesced refresh (dirty-flag consumer),
 *   - the menu "run now" / "force rebuild" / "force wiki" handlers.
 *
 * Keeping the render+publish composition here means callers never assemble
 * content themselves, so INV-3/INV-4/INV-6 are enforced uniformly.
 *
 * @param reddit Reddit API client.
 * @param redis  Redis handle.
 * @param cfg    Resolved app config.
 * @param opts.bypassHash  Forwarded to publish() (menu force-write).
 */
export async function publishFromStore(
  reddit: RedditAPIClient,
  redis: RedisClient,
  cfg: AppConfig,
  opts?: { bypassHash?: boolean },
): Promise<PublishResult> {
  // Newest-first records, already capped to maxWikiEntries by the storage layer.
  const records = await getAllRecords(redis, cfg.wikiPage);

  // render.buildContent is pure and owns the table layout, censor/escape
  // (INV-4), link gating (INV-2), and the byte-cap trim (INV-3).
  const nowIso = new Date().toISOString();
  const content = buildContent(records, cfg, nowIso);

  return publish(reddit, redis, cfg, content, opts);
}
