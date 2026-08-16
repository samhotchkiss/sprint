// The title switcher: when more than one sprint is running on this machine, the
// board's name becomes a dropdown of them.
//
// User, verbatim: "When there are multiple sprints going on my box, the title
// should turn into a dropdown. Also, it should show a dot when another sprint
// has something waiting on me, and when I invoke the dropdown, it should show
// the dot next to the sprint that needs me."
//
// So: one sprint on the box and the title is plain text, exactly as before —
// this is a switcher, not chrome. The dot on the title means SOMEWHERE ELSE
// wants you; this board's own needs-you pile is already on the page behind it.
// The server (`GET /api/siblings`) does the registry read, the health checks and
// the counting; this file only decides what to draw and where a click goes.

import { h, clear } from './util.js';
import { api } from './api.js';

const POLL_MS = 30000;      // no SSE: the other boards are not on our event log

const state = {
  sprints: [],              // [{name, self, alive, url, needs_you, ready, in_motion, ...}]
  needsElsewhere: 0,
  loaded: false,
  open: false,
};

export function siblings() { return state; }

/** Poll `/api/siblings`; call `onChange` only when something actually moved. */
export function startSiblings(onChange) {
  const notify = () => { try { onChange(); } catch {} };

  async function poll() {
    let body;
    try {
      body = await api.siblings();
    } catch (err) {
      // A 404 here means a server older than this page — one that never had
      // /api/siblings. That used to raise a banner asking the user to restart
      // the board; it no longer does, because the board notices its own code
      // changed and restarts itself, and the next poll (30s) finds the
      // endpoint. A failed poll leaves the last known list alone. Blanking the
      // title into plain text because one fetch timed out would be a worse lie
      // than a slightly stale menu, and this is never worth a toast.
      return;
    }
    const sprints = (body && body.sprints) || [];
    const before = signature();
    state.sprints = sprints;
    state.needsElsewhere = (body && body.needs_you_elsewhere) || 0;
    state.loaded = true;
    if (state.open && sprints.length < 2) state.open = false;
    if (signature() !== before) notify();
  }

  poll();
  setInterval(poll, POLL_MS);
  window.addEventListener('focus', poll);
  // Clicking anywhere else closes the menu. app.js hides every open `.menu` on
  // an outside click already; this keeps our own state in step with that.
  document.addEventListener('click', (e) => {
    if (!state.open) return;
    if (e.target.closest && e.target.closest('.title-wrap')) return;
    state.open = false;
    notify();
  });
  return poll;
}

function signature() {
  return JSON.stringify([
    state.needsElsewhere, state.open,
    state.sprints.map((s) => [s.name, !!s.self, s.needs_you, s.ready, s.in_motion, s.url]),
  ]);
}

/** "2 need you · 1 ready · 3 in motion" — zero counts are simply not said. */
function countsText(s) {
  const parts = [];
  if (s.needs_you) parts.push(`${s.needs_you} need${s.needs_you === 1 ? 's' : ''} you`);
  if (s.ready) parts.push(`${s.ready} ready`);
  if (s.in_motion) parts.push(`${s.in_motion} in motion`);
  return parts.length ? parts.join(' · ') : 'nothing waiting';
}

/**
 * The signed link to a sibling, on the host you are already using.
 *
 * The registry stores whatever host that board bound (its tailnet IP, or
 * loopback if tailscale was down). You are reaching THIS board on some host
 * that demonstrably works from where you are sitting, and the sibling is on the
 * same machine — so keep your host and change only the port.
 */
export function siblingHref(s) {
  if (!s || !s.url) return null;
  try {
    const u = new URL(s.url, location.href);
    u.protocol = location.protocol;
    u.hostname = location.hostname;
    return u.toString();
  } catch {
    return s.url;
  }
}

