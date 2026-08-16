// The client-side projection of the board. Tolerant normalizers: the server owns the
// truth, we only ever *display* it, so unknown/missing fields degrade instead of throwing.
import { age, firstLine, ms, h } from './util.js';
import { phaseOf } from './phase.js';
import { reviewUnits } from './units.js';

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
    // `integrating` is NOT here. User, verbatim: "Why do these cards stay in
    // 'needs you' once they're already approved?" — the moment you approve, the
    // card is the session's job, not yours. It runs in In motion as `merging`.
    states: ['needs_you', 'ready'],
    dot: 'var(--accent)',
  },
  {
    key: 'in_motion',
    title: 'In motion',
    board: 'In progress',
    // `integrating` is work being done BY the session — a phase like any other.
    states: ['triaging', 'in_progress', 'integrating'],
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
  // What the session running this board calls ITSELF ("Chuck"), or '' if it
  // never introduced itself. Not the sprint's name: the board is named after
  // the work, this is the colleague doing it. Every place that would print
  // "Session" prints this instead — see `sessionLabel`.
  agentName: '',
  columnOf: null,         // server-advised state -> column map (board.column_of)
  settings: null,         // dispatch policy: model, executors, concurrency
  // `cursor` is the session's real drain cursor: every event with seq <= cursor
  // has been read by the session. Never guessed — the server is the only writer.
  session: { status: 'online', online: true, since: null, cursor: null, waiterSeconds: null },
  cards: new Map(),       // num -> card
  patches: new Map(),     // num -> {state, ts} optimistic, reconciled on next board
  pending: [],            // submitted-but-unconfirmed cards
  sidebar: [],            // sprint-level chat events
  seq: 0,
  detail: null,           // {num, card, timeline, evidence, attachments, pendingLines}
  // Which work unit's outline the rail is showing, as {lead: <card num>} — the
  // unit itself is never stored, only the card you opened it from (card #55).
  // A unit is a projection over the cards under review, so it is recomputed
  // every paint and dissolves by itself when its members stop needing you.
  unit: null,
  drafts: new Map(),      // freeform text kept across re-renders
  bouncing: new Set(),    // card nums whose verdict row is mid-BOUNCE (see below)
  attached: new Map(),    // composer key -> images pasted but not sent yet
  expanded: new Set(),    // event keys whose long version you opened (see detail.js)
  doneOpen: false,
  // Whether the closed list is showing everything or just the newest 20. One
  // flag for both surfaces: the List's Done and the Board's Complete are never
  // on screen at the same time.
  doneMore: false,
  loaded: false,
  // How many report documents THIS sprint has. The header's Reports link exists
  // only when this is > 0 — user, verbatim: "link should only appear once
  // there's a report within the sprint".
  reports: 0,
  // Provider limit windows that are open ("fable until 11:50pm"). One quiet
  // board-level line each, and nothing else — the cards that got moved are
  // already wearing their model tag.
  limits: [],
  // The account window, if the whole Claude account is out: the big banner
  // every board on the machine shows, and the Resume button in it. Null means
  // the server said there is none.
  accountLimit: null,
  // Dead-session autoheal (card #68). Null means the server has no opinion —
  // older code, or nothing to say. When the session is offline AND the hub has
  // already tried to wake it, the offline banner says so instead of repeating
  // "items will queue" at somebody who is watching a board nobody is reading.
  autoheal: null,

  // ---- rail + layout (client only) ----------------------------------------
  // Only one thing owns the rail at a time: a card, or the session chat.
  view: 'list',           // 'list' | 'board' — persisted
  chatOpen: false,        // session chat wants the rail
  unseen: false,          // a session line arrived while the rail was closed
};

// Who a line is FROM, as a human would say it. The session is the only actor
// whose label is not fixed: user ruling, verbatim — "I also meant that the
// session agent gave themselves a name. Like "Chuck"". Once it has, the board
// calls it by that name everywhere a message is attributed, and falls straight
// back to "Session" for a session that never introduced itself.
const ACTOR_LABELS = { user: 'You', session: 'Session', worker: 'Agent', server: 'Board' };

