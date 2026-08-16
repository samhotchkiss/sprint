// Wiring: boot, live transport, optimistic actions, render loop.
import { h, clear, $, debounce, tickTimes, uid, firstLine } from './util.js';
import {
  api, ApiError, NetworkError, initAuth, onServerGeneration, onServerStale, serverIsStale,
} from './api.js';
import { Live } from './live.js';
import {
  store, applyBoard, applyEvents, applyCursor, normCard, normEvent, eventText,
  sections, headline, loadView, setView, setChatOpen, bounceComposing,
} from './state.js';
import { renderList } from './list.js';
import { renderBoard, renderFold } from './board.js';
import { renderPhone } from './phone.js';
import { renderRail, openLightbox, closeLightbox } from './rail.js';
import { initCompose, toBase64List, imageFiles } from './compose.js';
import { installNotifications, attention, armNotifications, clearBadge } from './notify.js';
import { loadSkin, installSkinToggle, installBlip } from './skin.js';
import { startSiblings, renderTitle, closeSiblingMenu } from './siblings.js';
import { installSettings, closeSettings, settingsOpen } from './settings.js';
import {
  installKeys, handleKey, paintNav, clearNav, isNavNode, focusNavCursor,
  closeKeysSheet, keysSheetOpen,
} from './keys.js';
import { renderReportsPage, renderReportPage } from './reports.js';

const el = {};
let compose = null;

const app = {
  transport: 'idle',
  eventText,
  render,
  openCard, closeCard, composeBounce,
  goBoard, goReports,
  pageOpen: () => !!page,
  answer, chat, sessionChat, verdict, cardAction, markDuplicate, retryCard, retrySubmit,
  lightbox: (urls, i, caps) => openLightbox(el.lightbox, urls, i, caps),
  toast: (msg) => toast(msg),
};

// ---- pages -----------------------------------------------------------------
//
// The board is the app. A "page" is the one exception: a report library and a
// single rendered report, reached from the header link and from stable URLs
// (#/reports, #/report/<sha>.<ext>). It takes over `main` — never the rail,
// never a third column. The user ruled a rail out by name.
let page = null;      // null | {kind: 'reports'|'report', sha, ext, data, error}

// Which shell we are in. The Fold is the spec's real mobile target (980×740),
// and the phone below it is an explicit fallback, not an optimisation.
const foldQuery = window.matchMedia('(max-width: 1199px)');
const phoneQuery = window.matchMedia('(max-width: 600px)');

// ---- render --------------------------------------------------------------

let renderQueued = false;
function render() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; paint(); });
}

/** The rail alone — for frames that provably touch nothing else (see onCursor). */
let railQueued = false;
function paintRail() {
  if (renderQueued || railQueued) return;
  railQueued = true;
  requestAnimationFrame(() => {
    railQueued = false;
    // This is the most frequent paint on the page (a live session moves its
    // cursor about once a second) and it lands squarely on the box you are
    // typing in. It has to preserve the caret exactly like the full paint does
    // — it used to preserve nothing at all.
    const focus = captureFocus();
    renderRail(el.rail, app);
    restoreFocus(focus);
    applyPendingFocus();
  });
}

/**
 * Card #57. Which layout the shell is in, as a class on <body>, because the
 * Board's columns scroll independently (card #59) and the List's page scrolls —
 * two different overflow stories that CSS has to be able to tell apart.
 */
function paintShell() {
  const board = !page && !phoneQuery.matches
    && (foldQuery.matches || store.view === 'board');
  document.body.classList.toggle('layout-board', board);
}

function paint() {
  const focus = captureFocus();
  const secs = sections();

  // Plain <h1> on a one-sprint machine; a switcher (with a dot when another
  // sprint on this box is waiting on you) when there is more than one.
  const title = store.sprint ? store.sprint.title : 'starting up…';
  renderTitle(el.titleWrap, title);
  // On the Fold the header is 54px and the whole headline will not fit; the one
  // number that changes what you do next survives, in the accent colour.
  const needs = secs.needs_you.cards.length;
  el.headline.classList.toggle('fold-need', foldQuery.matches);
  el.headline.textContent = !store.loaded ? ''
    : foldQuery.matches ? (needs ? `${needs} need you` : 'nothing needs you')
      : headline(secs);
  if (store.sprint && el.hold.checked !== !!store.sprint.hold_mode) el.hold.checked = !!store.sprint.hold_mode;
  document.body.classList.toggle('hold-on', !!(store.sprint && store.sprint.hold_mode));

  for (const btn of el.viewBtns) btn.classList.toggle('is-on', btn.dataset.view === store.view);
  // Quiet, and conditional: the link exists only once this sprint has a report
  // in it. A link to an empty library is a promise the board cannot keep.
  if (el.reportsLink) {
    el.reportsLink.hidden = !(store.loaded && store.reports > 0);
    el.reportsLink.classList.toggle('is-on', !!page);
    el.reportsLink.title = store.reports === 1 ? '1 report in this sprint'
      : `${store.reports} reports in this sprint`;
  }
  // "Unseen" means exactly that: the moment the session chat is the thing in the
  // rail, you have seen it. Closing a card back onto an already-open chat counts
  // just as much as clicking the button does.
  if (store.unseen && store.chatOpen && !store.detail) store.unseen = false;
  paintChatButton();

  clear(el.main);
  paintShell();
  if (page && page.kind === 'reports') renderReportsPage(el.main, app, page);
  else if (page && page.kind === 'report') renderReportPage(el.main, app, page);
  else if (phoneQuery.matches) renderPhone(el.main, app);
  else if (foldQuery.matches) renderFold(el.main, app);
  else if (store.view === 'board') renderBoard(el.main, app);
  else renderList(el.main, app);
  document.body.classList.toggle('page-open', !!page);

  const railOpen = !!store.detail || store.chatOpen;
  document.body.classList.toggle('rail-open', railOpen);
  renderRail(el.rail, app);

  // The scrim only exists where the rail floats over the board (Fold, phone).
  el.scrim.hidden = !(railOpen && foldQuery.matches);

  renderSessionBanner();
  restoreFocus(focus);
  applyPendingFocus();
}

