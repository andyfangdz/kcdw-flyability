import { chromium } from 'playwright-core';
import { FeedError, KCDW, MAX_BODY } from './core.mjs';
export const CDP_URL = 'http://127.0.0.1:9233';
// systemd active is not CDP readiness: allow the supervised browser to start.
export async function waitForCdp({
  probe = async () => { try { return (await fetch(`${CDP_URL}/json/version`, { signal: AbortSignal.timeout(2000), redirect: 'error' })).ok; } catch { return false; } },
  now = Date.now,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
} = {}) {
  const deadline = now() + 30000;
  while (now() < deadline) {
    if (await probe()) return;
    await sleep(Math.min(1000, Math.max(0, deadline - now())));
  }
  throw new FeedError('cdp_unavailable');
}
export const PAGE_URL = 'https://deepmind.google.com/science/weatherlab/#kcdw-private-feed';
export function validPage(value) {
  try { const u = new URL(value); return u.origin === 'https://deepmind.google.com' && /^\/science\/weatherlab\/?$/.test(u.pathname); } catch { return false; }
}
export function validRequest(id, args) {
  if (id === 'lBl2Zc') return JSON.stringify(args) === '[]';
  if (id !== 'Yx5Tmb' || !Array.isArray(args) || args.length !== 4) return false;
  return JSON.stringify(args[0]) === JSON.stringify([KCDW.latitude, KCDW.longitude]) && Array.isArray(args[1]) && args[1].length === 1 && Number.isSafeInteger(args[1][0]) && args[1][0] > 0 && args[1][0] % 21600 === 0 && JSON.stringify(args[2]) === '[1,5,2,3]' && args[3] === 12;
}
// Exported for a sandboxed browser-context test; CSRF never appears in the result.
export async function browserRpc({ id, args, cap, timeoutMs }) {
  const fail = code => ({ ok: false, code });
  if (location.origin !== 'https://deepmind.google.com' || !/^\/science\/weatherlab\/?$/.test(location.pathname)) return fail('auth_required');
  const csrf = window.WIZ_global_data?.SNlM0e;
  if (typeof csrf !== 'string' || !csrf) return fail('auth_required');
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const body = new URLSearchParams({ 'f.req': JSON.stringify([[[id, JSON.stringify(args), null, 'generic']]]), at: csrf });
    const response = await fetch(`/science/_/Deepmind/data/batchexecute?rpcids=${id}`, { method: 'POST', credentials: 'same-origin', redirect: 'manual', body, signal: controller.signal });
    if (response.type === 'opaqueredirect' || [401, 403].includes(response.status)) return fail('auth_required');
    if (!response.ok || Number(response.headers.get('content-length')) > cap || !response.body) return fail('upstream_error');
    const reader = response.body.getReader(), decoder = new TextDecoder(); let text = '', size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        size += value.byteLength; if (size > cap) { await reader.cancel(); return fail('upstream_error'); }
        text += decoder.decode(value, { stream: true });
      }
      text += decoder.decode();
    } finally { reader.releaseLock(); }
    const packets = text.split('\n').filter(line => line.startsWith('[[')).flatMap(line => JSON.parse(line));
    const matches = packets.filter(p => Array.isArray(p) && p[0] === 'wrb.fr' && p[1] === id);
    if (matches.length !== 1) return fail('upstream_error');
    const p = matches[0];
    if (p[5] != null || typeof p[2] !== 'string') return fail([7, 16].includes(p[5]?.[0]) ? 'auth_required' : 'upstream_error');
    // Discard all framing/side packets, especially arbitrary error text.
    return { ok: true, text: JSON.stringify([['wrb.fr', id, p[2]]]) };
  } catch { return fail(controller.signal.aborted ? 'timeout' : 'upstream_error'); }
  finally { clearTimeout(timer); }
}
export function createRpc() {
  let browser, page;
  return async (id, args) => {
    if (!validRequest(id, args)) throw new FeedError('validation_error');
    try {
      if (!browser?.isConnected()) { browser = await chromium.connectOverCDP(CDP_URL, { timeout: 10000 }); page = null; }
      if (!page || page.isClosed()) {
        const context = browser.contexts()[0]; if (!context) throw new FeedError('cdp_unavailable');
        const candidates = context.pages().filter(p => validPage(p.url()));
        page = candidates.find(p => new URL(p.url()).hash === '#kcdw-private-feed') || candidates[0];
        if (!page) { page = await context.newPage(); await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded', timeout: 15000 }); }
        if (!validPage(page.url())) throw new FeedError('auth_required');
        // No credentials or page globals are returned by this wait.
        await page.waitForFunction(() => Boolean(window.WIZ_global_data), { }, { timeout: 10000 }).catch(() => {});
      }
      if (!validPage(page.url())) throw new FeedError('auth_required');
      const result = await page.evaluate(browserRpc, { id, args, cap: MAX_BODY, timeoutMs: 15000 });
      if (!result.ok) throw new FeedError(result.code);
      return result.text;
    } catch (error) {
      if (error instanceof FeedError) throw error;
      page = null; throw new FeedError('cdp_unavailable');
    }
  };
}
