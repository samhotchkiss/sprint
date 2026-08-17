// Keyboard navigation — the whole board without a mouse.
//
// User, verbatim (card #57): "'.' opens the session switcher and each session
// has a number next to it, i can hit the number to go to the session … 1/2/3/4
// goes to the appropriate column in board view, and then i can arrow up down to
// select cards. when highlighting a card, it shows in the right sidebar. when I
// hit enter, focus switches to the input box in the sidebar … hitting esc takes
// me back to the session chat".
//
// Two decisions worth stating out loud, because they are what keeps this from
// being a second, parallel UI:
//
// 1. **The highlight IS focus.** Every card face is already a <button>, and
//    every List row is already `role=button tabindex=0` — so arrowing through a
//    column moves real DOM focus, and `document.activeElement` is the answer to
//    "what is highlighted". Nothing here invents a selection model, nothing
//    traps focus, and Tab still works from wherever you left off.
// 2. **A column is a set of card states, not a DOM container.** The numbers mean
//    the same four things in both layouts — 1 Waiting, 2 In progress, 3 Needs
//    you, 4 Review — because they are the Board's own columns, resolved off each
//    card's state. In Board that is literally the column you are looking at; in
//    List (which has no columns) the same number walks the same cards where they
//    sit, in reading order. So the shortcut map does not change under you when
//    you flip the layout toggle.
import { store, cardState, isConversation, BOARD_COLUMNS } from './state.js';
import { siblingMenuOpen, openSiblingMenu, closeSiblingMenu, siblingCount, gotoSibling } from './siblings.js';

/**
 * Everything on the board you can put the cursor on, in DOM order.
 *
 * Card #53 made the whole review row the click target (`.review-item`), so that
 * is the node that now carries `data-num` there — it is the same row, one
 * wrapper further out than it used to be. Card #55 then gave a multi-card branch
 * a single row of its own (`.unit-card`); it is one thing to review, so it is
 * one stop for the cursor, filed under its lead card's number.
 */
const NAV_SEL = '.card[data-num], .row[data-num], .review-item[data-num], .unit-card[data-num],'
  + ' .pill[data-num], .done-row[data-num]';

/** The four numbered columns, in the order the Board draws them. */
export const COLUMN_KEYS = BOARD_COLUMNS.map((c) => c.key);

/** card state → which numbered column it belongs to. */
const COL_OF_STATE = {};
for (const c of BOARD_COLUMNS) {
  for (const s of c.sections) for (const st of s.states) COL_OF_STATE[st] = c.key;
}

/**
 * Which column a card is DRAWN in — which is the question the cursor is asking,
 * and it is not always the same as which bucket its state falls in.
 *
 * A conversation (card #48) is the one card that is rendered somewhere its
 * state does not appear: `boardColumns()` deliberately keeps live threads out
 * of every bucket and the Needs-you column draws them as its own lower section
 * instead. So `COL_OF_STATE` has no entry for `conversation`, the fallback
 * filed every thread under Waiting, and the cursor could only reach a thread by
 * arrowing down a column it is not in — and then jumped across the board to get
 * to it. Ask the renderer's question, not the bucket's.
 */
function columnDrawnIn(card) {
  if (!card) return 'waiting';
  if (isConversation(card)) return 'needs_you';
  return COL_OF_STATE[cardState(card)] || 'waiting';
}

// Where the cursor is. `col` is one of COLUMN_KEYS, `num` the card it is on.
// Deliberately a card NUMBER and not a node: the board repaints constantly, and
// the one thing that survives a node being replaced is the identity of the thing
// it was drawn for (exactly the reasoning behind the caret's `id` in app.js).
//
// `num` is null when the cursor is on the COLUMN and not on a card in it, which
// is what happens when you press the number of a lane that has nothing in it
// (card #73). That is a real place to stand, not a broken cursor: the lane is
// held open while you are there, and the moment a card lands in it the cursor
// steps onto that card.
let nav = null;      // {col, num|null} | null

let app = null;
let keysWrap = null;

export function installKeys(theApp) {
  app = theApp;
  keysWrap = document.getElementById('keys-wrap');
  const btn = document.getElementById('keys-btn');
  if (btn) btn.addEventListener('click', () => toggleKeysSheet());
  if (keysWrap) {
    keysWrap.addEventListener('mousedown', (e) => { if (e.target === keysWrap) closeKeysSheet(); });
    const close = keysWrap.querySelector('#keys-close');
    if (close) close.addEventListener('click', () => closeKeysSheet());
  }
}

// ---- the shortcut map, so none of this is folklore ------------------------

