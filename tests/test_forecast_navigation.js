/* Exercise the shipped controller with pointer and scroll events. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../kcdw/assets/forecast-navigation.js'), 'utf8');

function harness({fine=true, width=1280, today=null, payload=null}={}) {
  const eventTarget = () => ({handlers:{},addEventListener(type,fn){(this.handlers[type]??=[]).push(fn);},fire(type,event={}){for(const fn of this.handlers[type]??[])fn(event);}});
  const charts = Array.from({length:2},()=> {
    const output={hidden:true,textContent:''};
    const group={dataset:{label:'GFS / deterministic',unit:'hPa',values:JSON.stringify(payload??Array(97).fill(1012.3))}};
    const plane=Object.assign(eventTarget(),{style:{},getBoundingClientRect:()=>({left:0,width:1200}),querySelectorAll:()=>[group]});
    const chart=Object.assign(eventTarget(),{clientWidth:600,scrollWidth:1200,scrollLeft:0,dataset:{axisStart:'2026-09-22T00:00:00Z',axisEnd:'2026-09-26T00:00:00Z',eventCenter:'2026-09-24T12:00:00Z'},
      querySelector:()=>plane,querySelectorAll:()=>[],closest:()=>({querySelector:()=>output}),plane,output});
    return chart;
  });
  const buttons=Object.fromEntries(['today','full','checkride'].map(mode=>[mode,Object.assign(eventTarget(),{dataset:{forecastView:mode}})]));
  const document=Object.assign(eventTarget(),{querySelectorAll:selector=>selector==='[data-sync-group="forecast"]'?charts:selector==='.chart-tooltip'?charts.map(c=>c.output):selector==='[data-forecast-view]'?Object.values(buttons):selector==='[data-forecast-today]'&&today?[{dataset:{forecastToday:today}}]:[]});
  const window=Object.assign(eventTarget(),{innerWidth:width,matchMedia:()=>({matches:fine})});
  vm.runInNewContext(code,{atob,document,window,innerWidth:width,matchMedia:window.matchMedia,getComputedStyle:()=>({display:'inline'}),requestAnimationFrame:fn=>fn(),console});
  return {charts,document,window,buttons};
}

test('losslessly compacted gaps retain exact hover-hour alignment',()=>{
  const {charts}=harness({payload:{v:1,n:97,s:48,d:[1012.3456,null,1010.75]}});
  for(const [x,expected] of [[0,'missing'],[600,'1012.35'],[612.5,'missing'],[625,'1010.75'],[1200,'missing']]) {
    charts[0].plane.fire('pointermove',{pointerType:'mouse',buttons:0,clientX:x});
    assert.ok(charts[0].output.textContent.includes(expected+' hPa'));
  }
});

test('binary float hover preserves precision and missing interior samples',()=>{
  const bytes=Buffer.alloc(24);[1012.3456,NaN,1010.75].forEach((v,i)=>bytes.writeDoubleLE(v,i*8));
  const {charts}=harness({payload:{v:2,n:97,s:48,b:bytes.toString('base64')}});
  for(const [x,expected] of [[0,'missing'],[600,'1012.35'],[612.5,'missing'],[625,'1010.75'],[1200,'missing']]) {
    charts[0].plane.fire('pointermove',{pointerType:'mouse',buttons:0,clientX:x});
    assert.ok(charts[0].output.textContent.includes(expected+' hPa'));
  }
});

test('Today targets collection day instead of the earliest historical date',()=>{
  const {charts,buttons}=harness({today:'2026-09-24T00:00:00Z'});
  const centered=charts[0].scrollLeft;
  buttons.today.fire('click');
  assert.equal(charts[0].scrollLeft,300);
  assert.equal(charts[1].scrollLeft,300);
  buttons.full.fire('click');
  assert.ok(charts.every(c=>c.plane.style.width==='600px'));
  buttons.checkride.fire('click');
  assert.equal(charts[0].scrollLeft,centered);
});

test('legacy or invalid Today anchors safely retain start-of-range behavior',()=>{
  for(const today of [null,'not-a-date']) {
    const {charts,buttons}=harness({today});
    buttons.today.fire('click');
    assert.ok(charts.every(c=>c.scrollLeft===0));
  }
});

test('touch down, move and drag never expose chart overlays',()=>{
  for(const options of [{fine:false,width:390},{fine:true,width:1280}]) {
    const {charts}=harness(options);
    for(const type of ['pointerdown','pointermove']) charts[0].plane.fire(type,{pointerType:'touch',buttons:1,clientX:600});
    assert.ok(charts.every(c=>c.output.hidden));
  }
});

test('small screens and non-hover devices reject mouse emulation too',()=>{
  for(const options of [{fine:true,width:390},{fine:false,width:1280}]) {
    const {charts}=harness(options);
    charts[0].plane.fire('pointermove',{pointerType:'mouse',buttons:0,clientX:600});
    assert.equal(charts[0].output.hidden,true);
  }
});

test('desktop hover still reports exact data; panning clears every overlay',()=>{
  const {charts}=harness();
  charts[0].plane.fire('pointermove',{pointerType:'mouse',buttons:0,clientX:600});
  assert.equal(charts[0].output.hidden,false);
  assert.match(charts[0].output.textContent,/2026-09-24T00:00:00Z.*1012\.30 hPa/);
  charts[1].output.hidden=false;
  charts[0].scrollLeft=100;
  charts[0].fire('scroll');
  assert.ok(charts.every(c=>c.output.hidden));
  assert.equal(charts[1].scrollLeft,100);
});

test('drag, cancel and a new pointer contact dismiss existing readouts',()=>{
  const {charts,document}=harness();
  charts[0].plane.fire('pointermove',{pointerType:'mouse',buttons:0,clientX:600});
  charts[0].plane.fire('pointermove',{pointerType:'mouse',buttons:1,clientX:610});
  assert.equal(charts[0].output.hidden,true);
  charts[0].output.hidden=false;
  charts[0].plane.fire('pointercancel');
  assert.equal(charts[0].output.hidden,true);
  charts[0].output.hidden=false;
  document.fire('pointerdown',{pointerType:'touch'});
  assert.equal(charts[0].output.hidden,true);
});
