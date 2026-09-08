import { test, before, after } from 'node:test';
import { readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
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