/** The session's name if it has one, else the generic label. */
export function sessionLabel() {
  return store.agentName || ACTOR_LABELS.session;
}

/** The label for any actor, with the session's chosen name folded in. */
export function actorLabel(actor) {
  if (actor === 'session') return sessionLabel();
  return ACTOR_LABELS[actor] || ACTOR_LABELS.worker;
}

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
    // Which card this one is waiting on, and why (#61). Structural, so the
    // rail can link it and the face can mark it; null on nearly every card,
    // which is why the marker is an exception rather than chrome.
    blocked_by: c.blocked_by != null ? num(c.blocked_by) : null,
    blocked_reason: c.blocked_reason || null,
    long_running: !!c.long_running,
    // What SHAPE of card this is. Anything a server we don't recognise sends is
    // work — the kind that has always existed — so an old board degrades to the
    // board it already was rather than losing cards off the page.
    kind: c.kind === 'conversation' ? 'conversation' : 'work',
    // The derived highlight, straight off the server: {state, last_seen_seq,
    // latest_incoming_seq, latest_user_seq, unread}. Never computed here — the
    // whole point is that one place owns the rule.
    conversation: c.conversation && typeof c.conversation === 'object'
      ? { ...c.conversation } : null,
    // Which model the agent was dispatched on, and what the sprint's default
    // is, so `modelTag` can decide whether it is worth drawing. This
    // normalizer builds an explicit shape — a field it doesn't name does not
    // exist in the tab, which is exactly how the tag silently didn't render.
    model: c.model || null,
    // Why it is on that model ("fable limited until 23:50"). The board-level
    // limit line reads this to say what the work moved TO.
    model_reason: c.model_reason || null,
    default_model: c.default_model || null,
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
    // What the agent says it is doing right now, and for how long it expects to
    // be doing it. Projected by the server from the event log.
    phase: c.phase || null,
    phase_since: c.phase_since || null,
    phase_expected_seconds: c.phase_expected_seconds != null ? num(c.phase_expected_seconds) : null,
    // Who ran this card and with what. Null on both means "the board's
    // defaults", and `dispatch` is the server's resolution of that — including
    // `is_default`, which is the only thing the face's tag asks about.
    executor: c.executor || null,
    model: c.model || null,
    dispatch: c.dispatch || null,
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
    // A DECISION REQUEST hands over the thing you need in order to answer —
    // a live URL, screenshots, a note. Null on a plain question, and null on
    // a board whose server is too old to have the field at all.
    artifacts: normArtifacts(q.artifacts),
    answered_at: q.answered_at || null,
  };
}

/**
 * What the agent attached to its question. Card #50, user verbatim: "needs you
 * is where we talk through things. review means the session genuinely thinks
 * the card is 100% complete. needs you is that the card is waiting for my input
 * before it can keep moving forward." Tolerant like every normalizer here: a
 * shape we do not recognise degrades to nothing rather than throwing.
 */
