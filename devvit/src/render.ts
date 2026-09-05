/**
 * render.ts — PURE markdown renderer.
 *
 * Ported from the legacy Python `modlog_wiki_publisher.py`:
 *   - build_wiki_content   -> buildContent / enforceByteCap
 *   - format_modlog_entry  -> renderRow
 *   - format_content_link  -> contentLink
 *   - generate_modmail_link-> modmailLink
 *   - censor_email_addresses -> censorEmail
 *   - sanitize_for_markdown  -> escapePipes
 *   - get_content_hash       -> contentHash
 *
 * INVARIANTS enforced here:
 *   INV-2  Never link user profiles. Only records carrying a `permalink`
 *          (post/comment) emit a hyperlink. Profile records have no permalink.
 *   INV-3  512 KB wiki cap. Content is trimmed oldest-day-first to <= 90% of cap.
 *   INV-4  Email censor + pipe-escape applied to every cell of free text.
 *   INV-6  contentHash() exposes the SHA-256 the wiki layer uses to hash-skip.
 *
 * This module has ZERO Reddit/Redis I/O and ZERO `@devvit/*` runtime imports.
 * It is fully deterministic and unit-testable. The only external dependency is
 * the Web Crypto API (`crypto.subtle`), which is available in the Devvit
 * serverless runtime.
 *
 * Types are imported from the shared contract module so the renderer never
 * redeclares the record/config shapes (coding-style: no duplication).
 */

import {
  type AppConfig,
  type ModRecord,
  WIKI_BYTE_CAP,
  WIKI_TRIM_TARGET,
} from './types.js';

// ---------------------------------------------------------------------------
// Constants ported verbatim from the Python source.
// ---------------------------------------------------------------------------

/** Reddit base URL used to absolutize relative permalinks. */
const REDDIT_BASE_URL = 'https://www.reddit.com';

/**
 * Email-detection regex (INV-4). Ported from Python:
 *   r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
 * The literal `|` inside the final character class is preserved exactly as the
 * Python original wrote it (it matches a literal pipe as well as letters — a
 * quirk we keep for byte-for-byte output parity). Global flag so every address
 * in a multi-address reason is censored.
 */
const EMAIL_REGEX = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b/g;

/** Placeholder substituted for any detected email address (INV-4). */
const EMAIL_PLACEHOLDER = '[EMAIL]';

/** Empty-cell marker matching the Python "-" sentinel. */
const EMPTY_CELL = '-';

/** Max characters of a content title before it is ellipsized in modmail. */
const MAX_TITLE_LENGTH = 50;

/**
 * Human-readable removal-type labels keyed by action type. Ported from the
 * Python `type_map` in generate_modmail_link.
 */
const REMOVAL_TYPE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  removelink: 'Post',
  removepost: 'Post',
  removecomment: 'Comment',
  spamlink: 'Spam Post',
  spamcomment: 'Spam Comment',
  removecontent: 'Content',
  addremovalreason: 'Removal Reason',
});

/** Table header + separator rows. Column order ported verbatim. */
const TABLE_HEADER = '| Time | Action | ID | Moderator | Content | Reason | Inquire |';
const TABLE_SEPARATOR = '|------|--------|----|-----------|---------|--------|---------|';

/** GitHub-credit footer (P-31), ported verbatim. */
const FOOTER_LINES: readonly string[] = Object.freeze([
  '---',
  '',
  '*This modlog is automatically maintained by ' +
    '[RedditModLog](https://github.com/bakerboy448/RedditModLog) bot.*',
]);

// ---------------------------------------------------------------------------
// Pure string transforms (INV-4).
// ---------------------------------------------------------------------------

/**
 * Censor email addresses in free text (INV-4).
 * Ported from `censor_email_addresses`. Returns the input unchanged when empty.
 */
export function censorEmail(text: string | undefined | null): string {
  if (!text) {
    return text ?? '';
  }
  // `replace` does not mutate the input string (immutability).
  return text.replace(EMAIL_REGEX, EMAIL_PLACEHOLDER);
}

