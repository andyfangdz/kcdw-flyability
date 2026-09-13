import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, readdir, writeFile, chmod, symlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const core = await import('../core.mjs').catch(e => { if (e.code === 'ERR_MODULE_NOT_FOUND') return {}; throw e; });
const { decodeRpc, normalize, discoverRuns, collectForecast, FeedError, KCDW, MAX_BODY } = core;
const INIT = Date.parse('2026-09-12T00:00:00Z') / 1000;
// Deliberately synthetic numerical fixtures, never represented as observations.
function raw(init = INIT) {
  return [[init], Array.from({ length: 360 }, (_, i) => [init + (i + 1) * 3600]),
    [1, 5, 2, 3].map(id => [id, Array(360).fill(id === 2 ? 101000 : 10)]),
    [1, 5, 2, 3].flatMap(id => [10, 90].map(p => [id, p, Array(360).fill(id === 2 ? 100000 + p * 10 : p / 10)]))];
}
const request = (init = INIT) => ({ latitude: 40.8752, longitude: -74.2814, init_seconds: init, model_id: 12, fields: [1, 5, 2, 3] });
const packet = (id, data) => ")]}'\n123\n" + JSON.stringify([['wrb.fr', id, JSON.stringify(data), null, null, null, 'generic']]) + '\n';
function record(init = INIT, fetched = INIT * 1000 + 3600000) {
  const data = raw(init), context = request(init);
  return { raw: data, context, normalized: normalize(data, context), requested_init_utc: new Date(init * 1000).toISOString(), attempted_init_utc: new Date(init * 1000).toISOString(), fetched_at: new Date(fetched).toISOString(), fallback: false };
}
async function temp(t) {
  // All writes stay in this task's owned subtree, including temporary test data.
  const root = fileURLToPath(new URL('../.test-tmp/', import.meta.url));
  const { mkdir } = await import('node:fs/promises'); await mkdir(root, { recursive: true });
  const dir = await mkdtemp(join(root, 'case-'));
  t.after(() => rm(dir, { recursive: true, force: true })); return dir;
}

