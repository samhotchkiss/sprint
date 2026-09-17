// The List layout — the daily driver, and the default view.
//
// Scan top to bottom: the meter says what shape the sprint is in, then the only
// generously-spaced section on the page is the one that wants something from
// you. Everything below it gets progressively quieter — running work is a
// compressed table, blocked work is dimmer still, and the queue is pills.
import { h, timeEl, firstLine, reconcile } from './util.js';
import {
  sections, meterSegments, cardState, needsKind, needsYouCount, motionState,
  blockedReason, waitingMark, isStuck, BLOCKED_NOTE, blockedByMark,
  conversations, conversationState, conversationAnswered, resolvedConversations,
  CONVERSATION_HINT, isOpenInRail, cardPaintVer, meterPaintVer, store,
} from './state.js';
import { phaseChip } from './phase.js';
import { executorTag } from './settings.js';
import { renderMeter } from './meter.js';
import { renderDone, completeList } from './done.js';
import { reviewBlock } from './review.js';
import { reviewUnits } from './units.js';

export function renderList(root, app) {
  const secs = sections();
  reconcile(root, [
    { key: 'meter', ver: meterPaintVer(secs), make: () => renderMeter(meterSegments(secs)) },
    sectionItem('needs', (sec) => fillNeeds(sec, sections().needs_you, app)),
    sectionItem('motion', (sec) => fillMotion(sec, sections().in_motion, app)),
    sectionItem('blocked', (sec) => fillBlocked(sec, sections().blocked, app)),
    sectionItem('waiting', (sec) => fillWaiting(sec, sections().waiting, app)),
    { key: 'done', ver: listSecVer(secs.done) + ':' + store.doneOpen + ':' + store.doneMore,
      make: () => renderDone(secs.done, app) },
  ]);
}

function sectionItem(key, fill) {
  return {
    key,
    ver: 1,
    make: () => {
      const sec = h('section.section');
      fill(sec);
      sec._sync = () => fill(sec);
      return sec;
    },
  };
}

function listSecVer(col) {
  return (col.cards || []).map(cardPaintVer).join(';');
}

function convoListVer() {
  return conversations().map(cardPaintVer).join(';')
    + '|' + resolvedConversations().map(cardPaintVer).join(',')
    + ':' + store.convoMore;
}

function syncCardRows(rows, cards, rowFn, app) {
  reconcile(rows, (cards || []).map((card) => ({
    key: card.pendingSubmit ? 'pending:' + (card.id || card.key) : 'card:' + card.num,
    ver: cardPaintVer(card),
    data: card,
    make: () => rowFn(card, app),
  })));
}

function rowGroup(key, cards, rowFn, app) {
  return {
    key,
    ver: 'rows',
    data: { cards, app },
    make: () => {
      const rows = h('div.rows');
      syncCardRows(rows, cards, rowFn, app);
      rows._sync = ({ cards: next, app: nextApp }) => syncCardRows(rows, next, rowFn, nextApp);
      return rows;
    },
  };
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
  // Signoffs are counted in WORK UNITS, not cards (card #55): six cards that
  // shipped on one branch are one thing to look at, and saying "six finished
  // and want a verdict" over a list showing one card is the old list talking.
  const signoffs = reviewUnits(cards.filter((c) => needsKind(c) !== 'question')).length;
  if (!cards.length) return 'Nothing is waiting on you. Every card on the board is either running, stuck on something outside this sprint, or in the queue.';
  const bits = [];
  if (asks) bits.push(asks === 1 ? 'One agent is waiting on an answer' : `${word(asks)} agents are waiting on an answer`);
  if (signoffs) bits.push(signoffs === 1 ? 'one finished piece of work wants a verdict' : `${word(signoffs)} finished pieces of work want a verdict`);
  return `${cap(bits.join('; '))}. Everything else is running without you.`;
}