/**
 * Escape pipe characters so reason/title text cannot break a markdown table
 * cell (INV-4). Ported from `sanitize_for_markdown` — pipes become spaces.
 * `null`/`undefined` collapse to empty string.
 */
export function escapePipes(text: string | undefined | null): string {
  if (text === null || text === undefined) {
    return '';
  }
  return String(text).replace(/\|/g, ' ');
}

/** Apply both censor + pipe-escape, the full per-cell sanitization (INV-4). */
function sanitizeCell(text: string | undefined | null): string {
  return escapePipes(censorEmail(text ?? ''));
}

// ---------------------------------------------------------------------------
// Per-record rendering helpers.
// ---------------------------------------------------------------------------

/**
 * Render the "Content" column hyperlink for a record (INV-2).
 *
 * A hyperlink is emitted ONLY when the record carries a `permalink`. Per the
 * ingest layer (modlog.ts), `permalink` is populated solely for post (`t3_`)
 * and comment (`t1_`) targets — user/subreddit targets arrive with no
 * permalink, so this function can never produce a profile link. Relative
 * permalinks are absolutized against the Reddit base URL; already-absolute
 * permalinks are used as-is.
 *
 * The link text is the (sanitized) `displayId` when present, otherwise a
 * generic content label. When there is no permalink, the bare display id is
 * returned (no link), or the empty-cell marker if even that is absent.
 */
export function contentLink(rec: ModRecord): string {
  const label = rec.displayId ? sanitizeCell(rec.displayId) : sanitizeCell('content');

  if (!rec.permalink) {
    // INV-2: no permalink -> never a hyperlink (covers user/subreddit targets).
    return rec.displayId ? label : EMPTY_CELL;
  }

  const href = rec.permalink.startsWith('http')
    ? rec.permalink
    : `${REDDIT_BASE_URL}${rec.permalink}`;

  // Markdown link. `href` is a reddit content URL (never a profile, per INV-2).
  return `[${label}](${href})`;
}

/**
 * Build the prefilled modmail "removal inquiry" link (P-12).
 * Ported from `generate_modmail_link`.
 *
 * The subject embeds the content id for tracking; the body is a templated
 * inquiry. Subject and body are URL-encoded via `encodeURIComponent`, which is
 * the JS equivalent of Python's `urllib.parse.quote` for these payloads.
 */
export function modmailLink(sub: string, rec: ModRecord): string {
  const removalType = REMOVAL_TYPE_LABELS[rec.actionType] ?? 'Content';
  const contentId = rec.displayId ?? EMPTY_CELL;

  // Title falls back to "Content by u/<author>" then "Unknown content".
  let title = rec.targetAuthor
    ? `Content by u/${rec.targetAuthor}`
    : 'Unknown content';
  if (title.length > MAX_TITLE_LENGTH) {
    title = `${title.slice(0, MAX_TITLE_LENGTH - 3)}...`;
  }

  // Absolutize the permalink for the body link, if any.
  const url = rec.permalink
    ? rec.permalink.startsWith('http')
      ? rec.permalink
      : `${REDDIT_BASE_URL}${rec.permalink}`
    : '';

  const subject = `${removalType} Removal Inquiry - ${title} [ID: ${contentId}]`;
  const body =
    `Hello Moderators of /r/${sub},\n\n` +
    `I would like to inquire about the recent removal of the following ${removalType.toLowerCase()}:\n\n` +
    `**Content ID:** ${contentId}\n\n` +
    `**Title:** ${title}\n\n` +
    `**Action Type:** ${rec.actionType}\n\n` +
    `**Link:** ${url}\n\n` +
    'Please provide details regarding this action.\n\n' +
    'Thank you!';

  const composeUrl =
    `${REDDIT_BASE_URL}/message/compose?to=/r/${sub}` +
    `&subject=${encodeURIComponent(subject)}` +
    `&message=${encodeURIComponent(body)}`;

  return `[Contact Mods](${composeUrl})`;
}

/**
 * Format a record's UTC time-of-day cell (e.g. "14:03:21 UTC").
 * Pure: derived only from the record's epoch-seconds timestamp.
 */
