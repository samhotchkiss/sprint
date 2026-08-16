// The phase chip — what an agent says it is DOING, on its own clock.
//
// User ruling, verbatim: "But these states need to be better so it doesn't look
// like everything is broken when it's not." A card face used to carry the last
// thing an agent said, which is stale by definition: three minutes after "fix
// applied, running tests" the face reads as a stalled job, and the amber that
// follows says so out loud. A phase is the opposite kind of statement — it is
// about right now, it carries its own clock, and it can say up front how long
// it expects to take.
//
// So the chip has exactly three faces:
//   testing · 2m                    — running, inside what it claimed
//   testing · 6m (expected 5m)      — amber: it said 5m and it is past that
//   coding · 7m                     — amber: no claim, and it has gone quiet
//
// This module is pure and dependency-light on purpose: state.js reads `phaseOf`
// for the recency bar and the amber rule, and all three surfaces (List rows,
// Board faces, the rail head) render the same `phaseChip`.
import { h, timeEl, ms, age } from './util.js';

/** How long a phase runs before it looks late with nothing else to go on. */
const NO_EXPECT_LATE_MS = 5 * 60 * 1000;

/**
 * The card's live phase, or null. The server projects it from the event log
 * (latest phase-carrying event since the card's last transition), so a card
 * that changed state has no phase — a card that moved on is not still testing.
 */
export function phaseOf(card, now = Date.now()) {
  if (!card || !card.phase) return null;
  const since = ms(card.phase_since);
  if (since == null) return null;
  const elapsed = Math.max(0, now - since);
  const expectMs = card.phase_expected_seconds ? card.phase_expected_seconds * 1000 : null;
  const overdue = expectMs ? elapsed > expectMs : elapsed > NO_EXPECT_LATE_MS;
  return {
    name: card.phase,
    since,
    elapsed,
    expectMs,
    overdue,
    // Left of the claim, the bar is time REMAINING on what the agent promised;
    // with no promise there is nothing to drain, so it stays full.
    left: expectMs ? Math.max(0, 1 - elapsed / expectMs) : 1,
    age: age(since, now),
    expectedLabel: expectMs ? durationLabel(expectMs) : null,
    text: chipText(card.phase, age(since, now), expectMs, overdue),
    title: chipTitle(card.phase, expectMs, overdue),
  };
}

/** "45s" / "5m" / "1h 30m" — how an expectation reads back to a human. */
export function durationLabel(msSpan) {
  const s = Math.round(msSpan / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60), rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

function chipText(name, ageText, expectMs, overdue) {
  const base = `${name} · ${ageText}`;
  return overdue && expectMs ? `${base} (expected ${durationLabel(expectMs)})` : base;
}

function chipTitle(name, expectMs, overdue) {
  if (expectMs && overdue) {
    return `${name} has run longer than the ${durationLabel(expectMs)} it expected — `
      + 'the session is checking on it';
  }
  if (expectMs) return `the agent expects ${name} to take about ${durationLabel(expectMs)}`;
  if (overdue) return `still ${name}, with no word for a while — the session is checking on it`;
  return `what the agent is doing right now: ${name}`;
}

/**
 * The chip itself. The clock inside it is a `.t[data-ts]` element, so it ticks
 * on the same 20-second timer as every other age on the page without anything
 * being re-rendered around it.
 */
export function phaseChip(card, { now = Date.now(), ph = null } = {}) {
  const p = ph || phaseOf(card, now);
  if (!p) return null;
  const chip = h('span.phase-chip', {
    class: p.overdue ? 'phase-chip is-late' : 'phase-chip',
    title: p.title,
  });
  chip.appendChild(h('span.phase-name', p.name));
  chip.appendChild(h('span.phase-sep', '·'));
  chip.appendChild(timeEl(p.since, { suffix: false }));
  if (p.overdue && p.expectMs) {
    chip.appendChild(h('span.phase-expected', `(expected ${durationLabel(p.expectMs)})`));
  }
  return chip;
}
