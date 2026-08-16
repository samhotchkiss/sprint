// The client-side projection of the board. Tolerant normalizers: the server owns the
// truth, we only ever *display* it, so unknown/missing fields degrade instead of throwing.
import { age, firstLine, ms } from './util.js';

export const SILENT_MS = 5 * 60 * 1000;   // spec: 5 minutes with no worker event

/**
 * The four sections the design reads the sprint as, top to bottom, plus Done.
 * "Needs you" deliberately holds both shapes of asking: a card with an open
 * question, and a card whose evidence packet is waiting on a verdict — both are
 * the same thing to a human ("this one is on me"), so they share a section and
 * are told apart by their rail colour and tag.
 */
/**
 * What "blocked" means, in the user's words back to him: he asked "what even is
 * blocked? how can it be blocked but not need me?" — so the answer lives on the
 * section itself, in both layouts, rather than in anyone's head.
 */
export const BLOCKED_NOTE = 'Blocked = an external wall (red CI, waiting on another branch). '
  + 'Nothing you type fixes these; the session re-checks and unblocks them itself.';

/** Which sorter a Board section borrows from the List's four. */
const SORT_AS = { queued: 'waiting', held: 'waiting', complete: 'done', awaiting: 'needs_you' };

export const COLUMNS = [
  {
    key: 'needs_you',
    title: 'Needs you',
    board: 'Needs you',
    states: ['needs_you', 'ready', 'integrating'],
    dot: 'var(--accent)',
  },
  {
    key: 'in_motion',
    title: 'In motion',
    board: 'In progress',
    states: ['triaging', 'in_progress'],
    dot: 'var(--good)',
  },
  {
    key: 'blocked',
    title: 'Blocked',
    board: 'Blocked',
    states: ['blocked', 'failed', 'stale'],
    dot: 'var(--bad)',
  },
  {
    key: 'waiting',
    title: 'Queued & held',
    board: 'Queued & held',
    states: ['queued', 'held'],
    dot: 'var(--faint)',
  },
  {
    key: 'done',
    title: 'Done',
    board: 'Done',
    states: ['completed', 'rejected', 'duplicate', 'canceled'],
    collapsed: true,
  },
];

/** The 4px meter, in the design's order and colours. Done is not on it. */
// Tokens, not hex: the meter is the one place the whole sprint's shape is drawn
// in colour, and a second skin has to be able to repaint it without touching JS.
export const METER = [
  { key: 'needs_you', color: 'var(--accent)', word: 'need you' },
  { key: 'in_motion', color: 'var(--good)', word: 'in motion' },
  { key: 'blocked', color: 'var(--seg-blocked)', word: 'blocked' },
  { key: 'waiting', color: 'var(--seg-queued)', word: 'queued' },
];

// State → section. The server ships an advisory `column_of`, but its columns are
// the state machine's (held/queued/ready/…) and these four are a reading order
// for a human, so the grouping is ours. Anything the server invents that we have
// never heard of lands in Queued & held rather than vanishing off the board.
const COL_OF = {};
for (const c of COLUMNS) for (const s of c.states) COL_OF[s] = c.key;

export function columnOf(state) {
  return COL_OF[state] || 'waiting';
}

export const STATE_LABEL = {
  held: 'Held', queued: 'Queued', triaging: 'Triaging', in_progress: 'In progress',
  needs_you: 'Needs you', blocked: 'Blocked', ready: 'Ready', integrating: 'Merging',
  completed: 'Done',
  rejected: 'Rejected', failed: 'Failed', stale: 'Stale', duplicate: 'Duplicate',
  canceled: 'Canceled',
};

