import { collectForecast, safeError } from './core.mjs';
import { createRpc, waitForCdp } from './transport.mjs';
let result;
try { await waitForCdp(); result = { ok: true, record: await collectForecast(createRpc()) }; }
catch (error) { const e = safeError(error); result = { ok: false, error: { code: e.code, requested_init_utc: e.requested_init_utc, attempted_init_utc: e.attempted_init_utc } }; }
// Exiting this isolated Node process drops CDP sockets, not the browser session.
// Never browser.close(), context.close(), page.close(), or launch().
process.stdout.write(JSON.stringify(result), () => process.exit(0));
