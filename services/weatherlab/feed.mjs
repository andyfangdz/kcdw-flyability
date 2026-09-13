import { mkdir, open, readFile, readdir, rename, stat, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { FeedError, safeError, normalize, MAX_BODY, MAX_RUN_AGE_MS, KCDW } from './core.mjs';
const hash = text => createHash('sha256').update(text).digest('hex');
const validDate = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value;
async function boundedRead(path, limit) { if ((await stat(path)).size > limit) throw new FeedError('cache_error'); return readFile(path, 'utf8'); }
function validateRecord(record) {
  if (!record || !validDate(record.fetched_at) || !validDate(record.requested_init_utc) || !validDate(record.attempted_init_utc)) throw new FeedError('validation_error');
  const normalized = normalize(record.raw, record.context), actual = Date.parse(normalized.response_init_utc), requested = Date.parse(record.requested_init_utc);
  if (record.attempted_init_utc !== normalized.response_init_utc || requested < actual || requested - actual > 86400000 || requested % 21600000 !== 0 || record.fallback !== (requested !== actual) || Date.parse(record.fetched_at) < requested || JSON.stringify(record.normalized) !== JSON.stringify(normalized)) throw new FeedError('validation_error');
  return { raw: record.raw, context: record.context, normalized, requested_init_utc: record.requested_init_utc, attempted_init_utc: record.attempted_init_utc, fetched_at: record.fetched_at, fallback: record.fallback };
}
async function durableWrite(path, content, flags = 'wx') {
  const file = await open(path, flags, 0o600);
  try { await file.writeFile(content); await file.sync(); } finally { await file.close(); }
}
export class Feed {
  static async open(options) { const feed = new Feed(options); await feed.load(); return feed; }
  constructor({ cacheDir, runner, now = Date.now }) {
    this.dir = cacheDir; this.runner = runner; this.now = now; this.lastGood = null; this.latest = null; this.inflight = null;
    this.outcome = { error_code: 'no_data', last_attempt_at: null, completed_at: null, requested_init_utc: null, attempted_init_utc: null };
  }
  async load() {
    await mkdir(this.dir, { recursive: true, mode: 0o700 });
    let pointerRead = false;
    try {
      const text = await boundedRead(join(this.dir, 'state.json'), 16384); pointerRead = true;
      const state = JSON.parse(text);
      if (state.version !== 1 || !state.outcome || !(state.outcome.error_code === null || safeError(new FeedError(state.outcome.error_code)).code === state.outcome.error_code)) throw new FeedError('cache_error');
      const outcome = {};
      for (const k of ['last_attempt_at', 'completed_at', 'requested_init_utc', 'attempted_init_utc']) {
        if (state.outcome[k] !== null && !validDate(state.outcome[k])) throw new FeedError('cache_error'); outcome[k] = state.outcome[k];
      }
      outcome.error_code = state.outcome.error_code;
      if (state.latest !== null) {
        if (!/^forecast-[a-f0-9]{64}\.json$/.test(state.latest)) throw new FeedError('cache_error');
        const text = await boundedRead(join(this.dir, state.latest), MAX_BODY);
        if (`forecast-${hash(text)}.json` !== state.latest) throw new FeedError('cache_error');
        this.lastGood = validateRecord(JSON.parse(text)); this.latest = state.latest;
      }
      this.outcome = outcome;
    } catch (error) { if (pointerRead || error.code !== 'ENOENT') this.outcome.error_code = 'cache_error'; }
  }
  async save(latest, outcome, record = null) {
    if (record) {
      const text = JSON.stringify(record);
      if (Buffer.byteLength(text) > MAX_BODY) throw new FeedError('cache_error');
      latest = `forecast-${hash(text)}.json`;
      try { await durableWrite(join(this.dir, latest), text); }
      catch (e) { if (e.code !== 'EEXIST') throw e; if (await boundedRead(join(this.dir, latest), MAX_BODY) !== text) throw new FeedError('cache_error'); }
    }
    const temporary = join(this.dir, `.state-${randomUUID()}.tmp`);
    try {
      await durableWrite(temporary, JSON.stringify({ version: 1, latest, outcome }));
      await rename(temporary, join(this.dir, 'state.json'));
      const directory = await open(this.dir, 'r'); try { await directory.sync(); } finally { await directory.close(); }
    } finally { await unlink(temporary).catch(() => {}); }
    // GC only after the new pointer is durable. A GC failure must not roll it back.
    await this.prune(latest).catch(() => {});
    return latest;
  }
  async prune(latest) {
    const cutoff = this.now() - 30 * 86400000, files = [];
    for (const entry of await readdir(this.dir, { withFileTypes: true })) {
      if (entry.name === latest || !entry.isFile() || !/^forecast-[a-f0-9]{64}\.json$/.test(entry.name)) continue;
      files.push({ name: entry.name, mtime: (await stat(join(this.dir, entry.name))).mtimeMs });
    }
    files.sort((a, b) => b.mtime - a.mtime || a.name.localeCompare(b.name));
    let kept = latest === null ? 0 : 1;
    for (const file of files) {
      if (file.mtime < cutoff || kept >= 720) await unlink(join(this.dir, file.name));
      else kept++;
    }
  }
  refresh() {
    if (this.inflight) return this.inflight;
    this.inflight = this.performRefresh().finally(() => { this.inflight = null; }); return this.inflight;
  }
  async performRefresh() {
    const outcome = { error_code: null, last_attempt_at: new Date(this.now()).toISOString(), completed_at: null, requested_init_utc: null, attempted_init_utc: null };
    let record = null;
    try {
      record = validateRecord(await this.runner());
      outcome.requested_init_utc = record.requested_init_utc; outcome.attempted_init_utc = record.attempted_init_utc;
      if (this.lastGood && record.context.init_seconds < this.lastGood.context.init_seconds) throw new FeedError('upstream_error');
    } catch (error) {
      record = null; const e = safeError(error); outcome.error_code = e.code;
      outcome.requested_init_utc = e.requested_init_utc ?? outcome.requested_init_utc; outcome.attempted_init_utc = e.attempted_init_utc ?? outcome.attempted_init_utc;
    }
    outcome.completed_at = new Date(this.now()).toISOString();
    try {
      this.latest = await this.save(this.latest, outcome, record);
      if (record) this.lastGood = record;
    } catch { outcome.error_code = 'cache_error'; }
    this.outcome = outcome;
    return this.status();
  }
  status() {
    const good = this.lastGood, now = this.now();
    const age = good ? now - good.context.init_seconds * 1000 : null;
    const fresh = age !== null && age >= 0 && age <= MAX_RUN_AGE_MS;
    const error = this.outcome.error_code;
    const state = error === 'auth_required' ? 'auth_required' : good && !fresh ? 'stale' : error ? 'degraded' : good?.fallback ? 'degraded' : good ? 'ready' : 'degraded';
    return {
      state, available: Boolean(good && fresh && !error), authentication: error === 'auth_required' ? 'required' : error ? 'unknown' : 'last_refresh_succeeded',
      freshness: good ? fresh ? 'fresh' : 'stale' : 'missing', station: 'KCDW', ...KCDW, model: 'WeatherNext 3', model_id: 12,
      max_run_age_hours: MAX_RUN_AGE_MS / 3600000, run_age_seconds: age === null ? null : Math.floor(age / 1000),
      ...this.outcome, actual_run_utc: good?.normalized.response_init_utc ?? null, fetched_at: good?.fetched_at ?? null,
      last_good_requested_init_utc: good?.requested_init_utc ?? null, fallback: good?.fallback ?? false,
      refreshing: Boolean(this.inflight), checked_at: new Date(now).toISOString(),
      action: error === 'auth_required' ? 'Sign in manually in the supervised Weather Lab browser; wait for the next hourly refresh.' : state === 'stale' ? 'Wait for a fresh advertised long run; do not use this as current weather.' : error ? 'Check the private feed and supervised loopback CDP service.' : null
    };
  }
}
