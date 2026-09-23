// Reproduces: "I send a message, it shows up, then it disappears -- a refresh
// brings it back." /api/board embeds only a recent window of the sidebar
// thread (sidebar_thread(limit=200) server-side); a client that wholesale
// REPLACES its local sidebar with that window on every poll will drop an
// already-confirmed message the instant enough newer chat pushes it out of
// that window, even though the message still exists on the server -- which
// is exactly why a hard refresh (a fresh, larger fetch) brings it back.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { store, applyBoard } from '../web/state.js';

function board(extra = {}) {
  return {
    sprint: { id: 1, title: 'Billing week', hold_mode: false },
    cards: [],
    sidebar: [],
    seq: 1,
    ...extra,
  };
}

test('a confirmed sidebar message survives its own send window scrolling past it', () => {
  applyBoard(board());

  // What web/app.js's sessionChat() does on send: push a local echo, then --
  // once the POST resolves -- stamp the real seq onto that SAME object. It
  // never clears localEcho; that omission is part of this bug.
  const mine = { actor: 'user', kind: 'chat', payload: { text: 'please check this' },
                local: true, localEcho: true, sortSeq: 1.5 };
  store.sidebar.push(mine);

  // The POST resolved: the server confirmed it as seq 2.
  mine.local = false;
  mine.seq = 2;

  // Poll #1: the board's own recent-window snapshot has caught up and
  // includes it now.
  applyBoard(board({ seq: 2, sidebar: [
    { seq: 2, actor: 'user', kind: 'chat', payload: { text: 'please check this' } },
  ] }));
  assert.ok(store.sidebar.some((e) => e.payload.text === 'please check this'),
    'still visible right after the server confirms it');

  // Poll #2: 200 newer sidebar-scope lines have since pushed seq 2 out of the
  // server's bounded embed window -- exactly what happens on a busy board.
  // The message was never deleted; it just is not in THIS snapshot anymore.
  const newer = [];
  for (let s = 3; s <= 202; s++) {
    newer.push({ seq: s, actor: 'session', kind: 'chat', payload: { text: 'noise ' + s } });
  }
  applyBoard(board({ seq: 202, sidebar: newer }));

  assert.ok(store.sidebar.some((e) => e.seq === 2 && e.payload.text === 'please check this'),
    'a confirmed message (real seq) must never be dropped just because a later, ' +
    'bounded snapshot happened not to include it -- that is what a page refresh ' +
    '(a fresh, larger fetch) papers back over, which is the bug as reported');
});
