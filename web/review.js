// Awaiting review — the signoff surface, in both layouts.
//
// User ruling, verbatim: "and, with that in mind, rework my 'awaiting review'
// list". Thirteen finished branches read as thirteen identical rows, and the
// only way to say yes to any of them was to open the drawer, read the packet,
// scroll to the bottom and click. Three things fix that, and they are all here:
//
//   1. GROUPED BY WORK UNIT. Cards that share a batch (or, failing that, an
//      agent) are one branch and one review — so they are one expandable row
//      that says "Restart-proof tabs + reply routing · 2 cards", not six rows
//      that say almost the same thing. A singleton stays exactly what it was.
//   2. THE VERDICT IS ON THE ROW. Every review row leads with the packet's claim
//      and its first Check-it-yourself step, carries the first screenshot as a
//      thumbnail and the live link, and puts Approve / Bounce right there. The
//      drawer is still one click away and still has the whole thread — it is no
//      longer the price of an easy yes.
//   3. REVIEW NEXT. One button walks the ready queue oldest-first, one STEP at a
//      time, in the rail: Approve / Bounce / Skip with a running "3 of 13".
//
// Two later rulings shaped what you see here.
//
// Card #28 — the row was titled "Agent card-23 · 2 cards" and the user could
// not tell what any of it was: a unit is named after the WORK IN IT (its member
// cards' own titles, falling back to the branch's claim), never after the agent
// that carried it, and the Review-next control is a filled button standing
// apart from the row stack rather than another line of text in it.
//
// Card #26 — user, verbatim: "i feel like i just hit approve way too many
// time", and his GO on the fix: "ONE Approve per work unit when the unit
// shipped as one branch with one packet (the six design cards = one click);
// per-card records still written underneath, and you can still expand a group
// to bounce a single member." So a unit that really did ship as one branch with
// one packet has ONE Approve — which still issues the same per-card POST for
// every member, one after another, so the server, the events and the per-card
// records are exactly what they were. It is not a blind bulk approve: the unit
// button only exists where one packet covers every member, i.e. where seeing
// one thing IS seeing all of them.
import { h, timeEl, firstLine, plural } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import { store, cardState, draft, bounceComposing } from './state.js';
import { shortAgent, openable } from './list.js';

// ---- the model -----------------------------------------------------------

/**
 * A card's work unit: the thing that was actually built as one piece. A batch is
 * explicit (the session drew it and the server records it); an agent that ended
 * up carrying several cards is the same shape by accident, and reads the same
 * way to a human, so it groups too. Anything else is only itself.
 */
export function unitKey(card) {
  if (card.batch_id != null && card.batch_id !== '') return 'batch:' + card.batch_id;
  if (card.agent_name) return 'agent:' + card.agent_name;
  return 'card:' + card.num;
}

/**
 * Group already-sorted review cards into work units, preserving the incoming
 * order (oldest first): a unit sits where its oldest member sat. Units of one
 * are marked `single` and render exactly as they always did.
 */
export function reviewGroups(cards) {
  const byKey = new Map();
  for (const card of cards || []) {
    const key = unitKey(card);
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(card);
  }
  const out = [];
  for (const [key, members] of byKey) {
    out.push({
      key,
      kind: members.length > 1 ? 'group' : 'single',
      cards: members,
      card: members[0],
      title: unitTitle(members),
      branch: sharedBranch(members),
      agent: members[0].agent_name || null,
    });
  }
  return out;
}

function sharedBranch(members) {
  const b = members[0].branch;
  if (!b) return null;
  return members.every((m) => m.branch === b) ? b : null;
}

// ---- what to call a work unit --------------------------------------------
//
// User, card #28: a row that reads "Agent card-23 · 2 cards" tells you the name
// of a process. You are being asked about WORK, so the row is named after the
// work: the member cards' own titles first (they are the condensed ≤8-word
// titles the workers already wrote), the branch's claim next, the branch name
// after that. The agent is metadata and lives on the right with the timestamp,
// where a process name belongs.

/** How long a derived unit title may run before its parts get trimmed. */
const UNIT_TITLE_MAX = 64;

/** Words not worth the characters when a title has to be cut short. */
const FILLER = /^(the|a|an|and|or|for|to|of|in|on|at|is|are|was|were|it|its|that|this|so|but|with|when|from|into|by)$/i;

