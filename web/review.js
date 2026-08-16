// Awaiting review — one card per work unit, and one Approve for the lot.
//
// User ruling, verbatim (card #55): "I just want the single card that lives in
// review, then when I open it up, it outlines everything that changed, and I
// can approve them together."
//
// We got here through grouped rows, per-member rows, a walkthrough and four
// mockup shapes — every one of them an answer to "how do we ARRANGE many review
// rows". That was the wrong question. The LIST is what had to go: work that
// shipped together is ONE reviewable object.
//
//   THE LIST     Awaiting review holds one entry per work unit, named after the
//                work in it (never after the agent). A six-card branch is one
//                entry. A card on its own is a unit of one and looks exactly as
//                it always did: its own card, with its packet on it.
//   THE OUTLINE  Opening a unit fills the rail with everything that changed:
//                the branch's claim and checks once at the top, then one
//                section per member card with its own claim, its own checks and
//                its own screenshots. One readable page, top to bottom.
//   THE VERDICT  One Approve at the bottom covers the unit. Underneath it is
//                the same per-card POST it always was (card #26), issued once
//                per member, so every card keeps its own verdict event and its
//                own completion. Bounce works per section (send back just that
//                part) and for the whole unit.
//
// Member cards still exist — for tracking, for chat, for history. They just do
// not each demand a verdict, and they do not each appear in this list.
//
// Deleted with this card, deliberately and not as a follow-up: the grouped
// (expand-to-see-the-members) row, the per-member review rows, and the
// Review-next walkthrough. All three existed to organise a list that should not
// be there.
import { h, timeEl, firstLine, plural } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import { store, cardState, draft, bounceComposing } from './state.js';
import { shortAgent, openable } from './list.js';
import { reviewUnits, unitOf, packetFor, memberPart } from './units.js';
import { splitAttachments, reportRow } from './reports.js';

export { reviewUnits, unitOf, packetFor } from './units.js';

/** A card is part of a review unit for exactly as long as it is under review. */
const REVIEW_STATES = ['ready', 'integrating'];

/**
 * The unit a card is in RIGHT NOW, recomputed from the board rather than
 * remembered. That is the whole reason a unit is a projection: a member you
 * bounce falls out of it by itself on the next frame, and a unit whose members
 * have all landed simply stops existing.
 */
export function unitInReview(num) {
  const cards = Array.from(store.cards.values())
    .filter((c) => REVIEW_STATES.includes(cardState(c)))
    .sort((a, b) => a.num - b.num);
  return unitOf(cards, num);
}

// ---- one work unit, one click -------------------------------------------
//
// Card #26. The Approve here is exactly the Approve the packet has always made,
// issued once per member card in order: the server is untouched, every card
// still gets its own verdict event and its own record, and a member that fails
// stops the run where it stands rather than leaving you guessing which of the
// six went through.

/** key → {kind, done, total, at, error} while a unit is being decided. */
const running = new Map();

export function unitProgress(key) { return running.get(key) || null; }

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
 * Decide a whole unit, one per-card POST at a time. Stops on the first failure
 * and leaves the failure on screen: nothing after it was sent, and the button
 * offers to pick up where it stopped.
 */
async function runUnit(unit, kind, notes, app) {
  const key = unit.key;
  const prev = running.get(key);
  if (prev && !prev.error) return false;          // already in flight
  const todo = readyMembers(unit.nums);
  if (!todo.length) { running.delete(key); return false; }
  const st = { kind, done: 0, total: todo.length, at: todo[0].num, error: null };
  running.set(key, st);
  app.render();
  for (const card of todo) {
    st.at = card.num;
    app.render();
    // eslint-disable-next-line no-await-in-loop
    const ok = await app.verdict(card, kind, notes, null, { quiet: true });
    if (!ok) { st.error = card.num; app.render(); return false; }
    st.done += 1;
    app.render();
  }
  running.delete(key);
  app.render();
  app.toast(kind === 'approve'
    ? `${plural(st.total, 'card')} approved as one work unit — merging now.`
    : `${plural(st.total, 'card')} sent back with your notes.`);
  return true;
}