const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
const word = (n) => (n < WORDS.length ? WORDS[n] : String(n));
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function fillNeeds(sec, col, app) {
  // The count is DECISIONS, not cards: a six-card branch is one thing waiting
  // on you (card #55), and it renders as one card in the list below.
  const asks = col.cards.filter((c) => needsKind(c) === 'question');
  const signoffs = col.cards.filter((c) => needsKind(c) !== 'question');
  const items = [
    { key: 'head', ver: String(needsYouCount(col.cards)),
      make: () => head('Needs you', needsYouCount(col.cards), { accent: true, tight: true }) },
    { key: 'intro', ver: needsIntro(col.cards),
      make: () => h('p.section-intro', needsIntro(col.cards)) },
  ];
  // The section still holds both shapes of asking, and they are still told apart
  // by rail colour and tag — but they no longer interleave. An open question is
  // answered in one line; a finished branch is a review, and a review is ONE
  // card per work unit (see review.js). Mixing the two by age made every pass
  // through this section start over from scratch.
  if (asks.length) items.push(rowGroup('asks', asks, needsRow, app));
  if (signoffs.length) {
    items.push({
      key: 'review',
      ver: signoffs.map(cardPaintVer).join(';'),
      make: () => reviewBlock(signoffs, app),
    });
  }
  if (!col.cards.length) {
    items.push({ key: 'empty', ver: 1, make: () => h('p.section-empty', 'Nothing needs you.') });
  }
  // …and below all of it, the ongoing threads.
  if (conversations().length || resolvedConversations().length) {
    items.push({
      key: 'convo',
      ver: convoListVer(),
      make: () => conversationBlock(app),
    });
  }
  reconcile(sec, items);
}

// ---- 1b. conversations ---------------------------------------------------

/**
 * User, verbatim: "a lower section in the 'needs you' column where the card
 * gets highlighted if there's an unread and unseen message. once I see the
 * message, the highlighting dims, and once I respond the highlight goes away
 * completely."
 *
 * Three weights, one row shape. The rail colour and the mark say which; nothing
 * counts anything, and a thread you are up to date on is as quiet as the queue.
 */
export function conversationBlock(app, { compact = false } = {}) {
  const cards = conversations();
  const done = resolvedConversations();
  if (!cards.length && !done.length) return null;
  const block = h('div', { class: compact ? 'convo-block is-compact' : 'convo-block' });
  block.appendChild(h('div.convo-head',
    h('span.convo-label', 'Conversations'),
    h('span.grow'),
    h('span.convo-note', 'ongoing threads — nothing here is blocking work')));
  const rows = h('div.rows');
  for (const card of cards) rows.appendChild(conversationRow(card, app));
  block.appendChild(rows);
  // Card #80: the threads you already finished. Not gone, and not filed in with
  // the merged branches — a compact list right here, one line each, click to
  // reread. It is the exact list Complete uses (same file, same expander),
  // capped shorter because it sits under live work rather than owning a column.
  if (done.length) {
    block.appendChild(h('div.convo-sub',
      h('span.convo-sub-label', 'Resolved'),
      h('span.grow'),
      h('span.convo-sub-note', 'finished threads — open one to reread it')));
    block.appendChild(completeList(done, app,
      { moreKey: 'convoMore', visible: RESOLVED_VISIBLE }));
  }
  return block;
}

/** How many resolved threads show before the rest fold behind the expander. */
export const RESOLVED_VISIBLE = 5;

const CONVO_MARK = { unseen: 'New message', seen: 'Your turn', clear: 'Up to date' };

export function conversationRow(card, app) {
  const st = conversationState(card);
  // "Answered — resolve?" is its own weight, and it is the whole point of card
  // #80: a thread whose question you already answered used to read exactly like
  // one nobody had touched. The row says so; the rail carries the button.
  const answered = conversationAnswered(card);
  const mark = answered ? 'is-answered' : `is-${st}`;
  const row = openable(`row convo-row ${mark}`, card, app);
  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(h('span.row-main',
    h('span.row-title', card.title),
    h('span.row-sub', lastWord(card, app))));
  row.appendChild(h('span.row-right',
    h('span.row-tag', {
      class: `row-tag convo-tag ${mark}`,
      title: answered ? CONVERSATION_HINT.answered : CONVERSATION_HINT[st],
    }, answered ? 'Answered — resolve?' : CONVO_MARK[st]),
    h('span.row-meta', timeEl(card.last_activity_at, { suffix: false }))));
  return row;
}

