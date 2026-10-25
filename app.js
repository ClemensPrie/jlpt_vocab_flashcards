const LEVELS = ['n5', 'n4', 'n3', 'n2', 'n1'];
const PROGRESS_PREFIX = 'jlpt-vocab-skim:v1:';
const PREFS_KEY = 'jlpt-vocab-skim:prefs:v1';
const $ = (id) => document.getElementById(id);

const state = {
  level: 'n5',
  side: 'ja',
  entries: [],
  seen: new Set(),
  current: null,
  revealed: false,
  cycleComplete: false,
};

function readPrefs() {
  try {
    const value = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    if (LEVELS.includes(value.level)) state.level = value.level;
    if (value.side === 'ja' || value.side === 'en') state.side = value.side;
  } catch (_) { /* Keep defaults when storage is unavailable. */ }
  $('level-select').value = state.level;
  $('side-select').value = state.side;
}

function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify({ level: state.level, side: state.side })); } catch (_) { /* In-memory session still works. */ }
}

function readSeen() {
  try {
    const stored = JSON.parse(localStorage.getItem(PROGRESS_PREFIX + state.level) || '[]');
    const validIds = new Set(state.entries.map((entry) => entry.id));
    return new Set(stored.filter((id) => validIds.has(id)));
  } catch (_) { return new Set(); }
}

function saveSeen() {
  const validIds = new Set(state.entries.map((entry) => entry.id));
  state.seen = new Set([...state.seen].filter((id) => validIds.has(id)));
  try { localStorage.setItem(PROGRESS_PREFIX + state.level, JSON.stringify([...state.seen])); } catch (_) { /* In-memory session still works. */ }
}

function toRubyHtml(value) {
  const escaped = String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  return escaped.replace(/\{([^{}|]+)\|([^{}|]+)\}/g, '<ruby>$1<rt>$2</rt></ruby>');
}

function headwordHtml(entry) {
  if (entry.wordFurigana) return toRubyHtml(entry.wordFurigana);
  return toRubyHtml(`{${entry.word}|${entry.reading}}`);
}

function pickRandom(items) {
  return items[Math.floor(Math.random() * items.length)];
}

function beginCard({ reset = false, message = '' } = {}) {
  if (!state.entries.length) {
    state.current = null;
    render();
    return;
  }
  const unseen = state.entries.filter((entry) => !state.seen.has(entry.id));
  if (!unseen.length) {
    state.seen.clear();
    state.cycleComplete = true;
    if (!message) message = 'Level complete — a new random round has started.';
  }
  state.current = pickRandom(state.entries.filter((entry) => !state.seen.has(entry.id)));
  state.seen.add(state.current.id);
  state.revealed = false;
  if (reset) state.cycleComplete = false;
  saveSeen();
  $('cycle-note').textContent = message;
  render();
}

function renderCard() {
  const button = $('flashcard');
  const content = $('card-content');
  const action = $('card-action');
  if (!state.current) {
    button.disabled = true;
    content.innerHTML = '<span class="card-front-english">No words loaded</span>';
    action.textContent = 'Check data/levels';
    return;
  }
  button.disabled = false;
  const entry = state.current;
  const english = entry.english || entry.meanings?.[0] || '';
  if (!state.revealed && state.side === 'ja') {
    content.innerHTML = `<span class="card-front-word">${headwordHtml(entry)}</span>`;
    action.textContent = 'Tap to reveal';
    action.className = 'card-action';
    return;
  }
  if (!state.revealed && state.side === 'en') {
    content.innerHTML = `<span class="card-front-english">${toRubyHtml(english)}</span>`;
    action.textContent = 'Tap to reveal';
    action.className = 'card-action';
    return;
  }
  const answer = state.side === 'ja'
    ? `<span class="answer">${toRubyHtml(english)}</span>`
    : `<span class="card-back-word">${headwordHtml(entry)}</span>`;
  const example = entry.example || {};
  content.innerHTML = `${answer}<span class="example"><span class="example-ja">${toRubyHtml(example.furigana || example.ja || '')}</span><span class="example-en">${toRubyHtml(example.en || '')}</span></span>`;
  action.textContent = 'Tap for next word';
  action.className = 'card-action next';
}

function renderTable() {
  const body = $('word-rows');
  const unseenOnly = $('unseen-only').checked;
  const rows = state.entries
    .filter((entry) => !unseenOnly || !state.seen.has(entry.id))
    .slice()
    .sort((a, b) => a.word.localeCompare(b.word, 'ja'));
  body.innerHTML = rows.map((entry) => {
    const example = entry.example || {};
    const english = entry.english || entry.meanings?.[0] || '';
    return `<tr><td>${headwordHtml(entry)}</td><td>${toRubyHtml(english)}</td><td><span class="table-example-ja">${toRubyHtml(example.furigana || example.ja || '')}</span><span class="table-example-en">${toRubyHtml(example.en || '')}</span></td></tr>`;
  }).join('');
  $('table-empty').classList.toggle('hidden', rows.length !== 0);
}

function render() {
  const label = state.level.toUpperCase();
  $('level-eyebrow').textContent = `JLPT ${label}`;
  $('table-eyebrow').textContent = `JLPT ${label}`;
  $('progress').textContent = `${state.seen.size.toLocaleString()} / ${state.entries.length.toLocaleString()} seen`;
  renderCard();
  renderTable();
}

async function loadLevel(level) {
  state.level = level;
  state.entries = [];
  state.current = null;
  state.revealed = false;
  state.cycleComplete = false;
  $('cycle-note').textContent = '';
  $('card-content').innerHTML = '<span class="card-front-english">Loading…</span>';
  $('card-action').textContent = 'Please wait';
  $('flashcard').disabled = true;
  savePrefs();
  try {
    const response = await fetch(`./data/levels/${level}.json`, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`Could not load ${level.toUpperCase()} data (${response.status}).`);
    state.entries = await response.json();
    state.seen = readSeen();
    beginCard();
  } catch (error) {
    state.entries = [];
    state.seen = new Set();
    $('cycle-note').textContent = error.message;
    render();
  }
}

function showTable(show) {
  $('cards-view').classList.toggle('hidden', show);
  $('table-view').classList.toggle('hidden', !show);
  $('cards-tab').classList.toggle('active', !show);
  $('table-tab').classList.toggle('active', show);
  $('cards-tab').setAttribute('aria-selected', String(!show));
  $('table-tab').setAttribute('aria-selected', String(show));
}

$('flashcard').addEventListener('click', () => {
  if (!state.current) return;
  if (!state.revealed) {
    state.revealed = true;
    $('cycle-note').textContent = '';
    render();
  } else {
    beginCard();
  }
});
$('level-select').addEventListener('change', (event) => loadLevel(event.target.value));
$('side-select').addEventListener('change', (event) => {
  state.side = event.target.value;
  savePrefs();
  renderCard();
});
$('reset-button').addEventListener('click', () => {
  state.seen.clear();
  state.cycleComplete = false;
  saveSeen();
  beginCard({ reset: true, message: 'This level’s seen list has been reset.' });
});
$('cards-tab').addEventListener('click', () => showTable(false));
$('table-tab').addEventListener('click', () => showTable(true));
$('unseen-only').addEventListener('change', renderTable);

readPrefs();
loadLevel(state.level);
if ('serviceWorker' in navigator && location.protocol.startsWith('http')) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
