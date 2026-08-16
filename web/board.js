// The Board layout — the spec's kanban, four columns wide.
//
// Same header, same meter, same rail. What changes is that the work stops being
// a reading order and becomes a distribution you can see across at a glance.
//
// Cards here are NOT interactive surfaces: there is no inline answering, no
// verdict buttons, no expanders. A card face says who and how long and what
// last happened, and clicking anywhere on it hands the card to the rail. That is
// the whole contract — every decision is made in one place, with the thread
// under it, rather than half on a tile and half in a panel.
// Column order is the life of a card, left to right, on the user's ruling:
// waiting (queued → held → blocked) · in progress · needs you · review
// (awaiting review → complete). The two things that are on HIM are deliberately
// apart: answering a question and signing off a finished branch are not the
// same job.
import { h, timeEl, firstLine } from './util.js';
import {
  boardColumns, cardState, needsKind, motionState, blockedReason, waitingMark,
  meterSegments, sections, isSilent, isStuck, STUCK_HINT, STATE_LABEL,
} from './state.js';
import { phaseChip } from './phase.js';
import { executorTag } from './settings.js';
import { renderMeter } from './meter.js';
import { shortAgent } from './list.js';
import { reviewBlock } from './review.js';

const scrollMemo = new Map();

export function renderBoard(root, app) {
  // The meter stays in urgency order (need you → in motion → blocked → queued),
  // not column order: it answers "what shape is this sprint in", and the column
  // strip right below it answers "where is everything".
  root.appendChild(renderMeter(meterSegments(sections())));
  root.appendChild(boardGrid(app, { fold: false }));
}

/**
 * The Fold interior: vertical space is the scarce thing on a 740px-tall screen,
 * so the three columns that are about live work get the grid and the waiting
 * pile drops to the "Elsewhere" strip at the bottom as pills.
 */
export function renderFold(root, app) {
  root.appendChild(boardGrid(app, { fold: true }));
  root.appendChild(elsewhereStrip(app));
}

function boardGrid(app, { fold }) {
  const grid = h('div', { class: fold ? 'fold-cols' : 'board' });
  // On the Fold the waiting pile becomes the Elsewhere strip; everything that is
  // live, or waiting on the user, keeps a real column.
  const wanted = fold
    ? ['in_motion', 'needs_you', 'review']
    : ['waiting', 'in_motion', 'needs_you', 'review'];

  for (const col of boardColumns()) {
    if (!wanted.includes(col.key)) continue;
    // remember where each column was scrolled to across re-renders
    const prev = scrollMemo.get(col.key);

    const body = h('div.col-body', { 'data-col': col.key });
    if (!col.count) body.appendChild(h('p.col-empty', col.empty));
    // Sections appear only when they have something in them — an empty "Held"
    // heading is a promise of a pile that isn't there.
    for (const sec of col.sections) {
      if (!sec.cards.length) continue;
      if (sec.label && col.sections.length > 1) {
        body.appendChild(h('div.col-sec',
          h('span.col-sec-name', sec.label),
          h('span.grow'),
          h('span.col-sec-count', String(sec.cards.length))));
      }
      if (sec.note) body.appendChild(h('p.col-note', sec.note));
      // Awaiting review is the one section whose tiles ARE interactive, on the
      // user's ruling that an easy yes should not cost a drawer. It groups by
      // work unit and carries the verdict on the row; everything else on the
      // Board stays a face that only opens the rail.
      if (col.key === 'review' && sec.key === 'awaiting') {
        body.appendChild(reviewBlock(sec.cards, app, { compact: true }));
        continue;
      }
      const group = h('div.col-group', { class: sec.quiet ? 'col-group is-quiet' : 'col-group' });
      for (const card of sec.cards) group.appendChild(renderCardFace(card, app));
      body.appendChild(group);
    }
    if (prev) requestAnimationFrame(() => { body.scrollTop = prev; });
    body.addEventListener('scroll', () => scrollMemo.set(col.key, body.scrollTop), { passive: true });

    grid.appendChild(h('section.col', { class: `col col-${col.key}` },
      h('div.col-head',
        h('span.col-dot', { style: { background: col.dot } }),
        h('span.col-name', col.board),
        h('span.grow'),
        h('span.col-count', String(col.count))),
      body));
  }
  return grid;
}

// ---- card face -----------------------------------------------------------