/** The latest thing said in the thread, whoever said it. */
function lastWord(card, app) {
  const ev = card.last_event;
  if (!ev) return firstLine(card.body || '', 160);
  const who = ev.actor === 'user' ? 'you' : ev.actor === 'session' ? 'session' : null;
  const text = firstLine(app.eventText(ev), 150);
  if (!text) return firstLine(card.body || '', 160);
  return who ? `${who}: ${text}` : text;
}

export function needsRow(card, app) {
  const kind = needsKind(card);
  const state = cardState(card);
  const q = kind === 'question' ? card.question : null;
  const row = openable(`row needs-row is-${kind === 'question' ? 'question' : 'signoff'}`, card, app);

  const main = h('span.row-main',
    h('span.row-title', card.title),
    h('span.row-ask', askLine(card, kind, state)));

  // Card #53, user verbatim: "get the actions out of cards. I click the card, it
  // loads in the sidebar, and that's where I review and act." The quick-reply
  // chips used to answer from this row; they are the same chips, in the rail, on
  // the question panel, with the thread and the agent's artifacts around them.
  // What the row keeps is the news that there are options at all.
  if (q && q.options && q.options.length) {
    main.appendChild(h('span.row-optnote',
      `${q.options.length} options — open it to choose`));
  }
  if (q && q.artifacts) {
    main.appendChild(h('span.row-optnote.is-artifacts',
      artifactNote(q.artifacts)));
  }

  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(main);
  row.appendChild(h('span.row-right',
    h('span.row-tag', kind === 'question' ? 'Asks' : state === 'integrating' ? 'Merging' : 'Signoff'),
    h('span.row-meta', shortAgent(card.agent_name), ' · ', timeEl(card.last_activity_at, { suffix: false }))));
  return row;
}

/**
 * What a DECISION REQUEST brought with it, in a few words. Card #50: the point
 * of the row is to say "there is something to look at in here", not to be the
 * place you look at it.
 */
