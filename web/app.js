// Wiring: boot, live transport, optimistic actions, render loop.
import { h, clear, $, debounce, tickTimes, uid, firstLine } from './util.js';
import { api, ApiError, initAuth } from './api.js';
import { Live } from './live.js';
import {
  store, applyBoard, applyEvents, applyCursor, normCard, normEvent, eventText,
} from './state.js';
import { renderBoard } from './board.js';
import { renderDrawer, openLightbox, closeLightbox } from './drawer.js';
import { renderSidebar, renderSessionStatus } from './sidebar.js';
import { initCompose, toBase64List } from './compose.js';
import { installNotifications, attention, armNotifications, clearBadge } from './notify.js';

const el = {};
const app = {
  transport: 'idle',
  eventText,
  render,
  openCard, closeCard,
  answer, chat, verdict, cardAction, markDuplicate, retryCard, retrySubmit,
  lightbox: (urls, i, caps) => openLightbox(el.lightbox, urls, i, caps),
};

// ---- render --------------------------------------------------------------

let renderQueued = false;
function render() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; paint(); });
}

function paint() {
  const focus = captureFocus();

  const title = store.sprint ? store.sprint.title : 'starting up…';
  if (el.sprintTitle.textContent !== title) el.sprintTitle.textContent = title;
  if (store.sprint && el.hold.checked !== !!store.sprint.hold_mode) el.hold.checked = !!store.sprint.hold_mode;
  document.body.classList.toggle('hold-on', !!(store.sprint && store.sprint.hold_mode));

  renderBoard(el.board, app);
  renderSidebar(el.thread, app);
  renderSessionStatus(app);
  renderDrawer(el.drawer, store.detail, app);
  el.drawer.hidden = !store.detail;
  el.scrim.hidden = !store.detail && !document.body.classList.contains('sidebar-open');

  restoreFocus(focus);
}

function captureFocus() {
  const a = document.activeElement;
  if (!a || !a.id || !(a instanceof HTMLTextAreaElement || a instanceof HTMLInputElement)) return null;
  return { id: a.id, start: a.selectionStart, end: a.selectionEnd };
}

function restoreFocus(f) {
  if (!f) return;
  const next = document.getElementById(f.id);
  if (!next || next === document.activeElement) return;
  next.focus();
  try { next.setSelectionRange(f.start, f.end); } catch {}
}

// ---- data ----------------------------------------------------------------

