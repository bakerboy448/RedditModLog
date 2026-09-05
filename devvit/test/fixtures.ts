/**
 * Synthetic ModAction fixtures + a test AppConfig. Shapes match the fields
 * modlog.extractRecord reads: id, type, moderatorName, createdAt (Date),
 * target?: { id, permalink, author }, description/details (reason source).
 */
import type { AppConfig } from '../src/types.js';

const NOW = 1_700_000_000; // fixed epoch seconds (no Date.now — deterministic)

function action(o: {
  id: string;
  type: string;
  mod: string;
  ageSec?: number;
  target?: { id: string; permalink?: string; author?: string };
  description?: string;
  details?: string;
}) {
  return {
    id: o.id,
    type: o.type,
    moderatorName: o.mod,
    createdAt: new Date((NOW - (o.ageSec ?? 0)) * 1000),
    target: o.target,
    description: o.description,
    details: o.details,
  };
}

export const FIXTURE_NOW_SEC = NOW;

export const ACTIONS = [
  // tracked removal w/ reason + post target + author
  action({ id: 'a1', type: 'removelink', mod: 'HumanMod1', ageSec: 10, description: 'Rule 1: off-topic',
    target: { id: 't3_aaa', permalink: '/r/test/comments/aaa/title/', author: 'alice' } }),
  // tracked comment removal whose reason has a PIPE char (must be escaped) + email (must be censored)
  action({ id: 'a2', type: 'removecomment', mod: 'HumanMod2', ageSec: 20, details: 'spam | contact me at evil@example.com',
    target: { id: 't1_bbb', permalink: '/r/test/comments/aaa/title/bbb/', author: 'bob' } }),
  // addremovalreason with an email in the reason (PII strip)
  action({ id: 'a3', type: 'addremovalreason', mod: 'HumanMod1', ageSec: 30, description: 'Appeal: mail admin@sub.example.org',
    target: { id: 't3_aaa', permalink: '/r/test/comments/aaa/title/', author: 'alice' } }),
  // approval
  action({ id: 'a4', type: 'approvelink', mod: 'HumanMod2', ageSec: 40,
    target: { id: 't3_ddd', permalink: '/r/test/comments/ddd/title/', author: 'carol' } }),
  // IGNORED moderator (AutoModerator is in default ignored list) -> filtered out
  action({ id: 'a5', type: 'spamlink', mod: 'AutoModerator', ageSec: 50, target: { id: 't3_eee' } }),
  // UNTRACKED action type (banuser not in wiki_actions) -> filtered out
  action({ id: 'a6', type: 'banuser', mod: 'HumanMod1', ageSec: 60 }),
  // OLD action (beyond retention) for cleanup test
  action({ id: 'a7', type: 'removelink', mod: 'HumanMod1', ageSec: 200 * 86_400,
    target: { id: 't3_fff', permalink: '/r/test/comments/fff/title/', author: 'dave' } }),
];

export const CFG: AppConfig = {
  subredditName: 'test',
  wikiPage: 'modlog',
  wikiActions: ['removelink', 'removecomment', 'spamlink', 'spamcomment', 'addremovalreason', 'approvelink', 'approvecomment'],
  ignoredModerators: ['automoderator'],
  retentionDays: 90,
  maxWikiEntries: 1000,
  fetchLimit: 500,
  anonymizeModerators: true,
};
