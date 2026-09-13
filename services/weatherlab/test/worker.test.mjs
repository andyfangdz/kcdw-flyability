import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

async function child(t, source) {
  const root = fileURLToPath(new URL('../.test-tmp/', import.meta.url)); await mkdir(root, { recursive: true });
  const dir = await mkdtemp(join(root, 'worker-')); t.after(() => rm(dir, { recursive: true, force: true }));
  const path = join(dir, 'child.mjs'); await writeFile(path, source); return path;
}
test('worker is hard-bounded, sanitized, and may recover on the next invocation', async t => {
  const { runWorker } = await import('../supervisor.mjs');
  const hung = await child(t, 'setInterval(() => {}, 1000);');
  const start = Date.now(); await assert.rejects(runWorker({ workerPath: hung, timeoutMs: 120 }), e => e.code === 'timeout'); assert.ok(Date.now() - start < 3000);
  const noisy = await child(t, 'console.log("SECRET UPSTREAM ERROR");');
  await assert.rejects(runWorker({ workerPath: noisy }), e => e.code === 'upstream_error' && !e.message.includes('SECRET'));
  const oversized = await child(t, 'process.stdout.write("x".repeat(3*1024*1024));');
  await assert.rejects(runWorker({ workerPath: oversized }), e => e.code === 'upstream_error');
  const good = await child(t, 'process.stdout.write(JSON.stringify({ok:true,record:{test_only:true}}));');
  assert.deepEqual(await runWorker({ workerPath: good }), { test_only: true });
});
test('browser startup readiness waits for actual CDP and times out without hanging', async () => {
  const { waitForCdp } = await import('../transport.mjs');
  let time = 0, calls = 0;
  await waitForCdp({ probe: async () => ++calls >= 3, now: () => time, sleep: async ms => { time += ms; } });
  assert.equal(calls, 3); assert.equal(time, 2000);
  time = 0;
  await assert.rejects(waitForCdp({ probe: async () => false, now: () => time, sleep: async ms => { time += ms; } }), e => e.code === 'cdp_unavailable');
  assert.equal(time, 30000);
});
test('browser RPC distinguishes generic HTTP errors, auth errors, and successful data without leaking CSRF', async () => {
  const { browserRpc } = await import('../transport.mjs');
  const { runInNewContext } = await import('node:vm');
  for (const [status, expected] of [[400, 'upstream_error'], [401, 'auth_required'], [403, 'auth_required'], [500, 'upstream_error'], [200, null]]) {
    let sent = false;
    const result = await runInNewContext(`(${browserRpc.toString()})({id:'lBl2Zc',args:[],cap:10000,timeoutMs:1000})`, {
      location: { origin: 'https://deepmind.google.com', pathname: '/science/weatherlab' },
      window: { WIZ_global_data: { SNlM0e: 'synthetic-private-csrf' } },
      URLSearchParams, AbortController, TextDecoder, setTimeout, clearTimeout,
      fetch: async (url, options) => {
        sent = true; assert.equal(options.credentials, 'same-origin'); assert.equal(options.body.get('at'), 'synthetic-private-csrf');
        return new Response(JSON.stringify([['wrb.fr', 'lBl2Zc', '[]']]), { status });
      },
    });
    assert.equal(sent, true); assert.equal(result.ok, expected === null);
    if (expected) assert.equal(result.code, expected); else assert.equal(result.text, '[["wrb.fr","lBl2Zc","[]"]]');
    assert.ok(!JSON.stringify(result).includes('synthetic-private-csrf'));
  }
});
test('transport executes only allowlisted requests on the exact Weather Lab origin', async () => {
  const { validPage, validRequest } = await import('../transport.mjs');
  assert.equal(validPage('https://deepmind.google.com/science/weatherlab/'), true);
  for (const url of ['http://deepmind.google.com/science/weatherlab', 'https://evil.test/science/weatherlab', 'https://deepmind.google.com/science/weatherlab-evil']) assert.equal(validPage(url), false);
  assert.equal(validRequest('lBl2Zc', []), true);
  assert.equal(validRequest('Yx5Tmb', [[40.8752, -74.2814], [1789171200], [1, 5, 2, 3], 12]), true);
  for (const [id, args] of [['other', []], ['lBl2Zc', [1]], ['Yx5Tmb', [[0, 0], [1789171200], [1, 5, 2, 3], 12]]]) assert.equal(validRequest(id, args), false);
});