/**
 * Paint the title area. `wrap` is the header's title slot; `title` is the open
 * sprint's name. Rebuilds only when something it draws changed — the header
 * repaints on every frame and a menu that rebuilt under the cursor would close
 * itself the moment a card moved.
 */
export function renderTitle(wrap, title) {
  const sprints = state.sprints;
  const multi = sprints.length > 1;
  const others = sprints.filter((s) => !s.self);
  const elsewhere = others.some((s) => s.needs_you > 0);
  const sig = JSON.stringify([title, multi, elsewhere, signature()]);
  if (wrap._sig === sig) return;
  wrap._sig = sig;
  clear(wrap);
  wrap.classList.toggle('has-menu', multi);

  if (!multi) {
    // One sprint on this machine: the title is a title, exactly as it was.
    wrap.appendChild(h('h1.sprint-name', { id: 'sprint-title' }, title));
    return;
  }

  const btn = h('button.title-btn', {
    type: 'button',
    id: 'sprint-switch',
    'aria-haspopup': 'menu',
    'aria-expanded': state.open ? 'true' : 'false',
    title: elsewhere ? 'another sprint on this machine needs you'
      : 'switch to another sprint on this machine',
    onclick: (e) => {
      e.stopPropagation();
      state.open = !state.open;
      renderTitle(wrap, title);
      const m = wrap.querySelector('.sprint-menu');
      if (state.open && m) { const first = m.querySelector('.menu-item'); if (first) first.focus(); }
    },
  },
  h('span.title-text', title),
  elsewhere ? h('span.title-dot', { 'aria-hidden': 'true' }) : null,
  h('span.title-caret', { 'aria-hidden': 'true' }, '▾'));

  const menu = h('div.menu.sprint-menu', { role: 'menu', hidden: !state.open },
    h('p.menu-head', 'Sprints on this machine'),
    sprints.map((s, i) => h('button.menu-item.sprint-item', {
      type: 'button',
      role: 'menuitem',
      class: s.self ? 'is-current' : null,
      onclick: (e) => {
        e.stopPropagation();
        state.open = false;
        if (s.self) { renderTitle(wrap, title); return; }
        const href = siblingHref(s);
        if (href) location.href = href;       // same tab: it is the same work
      },
    },
    // Card #57: "each session has a number next to it, i can hit the number to
    // go to the session". The number is drawn even for a mouse user, because a
    // shortcut nobody can see is a shortcut nobody uses.
    i < 9 ? h('span.si-key', { 'aria-hidden': 'true' }, String(i + 1)) : null,
    h('span.si-dot', { class: s.needs_you ? 'is-on' : null, 'aria-hidden': 'true' }),
    h('span.si-body',
      h('span.si-name', s.name || s.project_root || 'sprint'),
      h('span.si-counts', countsText(s))),
    s.self ? h('span.si-here', 'here') : null)));

  wrap.appendChild(h('h1.sprint-name', { id: 'sprint-title' }, btn));
  wrap.appendChild(menu);
}

/** Escape closes the menu. Returns true if it had anything to close. */
export function closeSiblingMenu() {
  if (!state.open) return false;
  state.open = false;
  return true;
}

// ---- the keyboard's half (card #57) --------------------------------------

export function siblingMenuOpen() { return !!state.open; }

/** How many rows the menu has, i.e. how high its numbers go. */
export function siblingCount() { return state.sprints.length; }

/**
 * "." opens the switcher. Returns false when there is nothing to switch BETWEEN
 * — one board on the machine and the title is a title, not a menu, so the caller
 * says that out loud instead of opening an empty dropdown.
 */
export function openSiblingMenu() {
  if (state.sprints.length < 2) return false;
  state.open = true;
  return true;
}

/** Go to the nth sprint (0-based, the order the menu draws). Own board = stay. */
export function gotoSibling(i) {
  const s = state.sprints[i];
  if (!s || s.self) return false;
  const href = siblingHref(s);
  if (!href) return false;
  location.href = href;
  return true;
}
