// The List layout — the daily driver, and the default view.
//
// Scan top to bottom: the meter says what shape the sprint is in, then the only
// generously-spaced section on the page is the one that wants something from
// you. Everything below it gets progressively quieter — running work is a
// compressed table, blocked work is dimmer still, and the queue is pills.
import { h, timeEl, firstLine } from './util.js';
import {
  sections, meterSegments, cardState, needsKind, motionState, blockedReason,
  waitingMark,
} from './state.js';
import { renderMeter } from './meter.js';
import { renderDone } from './done.js';

export function renderList(root, app) {
  const secs = sections();
  root.appendChild(renderMeter(meterSegments(secs)));

  root.appendChild(needsSection(secs.needs_you, app));
  root.appendChild(motionSection(secs.in_motion, app));
  root.appendChild(blockedSection(secs.blocked, app));
  root.appendChild(waitingSection(secs.waiting, app));
  root.appendChild(renderDone(secs.done, app));
}

// ---- section chrome ------------------------------------------------------

function head(title, count, { accent = false, quiet = false, tight = false } = {}) {
  return h('div.section-head', {
    class: `section-head${accent ? ' accent' : ''}${quiet ? ' quiet' : ''}${tight ? ' tight' : ''}`,
  },
    h('h2', title),
    count ? h('span.section-count', String(count)) : null,
    h('div.section-rule'));
}

// ---- 1. needs you --------------------------------------------------------

/** Plain English, and only about what is actually on the board right now. */
function needsIntro(cards) {
  const asks = cards.filter((c) => needsKind(c) === 'question').length;
  const signoffs = cards.length - asks;
  if (!cards.length) return 'Nothing is waiting on you. Every card on the board is either running, stuck on something outside this sprint, or in the queue.';
  const bits = [];
  if (asks) bits.push(asks === 1 ? 'One agent is waiting on an answer' : `${word(asks)} agents are waiting on an answer`);
  if (signoffs) bits.push(signoffs === 1 ? 'one finished and wants a verdict' : `${word(signoffs)} finished and want a verdict`);
  return `${cap(bits.join('; '))}. Everything else is running without you.`;
}

const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
const word = (n) => (n < WORDS.length ? WORDS[n] : String(n));
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function needsSection(col, app) {
  const sec = h('section.section');
  sec.appendChild(head('Needs you', col.cards.length, { accent: true, tight: true }));
  sec.appendChild(h('p.section-intro', needsIntro(col.cards)));
  const rows = h('div.rows');
  for (const card of col.cards) rows.appendChild(needsRow(card, app));
  sec.appendChild(col.cards.length ? rows : h('p.section-empty', 'Nothing needs you.'));
  return sec;
}

export function needsRow(card, app) {
  const kind = needsKind(card);
  const state = cardState(card);
  const q = kind === 'question' ? card.question : null;
  const row = openable(`row needs-row is-${kind === 'question' ? 'question' : 'signoff'}`, card, app);

  const main = h('span.row-main',
    h('span.row-title', card.title),
    h('span.row-ask', askLine(card, kind, state)));

  // Options answer inline, right here, because they are one tap and the design
  // says the top of the page is where decisions get made. Free-text answers open
  // the card — a sentence deserves the thread it lands in.
  if (q && q.options && q.options.length) {
    const chips = h('span.chips');
    for (const opt of q.options) {
      chips.appendChild(h('button.chip-btn', {
        type: 'button',
        title: `answer #${card.num}: ${opt.label}`,
        onclick: (e) => { e.stopPropagation(); app.answer(card, q, opt.value); },
      }, opt.label));
    }
    main.appendChild(chips);
  }

  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(main);
  row.appendChild(h('span.row-right',
    h('span.row-tag', kind === 'question' ? 'Asks' : state === 'integrating' ? 'Merging' : 'Signoff'),
    h('span.row-meta', shortAgent(card.agent_name), ' · ', timeEl(card.last_activity_at, { suffix: false }))));
  return row;
}

function askLine(card, kind, state) {
  if (kind === 'question') {
    const q = card.question;
    if (q && q.text) return firstLine(q.text, 200);
    return 'Waiting on you — open the card for what it is asking.';
  }
  if (state === 'integrating') return 'Approved — the session is merging the branch now.';
  const p = card.evidence || {};
  const bits = [];
  const steps = Array.isArray(p.validate) ? p.validate.length : (p.validate ? 1 : 0);
  if (steps) bits.push(`${steps} ${steps === 1 ? 'check' : 'checks'}`);
  const shots = Array.isArray(p.screenshots) ? p.screenshots.length : 0;
  if (shots) bits.push(`${shots} ${shots === 1 ? 'screenshot' : 'screenshots'}`);
  if (card.bounce_count) bits.push(`bounced ${card.bounce_count === 1 ? 'once' : `${card.bounce_count} times`} already`);
  if (!bits.length) return firstLine(p.claim || 'Evidence packet is in — it wants a verdict.', 160);
  return bits.join(', ') + '.';
}