export function approveUnit(unit, app) { return runUnit(unit, 'approve', null, app); }
export function bounceUnit(unit, notes, app) { return runUnit(unit, 'bounce', notes, app); }

/** What the progress line says while a unit is being decided, or after it broke. */
function progressLine(prog) {
  if (!prog) return null;
  const verb = prog.kind === 'approve' ? 'approved' : 'sent back';
  if (!prog.error) return null;
  return h('p.unit-error',
    `Stopped at #${prog.error} — ${prog.done} of ${prog.total} ${verb}, nothing after it was sent. `
    + 'The button picks up where it stopped.');
}

// ---- the list ------------------------------------------------------------

/**
 * The Awaiting-review block: ONE entry per work unit. Returns null when nothing
 * is waiting on a verdict, so the caller can leave the whole block out.
 */
export function reviewBlock(cards, app, { compact = false } = {}) {
  if (!cards.length) return null;
  const wrap = h('div.review-block', { class: compact ? 'review-block is-compact' : 'review-block' });
  const units = reviewUnits(cards);
  if (!compact) wrap.appendChild(reviewHead(cards, units));
  for (const unit of units) {
    wrap.appendChild(unit.kind === 'single'
      ? singleCard(unit.lead, app, { compact })
      : unitCard(unit, app, { compact }));
  }
  return wrap;
}

function reviewHead(cards, units) {
  const line = units.length === cards.length
    ? `${plural(units.length, 'thing', 'things')} waiting on a verdict.`
    : `${plural(cards.length, 'card')} shipped as ${plural(units.length, 'piece')} of work — `
      + 'one card each, approved together.';
  return h('div.review-head',
    h('span.review-head-name', 'Awaiting review'),
    h('span.review-head-note', line));
}

/**
 * ONE card for a work unit. It says what the unit is, what the branch claims,
 * and how many changes are inside it; clicking it opens the outline in the
 * rail. Approve is here too when the unit shipped one packet — the claim on
 * this card IS that packet's claim, so a yes here is a yes to something you can
 * read without opening anything.
 */
function unitCard(unit, app, { compact }) {
  const p = unit.packet;
  const item = h('div.unit-card', { 'data-unit': unit.key });

  const lead = h('div.unit-lead', {
    role: 'button',
    tabindex: '0',
    title: 'open the outline — everything that changed, in one page',
    onclick: () => app.openUnit(unit.lead.num),
    onkeydown: (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === lead) {
        e.preventDefault(); app.openUnit(unit.lead.num);
      }
    },
  });
  const body = h('div.unit-body',
    h('span.unit-title', unit.title),
    h('span.unit-count', `${plural(unit.size, 'change')} on one branch — open it to read them`));
  if (p && p.claim) body.appendChild(h('p.review-claim', p.claim));
  if (p && p.steps.length) {
    body.appendChild(h('div.review-check',
      h('span.step-n', '1'),
      h('p', firstLine(p.steps[0], compact ? 150 : 260)),
      p.steps.length > 1 ? h('span.review-more', `+${p.steps.length - 1} more`) : null));
  }
  lead.appendChild(body);
  const thumb = p ? thumbFor(p, app) : null;
  if (thumb) lead.appendChild(thumb);
  item.appendChild(lead);

  const acts = h('div.review-acts');
  const waiting = readyMembers(unit.nums);
  const prog = unitProgress(unit.key);
  // One Approve, on the card, when the unit shipped ONE packet — the claim
  // above it IS that packet's claim, so a yes here is a yes to something you
  // have read. A looser unit (one branch, different packets) has no single
  // sentence to lead with, so its only verdict is in the outline.
  if (p && waiting.length) {
    acts.appendChild(h('button.btn.tiny.approve', {
      type: 'button',
      disabled: !!(prog && !prog.error),
      title: `one branch, one packet — approves each of the ${waiting.length} cards in it`,
      onclick: (e) => { e.stopPropagation(); approveUnit(unit, app); },
    }, prog && !prog.error
      ? `approving ${Math.min(prog.done + 1, prog.total)} of ${prog.total}…`
      : (waiting.length > 1 ? `Approve all ${waiting.length}` : `Approve #${waiting[0].num}`)));
  }
  acts.appendChild(h('button.btn.tiny.unit-open', {
    type: 'button',
    onclick: (e) => { e.stopPropagation(); app.openUnit(unit.lead.num); },
  }, p ? 'Read the changes' : `Read all ${unit.size}`));
  if (p && p.live_url) {
    acts.appendChild(h('a.btn.tiny.ghost.review-live', {
      href: p.live_url, target: '_blank', rel: 'noreferrer noopener', title: p.live_url,
      onclick: (e) => e.stopPropagation(),
    }, 'See it live ↗'));
  }
  acts.appendChild(h('span.grow'));
  acts.appendChild(h('span.review-meta',
    [shortAgent(unit.agent), p ? unitMeta(p) : ''].filter(Boolean).join(' · '), ' · ',
    timeEl(unit.lead.last_activity_at, { suffix: false })));
  item.appendChild(acts);
  const err = progressLine(unitProgress(unit.key));
  if (err) item.appendChild(err);
  return item;
}

