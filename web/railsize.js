// The right rail's width, and the handle that changes it.
//
// User, verbatim: "allow the right sidebar to be resizable". The rail is the
// one place on the board where you actually read — a thread, an evidence
// packet, a screenshot — and 480px is a guess about your screen, not a fact.
//
// The whole mechanism is one custom property: `--rail-w` on <html>, which the
// grid column reads. Nothing else in the app knows this file exists, and the
// handle lives OUTSIDE the rail element on purpose — the rail is torn down and
// rebuilt whenever a different card takes it, and a handle inside it would be
// thrown away with the rest.
//
// It persists in localStorage because a width you have to re-drag every reload
// is not a setting, it is a chore.

const KEY = 'sprint.railWidth';

export const RAIL_DEFAULT = 480;
export const RAIL_MIN = 360;      // below this the composer and thread crowd
export const RAIL_MAX = 840;      // above this you are reading a column, not a rail
export const RAIL_MAX_FRACTION = 0.6;   // …and never more than this much of the window

// Below the Fold breakpoint the rail is a fixed-width slide-over on top of the
// board, not a grid column: there is nothing to trade width against, so there
// is nothing to drag. The handle hides itself there rather than pretending.
const DOCKED = '(min-width: 1200px)';

// Two numbers, deliberately. `desired` is what you asked for; `width` is what
// fits right now. A window you made narrow caps the rail without forgetting the
// width you chose, so widening the window gives it straight back.
let desired = RAIL_DEFAULT;
let width = RAIL_DEFAULT;
let grip = null;

/** The widest the rail may be right now — the fixed cap, or 60% of the window. */
export function railMax(vw = window.innerWidth || RAIL_MAX) {
  return Math.max(RAIL_MIN, Math.min(RAIL_MAX, Math.round(vw * RAIL_MAX_FRACTION)));
}

export function clampRail(px, vw) {
  const n = Number(px);
  if (!Number.isFinite(n)) return RAIL_DEFAULT;
  return Math.round(Math.min(railMax(vw), Math.max(RAIL_MIN, n)));
}

export function railWidth() { return width; }

function load() {
  let raw = null;
  try { raw = localStorage.getItem(KEY); } catch { raw = null; }
  if (raw == null) return RAIL_DEFAULT;
  const n = Number(raw);
  return Number.isFinite(n) ? clampRail(n) : RAIL_DEFAULT;
}

function save(px) {
  try {
    if (px === RAIL_DEFAULT) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, String(px));
  } catch { /* a private window is still allowed to resize things */ }
}

/** Apply a width without writing it down — used while a drag is in flight. */
function paint(px) {
  width = px;
  document.documentElement.style.setProperty('--rail-w', px + 'px');
  if (grip) {
    grip.setAttribute('aria-valuenow', String(px));
    grip.setAttribute('aria-valuemax', String(railMax()));
    grip.title = `rail width ${px}px — drag, or arrow keys; double-click to reset`;
  }
}

export function setRailWidth(px, { persist = true } = {}) {
  desired = clampRail(px);
  paint(desired);
  if (persist) save(desired);
  return desired;
}

/**
 * Wire up the handle. Called once at boot; everything after that is events.
 *
 * The handle is a real focusable `separator`, so the width is reachable without
 * a pointer at all: arrows nudge it, shift+arrows move it in bigger steps, Home
 * puts it back. That is cheap here because the whole state is one number.
 */
export function installRailResize(gripEl) {
  grip = gripEl || null;
  desired = load();
  paint(desired);
  if (!grip) return;

  grip.setAttribute('role', 'separator');
  grip.setAttribute('aria-orientation', 'vertical');
  grip.setAttribute('aria-label', 'rail width');
  grip.setAttribute('aria-valuemin', String(RAIL_MIN));
  grip.tabIndex = 0;

  let dragging = false;

  const move = (e) => {
    if (!dragging) return;
    e.preventDefault();
    // The rail is flush against the right edge, so its width is simply how far
    // the pointer is from that edge.
    setRailWidth(window.innerWidth - e.clientX, { persist: false });
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('rail-resizing');
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', stop);
    window.removeEventListener('pointercancel', stop);
    // One write at the end of the gesture, not one per pointer sample.
    save(desired);
  };

  grip.addEventListener('pointerdown', (e) => {
    if (e.button != null && e.button !== 0) return;
    if (!window.matchMedia(DOCKED).matches) return;
    dragging = true;
    document.body.classList.add('rail-resizing');
    // Pointer capture would keep events on the grip, but the grip moves under
    // the pointer as the rail resizes; window listeners are steadier.
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
    e.preventDefault();
  });

  // Double-click the handle and it goes back to what it shipped as. The one
  // gesture nobody has to be told about.
  grip.addEventListener('dblclick', (e) => {
    e.preventDefault();
    setRailWidth(RAIL_DEFAULT);
  });

  grip.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 64 : 16;
    // Left widens: the rail is on the right, so moving its edge left gives it
    // more room. Matching the direction of the drag, not of the number.
    if (e.key === 'ArrowLeft') { e.preventDefault(); setRailWidth(width + step); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); setRailWidth(width - step); }
    else if (e.key === 'Home' || e.key === 'Enter') { e.preventDefault(); setRailWidth(RAIL_DEFAULT); }
    else if (e.key === 'End') { e.preventDefault(); setRailWidth(railMax()); }
  });

  // A window that got narrower must not leave the rail wider than the cap it
  // now has. The STORED width is untouched — shrink the window and grow it back
  // and you get your rail back.
  window.addEventListener('resize', () => {
    const capped = clampRail(desired);
    if (capped !== width) paint(capped);
    else if (grip) grip.setAttribute('aria-valuemax', String(railMax()));
  });
}