/** A card's own words, or nothing — `normCard` fills a blank title with "Card #N". */
function ownTitle(card) {
  const t = firstLine((card && card.title) || '', 90).trim();
  if (!t || /^card\s*#?\d+$/i.test(t)) return '';
  return t;
}

/**
 * The human name of a work unit, derived from what is in it. Never the agent.
 * Two members "Board recovers from backend restarts" + "Every user event
 * carries reply_to" name the unit together; six members name it by the first
 * two and the row's own "· 6 cards" says how many more there are.
 */
export function unitTitle(members) {
  const titles = [];
  for (const m of members || []) {
    const t = ownTitle(m);
    if (t && !titles.includes(t)) titles.push(t);
  }
  if (titles.length) return joinTitles(titles);
  const claim = unitClaim(members);
  if (claim) return firstClause(claim);
  const branch = sharedBranch(members || []);
  const leaf = branch ? String(branch).split('/').pop() : '';
  if (leaf && !/^(card|batch)[-_]?\d+$/i.test(leaf)) return humanize(leaf);
  return `${plural((members || []).length, 'card')} on one branch`;
}

/** The branch-wide claim, when every member really is showing the same one. */
function unitClaim(members) {
  const first = (members && members[0] && members[0].evidence) || null;
  const claim = (first && first.claim) || '';
  if (!claim) return '';
  return members.every((m) => m.evidence && (m.evidence.claim || '') === claim) ? claim : '';
}

/**
 * Two titles, one line. When they do not both fit, the LONGER one gives up its
 * words first — trimming both halves evenly turns two readable phrases into two
 * unreadable ones, and one of them usually had room to spare.
 */
function joinTitles(titles) {
  if (titles.length === 1) return clipWords(titles[0], UNIT_TITLE_MAX);
  let [a, b] = titles.slice(0, 2);
  const room = UNIT_TITLE_MAX - 3;                  // the " + " between them
  const floor = 16;                                 // below this a title says nothing
  for (let pass = 0; pass < 2 && a.length + b.length > room; pass += 1) {
    const spare = pass === 0 ? room - Math.min(a.length, b.length) : Math.floor(room / 2);
    if (a.length >= b.length) a = clipWords(a, Math.max(floor, spare));
    else b = clipWords(b, Math.max(floor, spare));
  }
  return `${a} + ${b}`;
}

/** Cut to a length on a word boundary — never mid-word, never on a filler word. */
function clipWords(text, max) {
  const s = String(text || '').trim();
  if (s.length <= max) return s;
  const kept = [];
  let len = -1;
  for (const w of s.split(/\s+/)) {
    if (len + 1 + w.length > max && kept.length) break;
    kept.push(w);
    len += 1 + w.length;
  }
  while (kept.length > 1 && FILLER.test(kept[kept.length - 1])) kept.pop();
  return kept.length ? kept.join(' ') : s.slice(0, max);
}

/** The first clause of a claim — "X, so Y" and "X — Y" both lead with X. */
function firstClause(claim) {
  const s = firstLine(claim, 200).replace(/\.$/, '');
  const cut = s.split(/\s+—\s+|\s+–\s+|[;:]\s+|,\s+(?=so\b|and\b|which\b|without\b)/)[0];
  return clipWords(cut || s, UNIT_TITLE_MAX);
}

function humanize(s) {
  const t = String(s).replace(/[-_]+/g, ' ').trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : s;
}

/**
 * The packet as this surface needs it: one claim, the checks, the pictures, the
 * link. A batch ships one packet with a `per_card` entry per member, so a member
 * row shows ITS claim, never the branch-wide one.
 */
export function packetFor(card) {
  const p = (card && card.evidence) || {};
  let claim = p.claim || '';
  if (Array.isArray(p.per_card)) {
    const mine = p.per_card.find((e) => e && Number(e.card_num) === Number(card.num));
    if (mine && mine.claim) claim = mine.claim;
  }
  const steps = Array.isArray(p.validate) ? p.validate.filter(Boolean)
    : (typeof p.validate === 'string' && p.validate ? [p.validate] : []);
  let shots = Array.isArray(p.screenshots) ? p.screenshots : [];
  if (Array.isArray(p.per_card)) {
    const mine = p.per_card.find((e) => e && Number(e.card_num) === Number(card.num));
    if (mine && Array.isArray(mine.screenshots) && mine.screenshots.length) shots = mine.screenshots;
  }
  return {
    claim,
    steps: steps.map((s) => (typeof s === 'string' ? s : (s.text || s.step || ''))).filter(Boolean),
    shots,
    live_url: p.live_url || '',
    test_result: p.test_result || '',
    branch: p.branch || card.branch || '',
    empty: !p.claim && !steps.length,
  };
}

// ---- which groups you left open -----------------------------------------
//
// Client-only, and deliberately not persisted: which piles you had open is a
// property of the pass you are making, not of the board.

const open = new Set();

export function groupOpen(key, value) {
  if (value === undefined) return open.has(key);
  if (value) open.add(key); else open.delete(key);
  return value;
}

// ---- one work unit, one click -------------------------------------------
//
// Card #26. The Approve here is exactly the Approve the drawer has always made,
// issued once per member card in order: the server is untouched, every card
// still gets its own verdict event and its own record, and a member that fails
// stops the run where it stands rather than leaving you guessing which of the
// six went through.

/** key → {done, total, at, error} while a unit is being approved, or after it broke. */
const running = new Map();

export function unitProgress(key) { return running.get(key) || null; }

/**
 * True when this unit shipped as ONE thing: one branch, and one evidence packet
 * covering every member. That is the whole licence for a single Approve — you
 * are not approving six things you have not seen, you are approving the one
 * packet that is on screen. Anything looser keeps its per-card verdicts.
 */
export function shipsAsOne(g) {
  return !!(g && g.kind === 'group' && g.branch && sharedPacket(g.cards));
}

/** The members still asking for a verdict, in order. */
function readyMembers(nums) {
  const out = [];
  for (const n of nums || []) {
    const card = store.cards.get(Number(n));
    if (card && cardState(card) === 'ready') out.push(card);
  }
  return out;
}

/**
 * Approve every ready member of a unit, one per-card POST at a time. Stops on
 * the first failure and leaves the failure on the row: nothing after it was
 * sent, and the button offers to pick up where it stopped.
 */
export async function approveUnit(key, nums, app) {
  const prev = running.get(key);
  if (prev && !prev.error) return false;          // already in flight
  const todo = readyMembers(nums);
  if (!todo.length) { running.delete(key); return false; }
  const st = { done: 0, total: todo.length, at: todo[0].num, error: null };
  running.set(key, st);
  app.render();
  for (const card of todo) {
    st.at = card.num;
    app.render();
    // eslint-disable-next-line no-await-in-loop
    const ok = await app.verdict(card, 'approve', null, null, { quiet: true });
    if (!ok) { st.error = card.num; app.render(); return false; }
    st.done += 1;
    app.render();
  }
  running.delete(key);
  app.render();
  app.toast(`${plural(st.total, 'card')} approved as one work unit — merging now.`);
  return true;
}

// ---- the walkthrough -----------------------------------------------------
//
// A flow is a snapshot of the ready queue plus a finger on it. It never
// approves anything itself: it opens a card, waits for a real verdict to land,
// and then moves the finger. Nothing here can act on a card you have not seen.
//
// A STEP is one decision, not one card: a unit that shipped as one branch with
// one packet is a single step (approve the unit, bounce one member, or skip),
// because that is how many things you were actually asked about.

let flow = null;

export function reviewFlow() { return flow; }

/**
 * The steps the walkthrough offers, in reading order. Only cards actually
 * asking for a verdict count: a unit that is already merging is not a decision,
 * and counting it would make "1 of 7" a lie on a queue of six.
 */
export function reviewSteps(cards) {
  const steps = [];
  for (const g of reviewGroups(cards)) {
    const waiting = g.cards.filter((c) => cardState(c) === 'ready');
    if (!waiting.length) continue;
    if (shipsAsOne(g)) {
      steps.push({ key: g.key, nums: waiting.map((c) => c.num), unit: true, title: g.title });
    } else {
      for (const c of waiting) steps.push({ key: 'card:' + c.num, nums: [c.num], unit: false, title: c.title });
    }
  }
  return steps;
}

function step() { return flow ? flow.steps[flow.i] : null; }

export function flowActive(num) {
  const s = step();
  return !!s && s.nums.includes(Number(num));
}

/** The signature the rail memoises the verdict bar on. Null = no bar here. */
export function flowBarSig(card) {
  if (!card || !flowActive(card.num) || cardState(card) !== 'ready') return null;
  const s = step();
  const p = unitProgress(s.key);
  return [card.num, flow.i, flow.steps.length, card.bounce_count,
    // mid-bounce is part of what the bar looks like: Submit bounce + Cancel
    // instead of Approve / Bounce / Skip.
    bounceComposing(card.num) ? 'b' : '',
    p ? `${p.done}/${p.total}${p.error ? 'e' + p.error : ''}` : ''].join('|');
}

export function startFlow(app, steps) {
  const list = (steps || []).filter((s) => s && s.nums && s.nums.length);
  if (!list.length) return;
  flow = { steps: list, i: 0, approved: 0, bounced: 0, skipped: 0 };
  flow.i = nextIndex(0);
  if (flow.i >= list.length) { flow = null; return; }
  openStep(app);
}

/** Open the first member of the current step that is still asking for a verdict. */
function openStep(app) {
  const s = step();
  if (!s) return;
  const ready = readyMembers(s.nums);
  app.openCard((ready[0] || { num: s.nums[0] }).num);
}

/**
 * The queue is a snapshot, and the board moves underneath it: a card can be
 * approved from the row, bounced from the rail, or merged by the session while
 * you are three steps back. Walk past anything that is no longer asking for a
 * verdict rather than parking the walkthrough on a card with nothing to decide.
 */
function nextIndex(from) {
  for (let i = from; i < flow.steps.length; i += 1) {
    if (readyMembers(flow.steps[i].nums).length) return i;
  }
  return flow.steps.length;
}

export function stopFlow(app) {
  flow = null;
  if (app) app.render();
}

/** Stay on this step while it still has members asking; otherwise move on. */
function advance(app, { within = false } = {}) {
  if (!flow) return;
  if (within && readyMembers(step().nums).length) { openStep(app); app.render(); return; }
  flow.i = nextIndex(flow.i + 1);
  if (flow.i >= flow.steps.length) {
    const done = flow;
    flow = null;
    app.render();
    app.toast(finishLine(done));
    return;
  }
  openStep(app);
}

function finishLine(f) {
  const bits = [];
  if (f.approved) bits.push(`${f.approved} approved`);
  if (f.bounced) bits.push(`${f.bounced} bounced back`);
  if (f.skipped) bits.push(`${f.skipped} skipped`);
  const tail = bits.length ? bits.join(', ') : 'nothing decided';
  const cards = f.steps.reduce((n, s) => n + s.nums.length, 0);
  return `That was all ${plural(cards, 'card')} — ${tail}.`;
}

/** Approve/bounce inside a flow: the same per-card POST, then the next step. */
async function flowVerdict(app, card, kind, notes) {
  const ok = await app.verdict(card, kind, notes);
  if (!ok || !flow) return;
  if (kind === 'approve') flow.approved += 1; else flow.bounced += 1;
  // A bounced member leaves its siblings on the table — they are still ready
  // and still yours to decide, so the step is not over until they are gone.
  advance(app, { within: kind === 'bounce' });
}

/** Approve a whole unit from the walkthrough, then move to the next step. */
async function flowApproveUnit(app, s) {
  const n = readyMembers(s.nums).length;
  const ok = await approveUnit(s.key, s.nums, app);
  if (!ok || !flow) return;
  flow.approved += n;
  advance(app);
}

/**
 * The walkthrough's action bar, pinned at the bottom of the rail above the
 * composer. While it is up, the packet in the thread drops its own verdict
 * buttons — there is one place to decide, and it does not move.
 */
export function reviewBar(card, app) {
  const bar = h('div.review-bar');
  const key = `bounce:${card.num}`;
  const s = step();
  const unit = !!s && s.unit;
  const members = unit ? readyMembers(s.nums) : [];
  const many = unit && members.length > 1;
  const prog = s ? unitProgress(s.key) : null;

  // Card #44: once you have pressed Bounce, the bar offers Submit bounce and
  // Cancel and nothing else. Approve — and in the walkthrough, Skip — are gone
  // while you are writing, so a stray click cannot approve what you were in the
  // middle of sending back.
  const composing = bounceComposing(card.num) || !!draft(key);

  const notes = h('textarea.bounce-notes', {
    id: 'bounce-' + card.num,
    rows: '2',
    placeholder: 'What has to change? (goes straight to the agent)',
    oninput: (e) => draft(key, e.target.value),
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    },
  });
  notes.value = draft(key);
  const notesWrap = h('div.review-notes', { hidden: !composing }, notes);

  function send() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    bounceComposing(card.num, false);
    flowVerdict(app, card, 'bounce', text);
  }

  function cancel() {
    draft(key, null);
    bounceComposing(card.num, false);
    app.render();
  }

  bar.appendChild(h('div.review-bar-head',
    h('span.review-count', `${flow.i + 1} of ${flow.steps.length}`),
    h('span.review-bar-note', many
      ? `one branch, ${plural(members.length, 'card')} — one decision`
      : 'walking the ready queue, oldest first'),
    h('span.grow'),
    h('button.btn.ghost.tiny', { type: 'button', onclick: () => stopFlow(app) }, 'Stop')));
  if (many) {
    bar.appendChild(h('p.review-bar-unit',
      `${s.title} — ${members.map((c) => '#' + c.num).join(', ')}. `
      + 'Approve takes the whole branch; Bounce sends back only the card you are reading.'));
  }
  bar.appendChild(notesWrap);
  if (composing) {
    bar.appendChild(h('div.verdicts.is-bouncing',
      h('button.btn.bounce', { type: 'button', onclick: () => send() },
        many ? `Submit bounce for #${card.num}` : 'Submit bounce'),
      h('button.btn.ghost', { type: 'button', onclick: () => cancel() }, 'Cancel')));
    // Focus is asked for once, by app.composeBounce, and restored by id after
    // that (card #46) — never re-grabbed on every rebuild of this bar.
    if (prog && prog.error) {
      bar.appendChild(h('p.review-unit-error',
        `Stopped at #${prog.error} — ${prog.done} of ${prog.total} approved, nothing after it was sent.`));
    }
    return bar;
  }
  const approve = h('button.btn.approve', {
    type: 'button',
    disabled: !!(prog && !prog.error),
    onclick: () => (many ? flowApproveUnit(app, s) : flowVerdict(app, card, 'approve')),
  }, prog && !prog.error
    ? `approving ${Math.min(prog.done + 1, prog.total)} of ${prog.total}…`
    : (many ? `Approve all ${members.length}` : 'Approve'));
  bar.appendChild(h('div.verdicts',
    approve,
    h('button.btn.bounce', {
      type: 'button',
      onclick: () => app.composeBounce(card.num),
    }, many ? `Bounce #${card.num}` : 'Bounce'),
    h('button.btn.ghost.skip', {
      type: 'button',
      title: 'leave it in Awaiting review and come back to it',
      onclick: () => { if (flow) flow.skipped += 1; advance(app); },
    }, 'Skip')));
  if (prog && prog.error) {
    bar.appendChild(h('p.review-unit-error',
      `Stopped at #${prog.error} — ${prog.done} of ${prog.total} approved, nothing after it was sent.`));
  }
  return bar;
}

