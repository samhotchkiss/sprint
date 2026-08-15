// Chat with the session itself. Sprint-level events only — no worker telemetry.
import { h, clear, ageSuffix, richText } from './util.js';
import { store, messageStatus } from './state.js';

export function renderSidebar(threadEl, app) {
  const atBottom = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 80;
  clear(threadEl);

  // A line still in flight has no seq — it sorts by its local placeholder so it
  // sits at the bottom where you just typed it, without claiming to have landed.
  const orderOf = (e) => (e.seq != null ? e.seq : (e.sortSeq != null ? e.sortSeq : Infinity));
  const lines = store.sidebar.slice().sort((a, b) => orderOf(a) - orderOf(b));
  if (!lines.length) {
    threadEl.appendChild(h('div.thread-empty',
      h('p', 'This is the session itself — same brain as the terminal.'),
      h('p.dim', 'Ask it anything: “why have #123, #127 and #128 been blocked for so long?”')));
  }
  for (const ev of lines) {
    const mine = ev.actor === 'user';
    // Only your own messages carry a delivery status — the session's own lines
    // are already here, there is nothing to report about them.
    const st = mine ? messageStatus(ev) : null;
    threadEl.appendChild(h('div.smsg', { class: `smsg ${mine ? 'mine' : 'theirs'}${ev.local ? ' pending' : ''}${ev.failed ? ' failed' : ''}` },
      h('div.smsg-head',
        h('span.msg-actor', mine ? 'You' : 'Session'),
        h('span.grow'),
        st ? h('span.msg-status', { class: `msg-status is-${st.key}`, title: st.title }, st.label) : null,
        // in-flight/failed lines have no meaningful age yet — the status says it all
        h('span.msg-time', st && (st.key === 'sending' || st.key === 'failed') ? '' : ageSuffix(ev.ts))),
      h('div.smsg-text', richText(ev.payload && ev.payload.text, app.openCard))));
  }
  if (atBottom) requestAnimationFrame(() => { threadEl.scrollTop = threadEl.scrollHeight; });
}

// Three states, three dots, one banner. `busy` is the honest middle: the session
// is attached and polling, it just has not drained everything yet. That is not
// worth a banner — it lives on the dot, where it can be ignored.
const DOT_CLASS = { online: 'on', busy: 'busy', offline: 'off' };
const DOT_LABEL = { online: 'session live', busy: 'session catching up', offline: 'session offline' };

function pillTitle(status, cursor) {
  const read = cursor != null ? ` (read up to #${cursor})` : '';
  if (status === 'busy') return `session is on it — catching up${read}`;
  if (status === 'offline') return 'the session has not picked anything up lately — what you send waits in the queue';
  return `the session is keeping up — it is reading what you send${read}`;
}

export function renderSessionStatus(app) {
  const status = store.session.status || (store.session.online ? 'online' : 'offline');
  const label = document.getElementById('session-label');
  const pill = document.getElementById('session-pill');
  for (const dot of [document.getElementById('session-dot'), document.getElementById('sidebar-dot')]) {
    if (dot) dot.className = 'dot ' + (DOT_CLASS[status] || 'on');
  }
  if (label) label.textContent = DOT_LABEL[status] || DOT_LABEL.online;
  if (pill) pill.title = pillTitle(status, store.session.cursor);

  const slot = document.getElementById('banner-slot');
  const banner = document.getElementById('banner');
  if (!slot || !banner) return;
  // Banner ONLY on a real offline. A busy session used to raise this banner and
  // it read as "the backend restarted" — it never had.
  const offline = store.loaded && status === 'offline';
  const transportDown = app.transport === 'error';
  if (offline) {
    slot.hidden = false;
    clear(banner);
    banner.className = 'banner warn';
    banner.appendChild(h('span.banner-text', 'session offline — items will queue'));
  } else if (transportDown) {
    slot.hidden = false;
    clear(banner);
    banner.className = 'banner dim';
    banner.appendChild(h('span.banner-text', 'lost the board connection — retrying'));
  } else {
    slot.hidden = true;
  }
}
