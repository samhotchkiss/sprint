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
  sprints: [],              // [{name, self, alive, url, needs_you, ready, chat_unread, ...}]
  needsElsewhere: 0,
  askingElsewhere: 0,       // needs_you + unread session lines, anywhere but here
  reviewElsewhere: 0,       // ready, anywhere but here
  loaded: false,
  open: false,
};

export function siblings() { return state; }

/**
 * What colour a board's square is.
 *
 * User's ruling, verbatim: "gold for needs you or a new message from the
 * session chat, green for needs review" and "half and half if both — split the
 * square diagonally".
 *
 * The bug this exists for: the indicator only ever read `needs_you`, so a board
 * sitting at needs_you 0 / ready 8 — eight finished branches waiting on a
 * verdict — showed nothing at all.
 *
 * "A new message from the session chat" is a real number off that board's own
 * database (`chat_unread`: session lines in its manager channel past its
 * last-seen cursor), not a guess from over here. A board too old to report it
 * says none, which is a missing half rather than a wrong one.
 */
export function boardMark(s) {
  if (!s) return null;
  return markOf((s.needs_you || 0) + (s.chat_unread || 0), s.ready || 0);
}

export function markOf(asking, review) {
  const gold = asking > 0;
  const green = review > 0;
  if (gold && green) return 'split';
  if (gold) return 'gold';
  if (green) return 'green';
  return null;
}

const MARK_TITLE = {
  gold: 'wants an answer from you',
  green: 'has finished work waiting on your verdict',
  split: 'wants an answer AND has finished work waiting on your verdict',
};

/** The square itself. One component, both places it is drawn. */
function markSquare(mark, { where = 'that sprint' } = {}) {
  if (!mark) return null;
  return h('span.si-mark', {
    class: `si-mark is-${mark}`,
    'aria-hidden': 'true',
    title: `${where} ${MARK_TITLE[mark]}`,
  });
}

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
    // A server too old to send these still gives us the rows, so fall back to
    // adding them up here rather than drawing nothing.
    const others = sprints.filter((s) => !s.self);
    state.askingElsewhere = body && body.asking_elsewhere != null
      ? body.asking_elsewhere
      : others.reduce((n, s) => n + (s.needs_you || 0) + (s.chat_unread || 0), 0);
    state.reviewElsewhere = body && body.review_elsewhere != null
      ? body.review_elsewhere
      : others.reduce((n, s) => n + (s.ready || 0), 0);
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
    state.needsElsewhere, state.askingElsewhere, state.reviewElsewhere, state.open,
    state.sprints.map((s) => [s.name, !!s.self, s.needs_you, s.ready, s.in_motion,
      s.chat_unread, s.url]),
  ]);
}

/** "2 need you · 1 ready · 3 in motion" — zero counts are simply not said. */
function countsText(s) {
  const parts = [];
  if (s.needs_you) parts.push(`${s.needs_you} need${s.needs_you === 1 ? 's' : ''} you`);
  if (s.ready) parts.push(`${s.ready} ready`);
  if (s.chat_unread) {
    parts.push(s.chat_unread === 1 ? '1 new from the session'
      : `${s.chat_unread} new from the session`);
  }
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
  // The title's square summarises every OTHER board: gold if any of them wants
  // an answer, green if any has finished work waiting on a verdict, split when
  // both are true somewhere on this machine.
  const mark = markOf(state.askingElsewhere, state.reviewElsewhere);
  const sig = JSON.stringify([title, multi, mark, signature()]);
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
    title: mark ? `another sprint on this machine ${MARK_TITLE[mark]}`
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
  markSquare(mark, { where: 'another sprint on this machine' }),
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
    //
    // It is the row's position and nothing else, because the server already
    // guarantees the position holds still — registry order, first seen first,
    // never re-sorted by who is waiting on you (user: "so I can count on .1
    // always going to session a"). Do not sort `sprints` here, for any reason:
    // the numbering is a server-side contract precisely so that every board on
    // the machine draws the same list in the same order.
    i < 9 ? h('span.si-key', { 'aria-hidden': 'true' }, String(i + 1)) : null,
    // …and then the square, which replaced the old needs-you dot: a dot could
    // only say "something", and a board sitting at needs_you 0 / ready 8 lit
    // nothing at all. Every row keeps its slot whether or not it has a square,
    // so the names stay in one column instead of shuffling left when a board
    // goes quiet.
    h('span.si-slot', markSquare(boardMark(s), { where: s.self ? 'this sprint' : s.name })),
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