/**
 * Where the caret — or the keyboard cursor — was before we touched the DOM.
 *
 * Two shapes, one mechanism, because they are the same problem: the board
 * repaints on every event, and the thing you were pointing at has to survive
 * being redrawn. A text box survives by its `id` (card #46); a highlighted card
 * survives by its card NUMBER (card #57), which is the only identity a card face
 * carries across a rebuild.
 */
function captureFocus() {
  const a = document.activeElement;
  if (isNavNode(a)) return { kind: 'nav' };
  if (!a || !a.id || !(a instanceof HTMLTextAreaElement || a instanceof HTMLInputElement)) return null;
  return { kind: 'text', id: a.id, start: a.selectionStart, end: a.selectionEnd, len: (a.value || '').length };
}

function restoreFocus(f) {
  // The highlight is repainted on EVERY frame (so a live update can't lose it);
  // focus is only taken back if focus is what it had.
  paintNav({ refocus: !!(f && f.kind === 'nav') });
  if (!f || f.kind !== 'text') return;
  const next = document.getElementById(f.id);
  if (!next || next === document.activeElement) return;
  next.focus({ preventScroll: true });
  // The words come back from the draft store, so the text is usually identical —
  // but clamp anyway rather than throw and leave the caret at 0.
  const len = (next.value || '').length;
  const at = (n) => Math.max(0, Math.min(len, n == null ? len : n));
  try { next.setSelectionRange(at(f.start), at(f.end)); } catch {}
}

/**
 * The Chat button is the spec's single notification surface, and it has exactly
 * four states — closed, open, a session line you have not seen (gold, pulsing),
 * and dimmed because a card has taken the rail. There is no counter anywhere.
 */
function paintChatButton() {
  const b = el.chatBtn;
  const cardOpen = !!store.detail;
  b.classList.toggle('is-open', store.chatOpen && !cardOpen);
  b.classList.toggle('is-unseen', store.unseen && !cardOpen && !store.chatOpen);
  b.classList.toggle('is-dim', cardOpen);
  b.setAttribute('aria-pressed', store.chatOpen && !cardOpen ? 'true' : 'false');
  b.title = cardOpen ? 'a card has the rail — click to go back to the session'
    : store.unseen ? 'the session said something while you were not looking'
      : store.chatOpen ? 'hide the session chat' : 'chat with the session';
}

function renderSessionBanner() {
  const offline = store.loaded && store.session.online === false;
  const transportDown = app.transport === 'error';
  // The board's server process is older than the page it is serving: features
  // this page expects simply are not there. Quieter than "offline" (nothing is
  // broken, and nothing you type is lost) but it must be SAID — the whole bug
  // was that it was not. Lowest priority of the three: a board nobody is home
  // at is more urgent news than a board that is merely behind.
  const stale = serverIsStale();
  if (!offline && !transportDown && !stale) { el.bannerSlot.hidden = true; return; }
  el.bannerSlot.hidden = false;
  const cls = offline ? 'banner warn' : 'banner dim';
  const text = offline ? 'session offline — items will queue'
    : transportDown ? 'lost the board connection — retrying'
      : 'this board needs a restart to pick up new features — '
        + 'run `sprintd stop` then `sprintd start` in its project';
  // Same words, same banner: rewriting it on every paint is one more thing
  // flickering on a page that should be still.
  if (el.banner.className !== cls) el.banner.className = cls;
  if (el.banner.textContent !== text) {
    clear(el.banner);
    el.banner.appendChild(h('span', text));
  }
}

/** Is the caret in something that takes text? Then a keystroke is not a shortcut. */
function isTyping(node) {
  if (!node) return false;
  if (node instanceof HTMLTextAreaElement) return true;
  if (node instanceof HTMLInputElement) {
    return !['button', 'checkbox', 'radio', 'submit', 'file', 'reset'].includes(node.type);
  }
  return !!(node.isContentEditable);
}

/**
 * An empty box is not a message yet — card #30, user verbatim: "Typing / should
 * open the light box even when my focus is in a chat entry box unless there's
 * already other text in that box (i should be able to type a / in the middle of
 * a message, but not at the beginning)". So a caret parked in a blank composer
 * does not swallow the shortcut; one keystroke into a real message does.
 * Whitespace alone is still blank — nobody meant to send three spaces.
 */
function emptyTextTarget(node) {
  if (!isTyping(node)) return false;
  if (node.isContentEditable) return !String(node.textContent || '').trim();
  return !String(node.value || '').trim();
}

// Every text surface on this page carries a stable id for exactly this reason —
// the composers (`composer-<num>`, `sidebar-text`, `compose-text`) and the
// bounce-notes box (`bounce-<num>`) — because an id is the only thing that
// survives a node being replaced. See captureFocus/restoreFocus above.

// ---- put the caret where you just asked for it ---------------------------

/**
 * Card #46, the user's own design, verbatim: "I click a card, it loads in the
 * sidebar, and it focuses my cursor in the reply box." So opening a card asks
 * for the caret, once — and only on OPEN. Nothing else on this page ever moves
 * focus, because a board that grabs your cursor on a live update is the bug this
 * card was filed about.
 */
let pendingFocus = null;

function askFocus(num, kind) {
  pendingFocus = { num: Number(num), kind: kind || 'composer', at: Date.now() };
}

