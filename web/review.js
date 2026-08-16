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
// Card #53 holds all the way through, user verbatim: "get the actions out of
// cards. I click the card, it loads in the sidebar, and that's where I review
// and act." So nothing in this list does anything except open the rail — no
// verdict on a face, no lightbox on a thumbnail, no live link on a row. The
// verdict is a bar pinned at the bottom of the rail where it cannot be scrolled
// off: the unit's bar under its outline, a single card's bar under its thread.
//
// Deleted with this card, deliberately and not as a follow-up: the grouped
// (expand-to-see-the-members) row, the per-member review rows, and the
// Review-next walkthrough. All three existed to organise a list that should not
// be there.
import { h, timeEl, firstLine, plural } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import { store, cardState, draft, bounceComposing } from './state.js';
import { shortAgent, openable } from './list.js';
import { reviewUnits, unitOf, packetFor, memberPart, ownTitle } from './units.js';
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
 * and how many changes are inside it. That is all it does: clicking anywhere on
 * it opens the outline in the rail (card #53 — a card face is a single click
 * target and nothing else), and the Approve that covers the whole unit is at
 * the bottom of that outline, under the changes it decides.
 */
function unitCard(unit, app, { compact }) {
  const p = unit.packet;
  const item = h('div.unit-card', {
    'data-unit': unit.key,
    role: 'button',
    tabindex: '0',
    title: 'open the outline — everything that changed, in one page',
    onclick: () => app.openUnit(unit.lead.num),
    onkeydown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); app.openUnit(unit.lead.num); }
    },
  });

  const lead = h('div.unit-lead');
  const body = h('div.unit-body',
    h('span.unit-title', unit.title),
    h('span.unit-count', `${plural(unit.size, 'change')} on one branch`));
  if (p && p.claim) body.appendChild(h('p.review-claim', p.claim));
  if (p && p.steps.length) {
    body.appendChild(h('div.review-check',
      h('span.step-n', '1'),
      h('p', firstLine(p.steps[0], compact ? 150 : 260)),
      p.steps.length > 1 ? h('span.review-more', `+${p.steps.length - 1} more`) : null));
  }
  lead.appendChild(body);
  const thumb = p ? thumbFor(p) : null;
  if (thumb) lead.appendChild(thumb);
  item.appendChild(lead);

  // The status line says where this unit is up to and nothing else — the same
  // shape a single card's line has, so the two read as one list.
  const acts = h('div.review-acts');
  const waiting = readyMembers(unit.nums);
  const prog = unitProgress(unit.key);
  acts.appendChild(h('span.review-open-hint', unitHint(unit, waiting, prog)));
  acts.appendChild(h('span.grow'));
  acts.appendChild(h('span.review-meta',
    [shortAgent(unit.agent), p ? unitMeta(p) : ''].filter(Boolean).join(' · '), ' · ',
    timeEl(unit.lead.last_activity_at, { suffix: false })));
  item.appendChild(h('div.review-actwrap', acts));

  const err = progressLine(prog);
  if (err) item.appendChild(err);
  return item;
}

/** One line: what opening this unit gets you, in the words of what it is now. */
function unitHint(unit, waiting, prog) {
  if (prog && !prog.error) return `Approving ${Math.min(prog.done + 1, prog.total)} of ${prog.total}…`;
  if (!waiting.length) {
    return unit.cards.some((c) => cardState(c) === 'integrating')
      ? 'Approved — the session is merging this branch now.'
      : 'Nothing here is waiting on you any more.';
  }
  if (unit.cards.some((c) => bounceComposing(c.num))) return 'Bounce half-written — open it to finish';
  return waiting.length > 1
    ? `Open it to read all ${waiting.length} changes and approve them together`
    : 'Open it to read the changes and approve them';
}

