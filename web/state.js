// The client-side projection of the board. Tolerant normalizers: the server owns the
// truth, we only ever *display* it, so unknown/missing fields degrade instead of throwing.
import { firstLine, ms } from './util.js';

export const SILENT_MS = 5 * 60 * 1000;   // spec: 5 minutes with no worker event

export const COLUMNS = [
  { key: 'held', title: 'Held', states: ['held'], hideWhenEmpty: true },
  { key: 'queued', title: 'Queued', states: ['queued'] },
  { key: 'in_progress', title: 'In progress', states: ['triaging', 'in_progress'] },
  { key: 'needs_you', title: 'Needs you', states: ['needs_you'] },
  { key: 'blocked', title: 'Blocked', states: ['blocked', 'failed', 'stale'] },
  { key: 'ready', title: 'Ready', states: ['ready', 'integrating'] },
  { key: 'done', title: 'Done', states: ['completed', 'rejected', 'duplicate', 'canceled'], collapsed: true },
];

// Fallback mapping. The server ships an advisory `column_of` on every board and
// that wins whenever it's there — the server owns the state machine, so it also
// owns which column a state belongs in.
const COL_OF = {};
for (const c of COLUMNS) for (const s of c.states) COL_OF[s] = c.key;
const COL_KEYS = new Set(COLUMNS.map((c) => c.key));

export function columnOf(state) {
  const server = store.columnOf && store.columnOf[state];
  if (server && COL_KEYS.has(server)) return server;
  return COL_OF[state] || 'queued';
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
  session: { online: true, since: null },
  cards: new Map(),       // num -> card
  patches: new Map(),     // num -> {state, ts} optimistic, reconciled on next board
  pending: [],            // submitted-but-unconfirmed cards
  sidebar: [],            // sprint-level chat events
  seq: 0,
  detail: null,           // {num, card, timeline, evidence, attachments, pendingLines}
  drafts: new Map(),      // freeform text kept across re-renders
  doneOpen: false,
  loaded: false,
};

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

function normSession(board) {
  let s = board.session != null ? board.session : board.session_status;
  if (s == null && board.session_online != null) s = board.session_online ? 'online' : 'offline';
  if (typeof s === 'string') return { online: s !== 'offline', since: null, note: null };
  if (s && typeof s === 'object') {
    const st = s.status || s.state;
    const online = s.online != null ? !!s.online : (st ? st !== 'offline' : true);
    return { online, since: s.since || s.last_seen || s.last_seen_at || null, note: s.note || null };
  }
  return { online: true, since: null, note: null };
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
  store.session = normSession(board);
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
  for (const p of store.pending) {
    buckets.get(p.hold ? 'held' : 'queued').unshift(p);
  }
  const now = Date.now();
  for (const [key, arr] of buckets) arr.sort(sorterFor(key, now));
  return COLUMNS.map((c) => ({ ...c, cards: buckets.get(c.key) }));
}

function sorterFor(key, now) {
  const oldestFirst = (a, b) => (ms(a.last_activity_at) || 0) - (ms(b.last_activity_at) || 0);
  const newestFirst = (a, b) => (ms(b.updated_at || b.last_activity_at) || 0) - (ms(a.updated_at || a.last_activity_at) || 0);
  const pin = (a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
  const wrap = (fn) => (a, b) => pin(a, b) || fn(a, b) || (a.num || 0) - (b.num || 0);
  if (key === 'queued') return wrap((a, b) => {
    const qa = a.queue_position == null ? 9999 : a.queue_position;
    const qb = b.queue_position == null ? 9999 : b.queue_position;
    return qa - qb;
  });
  if (key === 'in_progress') return wrap((a, b) => (isSilent(b, now) ? 1 : 0) - (isSilent(a, now) ? 1 : 0) || oldestFirst(a, b));
  if (key === 'needs_you' || key === 'blocked' || key === 'ready') return wrap(oldestFirst);
  if (key === 'done') return wrap(newestFirst);
  return wrap(oldestFirst);
}

export function draft(key, value) {
  if (value === undefined) return store.drafts.get(key) || '';
  if (value === null || value === '') store.drafts.delete(key);
  else store.drafts.set(key, value);
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

export const SYSTEM_KINDS = new Set(['state', 'agent_silent', 'verdict', 'evidence', 'error', 'submitted']);