export function renderCardFace(card, app) {
  if (card.pendingSubmit) return pendingFace(card, app);

  const state = cardState(card);
  const col = colOf(state);
  const face = h('button.card', {
    type: 'button',
    'data-num': card.num,
    class: `card${col === 'needs_you' ? ' is-needs' : ''}${isSilent(card) ? ' is-quiet' : ''}`,
    onclick: () => app.openCard(card.num),
  });

  // A live phase IS the tag: "testing · 2m" on its own clock, in place of the
  // state age that went stale the moment the agent stopped typing.
  const chip = col === 'in_motion' ? phaseChip(card) : null;
  const tag = faceTag(card, state, col);
  face.appendChild(h('div.card-top',
    h('span.card-num', '#' + card.num),
    h('span.grow'),
    chip || h('span.card-tag', { style: tag.color ? { color: tag.color } : null }, tag.text)));
  face.appendChild(h('span.card-title', card.title));

  const sub = faceSub(card, state, col, app);
  if (sub.text) face.appendChild(h('span.card-sub', { style: sub.color ? { color: sub.color } : null }, sub.text));

  if (col === 'in_motion') {
    const st = motionState(card);
    face.appendChild(h('span.prog-track', { title: st.title },
      h('span.prog-fill', { style: { width: st.pct + '%', background: st.color } })));
  }

  // The age is the only thing on the face that can say "this has been sitting
  // here too long", so that is where the sweep's amber goes. No new chrome.
  // "grok · tmux" sits with the agent name, and only when this card was
  // dispatched differently from the board's default — a tag on every card
  // would say nothing.
  face.appendChild(h('div.card-foot',
    h('span', shortAgent(card.agent_name)),
    executorTag(card, { compact: true }),
    h('span.grow'),
    h('span', { class: isStuck(card) ? 'is-stuck-age' : null,
      title: isStuck(card) ? STUCK_HINT : null },
      timeEl(card.last_activity_at || card.state_since, { suffix: false }))));
  return face;
}

function colOf(state) {
  if (state === 'needs_you' || state === 'ready') return 'needs_you';
  // Approved and merging is work in flight, not a decision you owe — user,
  // verbatim: "Why do these cards stay in 'needs you' once they're approved?"
  if (state === 'triaging' || state === 'in_progress' || state === 'integrating') return 'in_motion';
  if (state === 'blocked' || state === 'failed' || state === 'stale') return 'blocked';
  if (state === 'queued' || state === 'held') return 'waiting';
  return 'done';
}

function faceTag(card, state, col) {
  if (col === 'needs_you') {
    if (state === 'integrating') return { text: 'Merging', color: 'var(--good)' };
    return needsKind(card) === 'question'
      ? { text: 'Asks', color: 'var(--accent)' }
      : { text: 'Signoff', color: 'var(--good)' };
  }
  if (col === 'in_motion') {
    const st = motionState(card);
    return { text: st.label, color: st.color };
  }
  if (col === 'blocked') {
    return { text: state === 'failed' ? 'failed' : state === 'stale' ? 'stale' : 'blocked', color: null };
  }
  if (col === 'waiting') return { text: waitingMark(card), color: null };
  return { text: STATE_LABEL[state] || state, color: null };
}

function faceSub(card, state, col, app) {
  if (col === 'needs_you') {
    if (needsKind(card) === 'question') {
      const q = card.question;
      return { text: q && q.text ? firstLine(q.text, 140) : 'Waiting on you.', color: 'var(--dim)' };
    }
    const p = card.evidence || {};
    return { text: firstLine(p.claim || 'Evidence packet is in.', 140), color: 'var(--good)' };
  }
  if (col === 'blocked') {
    const r = blockedReason(card);
    return { text: r.text, color: r.bad ? 'var(--bad)' : 'var(--faint)' };
  }
  if (col === 'in_motion') {
    if (state === 'integrating') {
      return { text: 'Approved — the session is merging the branch.', color: 'var(--good)' };
    }
    const ev = card.last_event;
    // the tag already says "quiet Nm" — the face does not repeat it
    if (!ev || ev.kind === 'agent_silent') return { text: '', color: null };
    // ...and if the tag is the phase chip, the chip already said this line.
    if (ev.payload && ev.payload.phase) return { text: '', color: null };
    return { text: firstLine(app.eventText(ev), 140), color: null };
  }
  return { text: '', color: null };
}

function pendingFace(card, app) {
  return h('div.card.is-pending',
    h('div.card-top', h('span.card-num', '#…'), h('span.grow'),
      h('span.card-tag', card.error ? 'not sent' : 'sending')),
    h('span.card-title', card.title || firstLine(card.text || '', 90) || 'New item'),
    card.error
      ? h('button.btn.tiny', { type: 'button', onclick: () => app.retrySubmit(card) }, 'Retry')
      : null);
}

// ---- the Fold's "Elsewhere" strip ---------------------------------------

function elsewhereStrip(app) {
  const col = boardColumns().find((c) => c.key === 'waiting');
  const strip = h('div.elsewhere');
  strip.appendChild(h('span.elsewhere-label', 'Elsewhere'));
  // queued, held AND blocked: the Fold has no column for them, and a blocked
  // card that is nowhere on the screen is a card you find out about too late.
  const cards = col.sections.reduce((all, s) => all.concat(s.cards), []);
  if (!cards.length) {
    strip.appendChild(h('span.pill-mark', 'nothing queued'));
    return strip;
  }
  for (const card of cards) {
    if (card.pendingSubmit) continue;
    const state = cardState(card);
    const blocked = ['blocked', 'failed', 'stale'].includes(state);
    const mark = blocked ? (STATE_LABEL[state] || state).toLowerCase() : waitingMark(card);
    strip.appendChild(h('button.pill', {
      type: 'button',
      class: `pill${mark === 'held' ? ' is-held' : ''}${blocked ? ' is-blocked' : ''}`,
      title: card.title,
      onclick: () => app.openCard(card.num),
    },
      h('span.pill-num', '#' + card.num),
      h('span.pill-title', card.title),
      h('span.pill-mark', mark)));
  }
  return strip;
}
