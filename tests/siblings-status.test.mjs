// Card #82: a board can be `alive: false` for two very different reasons --
// nothing answers at all (dropped before this ever runs, see
// `siblings_snapshot`'s admission rule in bin/sprintd), or something DOES
// answer, as this very project, but its counts could not be read (today:
// no token on disk -- which is also exactly what a board that refused to
// self-restart over a vanished data directory looks like from over here).
//
// `countsText` is the one place that second case becomes visible text. Before
// this fix it read identically to a normal, all-quiet, fully healthy board --
// "nothing waiting" -- which is precisely the "decoration that gets treated
// as blank/default" failure mode: a human staring at the switcher could not
// tell a board that needs attention from one that simply has no work.
//
// web/siblings.js is DOM-free at import time. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { countsText } from '../web/siblings.js';

test('a healthy quiet board reads as "nothing waiting"', () => {
  assert.equal(countsText({ needs_you: 0, ready: 0, chat_unread: 0,
                            in_motion: 0, status: 'live' }),
              'nothing waiting');
});

test('a healthz-reachable board with no token reads as "needs attention", not blank', () => {
  assert.equal(countsText({ needs_you: 0, ready: 0, chat_unread: 0,
                            in_motion: 0, status: 'no_token' }),
              'needs attention');
});

test('real counts still win over the degraded status', () => {
  // A board can only report status "no_token" with zero counts (they are
  // never fetched without a token) -- this just proves the ordering: counts
  // are checked first, so a future status added alongside real numbers can
  // never hide them.
  assert.equal(countsText({ needs_you: 2, ready: 0, chat_unread: 0,
                            in_motion: 0, status: 'no_token' }),
              '2 need you');
});

test('a genuinely dead/unreachable row (no status at all) still says "nothing waiting"', () => {
  // Dead rows are dropped by siblings_snapshot before they ever reach the
  // switcher, but the function itself must not invent a scary label out of
  // an absent status.
  assert.equal(countsText({ needs_you: 0, ready: 0, chat_unread: 0, in_motion: 0 }),
              'nothing waiting');
});
