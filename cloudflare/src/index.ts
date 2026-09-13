import reportStyles from '../../kcdw/report.css';
import fontStyles from '../../kcdw/assets/fonts.css';
import { timingSafeEqual } from 'node:crypto';

const MAX_REPORT_BYTES = 1_000_000;
const ID = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z\.[A-Za-z0-9_-]{1,40}$/;
const SLUG = /^[a-z0-9][a-z0-9-]{0,39}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
type Health = { generated_at: string; stale_after: number; [key: string]: unknown };
type Report = {
  version: 1; run_id: string; assessed_at: string; html: string; health: Health;
  analysis: { source_collected_at: string; summary: string; days: unknown[]; [key: string]: unknown };
  changes: unknown;
};
type EventMeta = { slug: string; title: string; date: string; window: string; nav_label: string };
type EventReport = { version: 1; slug: string; run_id: string; assessed_at: string; html: string; health: Health; summary: string; event: EventMeta };
type EventIndex = { version: 1; updated_at: string; events: EventMeta[] };
type Pointer = { key: string; run_id: string; assessed_at: string; stale_after?: number; source_status?: string };
type HistoryScope = { prefix: string; linkBase: string; page: string; api: string; title: string; eyebrow: string; intro: string; current: string; currentLabel: string; slug?: string };