function applyPendingFocus() {
  const f = pendingFocus;
  if (!f) return;
  // The rail may still be waiting on the card's timeline; try again next paint,
  // but never so long that a slow fetch yanks the cursor out of something else.
  const stale = Date.now() - f.at > 4000;
  if (!store.detail || store.detail.num !== f.num) { pendingFocus = null; return; }
  let node = f.kind === 'bounce' ? document.getElementById('bounce-' + f.num) : null;
  if (!node) node = document.getElementById('composer-' + f.num);
  if (!node) { if (stale) pendingFocus = null; return; }
  pendingFocus = null;
  node.focus({ preventScroll: false });
  const end = (node.value || '').length;
  try { node.setSelectionRange(end, end); } catch {}
}

// ---- data ----------------------------------------------------------------

const refreshBoard = debounce(async () => {
  try {
    const board = await api.board();
    applyBoard(board);
    if (store.detail) syncDetailCard();
    // A board we could actually fetch is proof our credentials are good — so a
    // sign-in wall raised by an earlier 401 comes back down by itself once a
    // restarted server accepts us again.
    hideAuthWall();
    render();
  } catch (err) { handleError(err, null); }
}, 200);

const refreshDetail = debounce(async () => {
  if (!store.detail) return;
  const num = store.detail.num;
  try {
    const res = await api.card(num);
    if (!store.detail || store.detail.num !== num) return;
    const d = normDetail(res, num);
    // Lines still in flight stay, and so does anything that FAILED: a message
    // the board never took has to survive every refresh until you retry it or
    // give up on it. Dropping it here is how a send became silence.
    store.detail = {
      ...store.detail,
      ...d,
      pendingLines: (store.detail.pendingLines || []).filter((p) => p.pending || p.failed),
    };
    // Only the rail changed. On a fresh load this fetch lands a beat after the
    // board does, and repainting the whole page for it is the "card comes in
    // and then blinks" the user saw: the board is redrawn by its own refresh,
    // driven by events, not by one card's timeline arriving.
    paintRail();
  } catch (err) {
    if (store.detail && store.detail.num === num && !store.detail.card) store.detail.error = 'Could not load this card.';
    handleError(err, null);
    paintRail();
  }
}, 150);

function normDetail(res, num) {
  const raw = res && (res.card || (res.num != null || res.card_num != null ? res : null));
  const card = normCard(raw) || store.cards.get(num) || null;
  const boardCard = store.cards.get(num);
  if (card && boardCard) {
    if (!card.question && boardCard.question) card.question = boardCard.question;
    if (!card.evidence && boardCard.evidence) card.evidence = boardCard.evidence;
    if (!card.last_activity_at) card.last_activity_at = boardCard.last_activity_at;
  }
  if (card) store.cards.set(card.num, { ...(boardCard || {}), ...card });
  const rawTl = (res && (res.timeline || res.events)) || (raw && raw.timeline) || [];
  const timeline = rawTl.map(normEvent).filter(Boolean).sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const evRaw = res && res.evidence;
  const evidence = evRaw ? (evRaw.packet || evRaw) : (card && card.evidence) || null;
  return { num, card: store.cards.get(num) || card, timeline, evidence, error: null };
}

function syncDetailCard() {
  if (!store.detail) return;
  const c = store.cards.get(store.detail.num);
  if (c) store.detail.card = c;
}

// ---- actions -------------------------------------------------------------

function patch(num, state) {
  store.patches.set(num, { state, ts: Date.now() });
}

async function submit(payload) {
  const hold = el.hold.checked;
  const pending = {
    pendingSubmit: true,
    id: uid(),
    key: uid(),
    text: payload.text || '',
    title: firstLine(payload.text || '', 90) || (payload.images.length ? 'Screenshot' : 'New item'),
    images: payload.images,
    hold,
    error: null,
  };
  store.pending.push(pending);
  closeCompose();
  render();
  await sendSubmit(pending);
}

async function sendSubmit(pending) {
  pending.error = null;
  render();
  try {
    const body = { hold: pending.hold };
    if (pending.text) body.text = pending.text;
    if (pending.images.length) body.images = toBase64List(pending.images);
    const res = await api.submit(body, pending.key);
    const card = normCard(res && (res.card || res));
    if (card) {
      store.cards.set(card.num, card);
      pending.num = card.num;
    }
    store.pending = store.pending.filter((p) => p.id !== pending.id);
    render();
    refreshBoard();
  } catch (err) {
    pending.error = errText(err, 'could not send');
    handleError(err, null);
    render();
  }
}

function retrySubmit(pending) { sendSubmit(pending); }

async function answer(card, question, text, images, key, reuse) {
  const before = card.state;
  const q = question || card.question || {};
  // One key per attempt-chain: a retry re-POSTs the SAME key, so an answer the
  // server actually took (and failed to tell us about) replays instead of
  // landing twice.
  const idem = key || uid();
  // A screenshot pasted while answering is its own line to the agent: the
  // answer itself is the thing that unblocks the card, and it goes second so
  // the image is already in the thread when the agent picks the answer up.
  if (images && images.length) {
    await chat(card, text, images);
    if (!text) text = '(see the image above)';
  }
  patch(card.num, 'in_progress');
  card.question = null;
  // "Delivered — the agent sees it next turn": the optimistic status line the
  // question panel is replaced by, in the thread, immediately.
  if (store.detail && store.detail.num === card.num) store.detail.justAnswered = true;
  const line = reuse
    || pushPending(card.num, { actor: 'user', kind: 'answer', payload: { text } });
  render();
  try {
    const res = await api.answer(card.num, q.id, text, idem);
    settlePending(card.num, false, res && res.event, line);
    refreshBoard();
    if (store.detail && store.detail.num === card.num) refreshDetail();
  } catch (err) {
    store.patches.delete(card.num);
    card.state = before;
    card.question = q.text ? q : card.question;
    if (store.detail && store.detail.num === card.num) store.detail.justAnswered = false;
    if (err instanceof ApiError && err.status === 409) {
      // Someone (or a second click) already answered this one. Say so gently
      // and catch up — never throw a 409 in the user's face.
      toast('That question was already answered — catching up.');
      settlePending(card.num, false, null, line);
      refreshBoard();
    } else {
      toast(errText(err, 'answer did not send'));
      handleError(err, null);
      // The answer is still on screen, marked failed, with the same key behind
      // its retry — never a question that silently un-answered itself.
      let target = line;
      if (!target) {
        // Answered inline from the List, so there is no thread to fail into.
        // Open the card: a lost answer is worth a rail, and a toast that is
        // gone in four seconds is the silence this card is about.
        openCard(card.num);
        target = pushPending(card.num, { actor: 'user', kind: 'answer', payload: { text } });
      }
      failPending(target, () => answer(card, q, text, null, idem, target));
    }
    render();
  }
}

