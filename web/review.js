// Awaiting review — the signoff surface, in both layouts.
//
// User ruling, verbatim: "and, with that in mind, rework my 'awaiting review'
// list". Thirteen finished branches read as thirteen identical rows, and the
// only way to say yes to any of them was to open the drawer, read the packet,
// scroll to the bottom and click. Three things fix that, and they are all here:
//
//   1. GROUPED BY WORK UNIT. Cards that share a batch (or, failing that, an
//      agent) are one branch and one review — so they are one expandable row
//      that says "Design core · 6 cards", not six rows that say almost the same
//      thing. A singleton stays exactly what it was. Thirteen rows become four.
//   2. THE VERDICT IS ON THE ROW. Every review row leads with the packet's claim
//      and its first Check-it-yourself step, carries the first screenshot as a
//      thumbnail and the live link, and puts Approve / Bounce right there. The
//      drawer is still one click away and still has the whole thread — it is no
//      longer the price of an easy yes.
//   3. REVIEW NEXT. One button walks the ready queue oldest-first, one card at a
//      time, in the rail: Approve / Bounce / Skip with a running "3 of 13".
//      Group members flow consecutively, because they are one piece of work.
//
// What this is NOT is a bulk approve. Every verdict here is the same per-card
// POST the drawer makes — the walkthrough only saves you the navigation, and
// the group row expands to individual cards rather than acting on them together.
import { h, timeEl, firstLine, plural } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import { store, cardState, draft } from './state.js';
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
      title: groupTitle(members),
      branch: sharedBranch(members),
      agent: members[0].agent_name || null,
    });
  }
  return out;
}

/** Flat, in reading order: what the walkthrough steps through. */
export function reviewQueue(cards) {
  const out = [];
  for (const g of reviewGroups(cards)) for (const c of g.cards) out.push(c.num);
  return out;
}

function sharedBranch(members) {
  const b = members[0].branch;
  if (!b) return null;
  return members.every((m) => m.branch === b) ? b : null;
}

/**
 * What to call a work unit. The branch is the honest name — it is literally the
 * one thing all these cards are — but a branch named after the card that started
 * it ("sprint/card-18") names nothing, so those fall back to the agent.
 */
function groupTitle(members) {
  const branch = sharedBranch(members);
  const leaf = branch ? String(branch).split('/').pop() : '';
  if (leaf && !/^(card|batch)[-_]?\d+$/i.test(leaf)) return humanize(leaf);
  const agent = members[0].agent_name;
  return agent ? `Agent ${shortAgent(agent)}` : `${members.length} cards`;
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

// ---- the walkthrough -----------------------------------------------------
//
// A flow is a snapshot of the queue plus a finger on it. It never approves
// anything itself: it opens a card, waits for a real per-card verdict to land,
// and then moves the finger. Nothing here can act on a card you have not seen.

let flow = null;

export function reviewFlow() { return flow; }
export function flowActive(num) { return !!flow && flow.queue[flow.i] === Number(num); }

export function startFlow(app, nums) {
  const queue = (nums || []).filter((n) => n != null);
  if (!queue.length) return;
  flow = { queue, i: 0, approved: 0, bounced: 0, skipped: 0 };
  flow.i = nextIndex(0);
  if (flow.i >= queue.length) { flow = null; return; }
  app.openCard(queue[flow.i]);
}

/**
 * The queue is a snapshot, and the board moves underneath it: a card can be
 * approved from the row, bounced from the rail, or merged by the session while
 * you are three cards back. Walk past anything that is no longer asking for a
 * verdict rather than parking the walkthrough on a card with nothing to decide.
 */
function nextIndex(from) {
  for (let i = from; i < flow.queue.length; i += 1) {
    const card = store.cards.get(flow.queue[i]);
    if (card && cardState(card) === 'ready') return i;
  }
  return flow.queue.length;
}

export function stopFlow(app) {
  flow = null;
  if (app) app.render();
}

function advance(app) {
  if (!flow) return;
  flow.i = nextIndex(flow.i + 1);
  if (flow.i >= flow.queue.length) {
    const done = flow;
    flow = null;
    app.render();
    app.toast(finishLine(done));
    return;
  }
  app.openCard(flow.queue[flow.i]);
}

function finishLine(f) {
  const bits = [];
  if (f.approved) bits.push(`${f.approved} approved`);
  if (f.bounced) bits.push(`${f.bounced} bounced back`);
  if (f.skipped) bits.push(`${f.skipped} skipped`);
  const tail = bits.length ? bits.join(', ') : 'nothing decided';
  return `That was all ${plural(f.queue.length, 'card')} — ${tail}.`;
}

/** Approve/bounce inside a flow: the same per-card POST, then the next card. */
async function flowVerdict(app, card, kind, notes) {
  const ok = await app.verdict(card, kind, notes);
  if (!ok || !flow) return;
  if (kind === 'approve') flow.approved += 1; else flow.bounced += 1;
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

  const notes = h('textarea.bounce-notes', {
    rows: '2',
    placeholder: 'What has to change? (goes straight to the agent)',
    oninput: (e) => draft(key, e.target.value),
    onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } },
  });
  notes.value = draft(key);
  const notesWrap = h('div.review-notes', { hidden: !draft(key) }, notes);

  function send() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    flowVerdict(app, card, 'bounce', text);
  }

  const bounceBtn = h('button.btn.bounce', {
    type: 'button',
    onclick: () => {
      if (notesWrap.hidden) {
        notesWrap.hidden = false;
        bounceBtn.textContent = 'Send bounce';
        notes.focus();
        return;
      }
      send();
    },
  }, notesWrap.hidden ? 'Bounce' : 'Send bounce');

  bar.appendChild(h('div.review-bar-head',
    h('span.review-count', `${flow.i + 1} of ${flow.queue.length}`),
    h('span.review-bar-note', 'walking the ready queue, oldest first'),
    h('span.grow'),
    h('button.btn.ghost.tiny', { type: 'button', onclick: () => stopFlow(app) }, 'Stop')));
  bar.appendChild(notesWrap);
  bar.appendChild(h('div.verdicts',
    h('button.btn.approve', {
      type: 'button', onclick: () => flowVerdict(app, card, 'approve'),
    }, 'Approve'),
    bounceBtn,
    h('button.btn.ghost.skip', {
      type: 'button',
      title: 'leave it in Awaiting review and come back to it',
      onclick: () => { if (flow) flow.skipped += 1; advance(app); },
    }, 'Skip')));
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
  const running = !!flow;
  const line = units === cards.length
    ? `${plural(cards.length, 'branch', 'branches')} waiting on a verdict.`
    : `${plural(cards.length, 'card')} in ${plural(units, 'work unit')} — one row per branch.`;

  const btn = h('button.btn.review-next', {
    type: 'button',
    title: running ? 'back to the card you are on' : 'step through them one at a time, oldest first',
    onclick: () => {
      if (flow) { app.openCard(flow.queue[flow.i]); return; }
      startFlow(app, reviewQueue(cards));
    },
  }, running ? `Reviewing ${flow.i + 1} of ${flow.queue.length}` : 'Review next');

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

  // No group-level Approve, ever: approving six cards with one click is the
  // blind bulk approve the spec rules out. The unit's action is to WALK it —
  // same per-card verdicts, one after another.
  const acts = h('div.review-acts');
  acts.appendChild(h('button.btn.tiny.review-walk', {
    type: 'button',
    title: 'step through these one at a time, oldest first',
    onclick: () => startFlow(app, g.cards.map((c) => c.num)),
  }, `Review these ${g.cards.length}`));
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