export const store = {
  sprint: null,           // {title, hold_mode, opened_at, closed_at}
  columnOf: null,         // server-advised state -> column map (board.column_of)
  // `cursor` is the session's real drain cursor: every event with seq <= cursor
  // has been read by the session. Never guessed — the server is the only writer.
  session: { status: 'online', online: true, since: null, cursor: null, waiterSeconds: null },
  cards: new Map(),       // num -> card
  patches: new Map(),     // num -> {state, ts} optimistic, reconciled on next board
  pending: [],            // submitted-but-unconfirmed cards
  sidebar: [],            // sprint-level chat events
  seq: 0,
  detail: null,           // {num, card, timeline, evidence, attachments, pendingLines}
  drafts: new Map(),      // freeform text kept across re-renders
  attached: new Map(),    // composer key -> images pasted but not sent yet
  expanded: new Set(),    // event keys whose long version you opened (see detail.js)
  doneOpen: false,
  loaded: false,

  // ---- rail + layout (client only) ----------------------------------------
  // Only one thing owns the rail at a time: a card, or the session chat.
  view: 'list',           // 'list' | 'board' — persisted
  chatOpen: false,        // session chat wants the rail
  unseen: false,          // a session line arrived while the rail was closed
};

const VIEW_KEY = 'sprint.view';
const CHAT_KEY = 'sprint.chat';

/** Which layout you chose last time. Persisted; anything unrecognised is List. */
export function loadView() {
  let v = null;
  try { v = localStorage.getItem(VIEW_KEY); } catch { v = null; }
  store.view = v === 'board' ? 'board' : 'list';
  // On a desktop the rail starts on the session, because a board with nothing
  // open should still show you the one conversation that is always there. On the
  // Fold and below it starts closed — there the rail is a slide-over on top of
  // the work, and opening over the board uninvited would be rude.
  let c = null;
  try { c = localStorage.getItem(CHAT_KEY); } catch { c = null; }
  const roomForIt = !(window.matchMedia && window.matchMedia('(max-width: 1199px)').matches);
  store.chatOpen = c == null ? roomForIt : c === '1';
  return store.view;
}

export function setView(v) {
  const next = v === 'board' ? 'board' : 'list';
  if (next === store.view) return false;
  store.view = next;
  try { localStorage.setItem(VIEW_KEY, next); } catch {}
  return true;
}

export function setChatOpen(on) {
  store.chatOpen = !!on;
  try { localStorage.setItem(CHAT_KEY, on ? '1' : '0'); } catch {}
}

// ---- normalizers ---------------------------------------------------------

function num(v) { const n = Number(v); return Number.isNaN(n) ? null : n; }

export function normEvent(ev) {
  if (!ev || typeof ev !== 'object') return null;
  const p = ev.payload && typeof ev.payload === 'object' ? ev.payload : (ev.payload ? { text: ev.payload } : {});
  return {
    seq: num(ev.seq != null ? ev.seq : ev.id),
    card_num: ev.card_num != null ? num(ev.card_num) : (ev.card != null ? num(ev.card) : null),
    ts: ev.ts || ev.created_at || ev.at || null,
    actor: ev.actor || 'server',
    kind: ev.kind || ev.type || 'note',
    payload: p,
  };
}

export function normCard(c) {
  if (!c || typeof c !== 'object') return null;
  const n = num(c.num != null ? c.num : (c.card_num != null ? c.card_num : c.id));
  if (n == null) return null;
  const last = c.last_event ? normEvent(c.last_event) : null;
  const q = c.question || c.open_question
    || (Array.isArray(c.questions) ? c.questions.find((x) => x && !x.answered_at) : null);
  return {
    num: n,
    state: c.state || 'queued',
    title: c.title || firstLine(c.body || '', 90) || `Card #${n}`,
    body: c.body || '',
    batch_id: c.batch_id || null,
    agent_name: c.agent_name || c.agent || null,
    worktree: c.worktree || null,
    branch: c.branch || null,
    bounce_count: c.bounce_count || 0,
    pinned: !!c.pinned,
    dup_of: c.dup_of != null ? num(c.dup_of) : null,
    long_running: !!c.long_running,
    created_at: c.created_at || null,
    updated_at: c.updated_at || null,
    queue_position: c.queue_position != null ? num(c.queue_position)
      : (c.queue_pos != null ? num(c.queue_pos) : null),
    last_event: last,
    last_activity_at: c.last_activity_at || c.last_activity || (last && last.ts) || c.updated_at || c.created_at,
    question: q ? normQuestion(q) : null,
    reason: c.reason || null,
    error: c.error || null,
    evidence: c.evidence ? (c.evidence.packet || c.evidence) : null,
    attachments: Array.isArray(c.attachments) ? c.attachments : [],
    silent: c.silent != null ? !!c.silent : null,
    // The staleness sweep's verdict, straight from the server (this normalizer
    // builds an explicit shape, so a field it doesn't name simply doesn't exist
    // in the tab).
    stuck: !!c.stuck,
    state_since: c.state_since || c.updated_at || null,
  };
}

