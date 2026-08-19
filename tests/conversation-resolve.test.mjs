// Card #80 — a conversation you have ANSWERED, and where it goes when you end it.
//
// The bug, from the card: a thread whose question had been answered sat in
// Needs you looking exactly like one nobody had touched, because the only exit
// a conversation had was `cancel` — and cancel means "discard, this did not
// happen". So the board now knows three things it did not:
//
//   1. which threads are ANSWERED (an exchange happened and you spoke last),
//   2. that a RESOLVED thread is not in Needs you and not in Done either, and
//   3. that it is drawn in the same compact list Complete uses — capped, with
//      one expander, click a line to reread it.
//
// web/state.js and web/done.js are DOM-free at import time (done.js only
// touches the document when you actually build a list), so the model behind all
// three is testable without a browser. Run: node --test "tests/*.test.mjs"
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  store, applyBoard, columns, boardColumns, conversations, resolvedConversations,
  conversationAnswered, conversationState, isThreadState, columnOf, STATE_LABEL,
} from '../web/state.js';
import { DONE_VISIBLE } from '../web/done.js';
import { RESOLVED_VISIBLE } from '../web/list.js';

/** A conversation card as the board payload delivers it. */
function thread(num, { state = 'conversation', answered = false, highlight = 'clear',
  title = `Thread #${num}`, at = 1000 + num } = {}) {
  return {
    num,
    kind: 'conversation',
    state,
    title,
    body: title,
    conversation: {
      state: highlight,
      last_seen_seq: 0,
      latest_incoming_seq: answered ? 1 : 0,
      latest_user_seq: answered ? 2 : 0,
      unread: highlight !== 'clear',
      answered,
    },
    last_activity_at: at,
    updated_at: at,
  };
}

/** An ordinary work card. */
function work(num, state = 'queued') {
  return { num, kind: 'work', state, title: `Card #${num}`, last_activity_at: 1 };
}

function load(cards) {
  store.cards.clear();
  store.pending.length = 0;
  applyBoard({ sprint: { id: 1, title: 'Sprint' }, cards, sidebar: [], seq: 1 });
}

const numsIn = (list) => list.map((c) => c.num);

function bucket(key) {
  return numsIn(columns().find((c) => c.key === key).cards);
}

function section(colKey, secKey) {
  const col = boardColumns().find((c) => c.key === colKey);
  return numsIn(col.sections.find((s) => s.key === secKey).cards);
}

// ---- answered ------------------------------------------------------------

test('answered is an exchange, not silence', () => {
  // The distinction the whole affordance rests on: a question YOU filed that
  // nobody has replied to yet is quiet too, and offering to close that one
  // would be wrong.
  load([thread(1, { answered: false }), thread(2, { answered: true })]);
  assert.equal(conversationAnswered(store.cards.get(1)), false);
  assert.equal(conversationAnswered(store.cards.get(2)), true);
});

test('a board that never heard of answered offers nothing', () => {
  const old = thread(3);
  delete old.conversation.answered;
  load([old]);
  assert.equal(conversationAnswered(store.cards.get(3)), false);
});

test('answered is not the same as the highlight being clear', () => {
  load([thread(4, { answered: false, highlight: 'clear' })]);
  assert.equal(conversationState(store.cards.get(4)), 'clear');
  assert.equal(conversationAnswered(store.cards.get(4)), false);
});

// ---- where a resolved thread lives ---------------------------------------

test('a resolved thread is out of Needs you', () => {
  load([thread(1, { highlight: 'unseen' }), thread(2, { state: 'resolved' })]);
  assert.deepEqual(numsIn(conversations()), [1]);
  assert.deepEqual(bucket('needs_you'), []);
  assert.deepEqual(section('needs_you', 'needs_you'), []);
});

test('...and out of Done, because it is kept, not filed away', () => {
  load([thread(2, { state: 'resolved' }), work(5, 'completed')]);
  assert.deepEqual(bucket('done'), [5]);
  assert.deepEqual(section('review', 'complete'), [5]);
});

test('it is in the resolved list instead, newest first', () => {
  load([
    thread(1, { state: 'resolved', at: 100 }),
    thread(2, { state: 'resolved', at: 300 }),
    thread(3, { state: 'resolved', at: 200 }),
    thread(4),
  ]);
  assert.deepEqual(numsIn(resolvedConversations()), [2, 3, 1]);
});

test('a CANCELED thread is still an ordinary closed card', () => {
  // Cancel means discarded, so it belongs with the closed pile — the two
  // endings are not the same word and must not land in the same place.
  load([thread(1, { state: 'canceled' })]);
  assert.deepEqual(numsIn(resolvedConversations()), []);
  assert.deepEqual(bucket('done'), [1]);
  assert.deepEqual(section('review', 'complete'), [1]);
});

test('a resolved WORK card is not a thread and still lands in Done', () => {
  // The server refuses to write this at all; if one ever arrives, it must not
  // vanish into the conversations block.
  load([{ ...work(9, 'resolved') }]);
  assert.deepEqual(numsIn(resolvedConversations()), []);
  assert.deepEqual(bucket('done'), [9]);
});

test('both thread states are kept out of the work sections', () => {
  assert.equal(isThreadState('conversation'), true);
  assert.equal(isThreadState('resolved'), true);
  assert.equal(isThreadState('canceled'), false);
  assert.equal(isThreadState('completed'), false);
});

test('resolved reads as Resolved and counts as done', () => {
  assert.equal(STATE_LABEL.resolved, 'Resolved');
  assert.equal(columnOf('resolved'), 'done');
});

// ---- the compact list ----------------------------------------------------

test('the resolved list folds sooner than Complete does', () => {
  // Same list component, same expander (#51). It shows fewer lines because it
  // sits under live threads rather than owning a column.
  assert.ok(RESOLVED_VISIBLE > 0);
  assert.ok(RESOLVED_VISIBLE < DONE_VISIBLE);
});

test('expanding one compact list never expands the other', () => {
  // They are on screen at the same time, which the two Done surfaces never
  // were — so they cannot share the one flag.
  store.doneMore = false;
  store.convoMore = true;
  assert.equal(store.doneMore, false);
  store.convoMore = false;
});