export function normArtifacts(a) {
  let raw = a;
  if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch { raw = null; } }
  if (!raw || typeof raw !== 'object') return null;
  const url = typeof raw.url === 'string' && /^https?:\/\//.test(raw.url.trim()) ? raw.url.trim() : null;
  const notes = typeof raw.notes === 'string' && raw.notes.trim() ? raw.notes.trim() : null;
  const attachments = Array.isArray(raw.attachments) ? raw.attachments.filter(Boolean) : [];
  if (!url && !notes && !attachments.length) return null;
  return { url, notes, attachments };
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
  // A name only ever arrives from the server, and an older board that does not
  // send the field is not the same as a session that dropped its name.
  if (typeof board.agent_name === 'string') store.agentName = board.agent_name.trim();
  const sess = normSession(board);
  // Keep the furthest cursor we've been told about: a board fetch that raced a
  // live cursor frame must not un-see messages the session has already read.
  const known = store.session && store.session.cursor;
  if (known != null && (sess.cursor == null || sess.cursor < known)) sess.cursor = known;
  store.session = sess;
  if (board.column_of && typeof board.column_of === 'object') store.columnOf = board.column_of;
  // The board's dispatch policy rides along so the Settings panel and the card
  // faces are never a second fetch behind what the board just said.
  if (board.settings && typeof board.settings === 'object') store.settings = board.settings;

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

  // Scoped to the open sprint by the server — never a lifetime total, because
  // the header link is a statement about THIS sprint.
  const reports = num(board.reports);
  store.reports = reports != null && reports > 0 ? reports : 0;

  // An older server sends no `limits` at all; that is not the same as "the
  // limits ended", so an absent field leaves what we have alone and only an
  // actual list replaces it.
  if (Array.isArray(board.limits)) {
    store.limits = board.limits.map(normLimit).filter(Boolean);
  }
  // The account window, hoisted by the server with its words and the id the
  // Resume button posts to. `undefined` means an older server (leave what we
  // have); `null` means the server looked and there is none.
  if (board.account_limit !== undefined) {
    store.accountLimit = board.account_limit ? normLimit(board.account_limit) : null;
  }
  // Same contract as the limits above: `undefined` is an older server with no
  // opinion (leave what we have), an object replaces it.
  if (board.autoheal !== undefined) {
    store.autoheal = board.autoheal ? normAutoheal(board.autoheal) : null;
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
        // A card that moved on is not still testing. Same rule the server
        // projects with, applied live so the chip never outlives its state.
        card.phase = null;
        card.phase_since = null;
        card.phase_expected_seconds = null;
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
    if (ev.payload && typeof ev.payload.phase === 'string' && ev.payload.phase.trim()) {
      // A phase-carrying event is the newest word on what is happening now.
      card.phase = ev.payload.phase.trim();
      card.phase_since = ev.ts || Date.now() / 1000;
      const exp = Number(ev.payload.expected_seconds);
      card.phase_expected_seconds = exp > 0 ? exp : null;
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
  // A phase still inside the time it claimed is not silence — the agent said in
  // advance that this stretch would be quiet. Same rule the server's timer uses;
  // this is the browser agreeing with it between board fetches.
  const ph = phaseOf(card, now);
  if (ph && ph.expectMs && !ph.overdue) return false;
  if (card.silent === true) return true;
  const t = ms(card.last_activity_at);
  if (t == null) return false;
  return now - t > SILENT_MS;
}

// ---- conversations -------------------------------------------------------
//
// User, verbatim: "we're discovering we need another card type for an ongoing
// conversation thread. I think it's a lower section in the 'needs you' column
// where the card gets highlighted if there's an unread and unseen message. once
// I see the message, the highlighting dims, and once I respond the highlight
// goes away completely. with russ, this lets me have distinct conversations on
// specific needs".
//
// A conversation is not work: it has no queue position, no agent, no packet and
// no verdict, so it is kept OUT of the four work sections entirely rather than
// filtered back out of each of them. It has one home, at the bottom of Needs
// you, in both layouts.

export function isConversation(card) {
  return !!(card && card.kind === 'conversation');
}

/**
 * unseen — they spoke after you last looked (strong highlight)
 * seen   — you have looked since, but you have not replied (dimmed)
 * clear  — your own message is the latest (nothing)
 *
 * Derived server-side from the log and one read receipt; the tab only reports
 * it. A card whose server never heard of conversations reads `clear`, which is
 * the state that asks for nothing.
 */
export function conversationState(card) {
  const c = card && card.conversation;
  const s = c && c.state;
  return s === 'unseen' || s === 'seen' ? s : 'clear';
}

export const CONVERSATION_HINT = {
  unseen: 'a new message you have not opened yet',
  seen: 'you have read it — it is waiting on your reply',
  clear: 'you spoke last — nothing is waiting on you',
};

/** Every open conversation, loudest first, then by most recent word. */
export function conversations() {
  const rank = { unseen: 0, seen: 1, clear: 2 };
  const out = [];
  for (const card of store.cards.values()) {
    if (isConversation(card) && cardState(card) === 'conversation') out.push(card);
  }
  out.sort((a, b) => (rank[conversationState(a)] - rank[conversationState(b)])
    || ((ms(b.last_activity_at) || 0) - (ms(a.last_activity_at) || 0))
    || (a.num || 0) - (b.num || 0));
  return out;
}

export function columns() {
  const buckets = new Map(COLUMNS.map((c) => [c.key, []]));
  for (const card of store.cards.values()) {
    // A live conversation has its own section and is in none of these; a
    // CLOSED one is an ordinary closed card and falls into Done like any other.
    if (isConversation(card) && cardState(card) === 'conversation') continue;
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
    sections: [{ key: 'in_motion', label: null,
                 states: ['triaging', 'in_progress', 'integrating'] }],
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
      // Awaiting review means awaiting YOU. An approved card is merging, and
      // it does that over in In progress until it lands in Complete.
      { key: 'awaiting', label: 'Awaiting review', states: ['ready'] },
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
  for (const card of store.cards.values()) {
    // Live conversations render in their own section under Needs you; a closed
    // one is an ordinary closed card and goes to Complete like any other.
    if (isConversation(card) && cardState(card) === 'conversation') continue;
    put(card, cardState(card));
  }
  // A card you just dropped is on the board before the server has confirmed it.
  for (const p of store.pending) buckets.get('waiting/queued').unshift(p);

  const now = Date.now();
  return BOARD_COLUMNS.map((c) => {
    const secs = c.sections.map((s) => {
      const cards = buckets.get(c.key + '/' + s.key);
      cards.sort(sorterFor(SORT_AS[s.key] || s.key, now));
      // Awaiting review counts WORK UNITS, because that is what it shows: one
      // card per unit (card #55). Every other section counts cards.
      const count = (c.key === 'review' && s.key === 'awaiting')
        ? reviewUnits(cards).length : cards.length;
      return { ...s, cards, count };
    });
    return { ...c, sections: secs, count: secs.reduce((n, s) => n + s.count, 0) };
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
      const n = countFor(m.key, secs);
      return { ...m, count: n, label: `${n} ${m.word}` };
    })
    .filter((m) => m.count > 0);
}

/**
 * How many things a section is asking of you. Everywhere but Needs you that is
 * simply how many cards are in it — but a review is ONE decision per work unit
 * (card #55), so six cards that shipped on one branch are one thing waiting on
 * you, and saying "6 need you" over a list showing one card is the old list
 * talking. The meter, the headline and the section head all count the same way.
 */
export function countFor(key, secs = sections()) {
  const cards = (secs[key] && secs[key].cards) || [];
  if (key !== 'needs_you') return cards.length;
  return needsYouCount(cards);
}

/** Open questions are one each; finished work is one per work unit. */
export function needsYouCount(cards) {
  const asks = cards.filter((c) => needsKind(c) === 'question');
  return asks.length + reviewUnits(cards.filter((c) => needsKind(c) !== 'question')).length;
}

/** The one-line headline beside the sprint title. Zero-count parts are dropped. */
export function headline(secs = sections()) {
  const n = (k) => countFor(k, secs);
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
 *
 * A DECLARED PHASE replaces all of that guessing while it is live: the label is
 * the phase and its own clock ("testing · 2m"), and the bar drains over the time
 * the agent said the phase would take. Amber only once the phase itself runs
 * past what it claimed — which is the whole point of card #32: a working card
 * must not look broken.
 */
export function motionState(card, now = Date.now()) {
  const st = cardState(card);
  const quiet = isSilent(card, now);
  const since = ms(card.last_activity_at);
  const elapsed = since == null ? SILENT_MS : Math.max(0, now - since);
  let pct = Math.round(100 * (1 - Math.min(1, elapsed / SILENT_MS)));

  // Approved, and the session is doing the git work. It is not a decision you
  // owe anybody — it is a job running, with a name for what it is doing.
  if (st === 'integrating') {
    return { key: 'merging', label: 'merging', color: 'var(--good)', pct: 100,
      title: 'you approved it — the session is rebasing, gating and merging the branch' };
  }
  // A declared phase outranks the recency bar's guesswork: it says what is
  // happening now, on its own clock, and the bar drains over the time the agent
  // said it would take instead of over the generic five-minute window.
  const ph = phaseOf(card, now);
  if (ph) {
    return {
      key: ph.overdue ? 'phase-late' : 'phase',
      label: ph.text,
      color: ph.overdue ? 'var(--warn)' : 'var(--good)',
      pct: ph.overdue ? 0 : Math.max(6, Math.round(100 * ph.left)),
      title: ph.title,
      phase: ph,
    };
  }

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
 * Are you in the middle of bouncing this card?
 *
 * User, verbatim: "once I hit 'Bounce' we should get rid of the 'Approve'
 * button — should just be a 'Submit bounce' button." Deciding to send something
 * back is a decision you have already made; leaving Approve sitting next to the
 * notes box you are typing into means one slip turns a bounce into a merge.
 * So Bounce opens a composing state — notes plus Submit bounce and Cancel, with
 * Approve and Reject gone until you send or back out.
 *
 * It lives here, beside the drafts, for the same reason drafts do: the row is
 * rebuilt whenever anything on the card moves, and a half-written bounce (and
 * the fact that you are writing one at all) has to survive that.
 */
export function bounceComposing(num, value) {
  if (value === undefined) return store.bouncing.has(num);
  if (value) store.bouncing.add(num);
  else store.bouncing.delete(num);
  return value;
}

/**
 * Images pasted into a composer but not sent yet. Same reasoning as `draft`:
 * the rail is rebuilt whenever the card it is showing moves, and a screenshot
 * you just pasted must survive that — it is part of the message you are still
 * writing.
 */
/**
 * The one key a card's composer files its half-written message under — words in
 * `drafts`, screenshots in `attached`.
 *
 * It lives here rather than in the rail because it is not only the rail's any
 * more: answering a decision request by CLICKING one of its options (card #66)
 * has to pick up the screenshot you pasted into the box under it, and the thread
 * that draws those buttons must be able to name the same drawer.
 */
export function cardComposerKey(num) { return 'card:' + num; }

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

/**
 * Which model this card's agent is running on, but ONLY when that is worth
 * saying: a badge that is on every card is decoration, and the thing the user
 * actually needs to see is the exception — "this one is on opus because fable
 * hit its usage limit and the first agent was killed."
 *
 * The server tells the tab what the default is (`default_model`, on the card
 * and on the board), so the tab never has to hold a copy of that list.
 */
export function modelTag(card) {
  if (!card || !card.model) return null;
  return card.model === card.default_model ? null : card.model;
}

export const MODEL_HINT = 'not the sprint default — this card was dispatched on a fallback model';

// ---- provider limit windows ----------------------------------------------
//
// A limit window is one fact — "fable is unavailable until 11:50pm" — and it
// gets one quiet line, in the same register as the session-offline banner. Not
// a modal, not a toast, no per-card chrome: the cards that moved are already
// wearing their model tag.

/** `11:50pm` — a reset time in this browser's own clock. */
export function clockLabel(ts) {
  const t = ms(ts);
  if (t == null) return '';
  const d = new Date(t);
  const h12 = d.getHours() % 12 || 12;
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${h12}:${mm}${d.getHours() < 12 ? 'am' : 'pm'}`;
}

export function normLimit(l) {
  if (!l || typeof l !== 'object') return null;
  const resets = ms(l.resets_at);
  if (resets == null) return null;
  return {
    id: l.id != null ? l.id : null,
    // "model" (one model went away, quiet line) or "account" (the whole Claude
    // account is out and NOTHING runs — the big banner). An older server sends
    // neither, and the thing it could only have meant is the quiet one.
    kind: l.kind === 'account' ? 'account' : 'model',
    model: l.model || '',
    resetsAt: resets,
    // Copy composed by the server, so the banner, the event log and the CLI
    // cannot tell the user three different stories. Absent on a model window.
    headline: l.headline || null,
    action: l.action || null,
    resumeLabel: l.resume_label || 'Resume',
    detail: l.detail || null,
    // The server's own rendering of the reset time, kept as the fallback for
    // the browser's — they agree unless the two are in different timezones,
    // and in that case the one in front of the user is the honest one.
    label: l.resets_at_label || null,
    source: l.source || null,
    note: l.note || null,
  };
}

/**
 * Dead-session autoheal, as the banner needs it (card #68).
 *
 * The board decides all of this; the tab only renders it. `lastAttemptLabel`
 * is the server's own clock rendering, kept for the same reason the limit
 * lines keep theirs — one composed string, no second opinion.
 */
export function normAutoheal(a) {
  if (!a || typeof a !== 'object') return null;
  return {
    registered: !!a.registered,
    window: a.tmux_window || null,
    dead: !!a.dead,
    reason: a.reason || null,
    attempts: num(a.attempts) || 0,
    maxAttempts: num(a.max_attempts) || 0,
    lastAttemptAt: ms(a.last_attempt_at),
    lastAttemptLabel: a.last_attempt_label || null,
    gaveUp: !!a.gave_up,
    gaveUpText: a.gave_up_text || null,
    deliveredButSilent: !!a.delivered_but_silent,
  };
}

/**
 * What the offline banner says after "session offline". Three endings, in
 * decreasing order of how much the user has to do about them:
 *
 *   * autoheal gave up  — go look at the terminal, in the server's own words.
 *   * a wake-up was sent — the machine is on it; here is when it tried.
 *   * nothing              — the original line, unchanged.
 *
 * Returns null when there is nothing extra worth saying, so the banner's
 * default text is never rewritten for the sake of it.
 */
export function autohealNote() {
  const a = store.autoheal;
  if (!a || !a.dead) return null;
  if (a.gaveUp && a.gaveUpText) return a.gaveUpText;
  if (a.lastAttemptLabel) return `revival attempted ${a.lastAttemptLabel}`;
  return null;
}

/**
 * Windows that are open RIGHT NOW. The server sends only these, but the tab
 * checks the clock again anyway: the page repaints on a 30s timer and a line
 * saying a model is limited until a time that has already passed is worse
 * than no line at all.
 */
export function activeLimits(now = Date.now()) {
  return store.limits.filter((l) => l.resetsAt > now && l.kind !== 'account');
}

/**
 * The account window, or null. Same clock re-check as the quiet lines: a
 * banner saying everything is stopped until a time that has already passed
 * would be the worst one on the page to leave up.
 *
 * `account_limit` is the server's hoisted copy; the array is the fallback, so
 * a board answering an older payload shape still raises the banner.
 */
export function accountLimit(now = Date.now()) {
  const live = (l) => l && l.kind === 'account' && l.resetsAt > now;
  if (live(store.accountLimit)) return store.accountLimit;
  return store.limits.find(live) || null;
}

/**
 * The sentence. "fable is rate-limited until 11:50pm — work is running on opus"
 *
 * The second half is only said when it is TRUE, and it is read off the board's
 * own cards: the models carrying work that was explicitly downgraded for a
 * limit (`model_reason` set by the session at dispatch). Nothing here guesses —
 * with no downgraded cards on the board the line is just the first half, which
 * is still the thing the user needed to know.
 */
export function limitLine(limit, cards = store.cards) {
  const until = clockLabel(limit.resetsAt) || limit.label || '';
  const head = `${limit.model} is rate-limited until ${until}`;
  const on = new Set();
  for (const card of cards.values()) {
    if (!card.model_reason || !card.model) continue;
    if (card.model === limit.model) continue;
    if (columnOf(card.state) === 'done') continue;
    on.add(card.model);
  }
  const fallbacks = Array.from(on).sort();
  if (!fallbacks.length) return head;
  const list = fallbacks.length === 1 ? fallbacks[0]
    : `${fallbacks.slice(0, -1).join(', ')} and ${fallbacks[fallbacks.length - 1]}`;
  return `${head} — work is running on ${list}`;
}

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

// ---- blocked by (#61) ----------------------------------------------------

/**
 * The quiet marker a blocked card's face or row carries: "waiting on #58".
 *
 * User verbatim: "when one card is blocked by another, show that in the card
 * details." The details are the rail's job; this is the two words that stop a
 * card looking abandoned from across the board. Null on every card with
 * nothing in its way, which is nearly all of them — a marker on every card
 * would say nothing.
 */
export function blockedByMark(card) {
  const n = card && card.blocked_by;
  if (n == null) return null;
  return h('span.blockedby-mark', {
    title: card.blocked_reason
      ? `waiting on #${n} — ${card.blocked_reason}`
      : `waiting on #${n} to land`,
  }, `waiting on #${n}`);
}