// ---- rows ----------------------------------------------------------------

/**
 * The List's Awaiting-review block: a Review-next button, then one row per work
 * unit. Returns null when nothing is waiting on a verdict, so the caller can
 * leave the whole block out.
 */
export function reviewBlock(cards, app, { compact = false } = {}) {
  if (!cards.length) return null;
  const wrap = h('div.review-block', { class: compact ? 'review-block is-compact' : 'review-block' });
  wrap.appendChild(reviewHead(cards, app, compact));
  const groups = reviewGroups(cards);
  for (const g of groups) {
    if (g.kind === 'single') wrap.appendChild(reviewRow(g.card, app, { compact }));
    else wrap.appendChild(groupRow(g, app, { compact }));
  }
  return wrap;
}

function reviewHead(cards, app, compact) {
  const groups = reviewGroups(cards);
  const units = groups.length;
  const walking = !!flow;
  const line = units === cards.length
    ? `${plural(cards.length, 'branch', 'branches')} waiting on a verdict.`
    : `${plural(cards.length, 'card')} in ${plural(units, 'work unit')} — one row per branch.`;

  // Card #28: this used to be a bordered chip sitting in the same stack as the
  // rows, and it read as another row — a title with no obvious verb. It is a
  // filled button with a label that says what it does to you, and it sits in
  // its own bar above the stack with a rule under it, so nothing about it can
  // be mistaken for one of the things being reviewed.
  const btn = h('button.btn.review-next', {
    type: 'button',
    title: walking ? 'back to the card you are on' : 'step through them one at a time, oldest first',
    onclick: () => {
      if (flow) { openStep(app); return; }
      startFlow(app, reviewSteps(cards));
    },
  },
    h('span.review-next-label', walking
      ? `Reviewing ${flow.i + 1} of ${flow.steps.length}`
      : 'Review next'),
    h('span.review-next-arrow', '→'));

  // On the Board the column already has an "Awaiting review" heading over it;
  // saying it twice, two lines apart, is how the first cut of this read.
  const head = h('div.review-head',
    compact ? null : h('span.review-head-name', 'Awaiting review'),
    compact ? null : h('span.review-head-note', line),
    h('span.grow'),
    btn);
  return head;
}