async function chat(card, text, images, key, reuse) {
  const imgs = images || [];
  const idem = key || uid();
  // A retry re-sends the line already in the thread rather than adding a second
  // copy of the same words.
  const line = reuse || pushPending(card.num, {
    actor: 'user', kind: 'chat',
    // The thumbnails you pasted are in the thread before the POST returns — the
    // data: URLs render as tiles directly, and the stored refs replace them the
    // moment the card refreshes.
    payload: localPayload(text, imgs),
  });
  render();
  try {
    const res = await api.chat(card.num, text, toBase64List(imgs), idem);
    // Stamp the real seq on our own line so it reads "landed" the instant the
    // POST returns, and flips to "session is on it" when the cursor passes it.
    settlePending(card.num, false, res && res.event, line);
    render();
    refreshDetail();
  } catch (err) {
    // The message stays in the thread, marked "failed to send — tap to retry",
    // and the retry re-POSTs the same Idempotency-Key. Only THIS line fails:
    // a message you sent a minute ago is not retroactively un-sent because a
    // later one timed out.
    failPending(line, () => chat(card, text, imgs, idem, line));
    toast(errText(err, 'message did not send'));
    handleError(err, null);
    render();
  }
}

/**
 * The optimistic copy of a message we are still sending — word for word what
 * the server will write, so the echo it sends back replaces ours cleanly. The
 * thumbnails render straight from their data: URLs until the stored refs land.
 */
function localPayload(text, images) {
  const imgs = images || [];
  const payload = {
    text: text || (imgs.length === 1 ? 'sent a screenshot' : `sent ${imgs.length} screenshots`),
    attachments: imgs.map((i) => ({ url: i.dataUrl, name: i.name || 'pasted image' })),
  };
  if (!text && imgs.length) payload.images_only = true;
  return payload;
}

/** A line to the session itself, in the sprint-level chat. */
async function sessionChat(text, images, key, reuse) {
  const imgs = images || [];
  const idem = key || uid();
  const line = reuse || normEvent({ actor: 'user', kind: 'chat', ts: new Date().toISOString(),
    payload: localPayload(text, imgs) });
  line.local = true;         // "sending…" — no seq yet, so nothing is claimed
  line.failed = false;
  line.retry = null;
  line.localEcho = true;     // replaced when the server's own copy arrives
  if (line.sortSeq == null) line.sortSeq = store.seq + 0.5;   // ordering only, never a delivery claim
  if (!reuse) store.sidebar.push(line);
  render();
  try {
    const res = await api.sidebar(text, toBase64List(imgs), idem);
    line.local = false;
    if (res && res.event && res.event.payload && res.event.payload.attachments) {
      line.payload.attachments = res.event.payload.attachments;
    }
    const seq = res && res.event && Number(res.event.seq);
    if (seq && !Number.isNaN(seq)) {
      line.seq = seq;
      line.ts = res.event.ts || line.ts;
      store.seq = Math.max(store.seq, seq);
    }
    render();
  } catch (err) {
    // Exactly the bug this card was filed for: a sidebar line the backend never
    // took used to sit there saying "sending…". It says "failed to send — tap
    // to retry" now, and the retry reuses the same key and the same line.
    line.local = false;
    line.failed = true;
    line.retry = () => sessionChat(text, imgs, idem, line);
    toast(errText(err, 'the session did not get that'));
    handleError(err, null);
    render();
  }
}

/**
 * `opts.quiet` suppresses the per-card toast — one Approve on a work unit is
 * one decision, and six toasts saying the same sentence about six cards is the
 * noise card #26 was about. The unit says one thing when it finishes; a FAILED
 * verdict still speaks, quiet or not.
 */
async function verdict(card, kind, notes, key, opts) {
  const quiet = !!(opts && opts.quiet);
  const before = card.state;
  const idem = key || uid();
  const word = kind === 'approve' ? 'Approve' : kind === 'bounce' ? 'Bounce' : 'Reject';
  // Approve does not mean Done: the card sits in Ready as "merging" until the branch lands.
  patch(card.num, kind === 'approve' ? 'integrating' : kind === 'bounce' ? 'in_progress' : 'rejected');
  bounceComposing(card.num, false);
  clearVerdictError(card.num);
  render();
  try {
    await api.verdict(card.num, kind, notes, idem);
    if (!quiet) {
      toast(kind === 'approve' ? `#${card.num} approved — merging now; it moves to Done when the branch lands.`
        : kind === 'bounce' ? `#${card.num} bounced back with your notes.`
          : `#${card.num} rejected.`);
    }
    refreshBoard();
    if (store.detail && store.detail.num === card.num) refreshDetail();
    // Whether the verdict actually landed — the Review-next walkthrough only
    // moves to the next card once the server has taken this one.
    return true;
  } catch (err) {
    store.patches.delete(card.num);
    card.state = before;
    toast(errText(err, 'verdict did not stick'));
    handleError(err, null);
    // A toast is gone in four seconds and a verdict is not a small thing to
    // lose. The failure goes IN the thread, above the verdict bar you are
    // looking at, and stays there until the retry succeeds.
    const line = pushPending(card.num, {
      actor: 'server', kind: 'error',
      payload: {
        text: `${word} did not go through — ${errText(err, 'the board did not take it')}. `
          + 'The card is still yours to sign off; try the button again.',
      },
    });
    if (line) {
      line.verdictError = true;
      // `failed` is what keeps it through the next detail refresh — a lost
      // verdict must not quietly disappear off the thread a second later.
      line.pending = false;
      line.failed = true;
    }
    render();
    return false;
  }
}

