// The proportional meter: the whole sprint's shape in 4px of height.
//
// Each segment's flex-grow IS its count, so the bar is the distribution and
// nothing has to be read as a number. The legend underneath carries the counts
// at 12.5px — deliberately the smallest thing on the page that anyone needs.
import { h } from './util.js';

export function renderMeter(segments) {
  // One node, not a fragment: keyed reconcile needs a place to stamp
  // data-k, and a no-op refresh has to be able to keep this exact element.
  const wrap = h('div.meter-block');
  if (!segments.length) return wrap;

  const bar = h('div.meter', { role: 'img', 'aria-label': segments.map((s) => s.label).join(', ') });
  for (const seg of segments) {
    bar.appendChild(h('div.meter-seg', {
      title: seg.label,
      style: { flex: String(seg.count), background: seg.color },
    }));
  }
  wrap.appendChild(bar);

  const legend = h('div.legend');
  for (const seg of segments) {
    legend.appendChild(h('div.legend-item',
      h('span.legend-dot', { style: { background: seg.color } }),
      h('span.legend-label', seg.label)));
  }
  wrap.appendChild(legend);
  return wrap;
}
