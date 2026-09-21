import { test, before, after } from 'node:test';
import { readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

let mf;
before(() => { mf = new Miniflare(convertV4MiniflareOptions({ workers: [{ name: "test", modules: [{ type: 'ESModule', path: 'dist/index.js' }, ...readdirSync('dist').filter(f => f.endsWith('.css')).map(f => ({ type: 'Text', path: 'dist/' + f }))], compatibilityDate: '2026-09-08', compatibilityFlags: ['nodejs_compat'], r2Buckets: ['REPORTS'], bindings: { PUBLISH_TOKEN: 'test-secret', READ_ACCESS: 'public' } }] })); });
after(async () => { await mf.dispose(); });
const request = (path, options) => mf.dispatchFetch('https://weather.example'+path,options);
const report = (stamp, assessment = '2026-09-08T12:00:00Z') => ({ version:1,run_id:stamp,assessed_at:assessment,html:'<!doctype html><html><body><h1>Report</h1></body></html>',health:{generated_at:assessment,stale_after:5400},analysis:{source_collected_at:assessment,summary:'Clear weather',days:Array(7).fill({})},changes:{} });
const publish = body => request('/api/publish',{method:'POST',headers:{Authorization:'Bearer test-secret'},body:JSON.stringify(body)});

test('publication, immutable IDs, backfill, history, and serving', async () => {
  assert.equal((await request('/')).status,503);
  assert.equal((await request('/api/publish',{method:'POST',body:'{}'})).status,401);
  const newest=report('20260908T120000Z.test');
  assert.equal((await publish(newest)).status,200);
  assert.equal((await publish(newest)).status,200);
  assert.equal((await publish({...newest,html:newest.html+'changed'})).status,409);
  const old=report('20260907T120000Z.old','2026-09-07T12:00:00Z');
  const backfilled=await (await publish(old)).json();assert.equal(backfilled.latest,false);
  assert.equal((await (await request('/api/latest')).json()).run_id,newest.run_id);
  const history=await (await request('/api/history')).json();assert.deepEqual(history.reports.map(r=>r.run_id),[newest.run_id,old.run_id]);
  const html=await (await request('/')).text();assert.match(html,/Report history/);
  const historical=await (await request('/reports/'+old.run_id)).text();assert.match(historical,/Historical assessment/);
  assert.equal((await (await request('/reports/'+old.run_id+'/analysis.json')).json()).assessed_at,old.assessed_at);
  const health=await (await request('/health.json')).json();assert.equal(health.stale,true);assert.equal(health.status,'stale');
  assert.equal((await request('/snapshot.json')).status,404);
  assert.equal((await request('/reports/not-an-id')).status,404);
  assert.equal((await request('/',{method:'DELETE'})).status,405);
  const head=await request('/',{method:'HEAD'});assert.equal(head.status,200);assert.equal(await head.text(),'');
  assert.equal(head.headers.get('Cache-Control'),'no-store');
});

test('reject malformed and oversized uploads without changing latest',async()=>{
  const prior=await (await request('/api/latest')).json();
  assert.equal((await publish({...report('20260908T130000Z.bad'),health:{generated_at:'wrong',stale_after:5400}})).status,400);
  assert.equal((await publish({...report('20260908T130000Z.big'),html:'x'.repeat(1000001)})).status,400);
  assert.equal((await (await request('/api/latest')).json()).run_id,prior.run_id);
});

test('concurrent publishers preserve the newest assessment',async()=>{
  const results=await Promise.all([publish(report('20260908T130000Z.a','2026-09-08T13:00:00Z')),publish(report('20260908T140000Z.b','2026-09-08T14:00:00Z'))]);
  assert.ok(results.every(r=>r.status===200));
  assert.equal((await (await request('/api/latest')).json()).run_id,'20260908T140000Z.b');
});

test('history follows R2 continuation cursors',async()=>{
  const bucket=await mf.getR2Bucket('REPORTS');
  for(let i=0;i<52;i++) await bucket.put('reports/9999999999999-'+i+'.json','{}',{customMetadata:{run_id:'older-'+i,assessed_at:'2020-01-01T00:00:00Z'}});
  const first=await (await request('/api/history')).json();assert.equal(first.reports.length,50);assert.ok(first.cursor);
  const second=await (await request('/api/history?cursor='+encodeURIComponent(first.cursor))).json();assert.ok(second.reports.length>0);
  assert.equal(new Set([...first.reports,...second.reports].map(r=>r.run_id)).size,56);
});

test('health uses the small publication pointer and supports older pointers',async()=>{
  const bucket=await mf.getR2Bucket('REPORTS');
  const pointer=await (await bucket.get('latest.json')).json();
  assert.equal(pointer.stale_after,5400);
  const modern=await (await request('/health.json')).json();
  assert.equal(modern.generated_at,pointer.assessed_at);
  assert.equal(modern.stale,true);
  const {stale_after,...legacy}=pointer;
  await bucket.put('latest.json',JSON.stringify(legacy));
  const compatible=await (await request('/health.json')).json();
  assert.equal(compatible.generated_at,modern.generated_at);
  assert.equal(compatible.stale_after,modern.stale_after);
  await bucket.put('latest.json',JSON.stringify(pointer));
});

const eventMeta = { slug:'commercial-checkride', title:'Commercial checkride', date:'2099-09-24', window:'08-17', nav_label:'Checkride · Sep 24' };
const eventReport = (stamp, assessment = '2026-09-12T20:00:00Z', slug = 'commercial-checkride') => ({ version:1, slug, run_id:stamp, assessed_at:assessment,
  html:'<!doctype html><html><body data-design="field-notes" data-page="event"><h1>Event</h1></body></html>', health:{generated_at:assessment,stale_after:28800},
  summary:'Per-model distributions; no calibrated flyability probability.', event:{...eventMeta, slug} });
const publishEvent = (slug, body) => request('/api/events/'+slug+'/publish',{method:'POST',headers:{Authorization:'Bearer test-secret'},body:JSON.stringify(body)});
const publishIndex = body => request('/api/events/index',{method:'POST',headers:{Authorization:'Bearer test-secret'},body:JSON.stringify(body)});

test('event pages publish, serve, archive, and appear in navigation', async () => {
  assert.equal((await request('/events/commercial-checkride')).status,404);
  assert.equal((await publishEvent('commercial-checkride', eventReport('20260912T200000Z.ev'), {})).status,200);
  assert.equal((await request('/api/events/commercial-checkride/publish',{method:'POST',body:'{}'})).status,401);
  assert.equal((await publishEvent('other-slug', eventReport('20260912T200000Z.ev'))).status,400);
  assert.equal((await publishEvent('Bad Slug', eventReport('20260912T200000Z.ev'))).status,404);
  assert.equal((await publishEvent('commercial-checkride', {...eventReport('20260912T200000Z.ev'), html:'<html>'})).status,400);
  const page=await (await request('/events/commercial-checkride')).text(); assert.match(page,/<h1>Event<\/h1>/); assert.doesNotMatch(page,/archive-banner/);
  const older=await (await publishEvent('commercial-checkride', eventReport('20260912T190000Z.old','2026-09-12T19:00:00Z'))).json(); assert.equal(older.latest,false);
  const archived=await (await request('/events/commercial-checkride/runs/20260912T190000Z.old')).text(); assert.match(archived,/Historical guidance pulled 2026-09-12T19:00:00Z/);
  const hist=await (await request('/api/events/commercial-checkride/history')).json(); assert.deepEqual(hist.reports.map(r=>r.run_id),['20260912T200000Z.ev','20260912T190000Z.old']);
  assert.match(await (await request('/events/commercial-checkride/history')).text(),/guidance history/);
  const health=await (await request('/events/commercial-checkride/health.json')).json(); assert.equal(health.run_id,'20260912T200000Z.ev'); assert.equal(health.stale_after,28800); assert.equal(health.source_status,'provenance_unverified'); assert.notEqual(health.status,'ok');
  assert.equal((await request('/events/commercial-checkride/analysis.json')).status,404);
  assert.equal((await request('/events/nope')).status,404);
  // Main history/report pointer untouched by event publication.
  assert.equal((await (await request('/api/latest')).json()).run_id,'20260908T140000Z.b');
  assert.equal((await publishIndex({version:1,updated_at:'2026-09-12T20:00:00Z',events:[eventMeta]})).status,200);
  assert.equal((await publishIndex({version:1,updated_at:'2026-09-12T20:00:00Z',events:[eventMeta,eventMeta]})).status,400);
  const events=await (await request('/api/events')).json(); assert.equal(events.events[0].slug,'commercial-checkride');
  assert.match(await (await request('/events')).text(),/Commercial checkride/);
  assert.match(await (await request('/history')).text(),/href="\/events\/commercial-checkride"/);
});

test('map uploads enforce authentication, PNG bounds, checksums and immutable serving', async () => {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6VLsAAAAASUVORK5CYII=', 'base64');
  const hash = createHash('sha256').update(png).digest('hex');
  const path = `/events/commercial-checkride/maps/${hash}.png`;
  const headers = { Authorization: 'Bearer test-secret', 'Content-Type': 'image/png' };
  const put = (body, route = '/api'+path, custom = headers) => request(route, { method: 'POST', headers: custom, body });
  assert.equal((await put(png, '/api'+path, {})).status, 401);
  assert.equal((await put('not a PNG')).status, 400);
  assert.equal((await put(Buffer.alloc(4_000_001))).status, 400);
  assert.equal((await put(png, '/api'+path.replace(hash,'0'.repeat(64)))).status, 400);
  const oversized = Buffer.from(png); oversized.writeUInt32BE(10000, 16);
  assert.equal((await put(oversized)).status, 400);
  assert.equal((await request(path)).status, 404);
  assert.deepEqual(await (await put(png)).json(), { stored: true, sha256: hash, width: 1, height: 1 });
  assert.equal((await put(png)).status, 200);
  const result = await request(path);
  assert.deepEqual(Buffer.from(await result.arrayBuffer()), png);
  assert.equal(result.headers.get('Content-Type'), 'image/png');
  assert.match(result.headers.get('Cache-Control'), /immutable/);
  const head = await request(path, { method:'HEAD' });
  assert.equal(head.headers.get('Content-Length'), String(png.length));
  assert.equal(await head.text(), '');
  assert.equal((await request(path, { headers:{'If-None-Match':`"${hash}"`} })).status, 304);
  assert.equal((await request(path.replace('commercial-checkride','another-event'))).status, 404);
  const history = await (await request('/api/events/commercial-checkride/history')).json();
  assert.equal(history.reports.length, 2);
});

test('map reads respect disabled public access', async () => {
  const privateWorker = new Miniflare(convertV4MiniflareOptions({ workers: [{ name:'private',
    modules:[{type:'ESModule',path:'dist/index.js'}, ...readdirSync('dist').filter(f=>f.endsWith('.css')).map(f=>({type:'Text',path:'dist/'+f}))],
    compatibilityDate:'2026-09-08', compatibilityFlags:['nodejs_compat'], r2Buckets:['REPORTS'], bindings:{PUBLISH_TOKEN:'private-secret',READ_ACCESS:'private'} }] }));
  try {
    const path = '/events/commercial-checkride/maps/'+'a'.repeat(64)+'.png';
    const bucket = await privateWorker.getR2Bucket('REPORTS');
    await bucket.put(path.slice(1), 'png');
    assert.equal((await privateWorker.dispatchFetch('https://weather.example'+path)).status, 403);
    const authorized = await privateWorker.dispatchFetch('https://weather.example'+path, { headers:{Authorization:'Bearer private-secret'} });
    assert.equal(authorized.status, 200);
    assert.equal(authorized.headers.get('Cache-Control'), 'private, no-store');
  } finally { await privateWorker.dispose(); }
});