/** Drop the last failed-verdict line for a card — a fresh attempt supersedes it. */
function clearVerdictError(num) {
  if (!store.detail || store.detail.num !== num) return;
  store.detail.pendingLines = (store.detail.pendingLines || []).filter((l) => !l.verdictError);
}

async function cardAction(card, action, extra) {
  const optimistic = { hold: 'held', release: 'queued', cancel: 'canceled', retry: 'queued', reopen: 'queued' }[action];
  const before = card.state;
  const wasPinned = card.pinned;
  if (optimistic) patch(card.num, optimistic);
  if (action === 'pin' || action === 'unpin') {
    // Say what we want, never "toggle whatever you have" — two clicks in flight
    // must not cancel each other out.
    const pinned = action === 'pin';
    card.pinned = pinned;
    extra = { ...(extra || {}), pinned };
  }
  render();
  try {
    await api.action(card.num, action, extra);
    refreshBoard();
    if (store.detail && store.detail.num === card.num) refreshDetail();
    return true;
  } catch (err) {
    store.patches.delete(card.num);
    card.state = before;
    card.pinned = wasPinned;
    toast(errText(err, `could not ${action} #${card.num}`));
    handleError(err, null);
    render();
    return false;
  }
}

function markDuplicate(card) {
  const raw = window.prompt(`#${card.num} is a duplicate of which card? (number)`);
  if (!raw) return;
  const n = Number(String(raw).replace(/[^0-9]/g, ''));
  if (!n) return;
  cardAction(card, 'duplicate_of', { dup_of: n });
}

/** A real state change, not a chat message: failed → queued, and the session
 *  dispatches a fresh agent (honestly labelled as a new one). */
async function retryCard(card) {
  if (await cardAction(card, 'retry')) {
    toast(`#${card.num} is back in the queue — a fresh agent will pick it up.`);
  }
}

function pushPending(num, line) {
  if (!store.detail || store.detail.num !== num) return null;
  store.detail.pendingLines = store.detail.pendingLines || [];
  const entry = { ...normEvent({ ...line, ts: new Date().toISOString() }), pending: true, localId: uid() };
  store.detail.pendingLines.push(entry);
  return entry;
}

/**
 * Settle our optimistic line. On success we keep it — now stamped with the
 * server's real seq, so it says "landed" and can flip to "session is on it" —
 * until the next detail fetch supplies the server's own copy (the timeline
 * dedupes on seq). On failure it stays visible, marked "not sent".
 */
function settlePending(num, failed, event, entry) {
  if (!store.detail || store.detail.num !== num) return;
  const lines = store.detail.pendingLines || [];
  if (failed) { lines.forEach((l) => { l.pending = false; l.failed = true; }); return; }
  const seq = event && Number(event.seq);
  const targets = entry ? [entry] : lines;
  for (const l of targets) {
    l.pending = false;
    if (seq && !Number.isNaN(seq)) {
      l.seq = seq;
      l.ts = event.ts || l.ts;
      store.seq = Math.max(store.seq, seq);
    }
  }
  if (!entry && !(seq && !Number.isNaN(seq))) store.detail.pendingLines = [];
}

/**
 * One line failed to send. It stays exactly where it is, marked, with the way
 * to try again hung on it — `retry` re-POSTs with the SAME Idempotency-Key, so
 * a send that was slow but actually landed replays rather than duplicating.
 *
 * Only the line that failed is marked. Marking every pending line (which is
 * what this used to do) told you a message had failed when it hadn't.
 */
function failPending(line, retry) {
  if (!line) return;
  line.pending = false;
  line.failed = true;
  line.retry = async () => {
    line.failed = false;
    line.retry = null;
    line.pending = true;
    render();
    await retry();
  };
}

// ---- the rail ------------------------------------------------------------

// ---- pages: the report library and one rendered report ---------------------

/** Back to the board from a page. Leaves the rail exactly as it was. */
function goBoard() {
  if (!page) return;
  page = null;
  if (location.hash.startsWith('#/report')) {
    history.replaceState(null, '', location.pathname + location.search);
  }
  render();
}

/** The library: every report in THIS sprint. */
function goReports(replace) {
  page = { kind: 'reports', data: null, error: null };
  if (location.hash !== '#/reports') {
    if (replace) history.replaceState(null, '', '#/reports');
    else location.hash = '#/reports';
  }
  render();
  loadPage(page, () => api.reports());
}

/** One report on its own page — a stable, linkable URL. */
function goReport(sha, ext, replace) {
  page = { kind: 'report', sha, ext, data: null, error: null };
  const want = `#/report/${sha}.${ext}`;
  if (location.hash !== want) {
    if (replace) history.replaceState(null, '', want);
    else location.hash = want;
  }
  render();
  loadPage(page, () => api.report(sha, ext));
}

/** Fetch for the page that is open NOW; a later navigation wins. */
async function loadPage(target, fetcher) {
  try {
    const data = await fetcher();
    if (page !== target) return;
    target.data = data;
  } catch (err) {
    if (page !== target) return;
    target.error = err;
    handleError(err, null);
  }
  render();
}