function unitMeta(p) {
  const bits = [];
  if (p.steps.length) bits.push(`${p.steps.length} ${p.steps.length === 1 ? 'check' : 'checks'}`);
  if (p.test_result) bits.push(firstLine(p.test_result, 26));
  return bits.join(' · ');
}

/**
 * A unit of one: one card, one packet. Exactly what it has always been — its
 * own card in review, leading with its own title and claim. Clicking it opens
 * the card itself, and the verdict is the bar pinned under its thread.
 */
export function singleCard(card, app, { compact = false } = {}) {
  const p = packetFor(card);
  const state = cardState(card);
  const merging = state === 'integrating';
  const item = openable(`review-item${merging ? ' is-merging' : ''}`, card, app);

  const lead = h('div.review-lead');
  lead.appendChild(h('span.row-num', '#' + card.num));
  const body = h('span.review-body',
    h('span.review-title', ownTitle(card) || firstLine(card.body || '', 70) || `Card #${card.num}`),
    h('p.review-claim', p.claim || 'No claim recorded — open it and ask the agent what it thinks it did.'));
  if (p.steps.length) {
    body.appendChild(h('div.review-check',
      h('span.step-n', '1'),
      h('p', firstLine(p.steps[0], compact ? 150 : 260)),
      p.steps.length > 1 ? h('span.review-more', `+${p.steps.length - 1} more`) : null));
  }
  lead.appendChild(body);
  const thumb = thumbFor(p);
  if (thumb) lead.appendChild(thumb);
  item.appendChild(lead);
  item.appendChild(statusRow(card, p, merging));
  return item;
}

/**
 * The line under a single card. Card #53: it says what this card is and where
 * it is up to, and it does not DO anything — the verdict is in the rail, one
 * click away, on the card you are already about to click.
 */