const refreshBoard = debounce(async () => {
  try {
    const board = await api.board();
    applyBoard(board);
    if (store.detail) syncDetailCard();
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
    store.detail = { ...store.detail, ...d, pendingLines: (store.detail.pendingLines || []).filter((p) => p.pending) };
    render();
  } catch (err) {
    if (store.detail && store.detail.num === num && !store.detail.card) store.detail.error = 'Could not load this card.';
    handleError(err, null);
    render();
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

async function answer(card, question, text) {
  const before = card.state;
  const q = question || card.question || {};
  patch(card.num, 'in_progress');
  card.question = null;
  pushPending(card.num, { actor: 'user', kind: 'answer', payload: { text } });
  render();
  try {
    await api.answer(card.num, q.id, text);
    settlePending(card.num);
    refreshBoard();
    if (store.detail && store.detail.num === card.num) refreshDetail();
  } catch (err) {
    store.patches.delete(card.num);
    card.state = before;
    card.question = q.text ? q : card.question;
    if (err instanceof ApiError && err.status === 409) {
      toast('That question was already answered — catching up.');
      refreshBoard();
    } else {
      toast(errText(err, 'answer did not send'));
      handleError(err, null);
    }
    settlePending(card.num, true);
    render();
  }
}

async function chat(card, text) {
  const line = pushPending(card.num, { actor: 'user', kind: 'chat', payload: { text } });
  render();
  try {
    const res = await api.chat(card.num, text);
    // Stamp the real seq on our own line so it reads "landed" the instant the
    // POST returns, and flips to "session is on it" when the cursor passes it.
    settlePending(card.num, false, res && res.event, line);
    render();
    refreshDetail();
  } catch (err) {
    settlePending(card.num, true);
    toast(errText(err, 'message did not send'));
    handleError(err, null);
    render();
  }
}

async function verdict(card, kind, notes) {
  const before = card.state;
  // Approve does not mean Done: the card sits in Ready as "merging" until the branch lands.
  patch(card.num, kind === 'approve' ? 'integrating' : kind === 'bounce' ? 'in_progress' : 'rejected');
  render();
  try {
    await api.verdict(card.num, kind, notes);
    toast(kind === 'approve' ? `#${card.num} approved — merging now; it moves to Done when the branch lands.`
      : kind === 'bounce' ? `#${card.num} bounced back with your notes.`
        : `#${card.num} rejected.`);
    refreshBoard();
    if (store.detail && store.detail.num === card.num) refreshDetail();
  } catch (err) {
    store.patches.delete(card.num);
    card.state = before;
    toast(errText(err, 'verdict did not stick'));
    handleError(err, null);
    render();
  }
}

async function cardAction(card, action, extra) {
  const optimistic = { hold: 'held', release: 'queued', cancel: 'canceled', retry: 'queued' }[action];
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

// ---- drawer --------------------------------------------------------------

function openCard(num) {
  if (num == null) return;
  const card = store.cards.get(Number(num)) || null;
  store.detail = { num: Number(num), card, timeline: [], evidence: card && card.evidence, pendingLines: [], error: null };
  document.body.classList.add('drawer-open');
  if (location.hash !== `#/c/${num}`) history.replaceState(null, '', `#/c/${num}`);
  render();
  refreshDetail();
}

function closeCard() {
  store.detail = null;
  document.body.classList.remove('drawer-open');
  if (location.hash.startsWith('#/c/')) history.replaceState(null, '', location.pathname + location.search);
  render();
}

// ---- chrome --------------------------------------------------------------

function toast(msg) {
  const t = h('div.toast', msg);
  el.toasts.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, 4200);
}

function errText(err, fallback) {
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
  el.authwall.appendChild(h('div.authwall-card',
    h('h2', 'This board needs its link'),
    h('p', 'The access token is missing or expired. Open the URL the session printed when it started the sprint — the one ending in ?t=… — and this page will work again.'),
    h('button.btn.primary', { type: 'button', onclick: () => location.reload() }, 'Reload')));
}

function toggleSidebar(force) {
  const on = force != null ? force : !document.body.classList.contains('sidebar-open');
  document.body.classList.toggle('sidebar-open', on);
  el.scrim.hidden = !on && !store.detail;
  if (on) setTimeout(() => el.sidebarText.focus(), 60);
}

// ---- boot ----------------------------------------------------------------

async function boot() {
  const params = new URLSearchParams(location.search);
  const theme = params.get('theme');
  if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
  const mock = params.get('mock');
  if (mock) {
    const m = await import('./mock.js');
    m.installMock(params);
    document.body.classList.add('is-mock');
  }

  initAuth();
  installNotifications();

  el.board = $('#board');
  el.drawer = $('#drawer');
  el.scrim = $('#scrim');
  el.thread = $('#sidebar-thread');
  el.lightbox = $('#lightbox');
  el.toasts = $('#toasts');
  el.authwall = $('#authwall');
  el.sprintTitle = $('#sprint-title');
  el.hold = $('#hold-toggle');
  el.sidebarText = $('#sidebar-text');

  initCompose({
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

  $('#sidebar-toggle').addEventListener('click', () => toggleSidebar());
  $('#sidebar-close').addEventListener('click', () => toggleSidebar(false));
  el.scrim.addEventListener('click', () => { toggleSidebar(false); closeCard(); });

  const sidebarForm = $('#sidebar-form');
  const sidebarText = el.sidebarText;
  sidebarText.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sidebarForm.requestSubmit(); }
  });
  sidebarText.addEventListener('input', () => {
    sidebarText.style.height = 'auto';
    sidebarText.style.height = Math.min(140, Math.max(sidebarText.scrollHeight, 38)) + 'px';
  });
  sidebarForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = sidebarText.value.trim();
    if (!text) return;
    sidebarText.value = '';
    sidebarText.style.height = '';
    const localLine = normEvent({ actor: 'user', kind: 'chat', ts: new Date().toISOString(), payload: { text } });
    localLine.local = true;        // "sending…" — no seq yet, so nothing is claimed
    localLine.localEcho = true;    // replaced when the server's own copy arrives
    localLine.sortSeq = store.seq + 0.5;   // ordering only, never a delivery claim
    store.sidebar.push(localLine);
    render();
    try {
      const res = await api.sidebar(text);
      localLine.local = false;
      // The server's own seq for this message: this is what "landed" means, and
      // what the drain cursor gets compared against.
      const seq = res && res.event && Number(res.event.seq);
      if (seq && !Number.isNaN(seq)) {
        localLine.seq = seq;
        localLine.ts = res.event.ts || localLine.ts;
        store.seq = Math.max(store.seq, seq);
      }
      render();
    } catch (err) {
      localLine.local = false;
      localLine.failed = true;
      toast(errText(err, 'the session did not get that'));
      handleError(err, null);
      render();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!el.lightbox.hidden) { closeLightbox(el.lightbox); return; }
      if (store.detail) { closeCard(); return; }
      if (document.body.classList.contains('sidebar-open')) toggleSidebar(false);
    }
    if (!el.lightbox.hidden && el.lightbox._nav) {
      if (e.key === 'ArrowRight') el.lightbox._nav(1);
      if (e.key === 'ArrowLeft') el.lightbox._nav(-1);
    }
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.menu-wrap')) {
      for (const m of document.querySelectorAll('.menu')) m.hidden = true;
    }
  });

  window.addEventListener('focus', () => { clearBadge(); refreshBoard(); });
  window.addEventListener('hashchange', () => {
    const m = location.hash.match(/^#\/c\/(\d+)/);
    if (m) openCard(Number(m[1]));
    else if (store.detail) closeCard();
  });

  setInterval(() => tickTimes(document), 20000);
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
    onStatus: (mode) => { app.transport = mode; renderSessionStatus(app); },
    // The session drained further: messages it has now read flip to
    // "session is on it" without waiting for the next board fetch.
    onCursor: (seq) => { if (applyCursor(seq)) render(); },
    onAuthError: showAuthWall,
  });
  live.start(store.seq);

  const m = location.hash.match(/^#\/c\/(\d+)/);
  if (m) openCard(Number(m[1]));
  setTimeout(armNotifications, 1500);
}

boot();
