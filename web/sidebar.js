// Chat with the session itself. Sprint-level events only — no worker telemetry.
import { h, clear, ageSuffix, richText } from './util.js';
import { store } from './state.js';

export function renderSidebar(threadEl, app) {
  const atBottom = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 80;
  clear(threadEl);

  const lines = store.sidebar.slice()
    .sort((a, b) => (a.seq == null ? Infinity : a.seq) - (b.seq == null ? Infinity : b.seq));
  if (!lines.length) {
    threadEl.appendChild(h('div.thread-empty',
      h('p', 'This is the session itself — same brain as the terminal.'),
      h('p.dim', 'Ask it anything: “why have #123, #127 and #128 been blocked for so long?”')));
  }
  for (const ev of lines) {
    const mine = ev.actor === 'user';
    threadEl.appendChild(h('div.smsg', { class: `smsg ${mine ? 'mine' : 'theirs'}${ev.local ? ' pending' : ''}${ev.failed ? ' failed' : ''}` },
      h('div.smsg-head',
        h('span.msg-actor', mine ? 'You' : 'Session'),
        h('span.grow'),
        h('span.msg-time', ev.local ? 'sending…' : ev.failed ? 'not sent' : ageSuffix(ev.ts))),
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
  if (pill) pill.title = online ? 'the session is draining events' : 'the session has not drained events for a while';

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
