// The expander: every event line is a one-liner you can skim; an event that
// carries `payload.detail` gets a quiet "more" underneath that opens the long
// version in place. Collapsed by default — history stays skimmable, the digging
// is opt-in, and nothing above the line moves when you open it.
import { h, autolink } from './util.js';
import { detailOpen } from './state.js';

/** The expanded text of an event, or '' when there isn't one. */
export function detailText(ev) {
  const d = ev && ev.payload ? ev.payload.detail : null;
  return typeof d === 'string' && d.trim() ? d.replace(/\s+$/, '') : '';
}

export function hasDetail(ev) { return detailText(ev) !== ''; }

/**
 * The toggle + collapsed body for one event. Returns null when the event has
 * no detail, so callers can append it unconditionally.
 *   detailBlock(ev, app, {small: true})
 */
export function detailBlock(ev, app, { small = false } = {}) {
  const text = detailText(ev);
  if (!text) return null;

  const key = detailKey(ev);
  let open = detailOpen(key);

  const body = h('div.detail-body', { hidden: !open });
  body.appendChild(preText(text, app && app.openCard));

  const label = h('span.detail-label', open ? 'Less' : 'More context');
  const toggle = h('button.detail-toggle', {
    type: 'button',
    'aria-expanded': open ? 'true' : 'false',
    title: open ? 'hide the long version' : lineHint(text),
    onclick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      open = !open;
      detailOpen(key, open);
      body.hidden = !open;
      label.textContent = open ? 'Less' : 'More context';
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.title = open ? 'hide the long version' : lineHint(text);
      toggle.classList.toggle('is-open', open);
    },
  }, label);
  if (open) toggle.classList.add('is-open');

  return h('div.detail', { class: small ? 'detail small' : 'detail' }, toggle, body);
}

/** Stable across re-renders: the seq is the event's identity in the log. */
function detailKey(ev) {
  if (!ev) return 'x';
  if (ev.seq != null) return 's' + ev.seq;
  if (ev.localId) return 'l' + ev.localId;
  return 'c' + (ev.card_num == null ? '-' : ev.card_num) + ':' + (ev.ts || '');
}

function lineHint(text) {
  const lines = text.split('\n').length;
  return lines > 1 ? `show the long version — ${lines} lines` : 'show the long version';
}

/** Preformatted, so a log keeps its shape — plus #N autolinks. */
function preText(text, onCard) {
  const frag = document.createDocumentFragment();
  text.split('\n').forEach((line, i) => {
    if (i) frag.appendChild(document.createTextNode('\n'));
    frag.appendChild(autolink(line, onCard));
  });
  return frag;
}
