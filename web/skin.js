// Calm ↔ Chaos: which skin the board wears, and the one sound Chaos makes.
//
// The skin is a class on <html> and nothing else. Every colour, radius, font and
// animation the interface uses is a custom property (see styles.css), so the
// whole switch is `documentElement.className` — no re-render, no reflow of the
// data, no second copy of any component. The information architecture and the
// copy are identical in both skins; only the surface moves.
//
// It persists in localStorage under `sprint.skin`, the same way the List/Board
// choice persists, and defaults to Calm — a board you have never touched should
// open quiet.
import { audioContext } from './notify.js';

const SKIN_KEY = 'sprint.skin';
const SKINS = ['calm', 'chaos'];

let current = 'calm';

export function skin() { return current; }
export function isChaos() { return current === 'chaos'; }

/** Read the stored choice and put it on <html>. Anything unrecognised is Calm. */
export function loadSkin() {
  let v = null;
  try { v = localStorage.getItem(SKIN_KEY); } catch { v = null; }
  apply(SKINS.includes(v) ? v : 'calm');
  return current;
}

/** Returns true if the skin actually changed. */
export function setSkin(v) {
  const next = SKINS.includes(v) ? v : 'calm';
  if (next === current) return false;
  apply(next);
  try { localStorage.setItem(SKIN_KEY, next); } catch {}
  return true;
}

function apply(v) {
  current = v;
  const root = document.documentElement;
  root.classList.toggle('skin-calm', v === 'calm');
  root.classList.toggle('skin-chaos', v === 'chaos');
}

/**
 * The 8-bit blip. Square wave, 660 Hz stepping to ×1.5 at 60 ms, gain 0.05 with
 * an exponential ramp to silence over 130 ms — a menu confirm, not a klaxon.
 *
 * Chaos only. Calm is silent apart from the one soft chime on flips to
 * needs_you / ready, which lives in notify.js and belongs to both skins.
 */
export function blip() {
  if (!isChaos()) return;
  const ac = audioContext();
  if (!ac) return;
  if (ac.state === 'suspended') ac.resume().catch(() => {});
  try {
    const t = ac.currentTime;
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.type = 'square';
    osc.frequency.setValueAtTime(660, t);
    osc.frequency.setValueAtTime(660 * 1.5, t + 0.06);
    gain.gain.setValueAtTime(0.05, t);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.13);
    osc.connect(gain);
    gain.connect(ac.destination);
    osc.start(t);
    osc.stop(t + 0.14);
  } catch {}
}

/**
 * One blip per interaction, on the way down, from anywhere in the app.
 *
 * A capture-phase pointerdown on the document is deliberately the whole wiring:
 * every button, row, chip and pill in this UI is a real <button> or carries
 * role="button", so there is exactly one place that decides what "an
 * interaction" is, and no call site has to remember to make a noise.
 */
export function installBlip() {
  document.addEventListener('pointerdown', (e) => {
    if (!isChaos()) return;
    const t = e.target;
    if (!t || !t.closest) return;
    if (!t.closest('button, [role="button"], a[href], label.switch, input, textarea, select')) return;
    blip();
  }, true);
}

/** Wire the Calm/Chaos segmented control. `onChange` re-paints the board. */
export function installSkinToggle(seg, onChange) {
  if (!seg) return;
  const paint = () => {
    for (const btn of seg.querySelectorAll('.seg-btn')) {
      const on = btn.dataset.skin === current;
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  };
  for (const btn of seg.querySelectorAll('.seg-btn')) {
    btn.addEventListener('click', () => {
      // The blip is Chaos's own voice, so the arrival into Chaos gets one and
      // the exit into Calm does not — the last thing you hear is the skin you
      // asked for.
      if (setSkin(btn.dataset.skin)) { paint(); blip(); if (onChange) onChange(); }
    });
  }
  paint();
}
