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
import { h, timeEl, firstLine } from './util.js';
import {
  columns, cardState, needsKind, motionState, blockedReason, waitingMark,
  meterSegments, sections, isSilent, STATE_LABEL,
} from './state.js';
import { renderMeter } from './meter.js';
import { renderDone } from './done.js';
import { shortAgent } from './list.js';

const scrollMemo = new Map();

export function renderBoard(root, app) {
  const secs = sections();
  root.appendChild(renderMeter(meterSegments(secs)));
  root.appendChild(boardGrid(app, { fold: false }));
  root.appendChild(renderDone(secs.done, app));
}

/**
 * The Fold interior: the same four buckets, but only three get a column —
 * vertical space is the scarce thing on a 740px-tall screen, so the queue moves
 * to the "Elsewhere" strip at the bottom and Done drops out entirely (it is a
 * desktop reading surface; on the Fold it would cost a third of the height).
 */
export function renderFold(root, app) {
  root.appendChild(boardGrid(app, { fold: true }));
  root.appendChild(elsewhereStrip(app));
}

function boardGrid(app, { fold }) {
  const grid = h('div', { class: fold ? 'fold-cols' : 'board' });
  const wanted = fold
    ? ['needs_you', 'in_motion', 'blocked']
    : ['needs_you', 'in_motion', 'blocked', 'waiting'];

  for (const col of columns()) {
    if (!wanted.includes(col.key)) continue;
    // remember where each column was scrolled to across re-renders
    const prev = scrollMemo.get(col.key);

    const body = h('div.col-body', { 'data-col': col.key });
    if (!col.cards.length) body.appendChild(h('p.col-empty', emptyText(col.key)));
    for (const card of col.cards) body.appendChild(renderCardFace(card, app));
    if (prev) requestAnimationFrame(() => { body.scrollTop = prev; });
    body.addEventListener('scroll', () => scrollMemo.set(col.key, body.scrollTop), { passive: true });

    grid.appendChild(h('section.col', { class: `col col-${col.key}` },
      h('div.col-head',
        h('span.col-dot', { style: { background: col.dot } }),
        h('span.col-name', col.board),
        h('span.grow'),
        h('span.col-count', String(col.cards.length))),
      body));
  }
  return grid;
}

function emptyText(key) {
  return {
    needs_you: 'Nothing needs you.',
    in_motion: 'No agent is running.',
    blocked: 'Nothing is stuck.',
    waiting: 'Nothing waiting.',
  }[key] || '—';
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

  const tag = faceTag(card, state, col);
  face.appendChild(h('div.card-top',
    h('span.card-num', '#' + card.num),
    h('span.grow'),
    h('span.card-tag', { style: tag.color ? { color: tag.color } : null }, tag.text)));
  face.appendChild(h('span.card-title', card.title));

  const sub = faceSub(card, state, col, app);
  if (sub.text) face.appendChild(h('span.card-sub', { style: sub.color ? { color: sub.color } : null }, sub.text));

  if (col === 'in_motion') {
    const st = motionState(card);
    face.appendChild(h('span.prog-track', { title: st.title },
      h('span.prog-fill', { style: { width: st.pct + '%', background: st.color } })));
  }

  face.appendChild(h('div.card-foot',
    h('span', shortAgent(card.agent_name)),
    h('span.grow'),
    h('span', timeEl(card.last_activity_at || card.state_since, { suffix: false }))));
  return face;
}

function colOf(state) {
  if (state === 'needs_you' || state === 'ready' || state === 'integrating') return 'needs_you';
  if (state === 'triaging' || state === 'in_progress') return 'in_motion';
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
    const ev = card.last_event;
    // the tag already says "quiet Nm" — the face does not repeat it
    if (!ev || ev.kind === 'agent_silent') return { text: '', color: null };
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
  const secs = sections();
  const strip = h('div.elsewhere');
  strip.appendChild(h('span.elsewhere-label', 'Elsewhere'));
  const cards = secs.waiting.cards;
  if (!cards.length) {
    strip.appendChild(h('span.pill-mark', 'nothing queued'));
    return strip;
  }
  for (const card of cards) {
    if (card.pendingSubmit) continue;
    const mark = waitingMark(card);
    strip.appendChild(h('button.pill', {
      type: 'button',
      class: `pill${mark === 'held' ? ' is-held' : ''}`,
      title: card.title,
      onclick: () => app.openCard(card.num),
    },
      h('span.pill-num', '#' + card.num),
      h('span.pill-title', card.title),
      h('span.pill-mark', mark)));
  }
  return strip;
}
