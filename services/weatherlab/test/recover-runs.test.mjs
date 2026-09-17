import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, symlink, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { recoverRuns, validateRuns, recordFromCache, MAX_CACHE_FILES } from '../recover-runs.mjs';
import { KCDW, MAX_BODY, normalize, FeedError } from '../core.mjs';
const run = '2026-09-13T00:00:00.000Z', next = '2026-09-13T06:00:00.000Z';
const fetched = '2026-09-13T03:17:01.123Z';
const context = time => ({ ...KCDW, init_seconds: Date.parse(time) / 1000, model_id: 12, fields: [1, 5, 2, 3] });
function raw(time) {
  const init = Date.parse(time) / 1000, values = { 1: 20, 5: 0, 2: 101000, 3: 5 };
  return [[init], Array.from({ length: 360 }, (_, i) => [init + (i + 1) * 3600]), [1, 5, 2, 3].map(id => [id, Array(360).fill(values[id])]), [1, 5, 2, 3].flatMap(id => [10, 90].map(p => [id, p, Array(360).fill(values[id])]))];
}
const wire = time => JSON.stringify([['wrb.fr', 'Yx5Tmb', JSON.stringify(raw(time))]]);
function cached(time = run) {
  const c = context(time), r = raw(time);
  return { context: c, raw: r, normalized: normalize(r, c), fetched_at: fetched, requested_init_utc: time, attempted_init_utc: time, fallback: false };
}
async function dir(t) { const d = await mkdtemp(join(tmpdir(), 'recover-test-')); t.after(() => rm(d, { recursive: true, force: true })); return d; }
async function save(d, record) { const text = JSON.stringify(record), name = `forecast-${createHash('sha256').update(text).digest('hex')}.json`; await writeFile(join(d, name), text); return name; }
test('strict canonical six-hour cycles and max eight', () => {
  assert.deepEqual(validateRuns({ runs: [run] }), [run]);
  for (const value of [null, {}, { runs: Array(9).fill(run) }, { runs: ['2026-09-13T00:00:00Z'] }, { runs: ['2026-09-13T01:00:00.000Z'] }, { runs: ['2026-02-30T00:00:00.000Z'] }, { runs: ['token-secret'] }]) assert.throws(() => validateRuns(value), /validation_error/);
});
test('cache re-normalizes raw and retains original retrieval time', () => {
  const record = cached(); record.normalized = { fabricated: true };
  const result = recordFromCache(record, run);
  assert.deepEqual(result.forecast, normalize(record.raw, record.context));
  assert.equal(result.retrieved_at, fetched);
  assert.throws(() => recordFromCache(record, next));
  delete record.fetched_at;
  assert.throws(() => recordFromCache(record, run));
  const tampered = cached(); tampered.raw[0][0] += 21600;
  assert.throws(() => recordFromCache(tampered, run));
  const wrongContext = cached(); wrongContext.context.longitude = 0;
  assert.throws(() => recordFromCache(wrongContext, run));
  const premature = cached(); premature.fetched_at = '2026-09-12T00:00:00.000Z';
  assert.throws(() => recordFromCache(premature, run));
});
test('RPC is exactly bound; completion timestamp is real, failures isolated', async t => {
  const cacheDir = await dir(t); let calls = 0;
  const now = () => Date.parse('2026-09-14T12:34:56.789Z');
  const result = await recoverRuns({ runs: [run, next] }, { cacheDir, now, rpc: async (id, args) => {
    assert.equal(id, 'Yx5Tmb'); assert.deepEqual(args, [[KCDW.latitude, KCDW.longitude], [Date.parse(calls++ ? next : run) / 1000], [1, 5, 2, 3], 12]);
    return wire(next);
  } });
  assert.deepEqual(result.errors, [{ run_time: run, error: 'validation_error' }]);
  assert.equal(result.records[0].forecast.response_init_utc, next);
  assert.equal(result.records[0].retrieved_at, new Date(now()).toISOString());
});
test('tampered SHA, symlink, oversized and missing timestamp never trusted', async t => {
  const cacheDir = await dir(t);
  await writeFile(join(cacheDir, `forecast-${'0'.repeat(64)}.json`), JSON.stringify(cached()));
  const target = join(cacheDir, 'token.json'); await writeFile(target, 'secret');
  await symlink(target, join(cacheDir, `forecast-${'1'.repeat(64)}.json`));
  await writeFile(join(cacheDir, `forecast-${'2'.repeat(64)}.json`), ' '.repeat(MAX_BODY + 1));
  const invalid = cached(); delete invalid.fetched_at; await save(cacheDir, invalid);
  await save(cacheDir, { ...cached(next), fetched_at: '2026-09-13T09:00:00.000Z' });
  const result = await recoverRuns({ runs: [run, next] }, { cacheDir, rpc: async () => { throw new Error('credential=secret'); } });
  assert.deepEqual(result.errors, [{ run_time: run, error: 'upstream_error' }]);
  assert.equal(result.records.length, 1); assert.equal(result.records[0].retrieved_at, '2026-09-13T09:00:00.000Z');
  assert.ok(!JSON.stringify(result).includes('secret'));
});
test('valid immutable cache needs no browser; directory symlinks rejected', async t => {
  const cacheDir = await dir(t); await save(cacheDir, cached());
  const rpc = async () => { throw new FeedError('no_data'); };
  const result = await recoverRuns({ runs: [run] }, { cacheDir, rpc });
  assert.equal(result.records[0].retrieved_at, fetched);
  const link = join(await dir(t), 'cache'); await symlink(cacheDir, link);
  assert.equal((await recoverRuns({ runs: [run] }, { cacheDir: link, rpc })).records.length, 0);
});
test('only latest 720 matching files are inspected', async t => {
  assert.equal(MAX_CACHE_FILES, 720);
  const cacheDir = await dir(t), old = await save(cacheDir, cached());
  await utimes(join(cacheDir, old), 1, 1);
  await Promise.all(Array.from({ length: 720 }, (_, i) => writeFile(join(cacheDir, `forecast-${i.toString(16).padStart(64, '0')}.json`), '{}')));
  const result = await recoverRuns({ runs: [run] }, { cacheDir, rpc: async () => { throw new FeedError('no_data'); } });
  assert.equal(result.records.length, 0); assert.equal(result.errors[0].error, 'no_data');
});
test('whole batch deadline isolates remaining cycles without invented dates', async t => {
  const result = await recoverRuns({ runs: [run, next] }, { cacheDir: await dir(t), timeoutMs: 25, rpc: () => new Promise(() => {}) });
  assert.deepEqual(result, { records: [], errors: [run, next].map(run_time => ({ run_time, error: 'timeout' })) });
});
test('CLI malformed input emits one safe JSON; imports do not execute main', () => {
  const result = spawnSync(process.execPath, ['services/weatherlab/recover-runs.mjs'], { cwd: new URL('../../../', import.meta.url), input: '{secret', encoding: 'utf8', timeout: 5000 });
  assert.deepEqual(JSON.parse(result.stdout), { records: [], errors: [{ run_time: null, error: 'validation_error' }] });
  assert.equal(result.stderr, '');
});