test('HTTP 200 RPC permission error is auth_required, never a forecast', () => {
  assert.equal(typeof decodeRpc, 'function', 'RPC error decoder must exist');
  assert.throws(() => decodeRpc(")]}'\n[[\"wrb.fr\",\"Yx5Tmb\",null,null,null,[7]]]\n", 'Yx5Tmb'), e => e.code === 'auth_required');
});
test('HTTP 200 generic RPC failure, malformed, duplicate and oversized envelopes fail closed', () => {
  assert.throws(() => decodeRpc('[["wrb.fr","Yx5Tmb",null,null,null,[13]]]', 'Yx5Tmb'), e => e.code === 'upstream_error');
  for (const body of ['not json SECRET', packet('other', raw()), packet('Yx5Tmb', raw()).repeat(2), 'x'.repeat(MAX_BODY + 1)]) {
    assert.throws(() => decodeRpc(body, 'Yx5Tmb'), e => !e.message.includes('SECRET'));
  }
  assert.deepEqual(decodeRpc(packet('Yx5Tmb', raw()), 'Yx5Tmb'), raw());
});
test('360 hourly +1h through +360h; units unscaled and means may exceed p90', () => {
  const result = normalize(raw(), request());
  assert.equal(result.valid_time_utc.length, 360);
  assert.equal(Date.parse(result.valid_time_utc.at(-1)) / 1000, INIT + 360 * 3600);
  assert.equal(result.fields.precipitation_1h.unit, 'mm');
  assert.equal(result.fields.precipitation_1h.mean[0], 10);
  assert.equal(result.fields.precipitation_1h.p90[0], 9);
});
test('request location, model, field set and response init must agree exactly', () => {
  for (const context of [{ ...request(), latitude: 41 }, { ...request(), longitude: -74 }, { ...request(), model_id: 11 }, { ...request(), fields: [1, 5, 2, 2] }, request(INIT - 21600)]) {
    assert.throws(() => normalize(raw(), context), e => e.code === 'validation_error');
  }
});
test('timeline shifts, missing hour, duplicate mean/percentile fields, missing percentile and ordering rejected', () => {
  const mutations = [d => d[1][0][0]--, d => d[1].pop(), d => d[1][4] = d[1][3], d => d[2][3][0] = 2,
    d => d[3][1] = d[3][0], d => d[3].pop(), d => d[3][0][2][0] = 50, d => d[2].push(d[2][0])];
  for (const mutate of mutations) { const d = raw(); mutate(d); assert.throws(() => normalize(d, request()), e => e.code === 'validation_error'); }
});
test('nonfinite, nonnumeric, out-of-range values in every series rejected', () => {
  for (const value of [NaN, Infinity, -Infinity, null, '10', 1e9]) {
    for (const slot of ['mean', 'p10', 'p90']) {
      const d = raw(); (slot === 'mean' ? d[2][0][1] : d[3][slot === 'p10' ? 0 : 1][2])[0] = value;
      assert.throws(() => normalize(d, request()), e => e.code === 'validation_error');
    }
  }
  for (const [index, value] of [[0, -121], [0, 81], [1, -1], [1, 1501], [2, 74999], [2, 115001], [3, -1], [3, 161]]) {
    const d = raw(); d[2][index][1][0] = value; assert.throws(() => normalize(d, request()));
  }
});
test('discover model-index runs when broader index absent; long cycles; max two within 24h', () => {
  const now = (INIT + 13 * 3600) * 1000;
  const payload = [null, [['WeatherNext2', [INIT + 12 * 3600]], ['WeatherNext3', [String(INIT), String(INIT + 3 * 3600), String(INIT + 6 * 3600), String(INIT + 12 * 3600), String(INIT - 86400)]]]];
  assert.deepEqual(discoverRuns(payload, now), [INIT + 12 * 3600, INIT + 6 * 3600]);
  assert.throws(() => discoverRuns([null, [['WeatherNext2', [INIT]]]], now));
  assert.throws(() => discoverRuns([null, [['WeatherNext3', [INIT - 86400]]]], now), e => e.code === 'stale');
});
test('lagging model index does not hide response-confirmed WN3 point runs', async () => {
  const latest = INIT + 30 * 3600, previous = INIT + 24 * 3600;
  const discovery = [null, [['weathernext3', [INIT + 12 * 3600]]], [INIT, previous, String(latest), latest]];
  const now = () => (INIT + 38 * 3600) * 1000;
  assert.deepEqual(discoverRuns(discovery, now()), [latest, previous]);
  const calls = [];
  const result = await collectForecast(async (id, args) => {
    if (id === 'lBl2Zc') return packet(id, discovery);
    calls.push(args);
    return packet(id, raw(args[1][0]));
  }, now, async () => {});
  assert.equal(result.normalized.response_init_utc, new Date(latest * 1000).toISOString());
  assert.equal(result.fallback, false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][3], 12);
});
test('generic advertised times are candidates only; mismatched response falls back explicitly', async () => {
  const latest = INIT + 12 * 3600, previous = INIT + 6 * 3600;
  const discovery = [null, [['weathernext3', [previous]]], [latest, previous]];
  const result = await collectForecast(async (id, args) => {
    if (id === 'lBl2Zc') return packet(id, discovery);
    return packet(id, raw(previous));
  }, () => (INIT + 13 * 3600) * 1000, async () => {});
  assert.equal(result.fallback, true);
  assert.equal(result.requested_init_utc, new Date(latest * 1000).toISOString());
  assert.equal(result.normalized.response_init_utc, new Date(previous * 1000).toISOString());
});
test('broader discovery rejects malformed data and never invents clock-based runs', () => {
  const base = [null, [['weathernext3', [INIT]]]];
  for (const values of ['bad', [true], [null], ['1oops'], [1.5]]) {
    assert.throws(() => discoverRuns([...base, values], (INIT + 3600) * 1000), e => e.code === 'upstream_error');
  }
  assert.throws(() => discoverRuns([...base, [INIT]], (INIT + 25 * 3600) * 1000), e => e.code === 'stale');
});