export function keysSheetOpen() { return !!(keysWrap && !keysWrap.hidden); }

export function toggleKeysSheet() {
  if (!keysWrap) return;
  // Closing it puts the keyboard back on the highlighted card, so `?` twice in
  // a row leaves you exactly where you started.
  if (keysSheetOpen()) { closeKeysSheet(); focusNavCursor(); }
  else {
    keysWrap.hidden = false;
    const close = keysWrap.querySelector('#keys-close');
    if (close) close.focus();
  }
}

/** Escape's first rung. Returns true if it had anything to close. */
export function closeKeysSheet() {
  if (!keysSheetOpen()) return false;
  keysWrap.hidden = true;
  return true;
}

// ---- where the cursor is --------------------------------------------------

export function navCursor() { return nav; }

/** Is this the node the cursor is sitting on (or could sit on)? */
export function isNavNode(node) {
  if (!node || !node.matches) return false;
  if (node.matches(NAV_SEL)) return true;
  // A column header counts, but only the one the cursor is actually standing on
  // (card #73's empty lane). Without this, app.js's captureFocus would call it
  // ordinary chrome, drop the focus on the next repaint, and the lane would fold
  // up under the keyboard. Without the second half, any header you happened to
  // click would start claiming to be the cursor.
  return !!(nav && nav.num == null && node.matches(`.col-head[data-col="${nav.col}"]`));
}

/** The header of one numbered column, which is where an empty lane's cursor sits. */
function columnHeadNode(col) {
  const main = document.getElementById('main');
  return main ? main.querySelector(`.col-head[data-col="${col}"]`) : null;
}

/**
 * Every card currently drawn, optionally narrowed to one numbered column, in the
 * order they appear on screen. Read off the DOM on purpose: the cursor can only
 * ever land on something that is actually painted, so a card filtered out of the
 * view (a collapsed Done list, the Fold's Elsewhere strip) is simply not in the
 * ring rather than being a hole you arrow into.
 */
function navNodes(col) {
  const main = document.getElementById('main');
  if (!main) return [];
  const out = [];
  const seen = new Set();
  for (const node of main.querySelectorAll(NAV_SEL)) {
    const num = Number(node.dataset.num);
    if (!Number.isFinite(num) || seen.has(num)) continue;
    const card = store.cards.get(num);
    const key = columnDrawnIn(card);
    if (col && key !== col) continue;
    seen.add(num);
    out.push({ node, num, col: key });
  }
  return out;
}

/**
 * Draw the cursor. Called at the end of every paint, so a live update that
 * rebuilds the board underneath you leaves the highlight exactly where it was —
 * the same capture/restore discipline the caret gets in app.js, on the same
 * frame, rather than a second mechanism that can drift out of step with it.
 *
 * `refocus` is true only when the cursor actually HELD focus before the paint:
 * a board that grabs your cursor on a live update is the bug card #46 was about,
 * and this must not reintroduce it from the other end.
 */
