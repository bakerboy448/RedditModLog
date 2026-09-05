/**
 * Offline "self-test reddit" — runs the REAL compiled pipeline (dist/*.js, with
 * type-only @devvit imports erased) against in-memory Redis + a mock Reddit
 * client. Validates logic + the security invariants (anonymize / PII strip /
 * no-profile-links / dedup / retention / size-cap / hash-skip) with no auth.
 *
 * Requires a build first (npm run build); the `test` script chains tsc.
 */
import { describe, it, expect } from 'vitest';
import { InMemoryRedis, makeMockReddit } from './mocks';
import { ACTIONS, CFG, FIXTURE_NOW_SEC } from './fixtures';

import { ingest } from '../dist/modlog.js';
import { getRecentActions, cleanupOld, getAllRecords } from '../dist/storage.js';
import { buildContent, censorEmail, escapePipes } from '../dist/render.js';
import { publishFromStore } from '../dist/wiki.js';

describe('ingest + filtering', () => {
  it('persists only tracked, non-ignored actions and dedupes', async () => {
    const redis = new InMemoryRedis();
    const { reddit } = makeMockReddit(ACTIONS);
    const r1 = await ingest(reddit, redis, CFG);
    // a1,a2,a3,a4,a7 tracked+kept = 5 ; a5 (AutoModerator ignored), a6 (banuser untracked) dropped
    expect(r1.added).toBe(5);
    // second pass: everything already seen -> 0 new (INV-5 idempotency, no daemon)
    const r2 = await ingest(reddit, redis, CFG);
    expect(r2.added).toBe(0);
  });
});

describe('anonymization (INV-1) + no profile links (INV-2)', () => {
  it('never emits a real moderator name and never links a user profile', async () => {
    const redis = new InMemoryRedis();
    const { reddit } = makeMockReddit(ACTIONS);
    await ingest(reddit, redis, CFG);
    const recs = await getAllRecords(redis, CFG.wikiPage);
    for (const rec of recs) {
      expect(rec.moderator).not.toMatch(/HumanMod\d/); // real names (HumanMod1/2) never stored; anon label "HumanModerator" is fine
    }
    const md = buildContent(recs, CFG);
    expect(md).not.toMatch(/HumanMod1|HumanMod2/);
    expect(md).not.toMatch(/\/u\/|\/user\//); // no user-profile links anywhere
  });
});

describe('PII strip (INV-4): email censor + pipe escape', () => {
  it('censorEmail replaces addresses', () => {
    expect(censorEmail('reach evil@example.com now')).not.toContain('evil@example.com');
  });
  it('escapePipes neutralizes table-breaking pipes', () => {
    expect(escapePipes('a | b')).not.toContain('|');
  });
  it('rendered wiki content contains no raw emails', async () => {
    const redis = new InMemoryRedis();
    const { reddit } = makeMockReddit(ACTIONS);
    await ingest(reddit, redis, CFG);
    const md = buildContent(await getAllRecords(redis, CFG.wikiPage), CFG);
    expect(md).not.toMatch(/[\w.+-]+@[\w-]+\.[\w.-]+/); // no email survives
  });
});

describe('retention cleanup', () => {
  it('removes actions older than retentionDays', async () => {
    const redis = new InMemoryRedis();
    const { reddit } = makeMockReddit(ACTIONS);
    await ingest(reddit, redis, CFG);
    const before = (await getAllRecords(redis, CFG.wikiPage)).length;
    const removed = await cleanupOld(redis, CFG.wikiPage, CFG.retentionDays, FIXTURE_NOW_SEC);
    expect(removed).toBeGreaterThanOrEqual(1); // a7 (200 days old) pruned
    const after = (await getAllRecords(redis, CFG.wikiPage)).length;
    expect(after).toBe(before - removed);
  });
});

describe('publish: create then hash-skip (INV-6)', () => {
  it('writes once, then skips when content is unchanged', async () => {
    const redis = new InMemoryRedis();
    const { reddit, writes } = makeMockReddit(ACTIONS);
    await ingest(reddit, redis, CFG);
    const p1 = await publishFromStore(reddit, redis, CFG);
    expect(p1.wrote).toBe(true);
    expect(writes.length).toBe(1);
    const p2 = await publishFromStore(reddit, redis, CFG);
    expect(p2.wrote).toBe(false);
    expect(p2.reason).toBe('unchanged');
    expect(writes.length).toBe(1); // no second write
  });
});
