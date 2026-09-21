(() => {
  const section = document.querySelector('[data-coastal]');
  if (!section) return;
  const data = JSON.parse(section.dataset.coastal);
  const run = section.querySelector('[data-map-run]');
  const time = section.querySelector('[data-map-time]');
  const prev = section.querySelector('[data-map-prev]');
  const next = section.querySelector('[data-map-next]');
  const play = section.querySelector('[data-map-play]');
  const cards = [...section.querySelectorAll('[data-map-model]')];
  const frames = new Map(data.frames.map(f => [[f.model, f.run, f.valid].join('|'), f]));
  let generation = 0, timer = null, playing = false;
  const stop = () => { playing = false; clearTimeout(timer); play.textContent = 'Play evolution'; };
  const update = async () => {
    const current = ++generation, index = Number(time.value), valid = data.times[index];
    prev.disabled = index === 0;
    next.disabled = index === data.times.length - 1;
    section.querySelector('.coastal-valid').textContent = `Valid ${data.labels[index]} · initialized ${run.selectedOptions[0].textContent}`;
    const success = await Promise.all(cards.map(async card => {
      const f = frames.get([card.dataset.mapModel, run.value, valid].join('|'));
      const image = card.querySelector('img'), link = card.querySelector('.coastal-expand');
      const status = card.querySelector('.coastal-frame-status');
      // Hide the prior frame immediately: never show an old image under a new timestamp.
      image.style.visibility = 'hidden';
      link.removeAttribute('href');
      status.textContent = `F${String(f.lead).padStart(3,'0')} · Loading map…`;
      card.setAttribute('aria-busy', 'true');
      const pending = new Image();
      pending.src = f.url;
      try {
        await pending.decode();
        if (current !== generation) return false;
        const model = data.models.find(m => m.id === f.model);
        image.src = f.url;
        image.alt = `${model.name} mean sea-level pressure and 10 m wind, valid ${data.labels[index]}, initialized ${f.run}, F${String(f.lead).padStart(3,'0')}`;
        image.style.visibility = 'visible';
        link.href = f.url;
        status.replaceChildren(document.createTextNode(`F${String(f.lead).padStart(3,'0')} · `));
        const expand = document.createElement('a');
        expand.href = f.url; expand.target = '_blank'; expand.rel = 'noopener'; expand.textContent = 'Expand map ↗';
        status.append(expand);
        return true;
      } catch {
        if (current === generation) status.textContent = `F${String(f.lead).padStart(3,'0')} · Map unavailable. Choose another time or retry.`;
        return false;
      } finally {
        if (current === generation) card.removeAttribute('aria-busy');
      }
    }));
    if (current !== generation) return;
    if (playing && success.every(Boolean)) {
      if (index === data.times.length - 1) stop();
      else timer = setTimeout(() => { time.value = String(index+1); update(); }, 1400);
    } else if (playing) stop();
  };
  const change = delta => { stop(); time.value = String(Math.max(0, Math.min(data.times.length-1, Number(time.value)+delta))); update(); };
  prev.addEventListener('click', () => change(-1));
  next.addEventListener('click', () => change(1));
  run.addEventListener('change', () => { stop(); update(); });
  time.addEventListener('change', () => { stop(); update(); });
  play.addEventListener('click', () => {
    if (playing) { stop(); return; }
    playing = true; play.textContent = 'Pause';
    if (Number(time.value) === data.times.length-1) time.value = '0';
    update();
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  const dialog = section.querySelector('.coastal-dialog');
  section.addEventListener('click', event => {
    const link = event.target.closest('.coastal-expand, .coastal-frame-status a');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || typeof dialog.showModal !== 'function') return;
    event.preventDefault();
    if (!link.getAttribute('href')) return;
    stop();
    const card = link.closest('[data-map-model]');
    const f = frames.get([card.dataset.mapModel, run.value, data.times[Number(time.value)]].join('|'));
    const model = data.models.find(m => m.id === f.model);
    dialog.querySelector('h3').textContent = `${model.name} · ${model.statistic} · ${model.resolution}`;
    dialog.querySelector('[data-map-detail-time]').textContent = `${data.labels[Number(time.value)]} · initialized ${run.selectedOptions[0].textContent} · F${String(f.lead).padStart(3,'0')}`;
    const image = dialog.querySelector('[data-map-detail-image]');
    image.src = f.url; image.alt = card.querySelector('img').alt;
    image.width = f.width; image.height = f.height;
    dialog.querySelector('[data-map-download]').href = f.url;
    dialog.querySelector('[data-map-detail-legend]').replaceChildren(section.querySelector('.coastal-legend').cloneNode(true));
    dialog.showModal();
  });
  dialog.querySelector('[data-map-close]').addEventListener('click', () => dialog.close());
  section.querySelector('.coastal-controls').hidden = false;
  prev.disabled = Number(time.value) === 0;
  next.disabled = Number(time.value) === data.times.length-1;
  // Preserve native lazy loading until the maps are near the viewport.
  const observer = new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) { observer.disconnect(); update(); }
  }, { rootMargin: '250px' });
  observer.observe(section);
})();