function normQuestion(q) {
  if (!q) return null;
  if (typeof q === 'string') return { id: null, text: q, options: null };
  let options = q.options;
  if (typeof options === 'string') { try { options = JSON.parse(options); } catch { options = null; } }
  if (Array.isArray(options)) {
    options = options.map((o) => (typeof o === 'string' ? { label: o, value: o } : {
      label: o.label || o.text || o.value || '', value: o.value != null ? o.value : (o.label || o.text || ''),
    })).filter((o) => o.label);
  } else options = null;
  return {
    id: q.id != null ? q.id : (q.question_id != null ? q.question_id : null),
    text: q.text || q.question || '',
    options,
    answered_at: q.answered_at || null,
  };
}

// online | busy | offline. `busy` means the session is attached and polling but
// its drain cursor is behind — a working session, not a missing one, so it never
// gets the offline banner.
const SESSION_STATUSES = ['online', 'busy', 'offline'];

function normSession(board) {
  let s = board.session != null ? board.session : board.session_status;
  if (s == null && board.session_online != null) s = board.session_online ? 'online' : 'offline';
  // A flat `cursor` on the payload wins nothing over session.cursor — they are
  // the same number; either shape is accepted so older/newer servers both work.
  const flat = num(board.cursor);
  if (typeof s === 'string') {
    const st = SESSION_STATUSES.includes(s) ? s : (s === 'offline' ? 'offline' : 'online');
    return { status: st, online: st !== 'offline', since: null, note: null, cursor: flat, waiterSeconds: null };
  }
  if (s && typeof s === 'object') {
    const raw = s.status || s.state;
    // An older server sends only online/offline; a newer one can also say busy.
    let status = SESSION_STATUSES.includes(raw) ? raw : null;
    if (status == null) {
      const online = s.online != null ? !!s.online : (raw ? raw !== 'offline' : true);
      status = online ? 'online' : 'offline';
    }
    const cursor = s.cursor != null ? num(s.cursor) : flat;
    return {
      status,
      online: status !== 'offline',
      since: s.since || s.last_seen || s.last_seen_at || null,
      note: s.note || null,
      cursor: cursor != null ? cursor : null,
      waiterSeconds: num(s.seconds_since_waiter),
    };
  }
  return { status: 'online', online: true, since: null, note: null, cursor: flat, waiterSeconds: null };
}

/** The session drained up to `seq`. Monotonic: a cursor never walks backwards
 *  in the UI, so a stale payload can't un-see a message. */
export function applyCursor(seq) {
  const n = num(seq);
  if (n == null) return false;
  const cur = store.session.cursor;
  if (cur != null && n <= cur) return false;
  store.session = { ...store.session, cursor: n };
  return true;
}

// ---- board ---------------------------------------------------------------