function unitMeta(p) {
  const bits = [];
  if (p.steps.length) bits.push(`${p.steps.length} ${p.steps.length === 1 ? 'check' : 'checks'}`);
  if (p.test_result) bits.push(firstLine(p.test_result, 26));
  return bits.join(' · ');
}

/**
 * A unit of one: one card, one packet. Exactly what it has always been — its
 * own card in review, leading with its own title and claim, with the verdict on
 * it. There is nothing to outline, so clicking it opens the card itself.
 */
export function singleCard(card, app, { compact = false } = {}) {
  const p = packetFor(card);
  const state = cardState(card);
  const merging = state === 'integrating';
  const item = h('div.review-item', {
    class: `review-item${merging ? ' is-merging' : ''}`,
    'data-num': card.num,
  });

  const lead = openable('review-lead', card, app);
  lead.appendChild(h('span.row-num', '#' + card.num));
  const body = h('span.review-body',
    h('span.review-title', card.title),
    h('p.review-claim', p.claim || 'No claim recorded — open it and ask the agent what it thinks it did.'));
  if (p.steps.length) {
    body.appendChild(h('div.review-check',
      h('span.step-n', '1'),
      h('p', firstLine(p.steps[0], compact ? 150 : 260)),
      p.steps.length > 1 ? h('span.review-more', `+${p.steps.length - 1} more`) : null));
  }
  lead.appendChild(body);
  const thumb = thumbFor(p, app);
  if (thumb) lead.appendChild(thumb);
  item.appendChild(lead);
  item.appendChild(singleActions(card, p, app, merging));
  return item;
}

function singleActions(card, p, app, merging) {
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
  // the words are typed in the RAIL, so this row carries the composing STATE and
  // none of the typing.
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
  } else {
    row.appendChild(h('button.btn.approve.tiny', {
      type: 'button',
      title: `approve #${card.num} — the session rebases, gates and merges the branch`,
      onclick: (e) => { e.stopPropagation(); app.verdict(card, 'approve'); },
    }, 'Approve'));
    row.appendChild(h('button.btn.bounce.tiny', {
      type: 'button',
      title: 'open it and send it back with notes',
      onclick: (e) => { e.stopPropagation(); app.openCard(card.num, { focus: 'bounce' }); },
    }, 'Bounce'));
    if (p.live_url) {
      row.appendChild(h('a.btn.tiny.ghost.review-live', {
        href: p.live_url, target: '_blank', rel: 'noreferrer noopener', title: p.live_url,
        onclick: (e) => e.stopPropagation(),
      }, 'See it live ↗'));
    }
  }
  row.appendChild(h('span.grow'));
  row.appendChild(h('span.review-meta', metaLine(card, p), ' · ',
    timeEl(card.last_activity_at, { suffix: false })));
  return h('div.review-actwrap', row);
}

