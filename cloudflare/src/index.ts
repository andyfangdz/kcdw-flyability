import reportStyles from '../../kcdw/report.css';
import fontStyles from '../../kcdw/assets/fonts.css';
import { timingSafeEqual } from 'node:crypto';

const MAX_REPORT_BYTES = 1_000_000;
const ID = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z\.[A-Za-z0-9_-]{1,40}$/;
type Report = {
  version: 1; run_id: string; assessed_at: string; html: string;
  health: { generated_at: string; stale_after: number; [key: string]: unknown };
  analysis: { source_collected_at: string; summary: string; days: unknown[]; [key: string]: unknown };
  changes: unknown;
};
type Pointer = { key: string; run_id: string; assessed_at: string; stale_after?: number };

export function reportKey(id: string): string {
  const m = ID.exec(id);
  if (!m) throw new Error('Invalid report ID');
  const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`;
  const timestamp = Date.parse(iso);
  if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 19) !== iso.slice(0, 19)) throw new Error('Invalid report timestamp');
  return `reports/${String(9999999999999 - timestamp).padStart(13, '0')}-${id}.json`;
}

function response(body: BodyInit | null, status = 200, type = 'text/plain; charset=utf-8'): Response {
  return new Response(body, { status, headers: {
    'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
  } });
}
function json(value: unknown, status = 200): Response { return response(JSON.stringify(value), status, 'application/json'); }
function escape(value: string): string { return value.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!)); }
function authenticated(request: Request, env: Env): boolean {
  const secret = env.PUBLISH_TOKEN;
  if (!secret) return false;
  const actual = new TextEncoder().encode(request.headers.get('Authorization') || '');
  const expected = new TextEncoder().encode(`Bearer ${secret}`);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}
async function readBounded(request: Request): Promise<string> {
  if (!request.body) throw new Error('Empty report');
  const reader = request.body.getReader();
  const decoder = new TextDecoder();
  let size = 0, text = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_REPORT_BYTES) { await reader.cancel(); throw new Error('Report exceeds size limit'); }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}
export function validateReport(value: unknown): Report {
  if (!value || typeof value !== 'object') throw new Error('Invalid report');
  const r = value as Partial<Report>;
  if (r.version !== 1 || typeof r.run_id !== 'string') throw new Error('Invalid report version or ID');
  reportKey(r.run_id);
  if (typeof r.assessed_at !== 'string' || !Number.isFinite(Date.parse(r.assessed_at)) || Date.parse(r.assessed_at) > Date.now() + 60000) throw new Error('Invalid assessment time');
  if (typeof r.html !== 'string' || !r.html.startsWith('<!doctype html>') || r.html.length > 800000) throw new Error('Invalid HTML');
  if (!r.health || r.health.generated_at !== r.assessed_at || !Number.isFinite(r.health.stale_after) || r.health.stale_after < 60 || r.health.stale_after > 86400) throw new Error('Invalid health');
  if (!r.analysis || r.analysis.source_collected_at !== r.assessed_at || typeof r.analysis.summary !== 'string' || r.analysis.summary.length > 1500 || !Array.isArray(r.analysis.days) || r.analysis.days.length !== 7) throw new Error('Invalid analysis');
  return { version: 1, run_id: r.run_id, assessed_at: r.assessed_at, html: r.html, health: r.health, analysis: r.analysis, changes: r.changes ?? null };
}
async function readReport(bucket: R2Bucket, key: string): Promise<Report | null> {
  const object = await bucket.get(key);
  if (!object) return null;
  if (object.size > MAX_REPORT_BYTES) throw new Error('Stored report exceeds limit');
  return object.json<Report>();
}
async function latest(bucket: R2Bucket): Promise<Pointer | null> {
  const object = await bucket.get('latest.json');
  return object ? object.json<Pointer>() : null;
}
async function publish(request: Request, env: Env): Promise<Response> {
  let report: Report;
  try { report = validateReport(JSON.parse(await readBounded(request))); }
  catch (error) { return json({ error: error instanceof Error ? error.message : 'Invalid report' }, 400); }
  const key = reportKey(report.run_id);
  const encoded = JSON.stringify(report);
  const stored = await env.REPORTS.put(key, encoded, {
    onlyIf: { etagDoesNotMatch: '*' },
    httpMetadata: { contentType: 'application/json' },
    customMetadata: { run_id: report.run_id, assessed_at: report.assessed_at, summary: report.analysis.summary.slice(0, 180) },
  });
  if (!stored) {
    const existing = await env.REPORTS.get(key);
    if (!existing || await existing.text() !== encoded) return json({ error: 'Report ID already contains different content' }, 409);
  }
  // Compare-and-swap prevents a slow/backfilled publisher from replacing a newer assessment.
  for (let attempt = 0; attempt < 5; attempt++) {
    const object = await env.REPORTS.get('latest.json');
    const current = object ? await object.json<Pointer>() : null;
    if (current && (Date.parse(current.assessed_at) > Date.parse(report.assessed_at) ||
      (Date.parse(current.assessed_at) === Date.parse(report.assessed_at) && current.run_id >= report.run_id))) {
      return json({ stored: true, latest: current.run_id === report.run_id, run_id: report.run_id });
    }
    const pointer: Pointer = { key, run_id: report.run_id, assessed_at: report.assessed_at, stale_after: report.health.stale_after };
    const result = await env.REPORTS.put('latest.json', JSON.stringify(pointer), {
      onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' },
      httpMetadata: { contentType: 'application/json' },
    });
    if (result) return json({ stored: true, latest: true, run_id: report.run_id });
  }
  return json({ error: 'Report stored; latest pointer busy, retry publication' }, 503);
}
function health(pointer: Pointer, staleAfter: number): Response {
  const age = Math.max(0, Math.floor((Date.now() - Date.parse(pointer.assessed_at)) / 1000));
  const stale = age > staleAfter;
  return json({ generated_at: pointer.assessed_at, run_id: pointer.run_id, stale_after: staleAfter,
    age_seconds: age, stale, status: stale ? 'stale' : 'ok' });
}
async function history(url: URL, env: Env): Promise<Response> {
  const cursor = url.searchParams.get('cursor') || undefined;
  if (cursor && cursor.length > 2048) return response('Invalid cursor', 400);
  const page = await env.REPORTS.list({ prefix: 'reports/', limit: 50, cursor, include: ['customMetadata'] });
  const reports = page.objects.map(o => ({ run_id: o.customMetadata?.run_id, assessed_at: o.customMetadata?.assessed_at,
    summary: o.customMetadata?.summary || '', url: `/reports/${encodeURIComponent(o.customMetadata?.run_id || '')}` }));
  const next = page.truncated ? page.cursor : null;
  if (url.pathname === '/api/history') return json({ reports, cursor: next });
  const dateFormat = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric' });
  const timeFormat = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  const rows = reports.map(r => {
    const time = new Date(r.assessed_at || '');
    const valid = Number.isFinite(time.getTime());
    const date = valid ? dateFormat.format(time) : 'Past assessment';
    const hour = valid ? timeFormat.format(time) : '';
    const summary = r.summary.length >= 178 ? r.summary.replace(/\s+\S*$/, '') + '…' : r.summary;
    return `<li><a class="history-row" href="${escape(r.url)}"><span class="history-date">${escape(date)}<small>${escape(hour)}</small></span><p>${escape(summary)}</p><span class="arrow" aria-hidden="true">↗</span></a></li>`;
  }).join('');
  return response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>KCDW report history</title><style>${fontStyles}${reportStyles}</style></head><body><a class="skip-link" href="#main">Skip to report history</a><header class="top"><div class="wrap"><a class="brand" href="/" aria-label="KCDW Flyability home"><span class="brand-symbol" aria-hidden="true"></span><span class="brand-name">KCDW<small>Flyability / Field notes</small></span></a><nav class="top-links" aria-label="Main navigation"><a href="/">Current outlook</a><a href="/history" aria-current="page">History ↗</a></nav></div></header><main class="wrap" id="main"><section class="history-intro"><p class="eyebrow">The archive / KCDW</p><h1>Report history</h1><p>Look back at how the forecast evolved. Each report preserves the evidence and outlook available at its assessment time.</p></section><div class="section-head"><div><p class="section-number">Past assessments</p><h2>Previous outlooks</h2></div><p class="muted">Newest first / All times Eastern</p></div><ol class="history-list">${rows || '<li class="history-row">No reports yet. Check back after the first assessment.</li>'}</ol>${next ? `<a class="history-more" href="/history?cursor=${encodeURIComponent(next)}">Older reports →</a>` : ''}<footer class="site-footer"><span>KCDW / Essex County Airport</span><a href="/">Back to the current outlook ↗</a></footer></main></body></html>`, 200, 'text/html; charset=utf-8');
}
async function serve(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname === '/api/publish') {
    if (!authenticated(request, env)) return response('Unauthorized', 401);
    if (request.method !== 'POST') return response('Method not allowed', 405);
    return publish(request, env);
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') return response('Method not allowed', 405);
  // Keep reads disabled until the intended publication access has been configured.
  if (String(env.READ_ACCESS) !== 'public' && !authenticated(request, env)) return response('Report access is not enabled', 403);
  if (url.pathname === '/history' || url.pathname === '/api/history') return history(url, env);
  const match = /^\/reports\/([^/]+)(\/analysis.json)?$/.exec(url.pathname);
  const currentRoute = ['/', '/index.html', '/health.json', '/api/latest'].includes(url.pathname);
  if (!match && !currentRoute) return response('Not found', 404);
  let pointer: Pointer | null;
  if (match) {
    try { pointer = { key: reportKey(match[1]), run_id: match[1], assessed_at: '' }; }
    catch { return response('Not found', 404); }
  } else { pointer = await latest(env.REPORTS); }
  if (!pointer) return response('No published report', 503);
  if (url.pathname === '/health.json' && typeof pointer.stale_after === 'number') return health(pointer, pointer.stale_after);
  const report = await readReport(env.REPORTS, pointer.key);
  if (!report) return response('Report unavailable', match ? 404 : 503);
  if (url.pathname === '/api/latest' || match?.[2]) return json({ run_id: report.run_id, assessed_at: report.assessed_at, analysis: report.analysis, changes: report.changes });
  // Compatibility for pointers published before health metadata was included.
  if (url.pathname === '/health.json') return health(pointer, report.health.stale_after);
  const navigation = `<nav style="padding:12px 20px;background:#123e35;color:#e8f2f5;font:15px system-ui"><a href="/" style="color:inherit">Current report</a> · <a href="/history" style="color:inherit">Report history</a>${match ? ` · <strong>Historical assessment: ${escape(report.assessed_at)}</strong>` : ''}</nav>`;
  const html = response(report.html, 200, 'text/html; charset=utf-8');
  return new HTMLRewriter().on('body', { element(element) {
    if (element.getAttribute('data-design') === 'field-notes') {
      if (match) element.prepend(`<div class="archive-banner"><a href="/">Current report</a> / Historical assessment: ${escape(report.assessed_at)}</div>`, { html: true });
    } else { element.prepend(navigation, { html: true }); }
  } }).transform(html);
}
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const result = await serve(request, env);
      return request.method === 'HEAD' ? new Response(null, result) : result;
    } catch (error) {
      console.error(JSON.stringify({ event: 'request_failed', message: error instanceof Error ? error.message : 'Unknown error' }));
      return response('Report service temporarily unavailable', 503);
    }
  },
} satisfies ExportedHandler<Env>;