/** Read the hash and put the app in the state it names. */
function routeFromHash(replace) {
  const hash = location.hash;
  const one = hash.match(/^#\/report\/([0-9a-f]{64})\.(md|html)$/);
  if (one) { closeRailForPage(); goReport(one[1], one[2], replace); return true; }
  if (hash === '#/reports') { closeRailForPage(); goReports(replace); return true; }
  const card = hash.match(/^#\/c\/(\d+)/);
  if (card) { page = null; openCard(Number(card[1])); return true; }
  if (page) { page = null; render(); }
  return false;
}

/** A page owns the whole main area; a card open behind it is just confusing. */
function closeRailForPage() {
  if (store.detail) store.detail = null;
}

/**
 * Open a card in the rail. `opts.focus` says which box wants the caret —
 * 'composer' (the default: the reply box, every entry point), 'bounce' (you
 * pressed Bounce on a review row, so it is the notes box you meant), or 'none'.
 *
 * 'none' is card #57's preview: arrowing down a column shows each card in the
 * rail as you pass it, and the caret must NOT follow, or the next arrow would
 * type into a reply box instead of moving the highlight. Same open, same rail,
 * one thing withheld — which is why it is an argument here rather than a second
 * "preview" path that could drift out of step with the real one.
 *
 * Re-opening the card that is already open keeps its thread as it stands: the
 * pending and failed lines in it are the user's own words, and throwing them
 * away to re-focus a box would be a worse bug than the one we are fixing.
 */
function openCard(num, opts) {
  if (num == null) return;
  const n = Number(num);
  const kind = (opts && opts.focus) || 'composer';
  page = null;
  const card = store.cards.get(n) || null;
  if (!store.detail || store.detail.num !== n) {
    store.detail = {
      num: n, card, timeline: [], evidence: card && card.evidence,
      pendingLines: [], justAnswered: false, error: null,
    };
  }
  // Bouncing is a composing state (card #44) and it is typed in the RAIL (card
  // #46): opening the card is what puts you in it, and `askFocus` is what puts
  // the caret in the box once the rail has painted it.
  if (kind === 'bounce') bounceComposing(n, true);
  if (kind !== 'none') askFocus(n, kind);
  else pendingFocus = null;
  if (location.hash !== `#/c/${n}`) history.replaceState(null, '', `#/c/${n}`);
  render();
  refreshDetail();
}

/**
 * Start writing a bounce for this card — the one entry point, wherever the
 * Bounce button lives (the packet in the rail, the walkthrough bar, a review
 * row). It does #44's half (the composing state: Submit bounce and Cancel, no
 * Approve) and #46's half (ask for the caret, once) in the same motion, so
 * there is exactly one implementation of "focus the notes box".
 */
function composeBounce(num) {
  const n = Number(num);
  bounceComposing(n, true);
  askFocus(n, 'bounce');
  render();
}

function closeCard() {
  store.detail = null;
  // A half-written bounce (and the fact that you were writing one) outlives the
  // rail closing, exactly like a draft does — only the caret request is dropped.
  pendingFocus = null;
  if (location.hash.startsWith('#/c/')) history.replaceState(null, '', location.pathname + location.search);
  render();
}

function toggleChat(force, opts) {
  const want = force != null ? force : !(store.chatOpen && !store.detail);
  setChatOpen(want);
  if (want) {
    store.unseen = false;
    store.detail = null;
    if (location.hash.startsWith('#/c/')) history.replaceState(null, '', location.pathname + location.search);
  }
  render();
  // Opening the chat asks for the caret — except on the Escape rung that is
  // explicitly about getting you OUT of a text box (see `escape`).
  const takeCaret = !(opts && opts.focus === false);
  if (want && takeCaret) setTimeout(() => { const t = $('#sidebar-text'); if (t) t.focus(); }, 60);
}

/**
 * The Escape ladder — one rung per press, first match wins.
 *
 * Escape already meant four things on this page before card #57 asked it to mean
 * more, so the ORDER is the whole design. The user's two rulings, verbatim:
 * "hitting esc takes me back to the session chat", and then "oh, and if I'm
 * already in the session chat, hitting esc toggles it open/closed". So repeated
 * Escape walks you out of typing, out of the card, back to the session chat, then
 * collapses the rail — and one more brings it back. Nothing is a dead end and
 * nothing traps focus.
 *
 * Chrome first (a sheet on top of everything is what Escape obviously means):
 *   1. the shortcut sheet   → close it
 *   2. the Settings sheet   → close it
 *   3. the sprint switcher  → close it
 *   4. the lightbox         → close it
 *   5. the Drop-work sheet  → close it
 *
 * Then the four rungs of the user's own ladder:
 *   6. the caret is in the bounce-notes box → the box cancels the bounce itself
 *      (card #44), and the document keeps its hands off. It used to fall through
 *      here and close the whole card out from under a half-written bounce.
 *   7. the caret is in a card's reply box   → leave the box, land back on the
 *      highlighted card. The rail keeps showing that card.
 *   8. a card has the rail                  → drop the highlight and put the
 *      session chat back in the rail. On the Fold the rail is a slide-over ON TOP
 *      of the work, so there it is dismissed instead of swapped.
 *   9. the session chat has the rail        → toggle it closed; pressing Escape
 *      once more opens it again, on the session chat, with the caret in the box.
 *
 * A report page and a stray column highlight get their own rungs in between —
 * both are "put the board back the way it was", which is the same verb.
 */
function escape(e) {
  const a = document.activeElement;
  if (closeKeysSheet()) return;
  if (closeSettings()) return;
  if (closeSiblingMenu()) { render(); return; }
  if (!el.lightbox.hidden) { closeLightbox(el.lightbox); return; }
  if (!el.composeWrap.hidden) { closeCompose(); return; }
  // The bounce box owns its own Escape (review.js / thread.js cancel the bounce).
  if (a && a.classList && a.classList.contains('bounce-notes')) return;
  // Rung 1: out of the reply box, back onto the card you were reading.
  if (store.detail && a && a.id === `composer-${store.detail.num}`) {
    e.preventDefault();
    a.blur();
    focusNavCursor();
    return;
  }
  if (page) { goBoard(); return; }
  // Rung 2: out of the card, back to the session chat.
  if (store.detail) {
    e.preventDefault();
    clearNav();
    if (a && a.blur) a.blur();
    if (foldQuery.matches) closeCard();
    // No caret grab here on purpose: the rung before this one just took you OUT
    // of a text box, and dropping you into a different one would undo it.
    else toggleChat(true, { focus: false });
    return;
  }
  if (clearNav()) { render(); return; }
  // Rungs 3 and 4: the session chat is a toggle from here on.
  e.preventDefault();
  toggleChat(!store.chatOpen);
}

// ---- compose sheet -------------------------------------------------------

function openCompose() {
  el.composeWrap.hidden = false;
  setTimeout(() => compose && compose.focus(), 40);
}

function closeCompose() {
  el.composeWrap.hidden = true;
}

// ---- chrome --------------------------------------------------------------

function toast(msg) {
  const t = h('div.toast', msg);
  el.toasts.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, 4200);
}

function errText(err, fallback) {
  if (err instanceof NetworkError) {
    return err.timedOut ? `${fallback} — the board did not answer in time`
      : `${fallback} — could not reach the board`;
  }
  if (err instanceof ApiError) {
    if (err.fields) return `${fallback} — missing ${[].concat(err.fields).join(', ')}`;
    if (err.status === 409) return 'that already happened — catching up';
    if (err.body && err.body.error) return err.body.error;
    return `${fallback} (${err.status})`;
  }
  return fallback;
}

function handleError(err, _ctx) {
  if (err instanceof ApiError && err.status === 401) showAuthWall();
}

function showAuthWall() {
  if (!el.authwall.hidden) return;
  el.authwall.hidden = false;
  clear(el.authwall);
  // After a restart this is the honest reason, and it is the one case where the
  // board genuinely cannot fix itself: a rotated token means a new link.
  const restarted = serverRestarted;
  el.authwall.appendChild(h('div.authwall-card',
    h('h2', restarted ? 'The board restarted — it needs its link again' : 'This board needs its link'),
    h('p', restarted
      ? 'The server came back as a new process and no longer accepts this tab’s token. Open the URL the session printed when it restarted the sprint — the one ending in ?t=… — and everything picks up where it left off. Nothing you typed is lost.'
      : 'The access token is missing or expired. Open the URL the session printed when it started the sprint — the one ending in ?t=… — and this page will work again.'),
    h('button.btn.send', { type: 'button', onclick: () => location.reload() }, 'Reload')));
}

/** Credentials work again (a board fetch came back) — take the wall down. */
function hideAuthWall() {
  if (el.authwall && !el.authwall.hidden) {
    el.authwall.hidden = true;
    clear(el.authwall);
  }
}

// ---- the backend restarted ----------------------------------------------

let serverRestarted = false;

/**
 * A different server process answered us. Everything the tab believed about
 * its connection is void: the stream is dead, the cursor has to be re-driven,
 * and the board has to be re-fetched. None of that touches what you were in
 * the middle of writing — drafts and pasted screenshots live in the store, not
 * in the DOM, and a half-written message survives this exactly like it
 * survives any other re-render.
 */
function onServerRestart(live) {
  serverRestarted = true;
  toast('The board restarted — reconnecting and catching up.');
  // Resume the stream from OUR cursor: the log is append-only and seq-stable
  // across a restart, so we pick up precisely where we stopped hearing.
  if (live) live.resync();
  refreshBoard();
  if (store.detail) refreshDetail();
  render();
}

// ---- boot ----------------------------------------------------------------

async function boot() {
  const params = new URLSearchParams(location.search);
  const mock = params.get('mock');
  if (mock) {
    const m = await import('./mock.js');
    m.installMock(params);
    document.body.classList.add('is-mock');
  }

  initAuth();
  installNotifications();
  installBlip();
  // Before the first paint, so the page never flashes Calm on its way to Chaos.
  loadSkin();
  loadView();

  el.main = $('#main');
  el.rail = $('#rail');
  el.scrim = $('#scrim');
  el.lightbox = $('#lightbox');
  el.toasts = $('#toasts');
  el.authwall = $('#authwall');
  el.titleWrap = $('#title-wrap');
  el.headline = $('#headline');
  el.hold = $('#hold-toggle');
  el.chatBtn = $('#chat-btn');
  el.bannerSlot = $('#banner-slot');
  el.banner = $('#banner');
  el.composeWrap = $('#compose-wrap');
  el.reportsLink = $('#reports-link');
  // scoped to the layout control — the skin control is a second .seg beside it
  el.viewBtns = Array.from(document.querySelectorAll('#view-seg .seg-btn'));

  el.reportsLink.addEventListener('click', (e) => { e.preventDefault(); goReports(); });

  compose = initCompose({
    form: $('#compose'),
    textarea: $('#compose-text'),
    thumbsEl: $('#compose-thumbs'),
    fileInput: $('#compose-file'),
    errEl: $('#compose-err'),
    onSubmit: submit,
  });

  el.hold.addEventListener('change', async () => {
    const on = el.hold.checked;
    if (store.sprint) store.sprint.hold_mode = on;
    document.body.classList.toggle('hold-on', on);
    try { await api.holdMode(on); refreshBoard(); } catch (err) {
      if (store.sprint) store.sprint.hold_mode = !on;
      el.hold.checked = !on;
      toast(errText(err, 'could not change hold mode'));
      handleError(err, null);
    }
  });

  for (const btn of el.viewBtns) {
    btn.addEventListener('click', () => { if (setView(btn.dataset.view)) render(); });
  }
  // The skin is pure CSS, but a re-paint costs nothing and keeps anything that
  // reads a computed colour honest.
  installSkinToggle($('#skin-seg'), render);
  // Saved settings change what the NEXT dispatch does, and they change what a
  // card face calls "the default" — so the board is refetched, not just
  // repainted.
  installSettings($('#settings-btn'), () => {
    toast('Settings saved — in effect for the next dispatch.');
    refreshBoard();
  });
  // The "?" button and its sheet: the shortcut map, quiet, on the page rather
  // than in anybody's head.
  installKeys(app);
  el.chatBtn.addEventListener('click', () => toggleChat());
  $('#drop-btn').addEventListener('click', () => openCompose());
  $('#compose-cancel').addEventListener('click', () => closeCompose());
  el.composeWrap.addEventListener('mousedown', (e) => { if (e.target === el.composeWrap) closeCompose(); });
  el.scrim.addEventListener('click', () => { if (store.detail) closeCard(); else toggleChat(false); });

  // Paste or drop an image anywhere and the sheet opens with it already attached.
  window.addEventListener('paste', (e) => {
    if (!el.composeWrap.hidden) return;
    const a = document.activeElement;
    if (a && (a instanceof HTMLTextAreaElement || a instanceof HTMLInputElement)) return;
    const files = imageFiles(e.clipboardData);
    if (!files.length) return;
    e.preventDefault();
    openCompose();
    compose.addFiles(files);
  });

  document.addEventListener('keydown', (e) => {
    // "/" is the shortcut to drop work — from anywhere on the board, and from a
    // text box that is still empty. Once there are words in the box a slash is
    // just a slash, and inside the Drop-work sheet itself it always is.
    if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey
        && el.composeWrap.hidden && !settingsOpen() && !keysSheetOpen()) {
      const a = document.activeElement;
      const inSheet = !!(a && el.composeWrap.contains(a));
      if (!inSheet && (!isTyping(a) || emptyTextTarget(a))) {
        e.preventDefault();
        openCompose();
        return;
      }
    }
    if (e.key === 'Escape') { escape(e); return; }
    if (!el.lightbox.hidden && el.lightbox._nav) {
      if (e.key === 'ArrowRight') el.lightbox._nav(1);
      if (e.key === 'ArrowLeft') el.lightbox._nav(-1);
      return;
    }
    // Everything else keyboard-shaped: the switcher, the column numbers, the
    // arrows, Enter. It owns its own don't-hijack rules and reports whether it
    // took the key.
    handleKey(e, { isTyping, emptyTextTarget });
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.menu-wrap')) {
      for (const m of document.querySelectorAll('.menu')) m.hidden = true;
    }
  });

  window.addEventListener('focus', () => { clearBadge(); refreshBoard(); });
  window.addEventListener('hashchange', () => {
    if (routeFromHash()) return;
    if (store.detail) closeCard();
  });
  for (const q of [foldQuery, phoneQuery]) {
    if (q.addEventListener) q.addEventListener('change', render);
    else if (q.addListener) q.addListener(render);
  }

  // The other sprints on this machine are not on our event log, so they are
  // polled (every 30s) rather than streamed.
  startSiblings(render);

  setInterval(() => tickTimes(document), 20000);
  setInterval(() => render(), 30000);          // the recency hairlines drain live
  setInterval(() => refreshBoard(), 45000);

  await firstLoad();
}