export function paintNav({ refocus } = {}) {
  for (const n of document.querySelectorAll('.is-cursor')) n.classList.remove('is-cursor');
  for (const n of document.querySelectorAll('.is-col-cursor')) n.classList.remove('is-col-cursor');
  if (!nav) return;

  // Standing on an empty lane (card #73). If something has arrived in it since
  // the last paint, step onto it — that is the whole reason you asked for this
  // lane by number. Focus only follows if focus is what the cursor had, so a
  // card landing here can never steal the caret out of the reply box (#46).
  if (nav.num == null) {
    const arrived = navNodes(nav.col);
    if (!arrived.length) {
      const head = columnHeadNode(nav.col);
      if (!head) { nav = null; return; }
      head.closest('.col').classList.add('is-col-cursor');
      if (refocus && document.activeElement !== head) head.focus({ preventScroll: true });
      return;
    }
    nav = { col: nav.col, num: arrived[0].num };
  }

  let entry = navNodes(nav.col).find((e) => e.num === nav.num);
  if (!entry) {
    // The card moved column (an agent answered, a packet landed). Follow it
    // rather than dropping the cursor on the floor.
    entry = navNodes(null).find((e) => e.num === nav.num);
    if (entry) nav = { col: entry.col, num: entry.num };
  }
  if (!entry) {
    // It is genuinely off the board now (cancelled, filtered out). Stay in the
    // same column and take whatever is at the top of it; if the column emptied,
    // let go — never leave a cursor pointing at nothing.
    const rest = navNodes(nav.col);
    if (!rest.length) { nav = null; return; }
    entry = rest[0];
    nav = { col: entry.col, num: entry.num };
  }
  entry.node.classList.add('is-cursor');
  const col = entry.node.closest('.col');
  if (col) col.classList.add('is-col-cursor');
  if (refocus && document.activeElement !== entry.node) {
    entry.node.focus({ preventScroll: true });
  }
  if (refocus) {
    // board.js restores each column's scroll position one frame after a rebuild
    // (card #59), so the "keep the highlight in view" scroll has to come AFTER
    // that or the two fight and the cursor ends up off-screen. One frame later,
    // re-found by card number, because the node may have been replaced again.
    const num = entry.num;
    requestAnimationFrame(() => {
      const node = document.querySelector(`#main [data-num="${num}"].is-cursor`);
      if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
  }
}

/**
 * Put keyboard focus back on the highlighted card. Escape's first rung: leaving
 * the reply box has to land you somewhere you can keep arrowing from, not on the
 * document body.
 */
export function focusNavCursor() {
  if (!nav) return false;
  if (nav.num == null) {
    const head = columnHeadNode(nav.col);
    if (!head) return false;
    head.focus({ preventScroll: true });
    return true;
  }
  const entry = navNodes(nav.col).find((e) => e.num === nav.num);
  if (!entry) return false;
  entry.node.focus({ preventScroll: true });
  if (entry.node.scrollIntoView) entry.node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  return true;
}

/** Drop the highlight. Returns true if there was one. */
export function clearNav() {
  if (!nav) return false;
  nav = null;
  for (const n of document.querySelectorAll('.is-cursor')) n.classList.remove('is-cursor');
  for (const n of document.querySelectorAll('.is-col-cursor')) n.classList.remove('is-col-cursor');
  return true;
}

/**
 * Put the cursor on one card and preview it in the rail.
 *
 * "Preview" is the distinction this card turns on: the rail shows the card, and
 * focus stays on the column so the next arrow keeps going. Opening it (Enter) is
 * a different verb, and it is the one that moves the caret.
 */
function select(entry, { preview = true } = {}) {
  nav = { col: entry.col, num: entry.num };
  entry.node.focus({ preventScroll: true });
  // Card #59: each Board column scrolls on its own, so arrowing past the bottom
  // of a column has to bring the column along. `nearest` scrolls the column and
  // nothing else — the page itself never moves.
  if (entry.node.scrollIntoView) entry.node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  paintNav({ refocus: false });
  // A work unit (card #55) previews as its OUTLINE, not as its lead card — the
  // outline is what clicking it opens, and a shortcut that showed you something
  // else would be a second, quieter way to open the same row.
  if (preview) {
    if (entry.node.classList.contains('unit-card')) app.openUnit(entry.num);
    else app.openCard(entry.num, { focus: 'none' });
  }
}

/**
 * 1–4: take the column and highlight its first card (which previews it).
 *
 * An EMPTY lane is now a 44px sliver (card #73), and pressing its number opens
 * it back up and stands the cursor in it. Two calls are being made here, both
 * deliberate:
 *
 * - **The numbers never renumber.** 1/2/3/4 mean Waiting / In progress / Needs
 *   you / Review whatever the board is holding, exactly as card #57 settled it.
 *   Skipping the collapsed lanes would make the mapping depend on what is on
 *   screen this second, and a shortcut you have to look up is not a shortcut.
 * - **The lane opens rather than the press being refused.** You asked for it by
 *   name; the honest answer is to show it to you, with its own words in it
 *   ("Nothing waiting.") instead of a toast that says the same thing somewhere
 *   else. It folds back the moment the cursor leaves — press another number, or
 *   Escape.
 */
export function focusColumn(index) {
  const col = COLUMN_KEYS[index];
  if (!col) return false;
  if (app.pageOpen()) { app.toast('Close the report first — the board is behind it.'); return true; }
  const nodes = navNodes(col);
  if (!nodes.length) {
    nav = { col, num: null };
    // The sliver expands on THIS paint, and the focus lands one frame later so
    // it lands on the header the paint drew, not the one it replaced.
    app.render();
    requestAnimationFrame(() => focusNavCursor());
    return true;
  }
  select(nodes[0]);
  return true;
}

/** Arrow up/down through the column the cursor is in. No wrap: the ends are real. */
export function moveNav(delta) {
  if (!nav) return false;
  const nodes = navNodes(nav.col);
  // Standing on an empty lane, the arrows have nowhere to go — and must not
  // answer by dropping the cursor, which would fold the lane up mid-keystroke.
  // Card #73: the highlight is never left inside a sliver, because the lane the
  // cursor is in is never drawn as one.
  if (!nodes.length && nav.num == null) return true;
  if (!nodes.length) { clearNav(); return false; }
  const at = nodes.findIndex((e) => e.num === nav.num);
  const next = at < 0 ? 0 : Math.min(nodes.length - 1, Math.max(0, at + delta));
  select(nodes[next]);
  return true;
}

/** Enter: hand the card to the rail and put the caret in its reply box (#46). */
function openCursor() {
  if (!nav) return false;
  // An empty lane has nothing to open. Swallow the key rather than opening
  // whatever the rail happened to be showing.
  if (nav.num == null) return true;
  const entry = navNodes(nav.col).find((e) => e.num === nav.num);
  if (entry && entry.node.classList.contains('unit-card')) {
    // A unit opens its outline; there is no single reply box to aim at, because
    // the outline is several cards' worth of change with one verdict under it.
    app.openUnit(nav.num);
    return true;
  }
  app.openCard(nav.num, { focus: 'composer' });
  return true;
}

// ---- the one keydown handler ---------------------------------------------

/**
 * Every shortcut here is a no-op while you are typing — the only exception is
 * Escape, and Escape's ladder lives in app.js where the sheets and the rail are.
 *
 * `.` is the single deliberate exception in the other direction: it follows the
 * rule the user wrote for `/` on card #30 — a caret parked in a BLANK composer
 * does not swallow it, one keystroke into a real message does. The digits are
 * stricter than that on purpose: a message that starts with "1440" is an
 * ordinary thing to type and a message that starts with "." is not.
 */
export function handleKey(e, { isTyping, emptyTextTarget }) {
  if (e.metaKey || e.ctrlKey || e.altKey) return false;
  // Whatever the key landed ON gets first refusal. Card #53 gave every List and
  // review row its own Enter/Space ("the whole row is one click target"), and
  // that handler runs before this one does — it opens the same card with the
  // same caret this would, so the right thing here is to stand down rather than
  // do it a second time.
  if (e.defaultPrevented) return false;
  const a = document.activeElement;

  // The shortcut map. `?` is Shift+/, so it can never collide with `/` itself.
  if (e.key === '?' && !isTyping(a)) { e.preventDefault(); toggleKeysSheet(); return true; }
  if (keysSheetOpen()) return false;      // the sheet is a wall; Escape closes it

  // "." — the sprint switcher, numbered.
  if (e.key === '.' && (!isTyping(a) || emptyTextTarget(a))) {
    e.preventDefault();
    if (siblingMenuOpen()) {
      closeSiblingMenu();
      app.render();
      requestAnimationFrame(() => focusNavCursor());
      return true;
    }
    if (openSiblingMenu()) {
      app.render();
      requestAnimationFrame(() => {
        const first = document.querySelector('.sprint-menu .menu-item');
        if (first) first.focus();
      });
    } else {
      app.toast('This is the only sprint running on this machine.');
    }
    return true;
  }

  const digit = /^[1-9]$/.test(e.key) ? Number(e.key) : null;

  // With the switcher open the digits belong to it — that is what the numbers
  // beside the sprint names are for.
  //
  // Those numbers hold still now: the server hands the list back in registry
  // order, so a sprint keeps its number for as long as it is running, however
  // much it starts or stops wanting your attention (card #57). What can still
  // change it is a board going away or a new one appearing — the list only
  // holds boards you can actually reach, so a death closes the gap behind it.
  //
  // Which is why the number goes to the ROW ON SCREEN, by clicking it, rather
  // than to the nth entry of the array the page last fetched. In the window
  // between a poll landing and the next paint, the array has moved and the
  // drawn numbers have not. Clicking the row you can see cannot be wrong: the
  // number is printed on it.
  if (siblingMenuOpen()) {
    if (digit && digit <= siblingCount()) {
      e.preventDefault();
      const rows = document.querySelectorAll('.sprint-menu .menu-item');
      const row = rows[digit - 1];
      if (row) { row.click(); return true; }
      closeSiblingMenu();
      gotoSibling(digit - 1);
      app.render();
      return true;
    }
    return false;
  }

  if (isTyping(a)) return false;          // from here down, typing wins outright

  if (digit && digit <= COLUMN_KEYS.length) { e.preventDefault(); return focusColumn(digit - 1); }

  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!nav) return false;               // no cursor: arrows scroll, as they always did
    e.preventDefault();
    return moveNav(e.key === 'ArrowDown' ? 1 : -1);
  }

  if (e.key === 'Enter' && nav && isNavNode(a)) { e.preventDefault(); return openCursor(); }

  return false;
}
