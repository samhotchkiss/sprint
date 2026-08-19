// Done / Complete — a compact list, and nothing more.
//
// User, verbatim: "we need completed to just be a compact list. you can click
// each item to open the card. and, beyond 20 completed, they're hidden and you
// can expand that list."
//
// So finished work reads like an index, not like work: one line per card —
// mark, number, title, age — every line opening that card in the rail. The
// newest 20 are on screen and the rest sit behind a single expander that says
// how many it is holding. No tiles, no cards, no celebration, and (per the
// spec's hard rule) no count anywhere in the chrome that isn't the expander's
// own sentence about itself.
import { h, timeEl } from './util.js';
import { cardState, store, STATE_LABEL, isOpenInRail } from './state.js';

const MARK = {
  completed: '✓', rejected: '✕', canceled: '✕', duplicate: '·',
  // A resolved thread ended by agreement, not by being thrown away — it gets
  // the tick, never the cross.
  resolved: '✓',
};

/** How many closed cards are on screen before the rest fold away. */
export const DONE_VISIBLE = 20;

export function renderDone(col, app) {
  const wrap = h('section.done');
  const count = col.cards.length;
  const open = store.doneOpen && count > 0;

  const toggle = h('button.done-toggle', {
    type: 'button',
    class: `done-toggle${open ? ' is-open' : ''}`,
    'aria-expanded': open ? 'true' : 'false',
    disabled: count === 0,
    onclick: () => { store.doneOpen = !store.doneOpen; app.render(); },
  },
    h('span.chev', '›'),
    h('span', 'Done'),
    h('span.section-count', String(count)));
  wrap.appendChild(toggle);

  if (!open) return wrap;
  wrap.appendChild(completeList(col.cards, app));
  return wrap;
}

/**
 * The list itself, shared by the List's Done section, the Board's Complete
 * section, and (card #80) the resolved conversations under the live threads, so
 * none of them can drift on what a finished thing looks like.
 *
 * `cards` arrives newest-first (state.js sorts the done bucket that way), so
 * "the newest N" is simply the head of it.
 *
 * `moreKey` is which store flag holds this list's expanded/collapsed state, and
 * `visible` how many lines it shows before folding. Both have defaults, so the
 * two Done surfaces call this exactly as they always did; a second list on the
 * same screen passes its own key so expanding one never expands the other.
 */
export function completeList(cards, app, { moreKey = 'doneMore', visible = DONE_VISIBLE } = {}) {
  const list = h('div.done-list');
  if (!cards.length) return list;

  const showAll = !!store[moreKey];
  const shown = showAll ? cards : cards.slice(0, visible);
  const hidden = cards.length - shown.length;

  for (const card of shown) list.appendChild(doneRow(card, app));

  // One expander, and it is the only thing here that says a number — because
  // the number IS the sentence ("show 31 older"), not a badge on the chrome.
  if (hidden > 0 || showAll) {
    const older = cards.length - visible;
    if (older > 0) {
      list.appendChild(h('button.done-more', {
        type: 'button',
        'aria-expanded': showAll ? 'true' : 'false',
        onclick: () => { store[moreKey] = !showAll; app.render(); },
      }, showAll ? `hide the ${older} older` : `show ${older} older`));
    }
  }
  return list;
}

function doneRow(card, app) {
  const st = cardState(card);
  return h('button.done-row', {
    type: 'button',
    class: `done-row is-${st}${isOpenInRail(card) ? ' is-open' : ''}`,
    'data-num': card.num,
    title: STATE_LABEL[st] || st,
    onclick: () => app.openCard(card.num),
  },
    h('span.done-mark', MARK[st] || '·'),
    h('span.card-num', '#' + card.num),
    h('span.done-title', card.title),
    h('span.done-age', timeEl(card.updated_at || card.state_since, { suffix: false })));
}
