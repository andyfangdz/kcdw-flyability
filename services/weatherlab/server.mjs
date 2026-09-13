import http from 'node:http';
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, timingSafeEqual } from 'node:crypto';
import { Feed } from './feed.mjs';
import { runWorker } from './supervisor.mjs';
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '../..');
const digest = text => createHash('sha256').update(text).digest();
export async function loadToken(path, repoRoot = REPO) {
  if (!path || !isAbsolute(path)) throw Error('token_configuration_invalid');
  const canonical = await realpath(path), root = await realpath(repoRoot).catch(() => resolve(repoRoot));
  const rel = relative(root, canonical);
  if (canonical !== resolve(path) || rel === '' || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel))) throw Error('token_configuration_invalid');
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || (stat.mode & 0o777) !== 0o600 || stat.uid !== process.getuid() || stat.size > 4096) throw Error('token_configuration_invalid');
    const value = (await file.readFile('utf8')).trim();
    if (!/^[A-Za-z0-9_-]{43,256}$/.test(value)) throw Error('token_configuration_invalid'); return value;
  } finally { await file.close(); }
}
export function createServer(feed, token) {
  const expected = digest(`Bearer ${token}`);
  const server = http.createServer({ maxHeaderSize: 8192, headersTimeout: 5000, requestTimeout: 10000 }, (req, res) => {
    const send = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', connection: 'close' }); res.end(JSON.stringify(body));
    };
    if (req.method !== 'GET') return send(405, { error: 'method_not_allowed' });
    // Exact allowlist: queries, encoded paths, arbitrary stations/coordinates fail closed.
    if (!['/health', '/v1/status', '/v1/forecast/KCDW', '/v1/last-good/KCDW'].includes(req.url)) return send(404, { error: 'not_found' });
    if (req.url === '/health') return send(200, { ok: true });
    const headers = req.rawHeaders.filter((_, i) => i % 2 === 0).filter(h => h.toLowerCase() === 'authorization');
    const authorized = timingSafeEqual(digest(req.headers.authorization ?? ''), expected);
    if (!authorized || headers.length !== 1) return send(401, { error: 'unauthorized' });
    const status = feed.status();
    if (req.url === '/v1/status') return send(200, status);
    const explicit = req.url === '/v1/last-good/KCDW';
    if (!feed.lastGood || (!explicit && !status.available)) return send(503, { error: 'forecast_unavailable', status });
    return send(200, { explicit_last_good: explicit, status, forecast: feed.lastGood.normalized });
  });
  server.maxConnections = 32; server.keepAliveTimeout = 1000;
  server.on('clientError', (_, socket) => { socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'); });
  return server;
}
async function main() {
  process.umask(0o077);
  const token = await loadToken(process.env.WEATHERLAB_TOKEN_FILE);
  const feed = await Feed.open({ cacheDir: resolve(process.env.WEATHERLAB_CACHE_DIR || resolve(HERE, 'cache')), runner: runWorker });
  const server = createServer(feed, token);
  await new Promise((yes, no) => { server.once('error', no); server.listen(8796, '127.0.0.1', yes); });
  server.on('error', () => { process.stderr.write('weatherlab: http_error\n'); });
  const refresh = () => feed.refresh().catch(() => { process.stderr.write('weatherlab: refresh_error\n'); });
  const timer = setInterval(refresh, 3600000); void refresh();
  process.stdout.write('weatherlab: listening on loopback port 8796\n');
  let closing = false;
  const stop = () => {
    if (closing) return; closing = true; clearInterval(timer);
    server.close(() => {}); server.closeAllConnections();
    // The worker watchdog stays active while shutdown awaits a bounded refresh.
    Promise.resolve(feed.inflight).finally(() => process.exit(0));
  };
  process.on('SIGINT', stop); process.on('SIGTERM', stop);
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(() => { process.stderr.write('weatherlab: startup_failed (check private configuration)\n'); process.exitCode = 1; });
