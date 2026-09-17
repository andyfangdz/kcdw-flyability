// Read-only bounded historical recovery. Never reads feed state or auth files.
import { constants } from 'node:fs';
import { open, readdir, lstat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { performance } from 'node:perf_hooks';
import { KCDW, MAX_BODY, FeedError, safeError, normalize, decodeRpc } from './core.mjs';
import { createRpc, waitForCdp, CDP_URL, validPage } from './transport.mjs';

export const MAX_CACHE_FILES = 720;
export const BATCH_TIMEOUT_MS = 120000;
export const CACHE_DIR = join(homedir(), '.local/state/weatherlab-feed/cache');
const filename = /^forecast-[a-f0-9]{64}\.json$/;
const canonicalDate = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value;
export const validRun = value => canonicalDate(value) && Date.parse(value) > 0 && Date.parse(value) % 21600000 === 0;
const contextFor = run => ({ ...KCDW, init_seconds: Date.parse(run) / 1000, fields: [1, 5, 2, 3], model_id: 12 });
export function validateRuns(input) {
  if (!input || typeof input !== 'object' || !Array.isArray(input.runs) || input.runs.length > 8 || !input.runs.every(validRun)) throw new FeedError('validation_error');
  return [...new Set(input.runs)];
}
export function recordFromCache(record, run) {
  if (!validRun(run) || !canonicalDate(record?.fetched_at) || !validRun(record?.requested_init_utc) || record?.attempted_init_utc !== run) throw new FeedError('cache_error');
  const forecast = normalize(record.raw, record.context);
  const requested = Date.parse(record.requested_init_utc), actual = Date.parse(run);
  if (forecast.response_init_utc !== run || requested < actual || requested - actual > 86400000 || record.fallback !== (requested !== actual) || Date.parse(record.fetched_at) < requested) throw new FeedError('cache_error');
  // Do not return arbitrary cache properties (including unvalidated context extras).
  return { context: contextFor(run), raw: record.raw, forecast, retrieved_at: record.fetched_at };
}
function deadlineGuard(timeoutMs) {
  const deadline = performance.now() + Math.min(BATCH_TIMEOUT_MS, Math.max(1, timeoutMs));
  const remaining = () => Math.max(0, deadline - performance.now());
  const check = () => { if (remaining() <= 0) throw new FeedError('timeout'); };
  const wait = async (operation, cap = BATCH_TIMEOUT_MS) => {
    check(); let timer;
    try {
      return await Promise.race([Promise.resolve().then(operation), new Promise((_, reject) => { timer = setTimeout(() => reject(new FeedError('timeout')), Math.min(cap, remaining())); })]);
    } finally { clearTimeout(timer); }
  };
  return { check, wait };
}
// Anchor Linux child reads to an opened no-follow directory, preventing path swaps.
// Only hash-named regular files are opened; even a FIFO cannot block open().
export async function readCache(runs, cacheDir = CACHE_DIR, guard = deadlineGuard(BATCH_TIMEOUT_MS)) {
  const records = new Map(); let directory;
  try {
    directory = await open(cacheDir, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    const root = `/proc/self/fd/${directory.fd}`;
    const candidates = [];
    for (const entry of await readdir(root, { withFileTypes: true })) {
      guard.check();
      if (!filename.test(entry.name) || !entry.isFile()) continue;
      try {
        const info = await lstat(join(root, entry.name));
        if (info.isFile()) candidates.push({ name: entry.name, mtime: info.mtimeMs });
      } catch { /* Concurrent GC: treat as a miss. */ }
    }
    candidates.sort((a, b) => b.mtime - a.mtime || a.name.localeCompare(b.name));
    for (const candidate of candidates.slice(0, MAX_CACHE_FILES)) {
      guard.check(); if (records.size === runs.length) break;
      let file;
      try {
        file = await open(join(root, candidate.name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        const info = await file.stat();
        if (!info.isFile() || info.size > MAX_BODY) continue;
        const bytes = Buffer.alloc(Math.min(info.size + 1, MAX_BODY + 1)); let used = 0;
        while (used < bytes.length) {
          guard.check();
          const { bytesRead } = await file.read(bytes, used, bytes.length - used, used);
          if (!bytesRead) break; used += bytesRead;
        }
        if (used !== info.size || used > MAX_BODY) continue;
        const body = bytes.subarray(0, used);
        if (`forecast-${createHash('sha256').update(body).digest('hex')}.json` !== candidate.name) continue;
        const record = JSON.parse(body.toString('utf8'));
        const run = record?.attempted_init_utc;
        if (!runs.includes(run) || records.has(run)) continue;
        records.set(run, recordFromCache(record, run));
      } catch { /* Invalid weather cache must not poison another cycle. */ }
      finally { await file?.close(); }
    }
  } catch { /* Missing/unreadable cache: try authenticated point RPC instead. */ }
  finally { await directory?.close(); }
  return records;
}
async function existingBrowserRpc() {
  await waitForCdp();
  // Refuse absent feed pages rather than letting createRpc create/navigate one.
  try {
    const response = await fetch(`${CDP_URL}/json/list`, { signal: AbortSignal.timeout(2000), redirect: 'error' });
    if (!response.ok) throw new FeedError('cdp_unavailable');
    const pages = await response.json();
    if (!Array.isArray(pages) || !pages.some(p => p.type === 'page' && validPage(p.url))) throw new FeedError('cdp_unavailable');
  } catch { throw new FeedError('cdp_unavailable'); }
  return createRpc();
}
export async function recoverRuns(input, { cacheDir = CACHE_DIR, rpc, now = Date.now, timeoutMs = BATCH_TIMEOUT_MS } = {}) {
  let runs;
  try { runs = validateRuns(input); }
  catch { return { records: [], errors: [{ run_time: null, error: 'validation_error' }] }; }
  const records = [], errors = [], guard = deadlineGuard(Number.isFinite(timeoutMs) ? timeoutMs : BATCH_TIMEOUT_MS);
  let cached = new Map(), cacheTimeout = false;
  try { cached = await guard.wait(() => readCache(runs, cacheDir, guard)); }
  catch { cacheTimeout = true; }
  let setup;
  for (const run_time of runs) {
    try {
      guard.check();
      if (cacheTimeout) throw new FeedError('timeout');
      if (cached.has(run_time)) { records.push(cached.get(run_time)); continue; }
      if (!rpc) { setup ??= existingBrowserRpc(); rpc = await guard.wait(() => setup); }
      const context = contextFor(run_time);
      const text = await guard.wait(() => rpc('Yx5Tmb', [[KCDW.latitude, KCDW.longitude], [context.init_seconds], context.fields, 12]), 30000);
      const retrieved_at = new Date(now()).toISOString();
      const raw = decodeRpc(text, 'Yx5Tmb'), forecast = normalize(raw, context);
      if (Date.parse(retrieved_at) < Date.parse(run_time)) throw new FeedError('validation_error');
      guard.check(); records.push({ context, raw, forecast, retrieved_at });
    } catch (error) { errors.push({ run_time, error: safeError(error).code }); }
  }
  return { records, errors };
}
export async function main(input = process.stdin, output = process.stdout) {
  const started = performance.now(); let text = '', result;
  try {
    await deadlineGuard(BATCH_TIMEOUT_MS).wait(async () => {
      let size = 0;
      for await (const chunk of input) {
        size += Buffer.byteLength(chunk);
        if (size > 4096) throw new FeedError('validation_error');
        text += chunk.toString('utf8');
      }
    });
    result = await recoverRuns(JSON.parse(text), { timeoutMs: BATCH_TIMEOUT_MS - (performance.now() - started) });
  } catch (error) {
    input.destroy?.();
    result = { records: [], errors: [{ run_time: null, error: error instanceof FeedError ? safeError(error).code : 'validation_error' }] };
  }
  await new Promise(resolve => output.write(JSON.stringify(result) + '\n', resolve));
  return result;
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  // createRpc owns a CDP socket; disconnect this process, never close the browser.
  await main(); process.exit(0);
}
