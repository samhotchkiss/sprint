// The review model: one entry per work unit, not one per card (card #55).
//
// User ruling, verbatim: "I just want the single card that lives in review,
// then when I open it up, it outlines everything that changed, and I can
// approve them together."
//
// web/units.js is deliberately DOM-free so this can test the part that has to
// be true — what Awaiting review contains, and what an outline is made of —
// without a browser. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  reviewUnits, unitOf, unitKey, unitTitle, packetFor, memberPart,
} from '../web/units.js';

/** A ready card as the board payload delivers it. */
function card(num, extra = {}) {
  return {
    num,
    title: extra.title || `Card #${num}`,
    state: 'ready',
    branch: null,
    batch_id: null,
    agent_name: null,
    bounce_count: 0,
    evidence: null,
    last_activity_at: 0,
    ...extra,
  };
}

/** One batch: N cards, one branch, one agent, ONE packet with per_card in it. */
function batch(nums, { id = 1, branch = 'sprint/batch-1', claim = 'Six design fixes on one branch.' } = {}) {
  const packet = {
    claim,
    branch,
    diffstat: '6 files changed',
    test_cmd: 'python3 tests/test_sprintd.py',
    test_result: '419 pass, 0 fail',
    validate: ['Open the board.', 'The review column has one card, not six.'],
    screenshots: ['/shots/branch.png'],
    ui_change: true,
    live_url: 'http://127.0.0.1:8455/',
    per_card: nums.map((n) => ({ card_num: n, claim: `fixed #${n}`, screenshots: [`/shots/${n}.png`] })),
  };
  return nums.map((n) => card(n, {
    title: `Change number ${n}`,
    batch_id: id,
    branch,
    agent_name: `sprint-batch-${id}`,
    evidence: packet,
  }));
}

function solo(num, claim) {
  return card(num, {
    title: `Solo change ${num}`,
    branch: `sprint/card-${num}`,
    agent_name: `sprint-card-${num}`,
    evidence: { claim, branch: `sprint/card-${num}`, validate: ['Look at it.'], screenshots: [] },
  });
}

test('Awaiting review holds one entry per work unit, not one per card', () => {
  const cards = [
    ...batch([10, 11, 12, 13, 14, 15], { id: 1, branch: 'sprint/batch-1' }),
    ...batch([20, 21], { id: 2, branch: 'sprint/batch-2', claim: 'Two fixes on one branch.' }),
    solo(30, 'The scrim is no longer black.'),
    solo(31, 'Restart-proof tabs.'),
  ];
  const units = reviewUnits(cards);
  assert.equal(units.length, 4, '10 cards shipped as 4 pieces of work → 4 entries');
  assert.deepEqual(units.map((u) => u.size), [6, 2, 1, 1]);
  assert.deepEqual(units.map((u) => u.kind), ['unit', 'unit', 'single', 'single']);
});

test('a unit sits where its oldest member sat', () => {
  const cards = [solo(5, 'first'), ...batch([6, 7]), solo(8, 'last')];
  assert.deepEqual(reviewUnits(cards).map((u) => u.lead.num), [5, 6, 8]);
});

test('a singleton is a work unit of one and keeps its own packet', () => {
  const [unit] = reviewUnits([solo(30, 'The scrim is no longer black.')]);
  assert.equal(unit.kind, 'single');
  assert.equal(unit.size, 1);
  assert.equal(unit.packet, null, 'a unit of one has no shared packet — it has ITS packet');
  assert.equal(packetFor(unit.lead).claim, 'The scrim is no longer black.');
});

test('cards on one branch without a batch are still one unit', () => {
  const cards = [
    card(40, { branch: 'sprint/card-40', agent_name: 'sprint-card-40', evidence: { claim: 'a' } }),
    card(41, { branch: 'sprint/card-40', agent_name: 'sprint-card-40', evidence: { claim: 'a' } }),
  ];
  const units = reviewUnits(cards);
  assert.equal(units.length, 1);
  assert.equal(units[0].size, 2);
  assert.equal(units[0].branch, 'sprint/card-40');
});

test('different branches are different units even under one agent', () => {
  const cards = [
    card(50, { branch: 'sprint/a', agent_name: 'sprint-batch-9' }),
    card(51, { branch: 'sprint/b', agent_name: 'sprint-batch-9' }),
  ];
  assert.equal(reviewUnits(cards).length, 2);
  assert.notEqual(unitKey(cards[0]), unitKey(cards[1]));
});

test('a unit is named after the work in it, never after the agent', () => {
  const members = batch([1, 2]).map((c, i) => ({ ...c, title: ['Restart-proof tabs', 'Reply routing'][i] }));
  const title = unitTitle(members);
  assert.match(title, /Restart-proof tabs/);
  assert.match(title, /Reply routing/);
  assert.doesNotMatch(title, /batch|agent|sprint-/i);
});

test('the outline gives every member its OWN claim and screenshots', () => {
  const cards = batch([10, 11, 12]);
  const unit = reviewUnits(cards)[0];
  assert.equal(unit.packet.claim, 'Six design fixes on one branch.');
  assert.deepEqual(unit.packet.shots, ['/shots/branch.png']);
  const parts = unit.cards.map((c) => memberPart(c, unit));
  assert.deepEqual(parts.map((p) => p.claim), ['fixed #10', 'fixed #11', 'fixed #12']);
  assert.deepEqual(parts.map((p) => p.shots), [['/shots/10.png'], ['/shots/11.png'], ['/shots/12.png']]);
  for (const p of parts) {
    assert.deepEqual(p.steps, [], "the branch's checks are printed once, not once per section");
  }
});

test('a member whose checks are its own keeps them in its section', () => {
  const cards = batch([10, 11]);
  cards[0].evidence = {
    ...cards[0].evidence,
    per_card: [
      { card_num: 10, claim: 'fixed #10', validate: ['Click the thing.'] },
      { card_num: 11, claim: 'fixed #11' },
    ],
  };
  cards[1].evidence = cards[0].evidence;
  const unit = reviewUnits(cards)[0];
  assert.deepEqual(memberPart(cards[0], unit).steps, ['Click the thing.']);
  assert.deepEqual(memberPart(cards[1], unit).steps, []);
});

test('the unit is a projection: bouncing one member leaves a smaller unit', () => {
  const cards = batch([10, 11, 12]);
  const stillUnderReview = cards.filter((c) => c.num !== 11);   // #11 went back
  const unit = unitOf(stillUnderReview, 10);
  assert.equal(unit.size, 2);
  assert.deepEqual(unit.nums, [10, 12]);
  assert.equal(unitOf(stillUnderReview, 11), null, 'a bounced member is not in a review unit at all');
});

test('a unit with no shared claim has no lead packet to approve from', () => {
  const cards = [
    card(60, { branch: 'sprint/x', evidence: { claim: 'one thing' } }),
    card(61, { branch: 'sprint/x', evidence: { claim: 'a different thing' } }),
  ];
  const unit = reviewUnits(cards)[0];
  assert.equal(unit.size, 2);
  assert.equal(unit.packet, null, 'no single sentence covers it — the verdict lives in the outline');
});