function metaLine(card, p) {
  const bits = [shortAgent(card.agent_name)];
  if (p.steps.length) bits.push(`${p.steps.length} ${p.steps.length === 1 ? 'check' : 'checks'}`);
  if (p.test_result) bits.push(firstLine(p.test_result, 26));
  if (card.bounce_count) bits.push(`bounced ${card.bounce_count === 1 ? 'once' : `${card.bounce_count}×`}`);
  return bits.join(' · ');
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

// ---- the outline: what the rail shows when you open a unit ---------------
//
// "when I open it up, it outlines everything that changed". One page, read top
// to bottom: what the branch says it did and how to check it, then a numbered
// section per member card with its own claim, its own checks and its own
// pictures. Nothing is behind an expander — an outline you have to unfold is
// the list we just deleted, wearing a different hat.

/** Everything the outline's look depends on — the rail rebuilds only on this. */
export function unitSig(unit) {
  if (!unit) return null;
  const prog = unitProgress(unit.key);
  return [unit.key, unit.size,
    unit.cards.map((c) => `${c.num}:${cardState(c)}:${c.bounce_count || 0}`).join(','),
    unit.cards.map((c) => (bounceComposing(c.num) ? 'b' + c.num : '')).join(''),
    bounceComposing(unitBounceNum(unit)) ? 'ball' : '',
    prog ? `${prog.kind}${prog.done}/${prog.total}${prog.error ? 'e' + prog.error : ''}` : '',
  ].join('|');
}

/**
 * The whole-unit bounce composes against a number nobody else uses, so a
 * half-written "send the whole thing back" and a half-written "send #7 back"
 * are two different drafts and neither eats the other.
 */
function unitBounceNum(unit) { return -Math.abs(Number(unit.lead.num)); }

export function unitHead(unit, app) {
  return h('div.rail-head',
    h('span.rail-num', plural(unit.size, 'change')),
    h('span.rail-title', { title: unit.title }, unit.title),
    h('span.rail-note.unit-branch', unit.branch || shortAgent(unit.agent)),
    h('button.rail-close', { type: 'button', onclick: () => app.closeUnit() }, 'Close'));
}

export function unitOutline(unit, app) {
  const root = h('div.unit-outline');
  const p = unit.packet;

  // Order matters here and it is the whole card: what the branch says it did,
  // how to check it, then EVERYTHING THAT CHANGED. The branch's own pictures,
  // reports and diffstat are supporting evidence and sit below the changes —
  // put them on top and you scroll past a screen of preamble to reach the six
  // things you were actually asked about.
  if (p && p.claim) {
    root.appendChild(h('div.unit-sec',
      h('p.packet-label.good', 'What the branch says it did'),
      h('p.packet-claim', p.claim)));
  }
  root.appendChild(h('p.unit-intro',
    `${plural(unit.size, 'card')} on one branch — approve them together at the bottom, `
    + 'or send back just the part that is wrong.'));

  if (p && p.steps.length) {
    const list = h('div.steps');
    p.steps.forEach((s, i) => {
      list.appendChild(h('div.step', h('span.step-n', String(i + 1)), h('p', s)));
    });
    root.appendChild(h('div.unit-sec', h('p.packet-label.accent', 'Check it yourself'), list));
  }
  if (p && p.readback.trim()) {
    root.appendChild(h('div.unit-sec',
      h('p.packet-label.accent', 'What came back'),
      h('pre.packet-readback', p.readback.trim())));
  }

  root.appendChild(h('p.packet-label.accent.unit-changes-label',
    `Everything that changed · ${unit.size}`));
  unit.cards.forEach((card, i) => {
    root.appendChild(changeSection(unit, card, i + 1, app));
  });

  if (p) {
    if (p.shots.length) {
      root.appendChild(h('div.unit-sec',
        h('p.packet-label.accent', 'The branch, in pictures'),
        shotStrip(p.shots, app)));
    }
    const [, docs] = splitAttachments(p.reports);
    if (docs.length) {
      root.appendChild(h('div.unit-sec',
        h('p.packet-label.accent', docs.length === 1 ? 'Report' : `Reports · ${docs.length}`),
        reportRow(docs, app, false)));
    }
    if (p.live_url) {
      root.appendChild(h('a.btn.packet-live', {
        href: p.live_url, target: '_blank', rel: 'noreferrer noopener', title: p.live_url,
      }, 'See it live ↗'));
    }
    const meta = [p.branch, p.diffstat,
      p.test_cmd && p.test_result ? `${p.test_cmd} → ${p.test_result}` : p.test_result]
      .filter(Boolean).join(' · ');
    if (meta) root.appendChild(h('p.packet-meta', meta));
  }
  return root;
}

/** One member card's own part of the unit, and the way to send just it back. */
function changeSection(unit, card, n, app) {
  const part = memberPart(card, unit);
  const state = cardState(card);
  const sec = h('div.unit-change', {
    class: `unit-change is-${state}`,
    'data-num': card.num,
  });

  sec.appendChild(h('div.unit-change-head',
    h('span.step-n', String(n)),
    h('span.unit-change-title', part.title),
    h('span.grow'),
    h('button.unit-change-num', {
      type: 'button',
      title: `open #${card.num} — its own timeline, and the agent that wrote it`,
      onclick: () => app.openCard(card.num, { fromUnit: unit.lead.num }),
    }, '#' + card.num)));

  const bodyWrap = h('div.unit-change-body');
  bodyWrap.appendChild(h('p.unit-change-claim', part.claim || 'No claim recorded for this one.'));
  if (part.steps.length) {
    const list = h('div.steps');
    part.steps.forEach((s, i) => {
      list.appendChild(h('div.step', h('span.step-n', String(i + 1)), h('p', s)));
    });
    bodyWrap.appendChild(list);
  }
  if (part.shots.length) bodyWrap.appendChild(shotStrip(part.shots, app));
  if (part.live_url) {
    bodyWrap.appendChild(h('a.btn.tiny.ghost.review-live', {
      href: part.live_url, target: '_blank', rel: 'noreferrer noopener', title: part.live_url,
    }, 'See it live ↗'));
  }
  sec.appendChild(bodyWrap);

  if (state === 'integrating') {
    sec.appendChild(h('p.unit-change-note.is-good', 'Approved — merging now.'));
    return sec;
  }
  if (state !== 'ready') {
    sec.appendChild(h('p.unit-change-note',
      `Back with the agent — this part is no longer waiting on you.`));
    return sec;
  }
  if (card.bounce_count) {
    sec.appendChild(h('p.unit-change-note',
      `Bounced ${card.bounce_count === 1 ? 'once' : `${card.bounce_count}×`} already.`));
  }
  sec.appendChild(bounceRow(card, app, {
    label: `Send #${card.num} back`,
    placeholder: 'What has to change about this one? (goes straight to the agent)',
    submit: `Send #${card.num} back`,
    run: (text) => app.verdict(card, 'bounce', text),
    hint: 'Only this part goes back — the rest of the unit stays yours to approve.',
  }));
  return sec;
}

/**
 * A bounce composer. Pressing the button is already the decision (card #44), so
 * from that moment there are exactly two things on offer: send it, or back out.
 * The words are typed HERE, in the rail (card #46) — never into a box in a list
 * that the next board frame rebuilds.
 */
function bounceRow(card, app, { label, placeholder, submit, run, hint, num }) {
  const n = num == null ? card.num : num;
  const key = `bounce:${n}`;
  const wrap = h('div.unit-bounce');
  const composing = bounceComposing(n) || !!draft(key);

  if (!composing) {
    wrap.appendChild(h('button.btn.tiny.bounce', {
      type: 'button',
      onclick: () => app.composeBounce(n),
    }, label));
    return wrap;
  }

  const notes = h('textarea.bounce-notes', {
    id: 'bounce-' + n,
    rows: '2',
    placeholder,
    oninput: (e) => draft(key, e.target.value),
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    },
  });
  notes.value = draft(key);

  function send() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    bounceComposing(n, false);
    run(text);
  }

  function cancel() {
    draft(key, null);
    bounceComposing(n, false);
    app.render();
  }

  wrap.appendChild(notes);
  wrap.appendChild(h('div.verdicts.is-bouncing',
    h('button.btn.bounce', { type: 'button', onclick: () => send() }, submit),
    h('button.btn.ghost', { type: 'button', onclick: () => cancel() }, 'Cancel')));
  if (hint) wrap.appendChild(h('p.unit-bounce-hint', hint));
  return wrap;
}

