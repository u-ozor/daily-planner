// ── Daily Planner — shared theme-picker widget ──────────────────────────────
// Used by morning.html and evening.html (pin.html has no theme buttons, it
// just reads cfg.theme). Injected by planner.py (run_window) into the
// // @@THEME_MENU_JS@@ marker — see ui/themes.css for why this is spliced
// in as text rather than loaded via <script src>.
//
// Shows the 5 most-recently-used themes as direct buttons and tucks the
// rest behind a "More" submenu, so the bar stays a fixed width no matter
// how many themes exist. Names/labels here must stay in sync with
// planner.py's VALID_THEMES.
const ALL_THEMES = [
  ['dark',       'Dark'],
  ['warm',       'Warm'],
  ['nebula',     'Nebula'],
  ['silvergold', 'Silver/Gold'],
  ['starlight',  'Starlight'],
  ['ember',      'Ember'],
];

function applyTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  document.querySelectorAll('.theme-btn[data-t], .theme-more-option').forEach(el =>
    el.classList.toggle('active', el.dataset.t === name)
  );
}

function renderThemeMenu(cfg) {
  const known = new Set(ALL_THEMES.map(([k]) => k));
  const seen  = new Set();
  const recent = [cfg.theme, ...(cfg.recent_themes || [])]
    .filter(t => known.has(t) && !seen.has(t) && seen.add(t));
  for (const [k] of ALL_THEMES) {
    if (recent.length >= 5) break;
    if (!seen.has(k)) { recent.push(k); seen.add(k); }
  }
  const shown = recent.slice(0, 5);
  const rest  = ALL_THEMES.filter(([k]) => !shown.includes(k));
  const label = k => (ALL_THEMES.find(([key]) => key === k) || [k, k])[1];

  const group = document.getElementById('theme-group');
  group.innerHTML = shown.map(k =>
    `<button class="theme-btn" data-t="${k}">${label(k)}</button>`
  ).join('') + (rest.length ? `
    <div class="theme-more-wrap">
      <button class="theme-btn" id="theme-more-btn" type="button">More &#9662;</button>
      <div class="theme-more-menu" id="theme-more-menu">
        ${rest.map(([k, lbl]) => `<div class="theme-more-option" data-t="${k}">${lbl}</div>`).join('')}
      </div>
    </div>` : '');

  group.querySelectorAll('.theme-btn[data-t]').forEach(btn => {
    btn.addEventListener('click', () => selectTheme(btn.dataset.t));
  });
  group.querySelectorAll('.theme-more-option').forEach(opt => {
    opt.addEventListener('click', () => {
      selectTheme(opt.dataset.t);
      document.getElementById('theme-more-menu').classList.remove('open');
    });
  });
  const moreBtn = document.getElementById('theme-more-btn');
  if (moreBtn) {
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      document.getElementById('theme-more-menu').classList.toggle('open');
    });
  }
  applyTheme(cfg.theme);
}

async function selectTheme(t) {
  applyTheme(t);
  try {
    await api().save_theme(t);
    const cfg = await api().get_config();
    renderThemeMenu(cfg);
  } catch(e) {}
}
