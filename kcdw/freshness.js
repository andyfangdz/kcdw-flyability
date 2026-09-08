// Freshness is a property of the assessment time, not of the last page render.
(() => {
  function refresh() {
    const now = new Date();
    const assessed = Date.parse(document.body.dataset.assessedAt);
    const stale = !Number.isFinite(assessed) || now.getTime() - assessed > Number(document.body.dataset.staleAfter) * 1000;
    const badge = document.getElementById('freshness');
    badge.className = `badge ${stale ? 'stale' : 'fresh'}`;
    badge.textContent = stale ? 'STALE · update needed' : 'CURRENT';
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(now).map(p => [p.type, p.value]));
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    const hour = Number(parts.hour) + Number(parts.minute) / 60;
    for (const cell of document.querySelectorAll('[data-window]')) {
      const [start, end] = cell.dataset.window.split('-').map(Number);
      const elapsed = cell.dataset.date < date || (cell.dataset.date === date && hour >= end);
      const current = !elapsed && cell.dataset.date === date && hour >= start;
      const state = elapsed ? 'elapsed' : (current ? 'current' : 'upcoming');
      cell.dataset.windowState = state;
      cell.classList.toggle('elapsed', elapsed);
      cell.classList.toggle('current', current && !stale);
      cell.querySelector('.window-state').textContent = elapsed ? 'Elapsed' : (current ? (stale ? 'Stale' : 'Now') : '');
    }
    for (const card of document.querySelectorAll('.day-card[data-date]')) {
      card.querySelector('.eyebrow').textContent = `${card.dataset.date === date ? 'Today · ' : ''}${card.dataset.weekday}`;
    }
  }
  refresh();
  setInterval(refresh, 30000);
  document.addEventListener('visibilitychange', refresh);
})();
