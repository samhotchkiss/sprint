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

export function renderSessionStatus(app) {
  const online = store.session.online;
  const label = document.getElementById('session-label');
  const pill = document.getElementById('session-pill');
  for (const dot of [document.getElementById('session-dot'), document.getElementById('sidebar-dot')]) {
    if (dot) dot.className = 'dot ' + (online ? 'on' : 'off');
  }
  if (label) label.textContent = online ? 'session live' : 'session offline';
  if (pill) {
    // Plain English, same vocabulary as the per-message status below.
    const cur = store.session.cursor;
    pill.title = online
      ? 'the session is keeping up — it is reading what you send' + (cur != null ? ` (read up to #${cur})` : '')
      : 'the session has not picked anything up lately — what you send waits in the queue';
  }

  const slot = document.getElementById('banner-slot');
  const banner = document.getElementById('banner');
  if (!slot || !banner) return;
  const offline = store.loaded && !online;
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
