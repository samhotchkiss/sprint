// Card #57, round 3: the switcher opens on the TOP row, and arrow keys move
// the highlight from there, wrapping at both ends.
//
// User's ruling, verbatim, when asked to confirm "open on current board,
// arrows move with wrap": **"Open on TOP row instead, arrows from there."**
// The open-on-top half is `openSiblingMenu` + the title button's click
// handler always focusing `.menu-item` #0 (real DOM focus, not a synthetic
// selection state — see keys.js's header comment on "the highlight IS
// focus"), so this file only covers `nextMenuIndex`, the pure wrap-around
// arithmetic that drives ArrowUp/ArrowDown once the menu is open. It is kept
// as a plain function specifically so it is testable without a DOM.
//
// web/siblings.js is DOM-free at import time. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { nextMenuIndex } from '../web/siblings.js';

test('opening the switcher lands on row 0 — arrow down from there moves to row 1', () => {
  // The menu opens by focusing row 0 directly (not through this function),
  // so "current" here is what a fresh open leaves behind: index 0.
  assert.equal(nextMenuIndex(0, 1, 4), 1);
});

test('arrow up from the top row wraps to the bottom row', () => {
  assert.equal(nextMenuIndex(0, -1, 4), 3);
});

test('arrow down from the bottom row wraps back to the top row', () => {
  assert.equal(nextMenuIndex(3, 1, 4), 0);
});

test('a full lap down returns to the row you started on', () => {
  let i = 0;
  for (let step = 0; step < 4; step++) i = nextMenuIndex(i, 1, 4);
  assert.equal(i, 0);
});

test('a full lap up returns to the row you started on', () => {
  let i = 2;
  for (let step = 0; step < 4; step++) i = nextMenuIndex(i, -1, 4);
  assert.equal(i, 2);
});

test('middle rows move by one, no wrap', () => {
  assert.equal(nextMenuIndex(1, 1, 4), 2);
  assert.equal(nextMenuIndex(2, -1, 4), 1);
});

test('a two-row switcher (the smallest one that can exist) still wraps both ways', () => {
  assert.equal(nextMenuIndex(0, -1, 2), 1);
  assert.equal(nextMenuIndex(1, 1, 2), 0);
});

test('an untracked focus (indexOf found nothing, -1) is treated as row 0', () => {
  // Belt and suspenders: the switcher always focuses a real row on open, so
  // `document.activeElement` should never fail to match a `.menu-item`. If it
  // ever does, this is the fallback keys.js relies on rather than crashing on
  // a negative array index.
  assert.equal(nextMenuIndex(-1, 1, 4), 1);
  assert.equal(nextMenuIndex(-1, -1, 4), 3);
});

test('a stale index past the current row count is also treated as row 0', () => {
  // A row died between render and keypress (a sibling board went offline);
  // the count the caller passes in is always the fresh one.
  assert.equal(nextMenuIndex(9, 1, 3), 1);
});

test('a single-row count still returns that row, not a crash', () => {
  assert.equal(nextMenuIndex(0, 1, 1), 0);
  assert.equal(nextMenuIndex(0, -1, 1), 0);
});

test('a zero count is a safe no-op index rather than a negative or NaN', () => {
  assert.equal(nextMenuIndex(0, 1, 0), 0);
});
