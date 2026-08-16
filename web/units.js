// Work units — the model behind Awaiting review. No DOM in this file, on
// purpose: it is the part that has to be true, and it is tested directly.
//
// User ruling, verbatim (card #55): "I just want the single card that lives in
// review, then when I open it up, it outlines everything that changed, and I
// can approve them together."
//
// Everything before this card was an answer to "how do we ARRANGE many review
// rows" — grouped rows, per-member rows, a walkthrough, four mockups. That was
// the wrong question. Work that shipped together is ONE reviewable object: one
// entry in Awaiting review, one outline of everything that changed, one
// Approve. The member cards still exist — they keep their own timelines, their
// own chat, their own verdict events and their own completion — they simply do
// not each demand a verdict and do not each appear in the review list.
//
// A unit is a PROJECTION, not a row in the database. See the note at the bottom
// of this file for why.
import { firstLine, plural } from './util.js';

/**
 * The key of the thing a card shipped inside. A batch is explicit (the session
 * drew it and the server records it); several cards landing on one branch are
 * the same shape by accident and read the same way to a human; an agent that
 * carried several cards without a branch is the loosest form of the same fact.
 * Anything else is only itself.
 */
export function unitKey(card) {
  if (card.batch_id != null && card.batch_id !== '') return 'batch:' + card.batch_id;
  if (card.branch) return 'branch:' + card.branch;
  if (card.agent_name) return 'agent:' + card.agent_name;
  return 'card:' + card.num;
}

/**
 * Group already-sorted review cards into work units, preserving the incoming
 * order (oldest first): a unit sits where its oldest member sat.
 *
 * The list this returns is what Awaiting review renders, one entry each — so a
 * six-card branch is ONE entry, not six, and never six inside one.
 */
export function reviewUnits(cards) {
  const byKey = new Map();
  for (const card of cards || []) {
    const key = unitKey(card);
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(card);
  }
  const out = [];
  for (const [key, members] of byKey) out.push(makeUnit(key, members));
  return out;
}

/** The unit a card belongs to, resolved live against the cards on the board. */
export function unitOf(cards, num) {
  const n = Number(num);
  const mine = (cards || []).find((c) => Number(c.num) === n);
  if (!mine) return null;
  const key = unitKey(mine);
  const members = (cards || []).filter((c) => unitKey(c) === key);
  return members.length ? makeUnit(key, members) : null;
}

function makeUnit(key, members) {
  const shared = sharedPacket(members);
  return {
    key,
    kind: members.length > 1 ? 'unit' : 'single',
    cards: members,
    lead: members[0],
    size: members.length,
    title: unitTitle(members),
    branch: sharedBranch(members),
    agent: members[0].agent_name || null,
    // The branch-wide packet, when every member really is showing the same one.
    // Null on a looser unit (same branch, different packets) — the row then says
    // how many changes are in it and sends you to the outline to read them.
    packet: shared,
    nums: members.map((c) => c.num),
  };
}

export function sharedBranch(members) {
  const b = members[0].branch;
  if (!b) return null;
  return members.every((m) => m.branch === b) ? b : null;
}

/**
 * ONE packet for the whole unit: every member is carrying the same claim, which
 * is what a batch's single `sprint-ready` produces. That is what licenses the
 * unit to lead with one sentence — and, with the outline open under it, what
 * makes one Approve an answer to something you have actually seen.
 */
export function sharedPacket(members) {
  if (!members || members.length < 2) return null;
  const first = members[0].evidence;
  if (!first) return null;
  const claim = first.claim || '';
  if (!claim) return null;
  if (!members.every((m) => m.evidence && (m.evidence.claim || '') === claim)) return null;
  return packetFor({ ...members[0], evidence: { ...first, per_card: null } });
}

// ---- what to call a work unit --------------------------------------------
//
// User, card #28: a row that reads "Agent card-23 · 2 cards" tells you the name
// of a process. You are being asked about WORK, so the unit is named after the
// work: the member cards' own titles first (they are the condensed ≤8-word
// titles the workers already wrote), the branch's claim next, the branch name
// after that. The agent is metadata and lives beside the timestamp, where a
// process name belongs.

/** How long a derived unit title may run before its parts get trimmed. */
const UNIT_TITLE_MAX = 64;

/** Words not worth the characters when a title has to be cut short. */
const FILLER = /^(the|a|an|and|or|for|to|of|in|on|at|is|are|was|were|it|its|that|this|so|but|with|when|from|into|by)$/i;

