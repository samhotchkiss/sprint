// Done — a count that opens into a plain list, and nothing more.
//
// The design does not draw this surface; the spec asks for "collapsed to a count
// + list". So it stays quiet on purpose: no column, no cards, no celebration —
// one uppercase label at the bottom of the page, and when you open it, one line
// per closed card with the mark that says how it ended. Every card here is
// reopenable from its own thread, which is the only reason the list exists.
import { h, timeEl } from './util.js';
import { cardState, store, STATE_LABEL } from './state.js';

const MARK = { completed: '✓', rejected: '✕', canceled: '✕', duplicate: '·' };

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

  const list = h('div.done-list');
  for (const card of col.cards) {
    const st = cardState(card);
    list.appendChild(h('button.done-row', {
      type: 'button',
      class: `done-row is-${st}`,
      // the keyboard's Review column (card #57) walks these too, once you have
      // opened the list — it is the same set of cards the Board draws there
      'data-num': card.num,
      title: STATE_LABEL[st] || st,
      onclick: () => app.openCard(card.num),
    },
      h('span.done-mark', MARK[st] || '·'),
      h('span.card-num', '#' + card.num),
      h('span.done-title', card.title),
      h('span.done-age', timeEl(card.updated_at || card.state_since, { suffix: false }))));
  }
  wrap.appendChild(list);
  return wrap;
}
