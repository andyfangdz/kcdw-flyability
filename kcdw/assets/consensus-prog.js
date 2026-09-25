(() => {
  const section = document.querySelector('[data-consensus]');
  if (!section) return;
  const data = JSON.parse(section.dataset.consensus);
  const viewer = section.querySelector('.consensus-viewer');
  const view = section.querySelector('[data-consensus-view]');
  const time = section.querySelector('[data-consensus-time]');
  const prev = section.querySelector('[data-consensus-prev]');
  const next = section.querySelector('[data-consensus-next]');
  const image = section.querySelector('[data-consensus-image]');
  const link = section.querySelector('[data-consensus-link]');
  const status = section.querySelector('.consensus-status');
  let generation = 0;
  const update = async () => {
    const current = ++generation, index = Number(time.value), frame = data.frames[index];
    const url = frame.urls[view.value];
    const lead = frame.analysis ? 'analysis (F000)' : `F${String(frame.lead).padStart(3, '0')}`;
    prev.disabled = index === 0;
    next.disabled = index === data.frames.length - 1;
    // Never show the previous chart under the new time.
    image.style.visibility = 'hidden';
    link.removeAttribute('href');
    status.textContent = `${data.views[view.value]} · valid ${frame.label} · ${lead} · loading…`;
    const pending = new Image();
    pending.src = url;
    try {
      await pending.decode();
      if (current !== generation) return;
      image.src = url;
      image.alt = `Consensus surface ${frame.analysis ? 'analysis' : 'prog'}, ${data.views[view.value]}, valid ${frame.label}`;
      image.style.visibility = 'visible';
      link.href = url;
      status.textContent = `${data.views[view.value]} · valid ${frame.label} · ${lead}`;
    } catch {
      if (current === generation) status.textContent = `${data.views[view.value]} · valid ${frame.label} · chart unavailable`;
    }
  };
  const step = delta => { time.value = String(Math.max(0, Math.min(data.frames.length - 1, Number(time.value) + delta))); update(); };
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  view.addEventListener('change', update);
  time.addEventListener('change', update);
  viewer.hidden = false;
  update();
})();
