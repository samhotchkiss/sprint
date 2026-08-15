// One tab badge + one soft chime when a card flips to needs_you / ready.
// No audio file, no counters, no repeats.

const BASE_TITLE = 'sprint';
const FAVICON_BASE = document.getElementById('favicon') && document.getElementById('favicon').getAttribute('href');

function faviconWithDot() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#3a6ea5"/>
<rect x="14" y="16" width="9" height="32" rx="3" fill="white"/>
<rect x="28" y="16" width="9" height="22" rx="3" fill="white" opacity=".75"/>
<rect x="42" y="16" width="9" height="14" rx="3" fill="white" opacity=".5"/>
<circle cx="49" cy="49" r="13" fill="#0f1114"/><circle cx="49" cy="49" r="10" fill="#e0a11a"/></svg>`;
  return 'data:image/svg+xml,' + encodeURIComponent(svg);
}

let badged = false;
let ctx = null;
let lastChime = 0;
let armed = false;   // don't chime for the initial board load

export function armNotifications() { armed = true; }

export function setBadge(on) {
  if (on === badged) return;
  badged = on;
  document.title = on ? '• ' + BASE_TITLE : BASE_TITLE;
  const link = document.getElementById('favicon');
  if (link) link.setAttribute('href', on ? faviconWithDot() : FAVICON_BASE);
}

export function clearBadge() { setBadge(false); }

function audio() {
  if (ctx) return ctx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  try { ctx = new Ctor(); } catch { ctx = null; }
  return ctx;
}

/** One AudioContext for the whole page — the Chaos blip borrows this one rather
 *  than opening a second, which browsers count against the same gesture unlock. */
export function audioContext() { return audio(); }

/** Two soft sine notes, ~0.5s, quiet. Browsers need a prior gesture; we unlock on first click. */
export function chime() {
  const now = Date.now();
  if (now - lastChime < 2500) return;
  lastChime = now;
  const ac = audio();
  if (!ac) return;
  if (ac.state === 'suspended') { ac.resume().catch(() => {}); }
  const t0 = ac.currentTime + 0.01;
  const gain = ac.createGain();
  gain.connect(ac.destination);
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(0.07, t0 + 0.04);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.75);
  [[587.33, 0], [880, 0.13]].forEach(([freq, delay]) => {
    const osc = ac.createOscillator();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, t0 + delay);
    osc.connect(gain);
    osc.start(t0 + delay);
    osc.stop(t0 + 0.8);
  });
}

/** Called when cards flip into an attention state. */
export function attention() {
  if (!armed) return;
  chime();
  if (document.hidden || !document.hasFocus()) setBadge(true);
}

export function installNotifications() {
  const unlock = () => { const ac = audio(); if (ac && ac.state === 'suspended') ac.resume().catch(() => {}); };
  window.addEventListener('pointerdown', unlock, { once: true });
  window.addEventListener('keydown', unlock, { once: true });
  window.addEventListener('focus', clearBadge);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) clearBadge(); });
}
