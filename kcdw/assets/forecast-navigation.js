/* Shared forecast navigation only; no network, storage or dependencies. */
(() => {
  'use strict';
  const charts = Array.from(document.querySelectorAll('[data-sync-group="forecast"]'));
  if (!charts.length) return;
  const first = charts[0], start = Date.parse(first.dataset.axisStart), end = Date.parse(first.dataset.axisEnd);
  if (!Number.isFinite(start) || !(end > start) || charts.some(el => el.dataset.axisStart !== first.dataset.axisStart || el.dataset.axisEnd !== first.dataset.axisEnd)) return;
  const eventCenter = (Date.parse(first.dataset.eventCenter) - start) / (end - start);
  const todayStamp = Date.parse(document.querySelectorAll('[data-forecast-today]')[0]?.dataset.forecastToday);
  const todayCenter = Number.isFinite(todayStamp) ? (todayStamp - start) / (end - start) : 0;
  const hours = (end - start) / 3600000, expected = new WeakMap();
  let center = Math.max(0, Math.min(1, eventCenter)), fit = false, scheduled = false;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const hideTooltips = () => document.querySelectorAll('.chart-tooltip').forEach(output => { output.hidden = true; });
  document.addEventListener('pointerdown', hideTooltips, {passive:true});
  window.addEventListener('scroll', hideTooltips, {passive:true});
  function position() {
    for (const el of charts) {
      const x = clamp(center * el.scrollWidth - el.clientWidth / 2, 0, el.scrollWidth - el.clientWidth);
      expected.set(el, x);
      el.scrollLeft = x;
    }
  }
  function layout() {
    hideTooltips();
    scheduled = false;
    const viewport = Math.min(...charts.map(el => el.clientWidth).filter(w => w > 0));
    if (!Number.isFinite(viewport)) return;
    const width = fit ? viewport : Math.max(viewport, hours * Math.max(12, viewport / 60));
    for (const el of charts) {
      el.querySelector('.forecast-plane').style.width = `${width}px`;
      const stride = Math.max(1, Math.ceil(78 / (width * 24 / hours)));
      el.querySelectorAll('.forecast-x-axis span').forEach((tick, i) => { tick.hidden = i % stride !== 0; });
    }
    position();
  }
  charts.forEach(el => {
    el.addEventListener('scroll', () => {
      hideTooltips();
      if (Math.abs(el.scrollLeft - (expected.get(el) ?? -10000)) < 1) return;
      center = (el.scrollLeft + el.clientWidth / 2) / el.scrollWidth;
      position();
    }, {passive:true});
    const plane = el.querySelector('.forecast-plane'), output = el.closest('.forecast-frame').querySelector('.chart-tooltip');
    const series = Array.from(plane.querySelectorAll('[data-values]')).map(group => {
      try { return {group, values:JSON.parse(group.dataset.values)}; } catch (_) { return {group, values:[]}; }
    });
    function tooltip(event) {
      // Touch gestures pan the charts; never treat them as hover inspection.
      if (event.pointerType !== 'mouse' || event.buttons !== 0 || window.innerWidth <= 760 || !window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
        hideTooltips();
        return;
      }
      const rect = plane.getBoundingClientRect();
      const i = clamp(Math.round((event.clientX - rect.left) / rect.width * hours), 0, Math.round(hours));
      const stamp = new Date(start + i * 3600000).toISOString().replace('.000Z', 'Z');
      const values = series.filter(s => getComputedStyle(s.group).display !== 'none').map(s => {
        const v = s.values[i];
        return `${s.group.dataset.label}: ${typeof v === 'number' && Number.isFinite(v) ? v.toFixed(2) : 'missing'} ${s.group.dataset.unit}`;
      });
      if (!values.length) return;
      output.textContent = stamp + ' · ' + values.join(' · ');
      output.hidden = false;
    }
    plane.addEventListener('pointermove', tooltip);
    plane.addEventListener('pointercancel', hideTooltips);
    plane.addEventListener('pointerleave', () => { output.hidden = true; });
  });
  document.querySelectorAll('[data-forecast-view]').forEach(button => button.addEventListener('click', () => {
    const mode = button.dataset.forecastView;
    fit = mode === 'full';
    center = mode === 'today' ? todayCenter : fit ? .5 : eventCenter;
    layout();
  }));
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(() => {
      if (!scheduled) { scheduled = true; requestAnimationFrame(layout); }
    });
    charts.forEach(el => observer.observe(el));
  } else window.addEventListener('resize', layout);
  layout();
})();
