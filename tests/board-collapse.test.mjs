// Card #73: an empty lane on the Board gives its width to the lanes that are
// holding work, and turns its header on its side so it still says what it is.
//
// User, verbatim: "When a lane is empt let's move its header to be vertical so
// that we have more room for the other lanes to get wider."
//
// The rule is the whole card, and it is three sentences long: an empty lane
// collapses, a board where EVERYTHING is empty does not (there is nobody to give
// the width to), and the lane the keyboard is standing in stays open however
// empty it is. What "empty" means is the other half — a Needs-you column holding
// only live threads is not empty, because a thread is something on screen.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { collapsePlan } from '../web/board.js';

const lane = (key, empty) => ({ key, empty });
const FOUR = ['waiting', 'in_motion', 'needs_you', 'review'];
const board = (...emptyKeys) => FOUR.map((k) => lane(k, emptyKeys.includes(k)));

test('an empty lane collapses and a busy one does not', () => {
  const plan = collapsePlan(board('waiting', 'review'), null);
  assert.deepEqual(plan, [true, false, false, true]);
});

test('a board with nothing in it keeps all four lanes', () => {
  // Four slivers is a worse way to say "this sprint is done" than four columns
  // saying it in words, and there is no width to win.
  assert.deepEqual(collapsePlan(board(...FOUR), null), [false, false, false, false]);
});

test('one card anywhere is enough to start collapsing the rest', () => {
  const plan = collapsePlan(board('waiting', 'in_motion', 'review'), null);
  assert.deepEqual(plan, [true, true, false, true]);
});

test('the lane the keyboard is standing in stays open', () => {
  // Pressing 1 is an explicit ask for Waiting. It opens, with its own words in
  // it, and folds back when the cursor leaves.
  const plan = collapsePlan(board('waiting', 'review'), 'waiting');
  assert.deepEqual(plan, [false, false, false, true]);
});

test('a cursor in a busy lane collapses nothing extra', () => {
  const plan = collapsePlan(board('review'), 'in_motion');
  assert.deepEqual(plan, [false, false, false, true]);
});

test('a lane holding only conversations is not empty', () => {
  // boardColumns() deliberately keeps live threads out of every bucket (card
  // #48), so Needs you can have a count of 0 and still be drawing rows. The
  // caller decides emptiness the same way it decides whether to print "Nothing
  // needs you." — count AND threads.
  const withThreads = FOUR.map((k) => lane(k, k !== 'in_motion' && k !== 'needs_you'));
  assert.deepEqual(collapsePlan(withThreads, null), [true, false, false, true]);
});

test('the Fold plans three lanes, not four', () => {
  const fold = [lane('in_motion', false), lane('needs_you', true), lane('review', true)];
  assert.deepEqual(collapsePlan(fold, null), [false, true, true]);
});