function groupSub(g) {
  const titles = g.cards.map((c) => c.title).filter(Boolean);
  const head = titles.slice(0, 2).join(' · ');
  const rest = titles.length - 2;
  const branch = g.branch ? `one branch, ${g.branch}` : 'one agent';
  return `${branch} — ${head}${rest > 0 ? ` +${rest} more` : ''}`;
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

  const lead = openable('review-lead', card, app);
  lead.appendChild(h('span.row-num', '#' + card.num));
  const body = h('span.review-body',
    h('span.review-title', card.title),
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

  const notes = h('textarea.bounce-notes', {
    rows: '2',
    placeholder: 'What has to change? (goes straight to the agent)',
    oninput: (e) => draft(key, e.target.value),
    onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } },
  });
  notes.value = draft(key);
  const pop = h('div.bounce-pop', { hidden: !draft(key) },
    notes,
    h('div.bounce-pop-acts',
      h('button.btn.tiny.bounce', { type: 'button', onclick: () => send() }, 'Send bounce'),
      h('button.btn.tiny.ghost', {
        type: 'button',
        onclick: () => { draft(key, null); notes.value = ''; pop.hidden = true; },
      }, 'Cancel')));

  function send() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    pop.hidden = true;
    app.verdict(card, 'bounce', text);
  }

  row.appendChild(h('button.btn.approve.tiny', {
    type: 'button',
    title: `approve #${card.num} — the session rebases, gates and merges the branch`,
    onclick: (e) => { e.stopPropagation(); app.verdict(card, 'approve'); },
  }, 'Approve'));
  row.appendChild(h('button.btn.bounce.tiny', {
    type: 'button',
    title: 'send it back with notes',
    onclick: (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; if (!pop.hidden) notes.focus(); },
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

  const wrap = h('div.review-actwrap', row, pop);
  return wrap;
}

function metaLine(card, p, shared) {
  const bits = [shortAgent(card.agent_name)];
  if (p.steps.length && !shared) bits.push(`${p.steps.length} ${p.steps.length === 1 ? 'check' : 'checks'}`);
  if (p.test_result && !shared) bits.push(firstLine(p.test_result, 26));
  if (card.bounce_count) bits.push(`bounced ${card.bounce_count === 1 ? 'once' : `${card.bounce_count}×`}`);
  return bits.join(' · ');
}