/**
 * A batch ships ONE packet for the whole branch, with a per-card claim inside
 * it. Repeating that packet's check step and screenshots on all six member rows
 * is six copies of one sentence — so when the members really do share a packet,
 * it is shown once on the unit and the member rows keep only what differs.
 */
function sharedPacket(members) {
  if (members.length < 2) return null;
  const first = members[0].evidence;
  if (!first || !Array.isArray(first.per_card) || !first.per_card.length) return null;
  const claim = first.claim || '';
  const same = members.every((m) => m.evidence && (m.evidence.claim || '') === claim);
  if (!same) return null;
  return packetFor({ ...members[0], evidence: { ...first, per_card: null } });
}

/** One work unit: what it is, what the branch claims, and a way into it. */
function groupRow(g, app, { compact }) {
  const isOpen = groupOpen(g.key);
  const wrap = h('div.review-unit', { class: isOpen ? 'review-unit is-open' : 'review-unit' });
  const shared = sharedPacket(g.cards);

  const toggle = h('button.review-group', {
    type: 'button',
    'aria-expanded': isOpen ? 'true' : 'false',
    title: isOpen ? 'collapse this work unit' : `show all ${g.cards.length} cards`,
    onclick: () => { groupOpen(g.key, !groupOpen(g.key)); app.render(); },
  },
    h('span.review-chev', isOpen ? '▾' : '▸'),
    h('span.review-group-main',
      h('span.review-group-title', `${g.title} · ${plural(g.cards.length, 'card')}`),
      h('span.review-group-sub', groupSub(g))),
    h('span.review-group-right',
      h('span.row-tag', 'Signoff'),
      h('span.row-meta', shortAgent(g.agent), ' · ', timeEl(g.card.last_activity_at, { suffix: false }))));

  wrap.appendChild(toggle);

  // The unit leads with the branch's claim and first check, exactly like a
  // single card does — when there is one branch-wide packet to lead with.
  const body = h('div.review-unit-body');
  if (shared) {
    const lead = h('div.review-unit-claim');
    lead.appendChild(h('p.review-claim', shared.claim));
    if (shared.steps.length) {
      lead.appendChild(h('div.review-check',
        h('span.step-n', '1'),
        h('p', firstLine(shared.steps[0], compact ? 150 : 260)),
        shared.steps.length > 1 ? h('span.review-more', `+${shared.steps.length - 1} more`) : null));
    }
    const thumb = thumbFor(shared, app);
    const holder = h('div.review-unit-lead', lead);
    if (thumb) holder.appendChild(thumb);
    body.appendChild(holder);
  }

  // One branch, one packet, one Approve (card #26) — issued as the same
  // per-card POST for every member, in order, so the records underneath are
  // unchanged. Units that did NOT ship as one thing keep their per-card
  // verdicts and only offer the walkthrough.
  const acts = h('div.review-acts');
  const one = shipsAsOne(g);
  const waiting = one ? g.cards.filter((c) => cardState(c) === 'ready') : [];
  const prog = unitProgress(g.key);
  if (one && waiting.length) {
    const label = prog && !prog.error
      ? `approving ${Math.min(prog.done + 1, prog.total)} of ${prog.total}…`
      : (waiting.length > 1 ? `Approve all ${waiting.length}` : `Approve #${waiting[0].num}`);
    acts.appendChild(h('button.btn.tiny.approve.review-approve-unit', {
      type: 'button',
      disabled: !!(prog && !prog.error),
      title: waiting.length > 1
        ? `one branch, one packet — approves each of the ${waiting.length} cards underneath`
        : `approve #${waiting[0].num}`,
      onclick: (e) => { e.stopPropagation(); approveUnit(g.key, g.cards.map((c) => c.num), app); },
    }, label));
  }
  acts.appendChild(h('button.btn.tiny.review-walk', {
    type: 'button',
    title: one
      ? 'read them one at a time before you decide'
      : 'step through these one at a time, oldest first',
    onclick: () => startFlow(app, reviewSteps(g.cards)),
  }, one ? 'Read them first' : `Review these ${g.cards.length}`));
  acts.appendChild(h('button.btn.tiny.ghost.review-toggle-cards', {
    type: 'button',
    onclick: () => { groupOpen(g.key, !isOpen); app.render(); },
  }, isOpen ? 'Hide the cards' : 'Show the cards'));
  if (shared && shared.live_url) {
    acts.appendChild(h('a.btn.tiny.ghost.review-live', {
      href: shared.live_url, target: '_blank', rel: 'noreferrer noopener', title: shared.live_url,
    }, 'See it live ↗'));
  }
  acts.appendChild(h('span.grow'));
  if (shared) acts.appendChild(h('span.review-meta', unitMeta(shared)));
  body.appendChild(acts);
  if (prog && prog.error) {
    body.appendChild(h('p.review-unit-error',
      `Stopped at #${prog.error} — ${prog.done} of ${prog.total} approved, nothing after it was sent. `
      + 'The button picks up where it stopped.'));
  }
  wrap.appendChild(body);

  if (isOpen) {
    const members = h('div.review-members');
    for (const card of g.cards) {
      members.appendChild(reviewRow(card, app, { compact, inGroup: true, shared: !!shared }));
    }
    wrap.appendChild(members);
  }
  return wrap;
}

