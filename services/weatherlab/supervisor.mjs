import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { FeedError, safeError, MAX_BODY } from './core.mjs';
// Kill only our bounded Node worker. Never send Browser.close or kill Chrome.
export function runWorker({ workerPath = fileURLToPath(new URL('./worker.mjs', import.meta.url)), timeoutMs = 100000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [workerPath], { stdio: ['ignore', 'pipe', 'ignore'], env: { PATH: process.env.PATH, HOME: process.env.HOME, LANG: 'C.UTF-8', NODE_ENV: 'production' } });
    let bytes = 0, chunks = [], failure = null;
    const fail = code => { failure ??= new FeedError(code); child.kill('SIGKILL'); };
    const timer = setTimeout(() => fail('timeout'), timeoutMs);
    child.on('error', () => { failure = new FeedError('cdp_unavailable'); });
    child.stdout.on('data', chunk => { bytes += chunk.length; if (bytes > MAX_BODY) fail('upstream_error'); else chunks.push(chunk); });
    child.on('close', code => {
      clearTimeout(timer); if (failure) return reject(failure);
      try {
        if (code !== 0) throw new FeedError('cdp_unavailable');
        const envelope = JSON.parse(Buffer.concat(chunks).toString('utf8')); chunks = [];
        if (envelope.ok !== true) {
          const error = new FeedError(envelope.error?.code);
          error.requested_init_utc = envelope.error?.requested_init_utc; error.attempted_init_utc = envelope.error?.attempted_init_utc;
          throw safeError(error);
        }
        resolve(envelope.record);
      } catch (error) { reject(safeError(error)); }
    });
  });
}