function reverseKey(id: string): string {
  const m = ID.exec(id);
  if (!m) throw new Error('Invalid report ID');
  const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`;
  const timestamp = Date.parse(iso);
  if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 19) !== iso.slice(0, 19)) throw new Error('Invalid report timestamp');
  return `${String(9999999999999 - timestamp).padStart(13, '0')}-${id}.json`;
}
export function reportKey(id: string): string { return `reports/${reverseKey(id)}`; }
export function eventRunKey(slug: string, id: string): string {
  if (!SLUG.test(slug)) throw new Error('Invalid event slug');
  return `events/${slug}/runs/${reverseKey(id)}`;
}
const eventPointerKey = (slug: string): string => `events/${slug}/latest.json`;

function response(body: BodyInit | null, status = 200, type = 'text/plain; charset=utf-8'): Response {
  return new Response(body, { status, headers: {
    'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
  } });
}
function json(value: unknown, status = 200): Response { return response(JSON.stringify(value), status, 'application/json'); }
function page(title: string, body: string): Response {
  return response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>${escape(title)}</title><style>${fontStyles}${reportStyles}</style></head><body>${body}</body></html>`, 200, 'text/html; charset=utf-8');
}
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
function checkTime(value: unknown, what: string): string {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value)) || Date.parse(value) > Date.now() + 60000) throw new Error(`Invalid ${what}`);
  return value;
}
function checkHtml(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('<!doctype html>') || value.length > 800000) throw new Error('Invalid HTML');
  return value;
}
function checkHealth(value: unknown, assessedAt: string): Health {
  const h = value as Partial<Health> | undefined;
  if (!h || typeof h !== 'object' || h.generated_at !== assessedAt || !Number.isFinite(h.stale_after) || (h.stale_after as number) < 60 || (h.stale_after as number) > 86400) throw new Error('Invalid health');
  return h as Health;
}
function checkEventMeta(value: unknown, slug?: string): EventMeta {
  const e = value as Partial<EventMeta> | undefined;
  if (!e || typeof e !== 'object' || typeof e.slug !== 'string' || !SLUG.test(e.slug) || (slug !== undefined && e.slug !== slug)) throw new Error('Invalid event slug');
  if (typeof e.title !== 'string' || !e.title || e.title.length > 80) throw new Error('Invalid event title');
  if (typeof e.nav_label !== 'string' || !e.nav_label || e.nav_label.length > 40) throw new Error('Invalid event label');
  if (typeof e.date !== 'string' || !DATE.test(e.date) || !Number.isFinite(Date.parse(e.date))) throw new Error('Invalid event date');
  if (typeof e.window !== 'string' || !/^\d{2}-\d{2}$/.test(e.window)) throw new Error('Invalid event window');
  const [start, end] = e.window.split('-').map(Number);
  if (!(0 <= start && start < end && end <= 24)) throw new Error('Invalid event hours');
  if (new Date(e.date).toISOString().slice(0, 10) !== e.date) throw new Error('Invalid event calendar date');
  return { slug: e.slug, title: e.title, date: e.date, window: e.window, nav_label: e.nav_label };
}
export function validateReport(value: unknown): Report {
  if (!value || typeof value !== 'object') throw new Error('Invalid report');
  const r = value as Partial<Report>;
  if (r.version !== 1 || typeof r.run_id !== 'string') throw new Error('Invalid report version or ID');
  reportKey(r.run_id);
  const assessedAt = checkTime(r.assessed_at, 'assessment time');
  const html = checkHtml(r.html);
  const health = checkHealth(r.health, assessedAt);
  if (!r.analysis || r.analysis.source_collected_at !== assessedAt || typeof r.analysis.summary !== 'string' || r.analysis.summary.length > 1500 || !Array.isArray(r.analysis.days) || r.analysis.days.length !== 7) throw new Error('Invalid analysis');
  return { version: 1, run_id: r.run_id, assessed_at: assessedAt, html, health, analysis: r.analysis, changes: r.changes ?? null };
}
export function validateEventReport(value: unknown, slug: string): EventReport {
  if (!value || typeof value !== 'object') throw new Error('Invalid event report');
  const r = value as Partial<EventReport>;
  if (r.version !== 1 || typeof r.run_id !== 'string' || r.slug !== slug) throw new Error('Invalid event report version, ID, or slug');
  eventRunKey(slug, r.run_id);
  const assessedAt = checkTime(r.assessed_at, 'assessment time');
  const html = checkHtml(r.html);
  const health = checkHealth(r.health, assessedAt);
  if (typeof r.summary !== 'string' || !r.summary || r.summary.length > 500) throw new Error('Invalid event summary');
  const event = checkEventMeta(r.event, slug);
  return { version: 1, slug, run_id: r.run_id, assessed_at: assessedAt, html, health, summary: r.summary, event };
}
export function validateEventIndex(value: unknown): EventIndex {
  if (!value || typeof value !== 'object') throw new Error('Invalid event index');
  const i = value as Partial<EventIndex>;
  if (i.version !== 1 || !Array.isArray(i.events) || i.events.length > 50) throw new Error('Invalid event index');
  const updatedAt = checkTime(i.updated_at, 'index time');
  const events = i.events.map(e => checkEventMeta(e));
  if (new Set(events.map(e => e.slug)).size !== events.length) throw new Error('Duplicate event slugs');
  return { version: 1, updated_at: updatedAt, events };
}
async function readJson<T>(bucket: R2Bucket, key: string): Promise<T | null> {
  const object = await bucket.get(key);
  if (!object) return null;
  if (object.size > MAX_REPORT_BYTES) throw new Error('Stored object exceeds limit');
  return object.json<T>();
}
async function store(bucket: R2Bucket, key: string, pointerKey: string, encoded: string, runId: string, assessedAt: string, staleAfter: number, customMetadata: Record<string, string>, sourceStatus?: string): Promise<Response> {
  const stored = await bucket.put(key, encoded, { onlyIf: { etagDoesNotMatch: '*' }, httpMetadata: { contentType: 'application/json' }, customMetadata });
  if (!stored) {
    const existing = await bucket.get(key);
    if (!existing || await existing.text() !== encoded) return json({ error: 'Report ID already contains different content' }, 409);
  }
  // Compare-and-swap prevents a slow/backfilled publisher from replacing a newer assessment.
  for (let attempt = 0; attempt < 5; attempt++) {
    const object = await bucket.get(pointerKey);
    const current = object ? await object.json<Pointer>() : null;
    if (current && (Date.parse(current.assessed_at) > Date.parse(assessedAt) ||
      (Date.parse(current.assessed_at) === Date.parse(assessedAt) && current.run_id >= runId))) {
      return json({ stored: true, latest: current.run_id === runId, run_id: runId });
    }
    const pointer: Pointer = { key, run_id: runId, assessed_at: assessedAt, stale_after: staleAfter, ...(sourceStatus ? { source_status: sourceStatus } : {}) };
    const result = await bucket.put(pointerKey, JSON.stringify(pointer), {
      onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: '*' },
      httpMetadata: { contentType: 'application/json' },
    });
    if (result) return json({ stored: true, latest: true, run_id: runId });
  }
  return json({ error: 'Report stored; latest pointer busy, retry publication' }, 503);
}
async function publish(request: Request, env: Env): Promise<Response> {
  let report: Report;
  try { report = validateReport(JSON.parse(await readBounded(request))); }
  catch (error) { return json({ error: error instanceof Error ? error.message : 'Invalid report' }, 400); }
  return store(env.REPORTS, reportKey(report.run_id), 'latest.json', JSON.stringify(report), report.run_id, report.assessed_at, report.health.stale_after,
    { run_id: report.run_id, assessed_at: report.assessed_at, summary: report.analysis.summary.slice(0, 180) });
}
async function publishEvent(request: Request, env: Env, slug: string): Promise<Response> {
  let report: EventReport;
  try { report = validateEventReport(JSON.parse(await readBounded(request)), slug); }
  catch (error) { return json({ error: error instanceof Error ? error.message : 'Invalid event report' }, 400); }
  return store(env.REPORTS, eventRunKey(slug, report.run_id), eventPointerKey(slug), JSON.stringify(report), report.run_id, report.assessed_at, report.health.stale_after,
    { run_id: report.run_id, assessed_at: report.assessed_at, summary: report.summary.slice(0, 180), slug }, String(report.health.source_status || "provenance_unverified"));
}
async function publishEventIndex(request: Request, env: Env): Promise<Response> {
  let index: EventIndex;
  try { index = validateEventIndex(JSON.parse(await readBounded(request))); }
  catch (error) { return json({ error: error instanceof Error ? error.message : 'Invalid event index' }, 400); }
  await env.REPORTS.put('events/index.json', JSON.stringify(index), { httpMetadata: { contentType: 'application/json' } });
  return json({ stored: true, events: index.events.length });
}
async function eventIndex(env: Env): Promise<EventIndex> {
  try { return (await readJson<EventIndex>(env.REPORTS, 'events/index.json')) ?? { version: 1, updated_at: '', events: [] }; }
  catch { return { version: 1, updated_at: '', events: [] }; }
}
function localDate(offsetDays = 0): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(Date.now() + offsetDays * 86400000));
}
function eventLinks(index: EventIndex, currentSlug?: string): string {
  const yesterday = localDate(-1);
  return index.events.filter(e => e.date >= yesterday).sort((a, b) => a.date.localeCompare(b.date)).slice(0, 4)
    .map(e => `<a class="nav-event" href="/events/${encodeURIComponent(e.slug)}"${e.slug === currentSlug ? ' aria-current="page"' : ''}>${escape(e.nav_label)}</a>`).join('');
}
function navigation(index: EventIndex, current: string, currentSlug?: string): string {
  const mark = (path: string) => (path === current ? ' aria-current="page"' : '');
  return `<header class="top"><div class="wrap"><a class="brand" href="/" aria-label="KCDW Flyability home"><span class="brand-symbol" aria-hidden="true"></span><span class="brand-name">KCDW<small>Flyability / Field notes</small></span></a><nav class="top-links" aria-label="Main navigation"><a href="/"${mark('/')}>Current outlook</a>${eventLinks(index, currentSlug)}<a href="/history"${mark('/history')}>History ↗</a></nav></div></header>`;
}
function health(pointer: Pointer, staleAfter: number): Response {
  const age = Math.max(0, Math.floor((Date.now() - Date.parse(pointer.assessed_at)) / 1000));
  const stale = age > staleAfter;
  return json({ generated_at: pointer.assessed_at, run_id: pointer.run_id, stale_after: staleAfter,
    age_seconds: age, stale, ...(pointer.source_status ? { source_status: pointer.source_status } : {}), status: stale ? 'stale' : (pointer.source_status || 'ok') });
}
const mainScope: HistoryScope = { prefix: 'reports/', linkBase: '/reports/', page: '/history', api: '/api/history', title: 'KCDW report history', eyebrow: 'The archive / KCDW', intro: 'Look back at how the forecast evolved. Each report preserves the evidence and outlook available at its assessment time.', current: '/history', currentLabel: 'Report history' };
function eventScope(slug: string, meta?: EventMeta): HistoryScope {
  return { prefix: `events/${slug}/runs/`, linkBase: `/events/${slug}/runs/`, page: `/events/${slug}/history`, api: `/api/events/${slug}/history`,
    title: `KCDW · ${meta?.title ?? slug} · guidance history`, eyebrow: `Dated event / ${meta?.date ?? slug}`, intro: 'Each entry preserves the ensemble guidance pulled at that time, so you can see the weather distributions change as the date approaches.',
    current: `/events/${slug}`, currentLabel: `${meta?.title ?? slug} guidance history`, slug };
}
async function history(url: URL, env: Env, scope: HistoryScope): Promise<Response> {
  const cursor = url.searchParams.get('cursor') || undefined;
  if (cursor && cursor.length > 2048) return response('Invalid cursor', 400);
  const listed = await env.REPORTS.list({ prefix: scope.prefix, limit: 50, cursor, include: ['customMetadata'] });
  const reports = listed.objects.map(o => ({ run_id: o.customMetadata?.run_id, assessed_at: o.customMetadata?.assessed_at,
    summary: o.customMetadata?.summary || '', url: `${scope.linkBase}${encodeURIComponent(o.customMetadata?.run_id || '')}` }));
  const next = listed.truncated ? listed.cursor : null;
  if (url.pathname === scope.api) return json({ reports, cursor: next });
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
  const index = await eventIndex(env);
  return page(scope.title, `<a class="skip-link" href="#main">Skip to history</a>${navigation(index, scope.current, scope.slug)}<main class="wrap" id="main"><section class="history-intro"><p class="eyebrow">${escape(scope.eyebrow)}</p><h1>${escape(scope.currentLabel)}</h1><p>${escape(scope.intro)}</p></section><div class="section-head"><div><p class="section-number">Past assessments</p><h2>Previous outlooks</h2></div><p class="muted">Newest first / All times Eastern</p></div><ol class="history-list">${rows || '<li class="history-row">No reports yet. Check back after the first assessment.</li>'}</ol>${next ? `<a class="history-more" href="${escape(scope.page)}?cursor=${encodeURIComponent(next)}">Older reports →</a>` : ''}<footer class="site-footer"><span>KCDW / Essex County Airport</span><a href="${escape(scope.current)}">Back to the current outlook ↗</a></footer></main>`);
}
async function eventsPage(env: Env): Promise<Response> {
  const index = await eventIndex(env);
  const yesterday = localDate(-1);
  const dateFormat = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'long', month: 'short', day: 'numeric', year: 'numeric' });
  const rows = [...index.events].sort((a, b) => a.date.localeCompare(b.date)).map(e => {
    const label = dateFormat.format(new Date(`${e.date}T12:00:00Z`));
    const past = e.date < yesterday;
    return `<li><a class="history-row" href="/events/${encodeURIComponent(e.slug)}"><span class="history-date">${escape(label)}<small>${escape(e.window.replace('-', ':00–'))}:00 Eastern${past ? ' · past' : ''}</small></span><p><strong>${escape(e.title)}</strong><br>Per-model weather distributions, hourly fans, and evidence ladder for this date.</p><span class="arrow" aria-hidden="true">↗</span></a></li>`;
  }).join('');
  return page('KCDW dated events', `<a class="skip-link" href="#main">Skip to events</a>${navigation(index, '/events')}<main class="wrap" id="main"><section class="history-intro"><p class="eyebrow">Dated events / KCDW</p><h1>Event outlooks</h1><p>Checkrides, trips, and other dates with a dedicated page that tracks ensemble guidance until higher-skill products take over.</p></section><ol class="history-list">${rows || '<li class="history-row">No dated events are configured.</li>'}</ol><footer class="site-footer"><span>KCDW / Essex County Airport</span><a href="/">Back to the current outlook ↗</a></footer></main>`);
}
async function serveEvent(url: URL, env: Env, slug: string, sub: string | undefined, runId: string | undefined): Promise<Response> {
  if (!SLUG.test(slug)) return response('Not found', 404);
  const index = await eventIndex(env);
  const meta = index.events.find(e => e.slug === slug);
  if (sub === 'history' || url.pathname === `/api/events/${slug}/history`) return history(url, env, eventScope(slug, meta));
  let pointer: Pointer | null;
  if (runId) {
    try { pointer = { key: eventRunKey(slug, runId), run_id: runId, assessed_at: '' }; }
    catch { return response('Not found', 404); }
  } else { pointer = await readJson<Pointer>(env.REPORTS, eventPointerKey(slug)); }
  if (!pointer) return response('No published outlook for this event', 404);
  if (sub === 'health.json' && typeof pointer.stale_after === 'number') return health(pointer, pointer.stale_after);
  const report = await readJson<EventReport>(env.REPORTS, pointer.key);
  if (!report) return response('Event outlook unavailable', 404);
  if (sub === 'health.json') return health(pointer, report.health.stale_after);
  if (url.pathname.endsWith('/analysis.json')) return response('Not found', 404);
  const html = response(report.html, 200, 'text/html; charset=utf-8');
  if (!runId) return html;
  return new HTMLRewriter().on('body', { element(element) {
    element.prepend(`<div class="archive-banner"><a href="/events/${encodeURIComponent(slug)}">Current guidance</a> / Historical guidance pulled ${escape(report.assessed_at)}</div>`, { html: true });
  } }).transform(html);
}
async function serve(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const eventPublish = /^\/api\/events\/([^/]+)\/publish$/.exec(url.pathname);
  if (url.pathname === '/api/publish' || url.pathname === '/api/events/index' || eventPublish) {
    if (!authenticated(request, env)) return response('Unauthorized', 401);
    if (request.method !== 'POST') return response('Method not allowed', 405);
    if (eventPublish) return SLUG.test(eventPublish[1]) ? publishEvent(request, env, eventPublish[1]) : response('Not found', 404);
    return url.pathname === '/api/publish' ? publish(request, env) : publishEventIndex(request, env);
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') return response('Method not allowed', 405);
  // Keep reads disabled until the intended publication access has been configured.
  if (String(env.READ_ACCESS) !== 'public' && !authenticated(request, env)) return response('Report access is not enabled', 403);
  if (url.pathname === '/history' || url.pathname === '/api/history') return history(url, env, mainScope);
  if (url.pathname === '/events') return eventsPage(env);
  if (url.pathname === '/api/events') return json(await eventIndex(env));
  const eventApi = /^\/api\/events\/([^/]+)\/history$/.exec(url.pathname);
  if (eventApi) return serveEvent(url, env, eventApi[1], 'history', undefined);
  const eventRoute = /^\/events\/([^/]+)(?:\/(health\.json|history|runs\/([^/]+)))?$/.exec(url.pathname);
  if (eventRoute) return serveEvent(url, env, eventRoute[1], eventRoute[2]?.startsWith('runs/') ? 'runs' : eventRoute[2], eventRoute[3]);
  const match = /^\/reports\/([^/]+)(\/analysis.json)?$/.exec(url.pathname);
  const currentRoute = ['/', '/index.html', '/health.json', '/api/latest'].includes(url.pathname);
  if (!match && !currentRoute) return response('Not found', 404);
  let pointer: Pointer | null;
  if (match) {
    try { pointer = { key: reportKey(match[1]), run_id: match[1], assessed_at: '' }; }
    catch { return response('Not found', 404); }
  } else { pointer = await readJson<Pointer>(env.REPORTS, 'latest.json'); }
  if (!pointer) return response('No published report', 503);
  if (url.pathname === '/health.json' && typeof pointer.stale_after === 'number') return health(pointer, pointer.stale_after);
  const report = await readJson<Report>(env.REPORTS, pointer.key);
  if (!report) return response('Report unavailable', match ? 404 : 503);
  if (url.pathname === '/api/latest' || match?.[2]) return json({ run_id: report.run_id, assessed_at: report.assessed_at, analysis: report.analysis, changes: report.changes });
  // Compatibility for pointers published before health metadata was included.
  if (url.pathname === '/health.json') return health(pointer, report.health.stale_after);
  const index = !match ? await eventIndex(env) : null;
  const legacyNavigation = `<nav style="padding:12px 20px;background:#123e35;color:#e8f2f5;font:15px system-ui"><a href="/" style="color:inherit">Current report</a> · <a href="/history" style="color:inherit">Report history</a>${match ? ` · <strong>Historical assessment: ${escape(report.assessed_at)}</strong>` : ''}</nav>`;
  const html = response(report.html, 200, 'text/html; charset=utf-8');
  return new HTMLRewriter().on('body', { element(element) {
    if (element.getAttribute('data-design') === 'field-notes') {
      if (match) element.prepend(`<div class="archive-banner"><a href="/">Current report</a> / Historical assessment: ${escape(report.assessed_at)}</div>`, { html: true });
    } else { element.prepend(legacyNavigation, { html: true }); }
  } }).on('nav.top-links a.nav-event', { element(element) { if (index) element.remove(); } })
    .on('nav.top-links', { element(element) { if (index) element.append(eventLinks(index), { html: true }); } }).transform(html);
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