test('bounded retry then advertised fallback; auth stops without fallback', async () => {
  const calls = []; const discovery = [null, [['weathernext3', [INIT, INIT - 21600]]]];
  const rpc = async (id, args) => {
    calls.push([id, args]);
    if (id === 'lBl2Zc') return packet(id, discovery);
    if (args[1][0] === INIT) throw new FeedError('upstream_error');
    return packet(id, raw(INIT - 21600));
  };
  const result = await collectForecast(rpc, () => (INIT + 3600) * 1000, async () => {});
  assert.equal(result.fallback, true); assert.equal(result.context.init_seconds, INIT - 21600);
  assert.equal(calls.filter(c => c[0] === 'Yx5Tmb').length, 3);
  assert.deepEqual(calls[0], ['lBl2Zc', []]);
  let attempts = 0;
  await assert.rejects(collectForecast(async id => { if (id === 'lBl2Zc') return packet(id, discovery); attempts++; throw new FeedError('auth_required'); }, () => (INIT + 3600) * 1000), e => e.code === 'auth_required');
  assert.equal(attempts, 1);
});
test('cache persists across restart, immutable records, auth/upstream/validation preserve last good', async t => {
  const { Feed } = await import('../feed.mjs'); const dir = await temp(t); let now = (INIT + 3600) * 1000;
  let fail = null; const runner = async () => { if (fail) throw new FeedError(fail); return record(); };
  const feed = await Feed.open({ cacheDir: dir, runner, now: () => now });
  await feed.refresh(); assert.equal(feed.status().state, 'ready');
  const pointer = JSON.parse(await readFile(join(dir, 'state.json')));
  const original = await readFile(join(dir, pointer.latest), 'utf8');
  for (const code of ['auth_required', 'upstream_error', 'validation_error']) {
    fail = code; await feed.refresh();
    assert.equal(feed.status().available, false); assert.equal(feed.status().error_code, code);
    assert.equal(await readFile(join(dir, pointer.latest), 'utf8'), original);
    const restarted = await Feed.open({ cacheDir: dir, runner, now: () => now });
    assert.equal(restarted.lastGood.normalized.response_init_utc, record().normalized.response_init_utc);
    assert.equal(restarted.status().error_code, code);
  }
  assert.equal((await readdir(dir)).filter(p => p.startsWith('forecast-')).length, 1);
  fail = null; await feed.refresh(); assert.equal(feed.status().state, 'ready');
});
test('freshness uses run age, not recent fetched time, and ages without refresh', async t => {
  const { Feed } = await import('../feed.mjs'); let now = (INIT + 17 * 3600) * 1000;
  const feed = await Feed.open({ cacheDir: await temp(t), runner: async () => record(INIT, now), now: () => now });
  await feed.refresh(); assert.equal(feed.status().available, true);
  now = (INIT + 19 * 3600) * 1000; assert.equal(feed.status().state, 'stale'); assert.equal(feed.status().available, false);
  await feed.refresh(); assert.equal(feed.status().state, 'stale');
});
test('refresh does not overlap', async t => {
  const { Feed } = await import('../feed.mjs'); let release; let count = 0;
  const feed = await Feed.open({ cacheDir: await temp(t), now: () => (INIT + 3600) * 1000, runner: () => { count++; return new Promise(r => release = r); } });
  const first = feed.refresh(); const second = feed.refresh(); release(record()); await Promise.all([first, second]); assert.equal(count, 1);
});
test('real loopback HTTP: bearer, minimal health, strict routes, stale and auth 503', async t => {
  const { Feed } = await import('../feed.mjs'); const { createServer } = await import('../server.mjs');
  let now = (INIT + 3600) * 1000, fail = false;
  const feed = await Feed.open({ cacheDir: await temp(t), now: () => now, runner: async () => { if (fail) throw new FeedError('auth_required'); return record(); } });
  await feed.refresh(); const token = 'test-only-'.padEnd(64, 'a'); const server = createServer(feed, token);
  await new Promise(r => server.listen(0, '127.0.0.1', r)); t.after(() => new Promise(r => server.close(r)));
  const url = `http://127.0.0.1:${server.address().port}`;
  const get = (p, auth = token) => fetch(url + p, { headers: auth ? { authorization: `Bearer ${auth}` } : {} });
  assert.deepEqual(await (await get('/health', null)).json(), { ok: true });
  for (const auth of [null, 'wrong', token + 'x']) assert.equal((await get('/v1/status', auth)).status, 401);
  assert.equal((await get('/v1/forecast/KCDW')).status, 200);
  for (const path of ['/v1/forecast/KCDW?lat=0', '/v1/forecast/KJFK', '/v1/status?url=evil']) assert.equal((await get(path)).status, 404);
  assert.equal((await fetch(url + '/v1/status', { method: 'POST', headers: { authorization: `Bearer ${token}` } })).status, 405);
  now += 19 * 3600000; let response = await get('/v1/forecast/KCDW'); assert.equal(response.status, 503); assert.equal((await response.json()).status.state, 'stale');
  now = (INIT + 3600) * 1000; fail = true; await feed.refresh(); response = await get('/v1/forecast/KCDW'); assert.equal(response.status, 503);
  assert.equal((await response.json()).status.state, 'auth_required');
  const last = await (await get('/v1/last-good/KCDW')).json(); assert.equal(last.explicit_last_good, true); assert.equal(last.status.available, false);
});
test('token loading rejects permissions, in-repo paths, symlinks and weak tokens', async t => {
  const { loadToken } = await import('../server.mjs'); const dir = await temp(t), path = join(dir, 'token');
  const token = 'test-not-a-secret'.padEnd(64, 'x'); await writeFile(path, token, { mode: 0o600 });
  // Simulated repository boundary permits testing without writing outside owned scope.
  const repoRoot = join(dir, 'different-repo'); assert.equal(await loadToken(path, repoRoot), token);
  await assert.rejects(loadToken(path, resolve(dir)));
  await chmod(path, 0o644); await assert.rejects(loadToken(path, repoRoot));
  await chmod(path, 0o600); const link = join(dir, 'link'); await symlink(path, link); await assert.rejects(loadToken(link, repoRoot));
  await writeFile(path, 'short'); await assert.rejects(loadToken(path, repoRoot));
});
test('fresh fallback is explicitly degraded/available; stale fallback unavailable; malformed results preserve cache', async t => {
  const { Feed } = await import('../feed.mjs'); let now = (INIT + 3600) * 1000, value = record(INIT - 21600);
  value.requested_init_utc = new Date(INIT * 1000).toISOString(); value.fallback = true;
  const feed = await Feed.open({ cacheDir: await temp(t), runner: async () => value, now: () => now });
  await feed.refresh(); assert.equal(feed.status().state, 'degraded'); assert.equal(feed.status().available, true); assert.equal(feed.status().fallback, true);
  const previous = feed.latest; value = record(); value.raw[2][0][1][0] = null; await feed.refresh();
  assert.equal(feed.latest, previous); assert.equal(feed.status().error_code, 'validation_error');
  now = (INIT + 19 * 3600) * 1000; assert.equal(feed.status().available, false);
});
test('cache retention removes only expired/overflow forecasts and always preserves pointed last-good', async t => {
  const { Feed } = await import('../feed.mjs'); const { utimes } = await import('node:fs/promises');
  const dir = await temp(t), now = Date.now();
  const feed = await Feed.open({ cacheDir: dir, runner: async () => record(), now: () => now });
  await feed.refresh(); const pointed = feed.latest;
  const old = new Date(now - 31 * 86400000), recent = new Date(now - 1000);
  await utimes(join(dir, pointed), old, old);
  const expired = `forecast-${'a'.repeat(64)}.json`;
  await writeFile(join(dir, expired), '{}'); await utimes(join(dir, expired), old, old);
  await writeFile(join(dir, 'keep-unrelated.txt'), 'keep');
  for (let i = 0; i < 722; i++) {
    const path = join(dir, `forecast-${i.toString(16).padStart(64, '0')}.json`);
    await writeFile(path, '{}'); await utimes(path, recent, recent);
  }
  await feed.refresh();
  const files = await readdir(dir);
  assert.equal(files.filter(n => /^forecast-[a-f0-9]{64}\.json$/.test(n)).length, 720);
  assert.ok(files.includes(pointed)); assert.ok(!files.includes(expired)); assert.ok(files.includes('keep-unrelated.txt'));
  const restarted = await Feed.open({ cacheDir: dir, runner: async () => record(), now: () => now });
  assert.equal(restarted.latest, pointed); assert.equal(restarted.status().error_code, null);
});
test('tampered or missing pointed cache record fails closed on restart', async t => {
  const { Feed } = await import('../feed.mjs'); const dir = await temp(t); const options = { cacheDir: dir, runner: async () => record(), now: () => (INIT + 3600) * 1000 };
  const feed = await Feed.open(options); await feed.refresh();
  const path = join(dir, feed.latest); await writeFile(path, '{}');
  let restart = await Feed.open(options); assert.equal(restart.status().error_code, 'cache_error'); assert.equal(restart.status().available, false);
  await rm(path); restart = await Feed.open(options); assert.equal(restart.status().error_code, 'cache_error'); assert.equal(restart.status().available, false);
});