function statusRow(card, p, merging) {
  const row = h('div.review-acts');

  if (merging) {
    row.appendChild(h('span.review-merging', 'Approved — the session is merging the branch now.'));
    row.appendChild(h('span.grow'));
    row.appendChild(h('span.review-meta', shortAgent(card.agent_name), ' · ',
      timeEl(card.state_since || card.last_activity_at, { suffix: false })));
    return row;
  }

  // Card #44's composing state survives, as a STATE rather than as a control:
  // the words are typed in the rail (#46), and the line only says that is where
  // you left them.
  const composing = bounceComposing(card.num) || !!draft(`bounce:${card.num}`);
  row.appendChild(composing
    ? h('span.review-composing', 'Bounce half-written — open it to finish')
    : h('span.review-open-hint', 'Open it to approve, bounce or reject'));
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

/**
 * The packet's first screenshot, as a picture and nothing else. It used to open
 * a lightbox; card #53 took every click off the face except the one that opens
 * the rail, and the lightbox is one click further in, on the packet itself.
 */
function thumbFor(p) {
  if (!p.shots.length) return null;
  const urls = p.shots.map(attachmentUrl).filter(Boolean);
  if (!urls.length) return null;
  const caps = p.shots.map(attachmentCaption);
  const frame = h('span.review-thumb-frame',
    h('img', {
      src: urls[0], alt: caps[0] || 'screenshot', loading: 'lazy',
      onerror: (e) => { e.target.remove(); },
    }));
  return h('span.review-thumb', {
    title: p.shots.length > 1 ? `${p.shots.length} screenshots` : (caps[0] || 'screenshot'),
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
      'Back with the agent — this part is no longer waiting on you.'));
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
 * The one verdict for the unit, pinned at the bottom of the rail — the same
 * place a single card's verdict bar sits (card #53), so the decision is always
 * in one spot and can never be scrolled off by an outline with six sections in
 * it. One Approve, covering everything the outline just showed you, and next to
 * it the way to send the whole thing back when the problem is the branch rather
 * than one part of it.
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

  // Two bounces and the server tags `escalate`: stop offering a blind third try
  // on the part that keeps coming back.
  const stuck = waiting.filter((c) => (c.bounce_count || 0) >= 2);
  if (stuck.length) {
    bar.appendChild(h('p.escalate',
      `${stuck.map((c) => '#' + c.num).join(', ')} bounced twice. The session stops retrying blind `
      + 'here and brings it to you to co-design.'));
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

// ---- one card's verdict, in the rail -------------------------------------
//
// Card #53, user verbatim: "get the actions out of cards. I click the card, it
// loads in the sidebar, and that's where I review and act."
//
// This is the bar for a card you opened on its own — a unit of one from the
// review list, or a member you clicked through to out of an outline. It is
// pinned at the bottom of the rail, above the composer, in exactly the place
// the unit's bar sits: whichever of the two the rail is showing, the decision
// is in the same spot and outside the scrolling thread.
//
// It decides ONE card. The whole-branch Approve is not repeated here, because
// the thing that approves a branch is the branch's own outline — a button that
// acted on five other cards you cannot see from here is exactly the blind bulk
// approve card #26 refused.

/** The signature the rail memoises this bar on. Null = no bar for this card. */
export function verdictBarSig(card) {
  if (!card || cardState(card) !== 'ready') return null;
  const unit = unitInReview(card.num);
  return [card.num, card.bounce_count,
    bounceComposing(card.num) || draft(`bounce:${card.num}`) ? 'b' : '',
    unit && unit.size > 1 ? unit.size : 0].join('|');
}

export function packetVerdictBar(card, app) {
  const key = `bounce:${card.num}`;
  const bar = h('div.review-bar.is-verdict');
  const unit = unitInReview(card.num);
  const many = !!unit && unit.size > 1;

  // Two bounces and the server tags `escalate`: stop offering a blind third try.
  if (card.bounce_count >= 2) {
    bar.appendChild(h('p.escalate',
      'Bounced twice. The session stops retrying blind here and brings it to you to co-design.'));
  }
  if (many) {
    bar.appendChild(h('p.review-bar-unit',
      `One of ${plural(unit.size, 'change')} on one branch. What you do here decides this card `
      + 'alone — the whole branch is approved from its outline.'));
  }

  // Card #44. Hitting Bounce is already the decision; from that moment the bar
  // offers exactly two things — send it, or back out. Approve and Reject are not
  // dimmed, they are GONE, because the failure being prevented is hitting
  // Approve with a half-written bounce in the box under it.
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

  function send() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    bounceComposing(card.num, false);
    app.verdict(card, 'bounce', text);
  }

  function cancel() {
    // Cancel puts the buttons back. It drops only what you typed HERE — every
    // other composer on the page keeps its draft.
    draft(key, null);
    bounceComposing(card.num, false);
    app.render();
  }

  if (composing) {
    bar.appendChild(h('div.review-notes', notes));
    bar.appendChild(h('div.verdicts.is-bouncing',
      h('button.btn.bounce', { type: 'button', onclick: () => send() }, 'Submit bounce'),
      h('button.btn.ghost', { type: 'button', onclick: () => cancel() }, 'Cancel')));
    // No focus grab here. Card #46: focus is asked for ONCE, when you press
    // Bounce (app.composeBounce), and restored by id on every re-render after.
    return bar;
  }

  bar.appendChild(h('div.verdicts',
    h('button.btn.approve', {
      type: 'button',
      title: `approve #${card.num} — the session rebases, gates and merges the branch`,
      onclick: () => app.verdict(card, 'approve'),
    }, many ? `Approve #${card.num} only` : 'Approve'),
    h('button.btn.bounce', {
      type: 'button',
      title: 'send it back with notes',
      onclick: () => app.composeBounce(card.num),
    }, 'Bounce'),
    h('button.btn.reject', {
      type: 'button',
      title: 'this should not have been built — the branch is dropped',
      onclick: () => app.verdict(card, 'reject', draft(key) || undefined),
    }, 'Reject')));
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