/**
 * The one verdict for the unit, pinned at the bottom of the rail. One Approve,
 * covering everything the outline just showed you — and, next to it, the way to
 * send the whole thing back when the problem is the branch rather than one part
 * of it.
 */
export function unitBar(unit, app) {
  const bar = h('div.unit-bar');
  const waiting = readyMembers(unit.nums);
  const prog = unitProgress(unit.key);

  if (!waiting.length) {
    bar.appendChild(h('p.unit-bar-note', unit.cards.some((c) => cardState(c) === 'integrating')
      ? 'Approved — the session is merging this branch now.'
      : 'Nothing here is waiting on you any more.'));
    return bar;
  }

  const n = unitBounceNum(unit);
  const composingAll = bounceComposing(n) || !!draft(`bounce:${n}`);
  if (composingAll) {
    bar.appendChild(bounceRow(unit.lead, app, {
      num: n,
      label: '',
      placeholder: `What has to change? (goes to the agent for all ${waiting.length})`,
      submit: `Send all ${waiting.length} back`,
      run: (text) => bounceUnit(unit, text, app),
      hint: `Every one of the ${waiting.length} cards goes back with the same notes.`,
    }));
    const err = progressLine(prog);
    if (err) bar.appendChild(err);
    return bar;
  }

  bar.appendChild(h('p.unit-bar-note',
    waiting.length === unit.size
      ? `One branch, ${plural(unit.size, 'change')} — one decision.`
      : `${plural(waiting.length, 'change')} still waiting on you; the rest already went back.`));
  bar.appendChild(h('div.verdicts',
    h('button.btn.approve', {
      type: 'button',
      disabled: !!(prog && !prog.error),
      onclick: () => approveUnit(unit, app),
    }, prog && !prog.error && prog.kind === 'approve'
      ? `approving ${Math.min(prog.done + 1, prog.total)} of ${prog.total}…`
      : (waiting.length > 1 ? `Approve all ${waiting.length}` : `Approve #${waiting[0].num}`)),
    h('button.btn.bounce', {
      type: 'button',
      title: 'the whole branch goes back with your notes',
      onclick: () => app.composeBounce(n),
    }, 'Send it all back')));
  const err = progressLine(prog);
  if (err) bar.appendChild(err);
  return bar;
}

function shotStrip(shots, app) {
  const urls = shots.map(attachmentUrl).filter(Boolean);
  const strip = h('div.shots');
  shots.forEach((ref) => {
    const url = attachmentUrl(ref);
    const cap = attachmentCaption(ref);
    const frame = h('div.shot-frame');
    if (url) {
      frame.appendChild(h('img', {
        src: url, alt: cap || 'screenshot', loading: 'lazy',
        onerror: (e) => { e.target.remove(); frame.appendChild(h('span.shot-slot', 'image unavailable')); },
      }));
    } else {
      frame.appendChild(h('span.shot-slot', typeof ref === 'string' ? firstLine(String(ref).split('/').pop(), 26) : 'screenshot'));
    }
    strip.appendChild(h('button.shot', {
      type: 'button', title: cap || 'screenshot',
      onclick: () => { if (url) app.lightbox(urls, urls.indexOf(url), shots.map(attachmentCaption)); },
    }, frame, cap ? h('span.shot-cap', cap) : null));
  });
  return strip;
}
