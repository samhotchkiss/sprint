// Narrow phone — an explicit fallback, not an optimisation.
//
// SPEC.md lists phone-portrait as a non-goal, so this screen does exactly two
// jobs and refuses the rest: answer a question, and sign off on a packet.
// Running work is a one-line-per-card list you can open but not act on, and
// blocked + queued collapse to a single read-only sentence. Everything that can
// be tapped is at least 44px.
import { h } from './util.js';
import { sections, meterSegments, motionState } from './state.js';
import { renderMeter } from './meter.js';
import { needsRow } from './list.js';

export function renderPhone(root, app) {
  const secs = sections();

  const head = h('div.col-head',
    h('span.col-dot', { style: { background: 'var(--accent)' } }),
    h('span.col-name', 'Needs you'),
    h('span.grow'),
    h('span.col-count', String(secs.needs_you.cards.length)));

  const meter = h('div.phone-meter');
  meter.appendChild(renderMeter(meterSegments(secs)));

  const list = h('div.phone-needs');
  if (!secs.needs_you.cards.length) {
    list.appendChild(h('p.section-empty', { style: { padding: '16px' } }, 'Nothing needs you.'));
  }
  for (const card of secs.needs_you.cards) list.appendChild(needsRow(card, app));

  const strip = h('div.phone-strip');
  strip.appendChild(h('span.phone-strip-label', `In progress · ${secs.in_motion.cards.length}`));
  for (const card of secs.in_motion.cards) {
    const st = motionState(card);
    strip.appendChild(h('button.phone-work', {
      type: 'button', onclick: () => app.openCard(card.num),
    },
      h('span.row-num', '#' + card.num),
      h('span.phone-work-title', card.title),
      h('span.prog-label', { style: { color: st.color } }, st.label)));
  }
  strip.appendChild(h('p.phone-readonly', readonlyLine(secs)));

  root.appendChild(h('div.fold-cols',
    h('section.col.col-needs_you', head, h('div.col-body', meter, list, strip))));
}

function readonlyLine(secs) {
  const b = secs.blocked.cards.length;
  const q = secs.waiting.cards.length;
  if (!b && !q) return 'Nothing blocked, nothing queued.';
  const bits = [];
  if (b) bits.push(`${b} blocked`);
  if (q) bits.push(`${q} queued`);
  return `${bits.join(' · ')} — read-only here.`;
}