export function applyBoard(board) {
  if (!board || typeof board !== 'object') return;
  const sprint = board.sprint && typeof board.sprint === 'object' ? board.sprint : {};
  store.sprint = {
    id: sprint.id != null ? sprint.id : null,
    title: sprint.title || board.title || 'Sprint',
    opened_at: sprint.opened_at || null,
    closed_at: sprint.closed_at || null,
    hold_mode: !!(sprint.hold_mode != null ? sprint.hold_mode
      : (board.hold_mode != null ? board.hold_mode : sprint.hold)),
  };
  const sess = normSession(board);
  // Keep the furthest cursor we've been told about: a board fetch that raced a
  // live cursor frame must not un-see messages the session has already read.
  const known = store.session && store.session.cursor;
  if (known != null && (sess.cursor == null || sess.cursor < known)) sess.cursor = known;
  store.session = sess;
  if (board.column_of && typeof board.column_of === 'object') store.columnOf = board.column_of;

  const list = Array.isArray(board.cards) ? board.cards : [];
  const next = new Map();
  for (const raw of list) {
    const c = normCard(raw);
    if (c) next.set(c.num, c);
  }
  store.cards = next;

  // drop optimistic patches the server has caught up with
  for (const [n, patch] of Array.from(store.patches.entries())) {
    const card = next.get(n);
    if (!card) { if (Date.now() - patch.ts > 15000) store.patches.delete(n); continue; }
    if (card.state === patch.state || Date.now() - patch.ts > 15000) store.patches.delete(n);
  }
  // drop pending submissions the board now knows about
  if (store.pending.length) {
    store.pending = store.pending.filter((p) => !(p.num && next.has(p.num)));
  }

  const sidebar = board.sidebar;
  if (Array.isArray(sidebar)) {
    const lines = sidebar.map(normEvent).filter(Boolean);
    const texts = new Set(lines.map((e) => e.payload && e.payload.text));
    // keep any of our own lines the board has not caught up with yet
    const echoes = store.sidebar.filter((e) => e.localEcho && !texts.has(e.payload && e.payload.text));
    store.sidebar = lines.concat(echoes);
  }

  const seq = num(board.seq != null ? board.seq : board.last_seq);
  if (seq != null) store.seq = Math.max(store.seq, seq);
  store.loaded = true;
}

export function isSidebarEvent(ev) {
  // Mirrors the server's sidebar projection exactly: sprint-level user/session
  // lines only, never worker telemetry.
  return ev.card_num == null && (ev.actor === 'user' || ev.actor === 'session')
    && (ev.kind === 'chat' || ev.kind === 'note' || ev.kind === 'answer');
}

/**
 * Fold live events into the local projection. Returns which cards flipped into an
 * attention state so the caller can chime/badge exactly once.
 */
export function applyEvents(events) {
  const out = { attention: [], touched: new Set(), sidebar: false, needsBoard: false };
  for (const raw of events) {
    const ev = normEvent(raw);
    if (!ev) continue;
    if (ev.seq != null) store.seq = Math.max(store.seq, ev.seq);

    if (isSidebarEvent(ev)) {
      // the server's own copy supersedes our optimistic echo
      if (ev.actor === 'user') {
        store.sidebar = store.sidebar.filter((e) => !(e.localEcho && e.payload.text === ev.payload.text));
      }
      if (!store.sidebar.some((e) => e.seq != null && e.seq === ev.seq)) store.sidebar.push(ev);
      out.sidebar = true;
      // The Chat button is the only "there's something here" signal in the
      // product: a line from the session that arrived while you were not
      // looking at the chat lights it gold, and opening the chat clears it.
      if (ev.actor === 'session' && !(store.chatOpen && !store.detail)) store.unseen = true;
      continue;
    }
    if (ev.card_num == null) { out.needsBoard = true; continue; }

    out.touched.add(ev.card_num);
    const card = store.cards.get(ev.card_num);
    if (!card) { out.needsBoard = true; continue; }

    if (ev.kind === 'state') {
      const to = ev.payload.to;
      if (to) {
        const before = card.state;
        card.state = to;
        card.state_since = ev.ts || card.state_since;
        if (to !== 'needs_you') card.question = null;
        if (ev.payload.reason) card.reason = ev.payload.reason;
        if (before !== to) card.stuck = false;   // it moved: the sweep re-arms
        store.patches.delete(card.num);
        if (before !== to && (to === 'needs_you' || to === 'ready')) out.attention.push(card.num);
      }
    } else if (ev.kind === 'question') {
      card.question = normQuestion(ev.payload.question || ev.payload);
      if (card.state !== 'needs_you') { card.state = 'needs_you'; out.attention.push(card.num); }
      else out.attention.push(card.num);
    } else if (ev.kind === 'answer') {
      if (card.question) card.question = null;
      if (card.state === 'needs_you') card.state = 'in_progress';
    } else if (ev.kind === 'evidence') {
      if (ev.payload.ok !== false && ev.payload.packet) card.evidence = ev.payload.packet;
    } else if (ev.kind === 'error') {
      card.error = ev.payload.text || card.error;
    } else if (ev.kind === 'agent_silent') {
      card.silent = true;
    } else if (ev.kind === 'stuck') {
      // The server's staleness sweep. Sticks until the card actually moves —
      // the transition above is what clears it, because moving the card is the
      // only thing that proves somebody dealt with it.
      card.stuck = true;
    }
    if (ev.actor === 'worker' || ev.actor === 'session' || ev.actor === 'user') {
      card.last_activity_at = ev.ts || card.last_activity_at;
      if (ev.actor === 'worker') card.silent = false;
    }
    card.last_event = ev;
    if (ev.kind === 'submitted' || ev.kind === 'verdict') out.needsBoard = true;
  }
  return out;
}

