// Undocumented RPC contract. Pure parsing/validation; no credentials enter Node.
export const KCDW = Object.freeze({ latitude: 40.8752, longitude: -74.2814 });
export const MAX_BODY = 2 * 1024 * 1024;
export const MAX_RUN_AGE_MS = 18 * 3600000;
const CODES = new Set(['auth_required', 'upstream_error', 'validation_error', 'cdp_unavailable', 'timeout', 'stale', 'no_data', 'cache_error']);
export class FeedError extends Error {
  constructor(code) { super(CODES.has(code) ? code : 'upstream_error'); this.code = this.message; }
}
export function safeError(error) {
  const safe = new FeedError(error instanceof FeedError ? error.code : 'upstream_error');
  for (const key of ['requested_init_utc', 'attempted_init_utc']) {
    if (typeof error?.[key] === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z$/.test(error[key])) safe[key] = error[key];
  }
  return safe;
}
function requireThat(ok, code = 'validation_error') { if (!ok) throw new FeedError(code); }
export const iso = seconds => new Date(seconds * 1000).toISOString();
export function decodeRpc(text, rpc) {
  requireThat(typeof text === 'string' && Buffer.byteLength(text) <= MAX_BODY, 'upstream_error');
  let packets;
  try { packets = text.split('\n').filter(line => line.startsWith('[[')).flatMap(line => JSON.parse(line)); }
  catch { throw new FeedError('upstream_error'); }
  const matches = packets.filter(p => Array.isArray(p) && p[0] === 'wrb.fr' && p[1] === rpc);
  requireThat(matches.length === 1, 'upstream_error'); const p = matches[0];
  if (p[5] != null || typeof p[2] !== 'string') {
    const code = p[5]?.[0]; throw new FeedError(code === 7 || code === 16 ? 'auth_required' : 'upstream_error');
  }
  try { return JSON.parse(p[2]); } catch { throw new FeedError('upstream_error'); }
}
const SPECS = { 1: ['temperature_2m', 'degC', -120, 80], 5: ['precipitation_1h', 'mm', 0, 1500], 2: ['sea_level_pressure', 'Pa', 75000, 115000], 3: ['wind_speed_10m', 'm/s', 0, 160] };
export function normalize(data, context) {
  requireThat(context?.latitude === KCDW.latitude && context?.longitude === KCDW.longitude && context?.model_id === 12 && JSON.stringify(context?.fields) === '[1,5,2,3]');
  const init = context.init_seconds;
  requireThat(Number.isSafeInteger(init) && init > 0 && init % 21600 === 0);
  requireThat(Array.isArray(data) && data.length === 4 && Array.isArray(data[0]) && data[0].length === 1 && data[0][0] === init);
  requireThat(Array.isArray(data[1]) && data[1].length === 360 && data[1].every((t, i) => Array.isArray(t) && t.length === 1 && t[0] === init + (i + 1) * 3600));
  requireThat(Array.isArray(data[2]) && data[2].length === 4 && Array.isArray(data[3]) && data[3].length === 8);
  const means = new Map(), percentiles = new Map();
  for (const row of data[2]) {
    requireThat(Array.isArray(row) && row.length === 2 && Number.isInteger(row[0]) && Object.hasOwn(SPECS, row[0]) && !means.has(row[0])); means.set(row[0], row[1]);
  }
  for (const row of data[3]) {
    requireThat(Array.isArray(row) && row.length === 3 && Number.isInteger(row[0]) && Object.hasOwn(SPECS, row[0]) && [10, 90].includes(row[1]));
    const key = `${row[0]}:${row[1]}`; requireThat(!percentiles.has(key)); percentiles.set(key, row[2]);
  }
  const fields = {};
  for (const id of [1, 5, 2, 3]) {
    const [name, unit, min, max] = SPECS[id], mean = means.get(id), p10 = percentiles.get(`${id}:10`), p90 = percentiles.get(`${id}:90`);
    for (const series of [mean, p10, p90]) requireThat(Array.isArray(series) && series.length === 360 && series.every(v => Number.isFinite(v) && v >= min && v <= max));
    requireThat(p10.every((v, i) => v <= p90[i]));
    fields[name] = { unit, mean: [...mean], p10: [...p10], p90: [...p90] };
  }
  return { source: 'Weather Lab GetForecastStatistics', model: 'WeatherNext 3', model_id: 12, ...KCDW, requested_init_utc: iso(init), response_init_utc: iso(init), valid_time_utc: data[1].map(t => iso(t[0])), fields };
}
export function discoverRuns(data, now = Date.now()) {
  requireThat(Array.isArray(data) && Array.isArray(data[1]), 'upstream_error');
  const models = data[1].filter(r => Array.isArray(r) && typeof r[0] === 'string' && r[0].toLowerCase() === 'weathernext3');
  requireThat(models.length === 1 && Array.isArray(models[0][1]), 'upstream_error');
  // The model-specific index can lag point statistics. Field 3 supplies broader
  // advertised candidates, NOT proof of model availability. collectForecast
  // must still request model 12 and bind the returned initialization exactly.
  requireThat(data[2] === undefined || Array.isArray(data[2]), 'upstream_error');
  // Protobuf int64 initialization times may be strings in either index.
  const seconds = [...models[0][1], ...(data[2] ?? [])].map(v => {
    requireThat((typeof v === 'string' && /^[1-9]\d{0,11}$/.test(v)) || (Number.isSafeInteger(v) && v > 0), 'upstream_error'); return Number(v);
  });
  const runs = [...new Set(seconds)].filter(s => Number.isSafeInteger(s) && s % 21600 === 0 && s * 1000 <= now && now - s * 1000 <= 24 * 3600000).sort((a, b) => b - a).slice(0, 2);
  requireThat(runs.length > 0, 'stale'); return runs;
}
const pause = ms => new Promise(r => setTimeout(r, ms));
export async function collectForecast(rpc, now = Date.now, sleep = pause) {
  const retry = async (id, args) => {
    for (let attempt = 0; attempt < 2; attempt++) {
      try { return decodeRpc(await rpc(id, args), id); }
      catch (error) {
        const e = safeError(error);
        if (attempt || !['upstream_error', 'cdp_unavailable', 'timeout'].includes(e.code)) throw e;
        await sleep(300);
      }
    }
  };
  const runs = discoverRuns(await retry('lBl2Zc', []), now()); let last;
  for (const init of runs) {
    try {
      const context = { ...KCDW, init_seconds: init, model_id: 12, fields: [1, 5, 2, 3] };
      const raw = await retry('Yx5Tmb', [[KCDW.latitude, KCDW.longitude], [init], context.fields, 12]);
      return { raw, context, normalized: normalize(raw, context), requested_init_utc: iso(runs[0]), attempted_init_utc: iso(init), fetched_at: new Date(now()).toISOString(), fallback: init !== runs[0] };
    } catch (error) {
      last = safeError(error); last.requested_init_utc = iso(runs[0]); last.attempted_init_utc = iso(init);
      if (last.code === 'auth_required') throw last;
    }
  }
  throw last;
}