function formatTimeCell(createdAtSec: number): string {
  const d = new Date(createdAtSec * 1000);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss} UTC`;
}

/** Derive the "YYYY-MM-DD" UTC day key used to group rows into tables. */
function dayKey(createdAtSec: number): string {
  return new Date(createdAtSec * 1000).toISOString().slice(0, 10);
}

/**
 * Render a single markdown table row for one record.
 * Ported from `format_modlog_entry` + the row-join in `build_wiki_content`.
 *
 * Note INV-1: `rec.moderator` is ALREADY anonymized at ingest, so the renderer
 * simply prints it — a real moderator name can never reach this layer.
 */
export function renderRow(rec: ModRecord, cfg: AppConfig): string {
  const time = formatTimeCell(rec.createdAtSec);
  const action = sanitizeCell(rec.actionType);
  const id = rec.displayId ? sanitizeCell(rec.displayId) : EMPTY_CELL;
  const moderator = sanitizeCell(rec.moderator) || 'Unknown'; // INV-1 (pre-anonymized)
  const content = contentLink(rec);
  const reason = rec.reason ? sanitizeCell(rec.reason) : EMPTY_CELL;
  const inquire = modmailLink(cfg.subredditName, rec);

  return `| ${time} | ${action} | ${id} | ${moderator} | ${content} | ${reason} | ${inquire} |`;
}

// ---------------------------------------------------------------------------
// Day-block assembly + byte-cap trimming (INV-3).
// ---------------------------------------------------------------------------

/** A rendered table for one calendar day, with its day key for sorting/trim. */
interface DayBlock {
  readonly date: string; // "YYYY-MM-DD"
  readonly markdown: string; // full "## date\n<table>\n" block
}

/**
 * Group records by UTC day (newest day first) and render one markdown table
 * per day. Records are not mutated; a new array of DayBlocks is produced.
 */
function buildDayBlocks(records: readonly ModRecord[], cfg: AppConfig): DayBlock[] {
  const byDate = new Map<string, ModRecord[]>();
  for (const rec of records) {
    const key = dayKey(rec.createdAtSec);
    const bucket = byDate.get(key);
    if (bucket) {
      bucket.push(rec);
    } else {
      byDate.set(key, [rec]);
    }
  }

  // Dates newest-first; within a day, rows newest-first (matches Python sort).
  const sortedDates = [...byDate.keys()].sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));

  return sortedDates.map((date) => {
    const rows = [...byDate.get(date)!].sort((a, b) => b.createdAtSec - a.createdAtSec);
    const lines = [`## ${date}`, TABLE_HEADER, TABLE_SEPARATOR];
    for (const rec of rows) {
      lines.push(renderRow(rec, cfg));
    }
    lines.push(''); // trailing blank line between day tables
    return { date, markdown: lines.join('\n') };
  });
}

/** UTF-8 byte length of a string (Devvit runtime provides TextEncoder). */
function utf8ByteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

/**
 * Assemble header + day blocks + footer, enforcing the 512 KB cap (INV-3).
 *
 * Mirrors the Python trimming loop: day blocks are added newest-first while the
 * running UTF-8 size stays at/under the trim target (90% of cap). The first day
 * that would push past the target stops inclusion, and a "trimmed" notice is
 * inserted. The header + footer bytes are pre-counted so the final document is
 * guaranteed <= WIKI_BYTE_CAP.
 */