function artifactNote(a) {
  const bits = [];
  if (a.attachments && a.attachments.length) {
    bits.push(a.attachments.length === 1 ? 'a screenshot' : `${a.attachments.length} screenshots`);
  }
  if (a.url) bits.push('a live preview');
  if (!bits.length && a.notes) bits.push('context');
  return `Handed over ${bits.join(' + ')} to look at`;
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

function fillMotion(sec, col, app) {
  const items = [
    { key: 'head', ver: String(col.cards.length), make: () => head('In motion', col.cards.length) },
  ];
  if (col.cards.length) items.push(rowGroup('rows', col.cards, motionRow, app));
  else items.push({ key: 'empty', ver: 1, make: () => h('p.section-empty', 'No agent is running.') });
  reconcile(sec, items);
}

export function motionRow(card, app) {
  const st = motionState(card);
  const row = openable('row work-row', card, app);
  // The silence notice is already the whole right-hand side of this row; saying
  // it twice would push out the last thing the agent actually did.
  const last = lastAction(card, app);
  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(h('span.row-main',
    h('span.row-title', card.title),
    last ? h('span.row-sub', last) : null));
  // A live phase takes the label slot: it is the same information the plain
  // "active · 4m" was trying to convey, except it says what the agent is DOING
  // and its clock is the phase's own, not the age of the last thing typed.
  const chip = st.phase ? phaseChip(card, { ph: st.phase }) : null;
  row.appendChild(h('span.row-prog', { title: st.title },
    h('span.prog-track', h('span.prog-fill', { style: { width: st.pct + '%', background: st.color } })),
    chip || h('span.prog-label', { style: { color: st.color } }, st.label)));
  // The agent's name, and — only when this card is not on the board's default
  // executor/model — how it was dispatched: "grok · tmux", or just "opus" for
  // a card whose only exception is the fallback model it was re-dispatched on.
  row.appendChild(h('span.row-agent', shortAgent(card.agent_name), executorTag(card, { compact: true })));
  return row;
}

function lastAction(card, app) {
  const ev = card.last_event;
  if (!ev || ev.kind === 'agent_silent') return '';
  // The chip already says "testing · 2m" — repeating "phase: testing" under the
  // title would spend the only free line on the row saying it twice.
  if (ev.payload && ev.payload.phase) return '';
  return firstLine(app.eventText(ev), 160);
}

// ---- 3. blocked ----------------------------------------------------------

function fillBlocked(sec, col, app) {
  const items = [
    { key: 'head', ver: String(col.cards.length),
      make: () => head('Blocked', col.cards.length, { quiet: true, tight: true }) },
    { key: 'intro', ver: 1, make: () => h('p.section-intro', BLOCKED_NOTE) },
  ];
  if (col.cards.length) items.push(rowGroup('rows', col.cards, blockedRow, app));
  else items.push({ key: 'empty', ver: 1, make: () => h('p.section-empty', 'Nothing is stuck.') });
  reconcile(sec, items);
}

export function blockedRow(card, app) {
  const r = blockedReason(card);
  const row = openable('row blocked-row', card, app);
  row.appendChild(h('span.row-num', '#' + card.num));
  row.appendChild(h('span.row-main',
    h('span.row-title', card.title),
    h('span.row-reason', { class: r.bad ? 'row-reason is-bad' : 'row-reason' }, r.text)));
  const waitingOn = blockedByMark(card);
  if (waitingOn) row.appendChild(waitingOn);
  row.appendChild(h('span.row-age', timeEl(card.state_since || card.updated_at, { suffix: false })));
  return row;
}

// ---- 4. queued & held ----------------------------------------------------

function fillWaiting(sec, col, app) {
  const held = col.cards.filter((c) => cardState(c) === 'held').length;
  const queued = col.cards.length - held;
  const bits = [];
  if (queued) bits.push(`${queued} queued`);
  if (held) bits.push(`${held} held`);
  const items = [
    { key: 'head', ver: bits.join(' · ') || '0',
      make: () => head('Queued & held', bits.join(' · ') || null, { quiet: true }) },
  ];
  if (col.cards.length) {
    items.push({
      key: 'pills',
      ver: 'pills',
      data: { cards: col.cards, app },
      make: () => {
        const pills = h('div.pills');
        syncCardRows(pills, col.cards, waitingPill, app);
        pills._sync = ({ cards, app: next }) => syncCardRows(pills, cards, waitingPill, next);
        return pills;
      },
    });
  } else {
    items.push({ key: 'empty', ver: 1, make: () => h('p.section-empty', 'Nothing waiting.') });
  }
  reconcile(sec, items);
}

export function waitingPill(card, app) {
  if (card.pendingSubmit) return pendingPill(card, app);
  const mark = waitingMark(card);
  return h('button.pill', {
    type: 'button',
    class: `pill${mark === 'held' ? ' is-held' : ''}${isOpenInRail(card) ? ' is-open' : ''}`,
    'data-num': card.num,        // the keyboard's Waiting column walks these
    onclick: () => app.openCard(card.num),
  },
    h('span.pill-num', '#' + card.num),
    h('span.pill-title', card.title),
    // A queued card can be perfectly dispatchable and still going nowhere,
    // because it is behind another card. Saying so here is what keeps the
    // queue honest (#61).
    blockedByMark(card),
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
  // Every List row is built here, so this is the one place the sweep's amber
  // has to be applied: `is-stuck` ambers whatever age text that row carries.
  // Card #76: same place `is-open` gets applied, so the row for whichever
  // card the rail is currently showing lights up here for free too.
  let full = cls;
  if (isStuck(card)) full += ' is-stuck';
  if (isOpenInRail(card)) full += ' is-open';
  const el = h('div', {
    class: full,
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
