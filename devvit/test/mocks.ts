/**
 * In-memory fakes for the Devvit primitives our pipeline touches, so the real
 * compiled modules (dist/*.js — type-only @devvit imports are erased at build)
 * can run fully offline. This is the "self-test reddit": no auth, no platform.
 */

type ZMember = { member: string; score: number };

/** Implements the exact RedisClient subset storage.ts uses. */
export class InMemoryRedis {
  private hashes = new Map<string, Map<string, string>>();
  private zsets = new Map<string, Map<string, number>>();

  private h(key: string): Map<string, string> {
    let m = this.hashes.get(key);
    if (!m) { m = new Map(); this.hashes.set(key, m); }
    return m;
  }
  private z(key: string): Map<string, number> {
    let m = this.zsets.get(key);
    if (!m) { m = new Map(); this.zsets.set(key, m); }
    return m;
  }

  async hSet(key: string, obj: Record<string, string>): Promise<number> {
    const m = this.h(key);
    for (const [k, v] of Object.entries(obj)) m.set(k, v);
    return Object.keys(obj).length;
  }
  async hSetNX(key: string, field: string, value: string): Promise<number> {
    const m = this.h(key);
    if (m.has(field)) return 0;
    m.set(field, value);
    return 1;
  }
  async hGet(key: string, field: string): Promise<string | undefined> {
    return this.hashes.get(key)?.get(field) ?? undefined;
  }
  async hGetAll(key: string): Promise<Record<string, string>> {
    const m = this.hashes.get(key);
    return m ? Object.fromEntries(m) : {};
  }
  async hDel(key: string, fields: string[]): Promise<number> {
    const m = this.h(key);
    let n = 0;
    for (const f of fields) if (m.delete(f)) n++;
    return n;
  }
  async zAdd(key: string, ...members: ZMember[]): Promise<number> {
    const m = this.z(key);
    for (const { member, score } of members) m.set(member, score);
    return members.length;
  }
  async zRange(
    key: string,
    min: number,
    max: number,
    opts: { by: string; reverse?: boolean; limit?: { offset: number; count: number } },
  ): Promise<ZMember[]> {
    const m = this.zsets.get(key);
    if (!m) return [];
    let arr: ZMember[] = [...m.entries()]
      .map(([member, score]) => ({ member, score }))
      .filter((e) => e.score >= min && e.score <= max);
    arr.sort((a, b) => (opts.reverse ? b.score - a.score : a.score - b.score));
    if (opts.limit) arr = arr.slice(opts.limit.offset, opts.limit.offset + opts.limit.count);
    return arr;
  }
  async zRemRangeByScore(key: string, min: number, max: number): Promise<number> {
    const m = this.zsets.get(key);
    if (!m) return 0;
    let n = 0;
    for (const [member, score] of [...m]) if (score >= min && score <= max) { m.delete(member); n++; }
    return n;
  }
}

/** Mock RedditAPIClient covering getModerationLog + the wiki read/create/update path. */
export function makeMockReddit(actions: any[]) {
  const wiki = new Map<string, string>(); // `${sub}/${page}` -> content
  const writes: Array<{ op: string; page: string; content: string }> = [];
  const reddit = {
    getModerationLog(o: { subredditName: string; limit?: number; pageSize?: number }) {
      const slice = actions.slice(0, o.limit ?? actions.length);
      return { all: async () => slice };
    },
    async getWikiPage(subredditName: string, page: string) {
      const key = `${subredditName}/${page}`;
      if (!wiki.has(key)) throw new Error(`WIKI_PAGE_NOT_FOUND: ${key}`);
      return { content: wiki.get(key)! };
    },
    async createWikiPage(o: { subredditName: string; page: string; content: string }) {
      wiki.set(`${o.subredditName}/${o.page}`, o.content);
      writes.push({ op: 'create', page: o.page, content: o.content });
      return { content: o.content };
    },
    async updateWikiPage(o: { subredditName: string; page: string; content: string }) {
      wiki.set(`${o.subredditName}/${o.page}`, o.content);
      writes.push({ op: 'update', page: o.page, content: o.content });
      return { content: o.content };
    },
  };
  return { reddit: reddit as any, wiki, writes };
}