export function enforceByteCap(
  header: string,
  dayBlocks: readonly DayBlock[],
  footerLines: readonly string[],
): string {
  const footer = footerLines.join('\n');

  // Pre-count header + footer so day inclusion respects the real budget.
  let runningSize = utf8ByteLength(`${header}\n${footer}`);

  const includedParts: string[] = [header];
  let skippedDays = 0;
  let lastIncludedDate: string | null = null;

  for (let i = 0; i < dayBlocks.length; i++) {
    const block = dayBlocks[i];
    const testSize = runningSize + utf8ByteLength(block.markdown);

    if (testSize > WIKI_TRIM_TARGET) {
      // Stop here; everything from i onward is trimmed (oldest-day-first).
      skippedDays = dayBlocks.length - i;
      break;
    }

    includedParts.push(block.markdown);
    lastIncludedDate = block.date;
    runningSize = testSize;
  }

  if (skippedDays > 0) {
    const fromDate = lastIncludedDate ?? 'today';
    includedParts.push(
      `\n**Note:** ${skippedDays} older day(s) trimmed due to wiki size limits.`,
    );
    includedParts.push(`Only showing entries from ${fromDate} onwards.\n`);
  }

  includedParts.push(...footerLines);

  let result = includedParts.join('\n');

  // Defensive hard guard: even after day-trimming, a single oversized day plus
  // header/footer could (pathologically) exceed the absolute cap. Truncate on a
  // UTF-8 boundary so we never hand the wiki layer an over-cap document (INV-3).
  if (utf8ByteLength(result) > WIKI_BYTE_CAP) {
    result = truncateToBytes(result, WIKI_BYTE_CAP);
  }

  return result;
}

/**
 * Truncate a string so its UTF-8 encoding is at most `maxBytes`, never
 * splitting a multi-byte character. Used only as the defensive last-resort
 * guard in enforceByteCap.
 */
function truncateToBytes(s: string, maxBytes: number): string {
  const encoder = new TextEncoder();
  if (encoder.encode(s).length <= maxBytes) {
    return s;
  }
  // Binary search the largest prefix that fits.
  let lo = 0;
  let hi = s.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (encoder.encode(s.slice(0, mid)).length <= maxBytes) {
      lo = mid;
    } else {
      hi = mid - 1;
    }
  }
  return s.slice(0, lo);
}

// ---------------------------------------------------------------------------
// Top-level entry point.
// ---------------------------------------------------------------------------

/**
 * Build the full wiki markdown document from render-ready records (P-10/P-31).
 * Ported from `build_wiki_content`.
 *
 * @param records  Render-ready, already-anonymized records (INV-1 satisfied
 *                 upstream). Order is irrelevant; grouped/sorted here.
 * @param cfg      Resolved app config (subreddit name, max entries, etc.).
 * @param nowIso   Caller-supplied timestamp string for the "Last Updated"
 *                 header. Passing it in (rather than reading the clock here)
 *                 keeps this function pure/deterministic for tests.
 * @returns        Capped markdown ready for the wiki layer.
 */
export function buildContent(
  records: readonly ModRecord[],
  cfg: AppConfig,
  nowIso: string,
): string {
  const header = `**Last Updated:** ${nowIso}\n\n---\n`;

  if (records.length === 0) {
    return `${header}\nNo recent moderation actions found.`;
  }

  // Cap to the newest `maxWikiEntries` records before rendering (P-10).
  // Records are sorted newest-first so the cap keeps the most recent entries.
  const ordered = [...records].sort((a, b) => b.createdAtSec - a.createdAtSec);
  const limited =
    cfg.maxWikiEntries > 0 && ordered.length > cfg.maxWikiEntries
      ? ordered.slice(0, cfg.maxWikiEntries)
      : ordered;

  const dayBlocks = buildDayBlocks(limited, cfg);
  return enforceByteCap(header, dayBlocks, FOOTER_LINES);
}

// ---------------------------------------------------------------------------
// Content hashing (INV-6).
// ---------------------------------------------------------------------------

/**
 * SHA-256 hex digest of the rendered markdown (INV-6). Async because Web
 * Crypto's `subtle.digest` returns a Promise. The wiki layer compares this
 * against the last-written hash to skip no-op writes.
 *
 * Ported from `get_content_hash` (Python used hashlib.sha256 hexdigest).
 */
export async function contentHash(markdown: string): Promise<string> {
  // Exclude the volatile "**Last Updated:** <timestamp>" header line so an
  // unchanged modlog hashes stably (INV-6). Hashing the timestamped content
  // would differ every run, defeating the skip and rewriting the wiki each
  // cycle. Mirrors the Python get_content_hash fix.
  const hashable = markdown.replace(/^\*\*Last Updated:\*\* .*\r?\n/, '');
  const data = new TextEncoder().encode(hashable);
  // crypto.subtle is available globally in the Devvit serverless runtime.
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}
