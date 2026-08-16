// The one clause dead-session autoheal adds to the board (card #68).
//
// The banner already knew how to say "session offline — items will queue".
// That sentence is the wrong half of the truth once the machine has noticed
// and gone to do something about it, so the page has to be able to say the
// other half — and, when autoheal has run out of tries, the sentence that
// tells a person what to physically go and do.
//
// web/state.js owns the words; app.js only concatenates. That is what makes
// this testable without a browser. Run: node --test tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { store, applyBoard, normAutoheal, autohealNote } from '../web/state.js';

/** The smallest board payload applyBoard will accept, plus an autoheal block. */
function board(autoheal) {
  return {
    sprint: { id: 1, title: 'a sprint', opened_at: 0, closed_at: null, hold_mode: false },
    cards: [],
    sidebar: [],
    seq: 10,
    session: { status: 'offline', online: false, cursor: 1, head: 10 },
    autoheal,
  };
}

const DEAD = {
  registered: true,
  tmux_window: 'russ-machine',
  dead: true,
  reason: null,
  attempts: 1,
  max_attempts: 3,
  last_attempt_at: new Date().toISOString(),
  last_attempt_label: '12:03',
  gave_up: false,
  gave_up_text: null,
  delivered_but_silent: false,
};

test('a live session adds nothing to the banner', () => {
  applyBoard(board({ ...DEAD, dead: false, reason: 'alive', last_attempt_label: null }));
  assert.equal(autohealNote(), null);
});

test('a dead session nobody has tried to wake yet adds nothing either', () => {
  applyBoard(board({ ...DEAD, attempts: 0, last_attempt_at: null, last_attempt_label: null }));
  assert.equal(autohealNote(), null,
    'the original "items will queue" line is right until something has been tried');
});

test('a wake-up that was sent says when', () => {
  applyBoard(board(DEAD));
  assert.equal(autohealNote(), 'revival attempted 12:03');
});

test('giving up says the sentence the user can act on', () => {
  const text = 'autoheal gave up after 3 tries — the wake-up was delivered but the '
    + 'session never stirred; its terminal may be blocked by an open dialog. '
    + 'Check the window by hand.';
  applyBoard(board({ ...DEAD, attempts: 3, gave_up: true, gave_up_text: text,
    delivered_but_silent: true }));
  assert.equal(autohealNote(), text);
  assert.match(autohealNote(), /Check the window by hand/,
    'the next physical act is the point of the line');
});

test('the give-up line wins over the attempt line', () => {
  applyBoard(board({ ...DEAD, gave_up: true, gave_up_text: 'gave up' }));
  assert.equal(autohealNote(), 'gave up');
});

test('a server with no opinion leaves the banner alone', () => {
  applyBoard(board(null));
  assert.equal(store.autoheal, null);
  assert.equal(autohealNote(), null);
});

test('an older server that does not send the field at all changes nothing', () => {
  applyBoard(board(DEAD));
  const before = store.autoheal;
  const older = board(DEAD);
  delete older.autoheal;
  applyBoard(older);
  assert.deepEqual(store.autoheal, before,
    'absent means "no opinion", never "there is nothing wrong"');
});

test('normAutoheal keeps only what the banner needs, in its own shape', () => {
  const a = normAutoheal(DEAD);
  assert.equal(a.window, 'russ-machine');
  assert.equal(a.dead, true);
  assert.equal(a.attempts, 1);
  assert.equal(a.maxAttempts, 3);
  assert.equal(a.lastAttemptLabel, '12:03');
  assert.equal(a.gaveUp, false);
});

test('normAutoheal refuses junk rather than half-rendering it', () => {
  assert.equal(normAutoheal(null), null);
  assert.equal(normAutoheal('offline'), null);
  assert.equal(normAutoheal(42), null);
});