function unitMeta(p) {
  const bits = [];
  if (p.steps.length) bits.push(`${p.steps.length} ${p.steps.length === 1 ? 'check' : 'checks'}`);
  if (p.test_result) bits.push(firstLine(p.test_result, 26));
  return bits.join(' · ');
}

/**
 * The line under a unit's name: which cards are in it and what holds them
 * together. The titles are up in the name now (card #28), so this says the
 * things the name cannot — the card numbers and the branch.
 */
function groupSub(g) {
  const nums = g.cards.map((c) => '#' + c.num);
  const shown = nums.length > 6 ? `${nums.slice(0, 6).join(', ')} +${nums.length - 6}` : nums.join(', ');
  const where = g.branch ? `one branch, ${g.branch}` : 'one agent, one worktree';
  return `${shown} · ${where}`;
}

/**
 * One card, with the verdict on it. The claim and the first check are the whole
 * point: they are what the drawer used to make you go and find.
 */
export function reviewRow(card, app, { compact = false, inGroup = false, shared = false } = {}) {
  const p = packetFor(card);
  const state = cardState(card);
  const merging = state === 'integrating';
  const item = h('div.review-item', {
    class: `review-item${inGroup ? ' in-group' : ''}${merging ? ' is-merging' : ''}`,
    'data-num': card.num,
  });

  // Card #28: the row leads with the card's OWN title — what you asked for —
  // and the claim is the second line under it. A review row never leads with a
  // control's label or an agent's name.
  const lead = openable('review-lead', card, app);
  lead.appendChild(h('span.row-num', '#' + card.num));
  const body = h('span.review-body',
    h('span.review-title', ownTitle(card) || firstLine(card.body || '', 70) || `Card #${card.num}`),
    h('p.review-claim', p.claim || 'No claim recorded — open it and ask the agent what it thinks it did.'));
  // In a batch the check step and the screenshots belong to the branch, not to
  // this card, and the unit above already showed them once.
  if (p.steps.length && !shared) {
    body.appendChild(h('div.review-check',
      h('span.step-n', '1'),
      h('p', firstLine(p.steps[0], compact ? 150 : 260)),
      p.steps.length > 1
        ? h('span.review-more', `+${p.steps.length - 1} more`)
        : null));
  }
  lead.appendChild(body);
  const thumb = shared ? null : thumbFor(p, app);
  if (thumb) lead.appendChild(thumb);
  item.appendChild(lead);

  item.appendChild(actionsRow(card, p, app, merging, shared));
  return item;
}