// ---- 2. in motion --------------------------------------------------------

function motionSection(col, app) {
  const sec = h('section.section');
  sec.appendChild(head('In motion', col.cards.length));
  const rows = h('div.rows');
  for (const card of col.cards) rows.appendChild(motionRow(card, app));
  sec.appendChild(col.cards.length ? rows : h('p.section-empty', 'No agent is running.'));
  return sec;
}

export function motionRow(card, app) {
  const st = motionState(card);
  const row = openable('row work-row', card, app);
  const last = card.last_event ? firstLine(app.eventText(card.last_event), 160) : '';
  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(h('span.row-main',
    h('span.row-title', card.title),
    last ? h('span.row-sub', last) : null));
  row.appendChild(h('span.row-prog', { title: st.title },
    h('span.prog-track', h('span.prog-fill', { style: { width: st.pct + '%', background: st.color } })),
    h('span.prog-label', { style: { color: st.color } }, st.label)));
  row.appendChild(h('span.row-agent', shortAgent(card.agent_name)));
  return row;
}

// ---- 3. blocked ----------------------------------------------------------

function blockedSection(col, app) {
  const sec = h('section.section');
  sec.appendChild(head('Blocked', col.cards.length, { quiet: true, tight: true }));
  sec.appendChild(h('p.section-intro', 'Nothing you type moves these. The session re-checks them on its own.'));
  const rows = h('div.rows');
  for (const card of col.cards) rows.appendChild(blockedRow(card, app));
  sec.appendChild(col.cards.length ? rows : h('p.section-empty', 'Nothing is stuck.'));
  return sec;
}

export function blockedRow(card, app) {
  const r = blockedReason(card);
  const row = openable('row blocked-row', card, app);
  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(h('span.row-main',
    h('span.row-title', card.title),
    h('span.row-reason', { class: r.bad ? 'row-reason is-bad' : 'row-reason' }, r.text)));
  row.appendChild(h('span.row-age', timeEl(card.state_since || card.updated_at, { suffix: false })));
  return row;
}

// ---- 4. queued & held ----------------------------------------------------

function waitingSection(col, app) {
  const held = col.cards.filter((c) => cardState(c) === 'held').length;
  const queued = col.cards.length - held;
  const bits = [];
  if (queued) bits.push(`${queued} queued`);
  if (held) bits.push(`${held} held`);

  const sec = h('section.section');
  sec.appendChild(head('Queued & held', bits.join(' · ') || null, { quiet: true }));
  const pills = h('div.pills');
  for (const card of col.cards) pills.appendChild(waitingPill(card, app));
  sec.appendChild(col.cards.length ? pills : h('p.section-empty', 'Nothing waiting.'));
  return sec;
}

export function waitingPill(card, app) {
  if (card.pendingSubmit) return pendingPill(card, app);
  const mark = waitingMark(card);
  return h('button.pill', {
    type: 'button',
    class: `pill${mark === 'held' ? ' is-held' : ''}`,
    onclick: () => app.openCard(card.num),
  },
    h('span.pill-num', '#' + card.num),
    h('span.pill-title', card.title),
    h('span.pill-mark', mark));
}

function pendingPill(card, app) {
  return h('span.pill.is-pending',
    h('span.pill-num', '#…'),
    h('span.pill-title', card.title || firstLine(card.text || '', 60) || 'New item'),
    card.error
      ? h('button.chip-btn', { type: 'button', onclick: () => app.retrySubmit(card) }, 'not sent — retry')
      : h('span.pill-mark', 'sending'));
}

// ---- shared --------------------------------------------------------------

/** A row you can click or tab to. Chips inside stop the click themselves. */
export function openable(cls, card, app) {
  const el = h('div', {
    class: cls,
    'data-num': card.num,
    role: 'button',
    tabindex: '0',
    onclick: () => app.openCard(card.num),
    onkeydown: (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === el) { e.preventDefault(); app.openCard(card.num); }
    },
  });
  return el;
}

export function shortAgent(name) {
  if (!name) return 'unassigned';
  return String(name).replace(/^sprint-/, '');
}