// ---- derived -------------------------------------------------------------

export function cardState(card) {
  const p = store.patches.get(card.num);
  return p ? p.state : card.state;
}

export function isSilent(card, now = Date.now()) {
  const st = cardState(card);
  if (st !== 'in_progress' && st !== 'triaging') return false;
  if (card.long_running) return false;
  if (card.silent === true) return true;
  const t = ms(card.last_activity_at);
  if (t == null) return false;
  return now - t > SILENT_MS;
}

export function columns() {
  const buckets = new Map(COLUMNS.map((c) => [c.key, []]));
  for (const card of store.cards.values()) {
    buckets.get(columnOf(cardState(card))).push(card);
  }
  // A card you just dropped is on the board before the server has confirmed it.
  for (const p of store.pending) buckets.get('waiting').unshift(p);
  const now = Date.now();
  for (const [key, arr] of buckets) arr.sort(sorterFor(key, now));
  return COLUMNS.map((c) => ({ ...c, cards: buckets.get(c.key) }));
}

function sorterFor(key, now) {
  const oldestFirst = (a, b) => (ms(a.last_activity_at) || 0) - (ms(b.last_activity_at) || 0);
  const newestFirst = (a, b) => (ms(b.updated_at || b.last_activity_at) || 0) - (ms(a.updated_at || a.last_activity_at) || 0);
  const pin = (a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
  const wrap = (fn) => (a, b) => pin(a, b) || fn(a, b) || (a.num || 0) - (b.num || 0);
  if (key === 'waiting') return wrap((a, b) => {
    // held sinks below queued: it is the pile nobody has been told to start
    const ha = cardState(a) === 'held' ? 1 : 0;
    const hb = cardState(b) === 'held' ? 1 : 0;
    if (ha !== hb) return ha - hb;
    const qa = a.queue_position == null ? 9999 : a.queue_position;
    const qb = b.queue_position == null ? 9999 : b.queue_position;
    return qa - qb;
  });
  if (key === 'in_motion') return wrap((a, b) => (isSilent(b, now) ? 1 : 0) - (isSilent(a, now) ? 1 : 0) || oldestFirst(a, b));
  if (key === 'done') return wrap(newestFirst);
  return wrap(oldestFirst);
}

/**
 * The Board's columns — a different grouping from the List's reading order, on
 * the user's ruling: "column 1 should have 3 sections (when needed) queued,
 * then held, then blocked … then column 2 is in progress, then column 3 is
 * needs you, then column 4 is two sections: awaiting review and complete".
 *
 * So the Board is left-to-right the life of a card, and the two things that are
 * on YOU are separated: a question (Needs you) is not the same as a finished
 * branch waiting for your ack (Awaiting review). The List keeps its own
 * grouping — it is a reading order, not a pipeline.
 */
export const BOARD_COLUMNS = [
  {
    key: 'waiting',
    board: 'Waiting',
    dot: 'var(--faint)',
    empty: 'Nothing waiting.',
    sections: [
      { key: 'queued', label: 'Queued', states: ['queued'], empty: 'Nothing queued.' },
      { key: 'held', label: 'Held', states: ['held'] },
      {
        key: 'blocked',
        label: 'Blocked',
        states: ['blocked', 'failed', 'stale'],
        note: BLOCKED_NOTE,
      },
    ],
  },
  {
    key: 'in_motion',
    board: 'In progress',
    dot: 'var(--good)',
    empty: 'No agent is running.',
    sections: [{ key: 'in_motion', label: null, states: ['triaging', 'in_progress'] }],
  },
  {
    key: 'needs_you',
    board: 'Needs you',
    dot: 'var(--accent)',
    empty: 'Nothing needs you.',
    sections: [{ key: 'needs_you', label: null, states: ['needs_you'] }],
  },
  {
    key: 'review',
    board: 'Review',
    dot: 'var(--good)',
    empty: 'Nothing to review.',
    sections: [
      { key: 'awaiting', label: 'Awaiting review', states: ['ready', 'integrating'] },
      {
        key: 'complete',
        label: 'Complete',
        states: ['completed', 'rejected', 'duplicate', 'canceled'],
        quiet: true,
      },
    ],
  },
];

const BOARD_COL_OF = {};
for (const c of BOARD_COLUMNS) {
  for (const s of c.sections) for (const st of s.states) BOARD_COL_OF[st] = [c.key, s.key];
}

/** The Board's columns, each with its (non-empty) sections filled in. */
export function boardColumns() {
  const buckets = new Map();
  for (const c of BOARD_COLUMNS) for (const s of c.sections) buckets.set(c.key + '/' + s.key, []);
  const put = (card, state) => {
    const at = BOARD_COL_OF[state] || ['waiting', 'queued'];
    buckets.get(at[0] + '/' + at[1]).push(card);
  };
  for (const card of store.cards.values()) put(card, cardState(card));
  // A card you just dropped is on the board before the server has confirmed it.
  for (const p of store.pending) buckets.get('waiting/queued').unshift(p);

  const now = Date.now();
  return BOARD_COLUMNS.map((c) => {
    const secs = c.sections.map((s) => {
      const cards = buckets.get(c.key + '/' + s.key);
      cards.sort(sorterFor(SORT_AS[s.key] || s.key, now));
      return { ...s, cards };
    });
    return { ...c, sections: secs, count: secs.reduce((n, s) => n + s.cards.length, 0) };
  });
}

/** Sections keyed for the callers that want one by name. */
export function sections() {
  const out = {};
  for (const col of columns()) out[col.key] = col;
  return out;
}

/**
 * The 4px meter. Each segment grows by its count, so the bar IS the shape of the
 * sprint — no chart, no axis, and no number on it bigger than the legend's 12.5px.
 * A segment with nothing in it is dropped rather than drawn as a sliver.
 */
export function meterSegments(secs = sections()) {
  return METER
    .map((m) => {
      const n = (secs[m.key] && secs[m.key].cards.length) || 0;
      return { ...m, count: n, label: `${n} ${m.word}` };
    })
    .filter((m) => m.count > 0);
}

/** The one-line headline beside the sprint title. Zero-count parts are dropped. */
export function headline(secs = sections()) {
  const n = (k) => (secs[k] && secs[k].cards.length) || 0;
  const parts = [];
  if (n('needs_you')) parts.push(`${n('needs_you')} need you`);
  if (n('in_motion')) parts.push(`${n('in_motion')} running`);
  if (n('blocked')) parts.push(`${n('blocked')} blocked`);
  if (n('waiting')) parts.push(`${n('waiting')} waiting`);
  return parts.join(' · ');
}

/**
 * Which of the two "this one is on me" shapes a Needs-you card is:
 *   question — an agent is stuck on an answer only you can give
 *   signoff  — an evidence packet is waiting on your verdict
 */
export function needsKind(card) {
  const st = cardState(card);
  if (st === 'ready' || st === 'integrating') return 'signoff';
  return 'question';
}

/**
 * The In-motion row's 2px hairline and state label.
 *
 * The bar is NOT progress — nothing on the wire knows how far along a job is,
 * and inventing a percentage would be the spinner the spec bans, only worse
 * because it looks like a fact. It is *recency*: full the moment the agent says
 * something, draining to empty across the five-minute silence window that trips
 * the server's own agent_silent timer. Full bar = just heard from it; empty bar
 * = the session is about to go check on it. Long jobs suppress the drain, since
 * silence there is expected and the amber would be a lie.
 */
export function motionState(card, now = Date.now()) {
  const st = cardState(card);
  const quiet = isSilent(card, now);
  const since = ms(card.last_activity_at);
  const elapsed = since == null ? SILENT_MS : Math.max(0, now - since);
  let pct = Math.round(100 * (1 - Math.min(1, elapsed / SILENT_MS)));

  if (card.long_running) {
    return { key: 'long', label: 'long job', color: 'var(--info)', pct: 100,
      title: 'flagged as a long job — the five-minute silence timer is suppressed' };
  }
  if (quiet) {
    // The number has to be how long the AGENT has been silent. Once the server
    // appends its own agent_silent event, last_activity_at is the age of that
    // notice, not of the silence — so below the window we say "quiet" flat
    // rather than "quiet just now", which would be both wrong and absurd.
    const stale = elapsed >= SILENT_MS ? ` ${age(card.last_activity_at, now)}` : '';
    return { key: 'quiet', label: `quiet${stale}`, color: 'var(--warn)', pct: 0,
      title: 'no word from the agent for five minutes — the session is checking on it' };
  }
  if (st === 'triaging') {
    return { key: 'triaging', label: 'triaging', color: 'var(--alt)', pct: Math.max(pct, 12),
      title: 'the agent is reading the card and restating it before it writes anything' };
  }
  return { key: 'active', label: `active · ${age(card.last_activity_at)}`, color: 'var(--good)', pct: Math.max(pct, 6),
    title: 'how recently the agent said something — the bar drains over five minutes of silence' };
}

/** The machine reason a blocked card carries, and whether it reads as bad news. */
export function blockedReason(card) {
  const st = cardState(card);
  const raw = (st === 'failed' && card.error) ? card.error : (card.reason || card.error || '');
  const text = firstLine(raw, 160) || (st === 'failed' ? 'the agent died mid-run' : 'blocked — no reason recorded');
  const bad = st === 'failed' || /^(ci_red|ci red|failed|error)/i.test(String(raw).trim());
  return { text, bad };
}

/** The word on a Queued-&-held pill. */
export function waitingMark(card) {
  if (card.pendingSubmit) return card.error ? 'not sent' : 'sending';
  if (card.pinned) return 'pinned';
  return cardState(card) === 'held' ? 'held' : 'queued';
}

/** Which expanded details you left open, so a re-render doesn't slam them shut. */
export function detailOpen(key, value) {
  if (value === undefined) return store.expanded.has(key);
  if (value) store.expanded.add(key);
  else store.expanded.delete(key);
  return value;
}

export function draft(key, value) {
  if (value === undefined) return store.drafts.get(key) || '';
  if (value === null || value === '') store.drafts.delete(key);
  else store.drafts.set(key, value);
  return value;
}

/**
 * Images pasted into a composer but not sent yet. Same reasoning as `draft`:
 * the rail is rebuilt whenever the card it is showing moves, and a screenshot
 * you just pasted must survive that — it is part of the message you are still
 * writing.
 */
export function attachedImages(key, value) {
  if (value === undefined) return store.attached.get(key) || [];
  if (!value || !value.length) store.attached.delete(key);
  else store.attached.set(key, value);
  return value;
}

// ---- event → plain english ----------------------------------------------

export function eventText(ev) {
  // The server puts a human one-liner in `text` on EVERY event; a couple of kinds
  // get a nicer local rendering from their structured fields.
  const p = ev.payload || {};
  const direct = p.text;
  switch (ev.kind) {
    case 'state': {
      const to = p.to;
      const label = STATE_LABEL[to] || to || 'updated';
      return p.reason ? `→ ${label} — ${p.reason}` : `→ ${label}`;
    }
    case 'submitted': return direct || 'Submitted';
    case 'question': return direct || 'Asked a question';
    case 'answer': return direct || 'Answered';
    case 'agent_silent': return direct || 'No word from the agent — checking on it';
    // The sweep's own words, prefixed so the line says what kind of line it is.
    case 'stuck': return `stuck: ${direct || 'parked with nobody acting on it'}`;
    case 'evidence': return direct || 'Posted an evidence packet';
    case 'verdict': {
      const v = p.verdict;
      const label = v === 'approve' ? 'Approved' : v === 'bounce' ? 'Bounced back' : v === 'reject' ? 'Rejected' : 'Verdict';
      return p.notes ? `${label} — ${p.notes}` : label;
    }
    case 'error': return direct || 'Error';
    default: return direct || '';
  }
}

export const SYSTEM_KINDS = new Set(['state', 'agent_silent', 'stuck', 'verdict', 'evidence', 'error', 'submitted']);

/**
 * The server's staleness sweep has flagged this card and it hasn't moved since.
 *
 * Deliberately NOT a second silence rule computed in the browser: the server
 * owns the clock (five states, five thresholds, a backoff ladder), and the tab
 * just reports what it was told. A card that moves clears the flag on its own
 * `state` event, so this can never be stale in the other direction.
 */
export function isStuck(card) {
  return !!(card && card.stuck);
}

export const STUCK_HINT = 'parked here longer than it should be — the board said so on the card';

// ---- what happened to the message I just sent ---------------------------
//
// Three honest states, each standing on something the server actually told us:
//   sending…          the POST is still in flight — we know nothing yet
//   landed            the server gave the event a seq: it is in the log, durably
//   session is on it  the session's drain cursor has passed that seq — it read it
// Nothing here is a timer or an animation. If we can't prove a step, we don't
// claim it: an unknown cursor leaves the message at "landed" forever, which is
// true, rather than pretending it was picked up.

export function messageStatus(ev) {
  if (!ev) return null;
  if (ev.failed) {
    // Never a dead end. If the line knows how to re-send itself the pill says
    // so and IS the button; if it somehow doesn't, it still says out loud that
    // nothing landed rather than sitting on "sending…" forever.
    return typeof ev.retry === 'function'
      ? { key: 'failed', retry: true, label: 'failed to send — tap to retry',
        title: 'the board never took this — tap to send it again (same key, so it cannot land twice)' }
      : { key: 'failed', label: 'failed to send',
        title: 'the board never acknowledged this — send it again' };
  }
  if (ev.pending || ev.local || ev.seq == null) {
    return { key: 'sending', label: 'sending…', title: 'still on its way to the board' };
  }
  const cursor = store.session ? store.session.cursor : null;
  if (cursor != null && cursor >= ev.seq) {
    return {
      key: 'seen',
      label: 'session is on it',
      title: `the session has read this message (drained past #${ev.seq})`,
    };
  }
  if (store.session && store.session.online === false) {
    return {
      key: 'landed',
      label: 'landed — session offline',
      title: 'saved on the board; the session is not reading right now, so it waits in the queue',
    };
  }
  return {
    key: 'landed',
    label: 'landed',
    title: 'saved on the board; the session has not read it yet',
  };
}