/** A card's own words, or nothing — `normCard` fills a blank title with "Card #N". */
export function ownTitle(card) {
  const t = firstLine((card && card.title) || '', 90).trim();
  if (!t || /^card\s*#?\d+$/i.test(t)) return '';
  return t;
}

/**
 * The human name of a work unit, derived from what is in it. Never the agent.
 * Two members "Board recovers from backend restarts" + "Every user event
 * carries reply_to" name the unit together; six members name it by the first
 * two and the entry's own "· 6 changes" says how many more there are.
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
 * The packet as the review surface needs it: one claim, the checks, the
 * pictures, the link. A batch ships ONE packet with a `per_card` entry per
 * member, so a member's section in the outline shows ITS claim and ITS
 * screenshots, never the branch-wide ones repeated six times.
 */
export function packetFor(card) {
  const p = (card && card.evidence) || {};
  const mine = Array.isArray(p.per_card)
    ? p.per_card.find((e) => e && Number(e.card_num) === Number(card.num))
    : null;
  const claim = (mine && mine.claim) || p.claim || '';
  const steps = stepList(mine && mine.validate ? mine.validate : p.validate);
  let shots = Array.isArray(p.screenshots) ? p.screenshots : [];
  if (mine && Array.isArray(mine.screenshots) && mine.screenshots.length) shots = mine.screenshots;
  return {
    claim,
    steps,
    shots,
    reports: Array.isArray(p.reports) ? p.reports : [],
    live_url: p.live_url || '',
    test_result: p.test_result || '',
    test_cmd: p.test_cmd || '',
    diffstat: p.diffstat || '',
    work_kind: p.work_kind || '',
    readback: Array.isArray(p.readback) ? p.readback.join('\n') : (p.readback || ''),
    branch: p.branch || card.branch || '',
    empty: !claim && !steps.length,
  };
}

function stepList(validate) {
  const raw = Array.isArray(validate) ? validate.filter(Boolean)
    : (typeof validate === 'string' && validate ? [validate] : []);
  return raw.map((s) => (typeof s === 'string' ? s : (s.text || s.step || ''))).filter(Boolean);
}

/**
 * The member's own part of the unit: what changed HERE. `ownSteps` is true only
 * when this member's checks are its own rather than the branch's — the branch's
 * checks are printed once, at the top of the outline, not once per section.
 */
export function memberPart(card, unit) {
  const p = packetFor(card);
  const shared = unit && unit.packet;
  const sameSteps = !!shared && sameList(p.steps, shared.steps);
  const sameShots = !!shared && sameList(p.shots.map(String), shared.shots.map(String));
  return {
    card,
    num: card.num,
    title: ownTitle(card) || firstLine(card.body || '', 70) || `Card #${card.num}`,
    claim: p.claim,
    steps: sameSteps ? [] : p.steps,
    shots: sameShots ? [] : p.shots,
    live_url: shared ? '' : p.live_url,
  };
}

function sameList(a, b) {
  if (a.length !== b.length) return false;
  return a.every((v, i) => v === b[i]);
}

/**
 * ---------------------------------------------------------------------------
 * WHY A PROJECTION AND NOT A REAL CARD
 * ---------------------------------------------------------------------------
 * A unit could have been a synthetic card row owning its members. It is not,
 * and the reasons are load-bearing:
 *
 *   1. It would be a SECOND card identity. Everything on this board is keyed by
 *      card number — events, evidence, verdicts, states, the rail, the hash
 *      route, the meter, the sweep. A card that is not a card would need an
 *      exception in every one of them.
 *   2. The facts are already recorded. `batch_id`, `branch`, `agent_name` and
 *      the shared packet are on the board payload already (the server was made
 *      to carry exactly this, see the test named for it). Writing a row would be
 *      storing a derivation of data we already have — and derivations go stale.
 *   3. It would need a state machine of its own. What is a unit whose members
 *      are half ready and half bounced? A projection answers that for free: the
 *      unit is whatever its members are RIGHT NOW, recomputed every frame, so a
 *      bounced member simply falls out of it.
 *   4. The audit trail stays honest without any effort. Per-card verdict events
 *      and per-card completion are what the server already writes; the unit
 *      never becomes a thing that "was approved", because the unit is a way of
 *      LOOKING at six approvals, not a seventh record of them.
 *
 * The cost of a projection is that a unit has no durable identity — you cannot
 * link to one that has dissolved. That is the right trade: a dissolved unit is
 * one whose members are no longer waiting on you, and there is nothing there to
 * link to.
 */