function thumbFor(p, app) {
  if (!p.shots.length) return null;
  const urls = p.shots.map(attachmentUrl).filter(Boolean);
  if (!urls.length) return null;
  const caps = p.shots.map(attachmentCaption);
  const frame = h('span.review-thumb-frame',
    h('img', {
      src: urls[0], alt: caps[0] || 'screenshot', loading: 'lazy',
      onerror: (e) => { e.target.remove(); },
    }));
  return h('button.review-thumb', {
    type: 'button',
    title: p.shots.length > 1 ? `${p.shots.length} screenshots` : (caps[0] || 'screenshot'),
    onclick: (e) => { e.stopPropagation(); app.lightbox(urls, 0, caps); },
  }, frame, p.shots.length > 1 ? h('span.review-thumb-n', String(p.shots.length)) : null);
}

function actionsRow(card, p, app, merging, shared) {
  const key = `bounce:${card.num}`;
  const row = h('div.review-acts');

  if (merging) {
    row.appendChild(h('span.review-merging', 'Approved — the session is merging the branch now.'));
    row.appendChild(h('span.grow'));
    row.appendChild(h('span.review-meta', shortAgent(card.agent_name), ' · ',
      timeEl(card.state_since || card.last_activity_at, { suffix: false })));
    return row;
  }

  // Cards #44 and #46 together. #44: once you have pressed Bounce, Approve is
  // gone — a stray click must not merge the thing you were sending back. #46:
  // the words are typed in the RAIL, so the row carries the composing STATE and
  // none of the typing. While a bounce is being written the row says where it
  // is being written and offers the way out; the notes box itself is in the
  // panel, under the packet, with the caret already in it.
  const composing = bounceComposing(card.num) || !!draft(key);

  function cancel() {
    draft(key, null);
    bounceComposing(card.num, false);
    app.render();
  }

  if (composing) {
    row.appendChild(h('button.btn.bounce.tiny', {
      type: 'button',
      title: 'back to the notes box in the panel',
      onclick: (e) => { e.stopPropagation(); app.openCard(card.num, { focus: 'bounce' }); },
    }, 'Writing the bounce →'));
    row.appendChild(h('button.btn.tiny.ghost', {
      type: 'button',
      onclick: (e) => { e.stopPropagation(); cancel(); },
    }, 'Cancel'));
    row.appendChild(h('span.grow'));
    row.appendChild(h('span.review-meta', metaLine(card, p, shared), ' · ',
      timeEl(card.last_activity_at, { suffix: false })));
    return h('div.review-actwrap', row);
  }

  row.appendChild(h('button.btn.approve.tiny', {
    type: 'button',
    title: `approve #${card.num} — the session rebases, gates and merges the branch`,
    onclick: (e) => { e.stopPropagation(); app.verdict(card, 'approve'); },
  }, 'Approve'));
  // Card #46, user verbatim: "when I'm typing a response into a card and the
  // board moves, it takes my cursor focus out so I keep typing but it goes
  // nowhere." Approve is one tap and stays on the row; bounce is a SENTENCE, and
  // a sentence is typed in the rail with the thread under it — never into a box
  // sitting in a list that the next board frame rebuilds. So this button opens
  // the card and puts the caret in the notes box there.
  row.appendChild(h('button.btn.bounce.tiny', {
    type: 'button',
    title: 'open it and send it back with notes',
    // `openCard` sets the composing state AND asks for the caret, so one click
    // gets you a notes box you are already typing into.
    onclick: (e) => { e.stopPropagation(); app.openCard(card.num, { focus: 'bounce' }); },
  }, 'Bounce'));
  if (p.live_url && !shared) {
    row.appendChild(h('a.btn.tiny.ghost.review-live', {
      href: p.live_url, target: '_blank', rel: 'noreferrer noopener', title: p.live_url,
      onclick: (e) => e.stopPropagation(),
    }, 'See it live ↗'));
  }
  row.appendChild(h('span.grow'));
  row.appendChild(h('span.review-meta', metaLine(card, p, shared), ' · ',
    timeEl(card.last_activity_at, { suffix: false })));

  return h('div.review-actwrap', row);
}

function metaLine(card, p, shared) {
  const bits = [shortAgent(card.agent_name)];
  if (p.steps.length && !shared) bits.push(`${p.steps.length} ${p.steps.length === 1 ? 'check' : 'checks'}`);
  if (p.test_result && !shared) bits.push(firstLine(p.test_result, 26));
  if (card.bounce_count) bits.push(`bounced ${card.bounce_count === 1 ? 'once' : `${card.bounce_count}×`}`);
  return bits.join(' · ');
}