async function firstLoad() {
  try {
    const board = await api.board();
    applyBoard(board);
  } catch (err) {
    handleError(err, null);
    if (!(err instanceof ApiError && err.status === 401)) {
      if (!firstLoad.warned) { firstLoad.warned = true; toast('Could not reach the board — retrying.'); }
      setTimeout(firstLoad, 4000);
    }
    render();
    return;
  }
  // A deep link opens its card (or its report) BEFORE the first paint, never
  // after. Painting the session chat into the rail and then replacing it with
  // the card one frame later is a blink you cannot un-see, and it costs nothing
  // to get right.
  routeFromHash(true);

  // The sidebar thread ships with the board (`board.sidebar`) — no event-log scan.
  render();

  const live = new Live({
    onEvents: (evs) => {
      const out = applyEvents(evs);
      if (out.attention.length) attention();
      if (out.needsBoard || out.touched.size) refreshBoard();
      if (store.detail && (out.touched.has(store.detail.num) || out.needsBoard)) refreshDetail();
      render();
    },
    // The transport's only reader is the banner, and it flips idle → sse on
    // every single load. Repainting the whole page for it was the second half
    // of the "it comes in and then blinks" on a fresh load.
    onStatus: (mode) => { app.transport = mode; renderSessionBanner(); },
    // The session drained further: messages it has now read flip to
    // "session is on it" without waiting for the next board fetch.
    //
    // This is the most frequent frame on the wire — an active session moves its
    // cursor every second or so — and the ONLY thing on screen that reads the
    // cursor is the delivery pill under your own messages in the rail. So it
    // paints the rail and nothing else; the rail then patches those pills in
    // place rather than rebuilding the thread. That is what stopped the rail
    // blinking once a second while an agent was working.
    onCursor: (seq) => { if (applyCursor(seq)) paintRail(); },
    onAuthError: showAuthWall,
  });
  // The first /api/board already recorded this server's generation, so it is
  // the baseline: anything different from here on is a NEW backend.
  onServerGeneration(() => onServerRestart(live));
  // "This board is behind its own UI" can become true (or stop being true, once
  // it is actually restarted) at any point; the banner is the only thing that
  // reads it, so nothing else has to repaint.
  onServerStale(() => renderSessionBanner());
  live.start(store.seq);

  setTimeout(armNotifications, 1500);
}

boot();
