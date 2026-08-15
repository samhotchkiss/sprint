// The thread: what a card's history looks like when it is read rather than
// audited. Oldest at the top, newest at the bottom, and exactly four shapes.
//
//   message        actor + relative time above a bubble. Yours right-aligned.
//                  A substantive line carries a "More context" disclosure that
//                  opens inline — only when there is really more to show.
//   screenshot     tiles, captioned, opening a lightbox.
//   question       a full-width panel of 46px option rows. Answering replaces
//                  the panel with "Delivered — the agent sees it next turn".
//   status change  centred dot + small-caps label + time. Never a bubble:
//                  these are events, not speech.
//
// The evidence packet is a fifth thing that renders AS a message rather than as
// a fixed panel — it arrived at a moment in the conversation and it reads in
// that place, with the claim first and "Check it yourself" as its visual centre.
import { h, ageSuffix, richText, firstLine } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import { SYSTEM_KINDS, eventText, messageStatus, STATE_LABEL, draft } from './state.js';
import { detailBlock } from './detail.js';

const ACTOR = {
  user: { label: 'You', cls: 'from-you' },
  session: { label: 'Session', cls: 'from-session' },
  worker: { label: 'Agent', cls: 'from-agent' },
  server: { label: 'Board', cls: 'from-agent' },
};

/**
 * Build the whole stream for a card: its timeline, then the open question, then
 * the evidence packet. Both of the latter are the live thing the card is waiting
 * on, so they sit at the bottom where your eye already is.
 */
export function renderThread(root, detail, app) {
  const card = detail.card;
  const state = detail.state;

  root.appendChild(h('p.thread-opened', `Card #${detail.num} opened`));

  const items = (detail.timeline || []).slice();
  const known = new Set(items.map((e) => e.seq).filter((s) => s != null));
  for (const p of detail.pendingLines || []) {
    if (p.seq != null && known.has(p.seq)) continue;
    items.push(p);
  }

  // The user's own words, verbatim, are the first thing in the thread — the face
  // carries a condensed title, and this is where the untouched submission lives.
  if (card && card.body && !items.some((e) => e.kind === 'submitted')) {
    root.appendChild(message({
      actor: 'user', ts: card.created_at, payload: { text: card.body },
    }, app));
  }

  for (const ev of items) {
    if (SYSTEM_KINDS.has(ev.kind) && ev.kind !== 'submitted' && ev.kind !== 'evidence') {
      root.appendChild(statusChange(ev, app));
      continue;
    }
    if (ev.kind === 'evidence') continue;      // the packet itself renders below
    root.appendChild(message(ev, app));
    const atts = ev.payload && (ev.payload.attachments || ev.payload.images);
    if (Array.isArray(atts) && atts.length) {
      root.appendChild(shotRow(atts, app, ev.actor === 'user'));
    }
  }

  if (Array.isArray(card && card.attachments) && card.attachments.length) {
    root.appendChild(shotRow(card.attachments, app, true, 'you attached this'));
  }

  if (state === 'needs_you' && card && card.question) {
    root.appendChild(questionPanel(card, card.question, app));
  } else if (detail.justAnswered) {
    root.appendChild(statusLine('Delivered — the agent sees it next turn', 'now', 'good'));
  }

  const packet = detail.evidence || (card && card.evidence);
  if (packet && (state === 'ready' || state === 'integrating')) {
    if (state === 'integrating') {
      root.appendChild(statusLine('Approved — merging', ageSuffix(card.state_since), 'good'));
    }
    root.appendChild(evidencePacket(packet, card, state, app));
  }
}

/** The session chat: same bubbles, no card machinery. */
export function renderChat(root, lines, app) {
  if (!lines.length) {
    root.appendChild(h('div.thread-empty',
      h('p', 'This is the session itself — same brain as the terminal.'),
      h('p', 'Ask it anything: “why have #123, #127 and #128 been blocked for so long?”')));
    return;
  }
  for (const ev of lines) root.appendChild(message(ev, app));
}

// ---- item types ----------------------------------------------------------

function message(ev, app) {
  const who = ACTOR[ev.actor] || ACTOR.worker;
  const mine = ev.actor === 'user';
  const st = mine ? messageStatus(ev) : null;

  const item = h('div.item', {
    class: `item ${who.cls}${mine ? ' mine' : ''}${ev.pending || ev.local ? ' is-pending' : ''}${ev.failed ? ' is-failed' : ''}`,
  });
  item.appendChild(h('div.msg-head',
    h('span.msg-who', who.label),
    st ? h('span.msg-status', { class: `msg-status is-${st.key}`, title: st.title }, st.label) : null,
    st && (st.key === 'sending' || st.key === 'failed') ? null : h('span.msg-when', ageSuffix(ev.ts))));

  const bubble = h('div.bubble', h('p', richText(eventText(ev), app.openCard)));
  // Only a line with real detail gets an affordance — a chevron over nothing is
  // a promise the history cannot keep.
  const more = detailBlock(ev, app);
  if (more) bubble.appendChild(more);
  item.appendChild(bubble);
  return item;
}

function statusChange(ev, app) {
  const tone = ev.kind === 'error' || ev.kind === 'agent_silent' ? 'bad'
    : (ev.kind === 'state' && (ev.payload.to === 'ready' || ev.payload.to === 'completed')) ? 'good'
      : ev.kind === 'verdict' && ev.payload.verdict === 'approve' ? 'good' : '';
  const label = statusLabel(ev);
  const item = statusLine(label, ageSuffix(ev.ts), tone);
  // an error's stack, a silence note's findings — tucked under, closed
  const more = detailBlock(ev, app, { small: true });
  if (more) item.appendChild(more);
  return item;
}

function statusLabel(ev) {
  if (ev.kind === 'state') {
    const to = ev.payload.to;
    const label = STATE_LABEL[to] || to || 'updated';
    return ev.payload.reason ? `${label} — ${firstLine(ev.payload.reason, 80)}` : label;
  }
  return firstLine(eventText(ev), 90);
}

export function statusLine(label, when, tone) {
  return h('div.item.centered',
    h('div.status-line', { class: `status-line${tone ? ' is-' + tone : ''}` },
      h('span.status-dot'),
      h('span.status-label', label),
      when ? h('span.status-when', when) : null));
}

function shotRow(refs, app, mine, fallbackCaption) {
  const urls = refs.map(attachmentUrl).filter(Boolean);
  const row = h('div.item', { class: `item${mine ? ' mine' : ''}` });
  const strip = h('div.shots');
  refs.forEach((ref, i) => {
    const url = attachmentUrl(ref);
    const cap = attachmentCaption(ref) || fallbackCaption || '';
    const frame = h('div.shot-frame');
    if (url) {
      frame.appendChild(h('img', {
        src: url, alt: cap || 'screenshot', loading: 'lazy',
        onerror: (e) => { e.target.remove(); frame.appendChild(h('span.shot-slot', 'image unavailable')); },
      }));
    } else {
      frame.appendChild(h('span.shot-slot', typeof ref === 'string' ? firstLine(ref, 28) : 'screenshot'));
    }
    strip.appendChild(h('button.shot', {
      type: 'button',
      title: cap || 'screenshot',
      onclick: () => { if (url) app.lightbox(urls, urls.indexOf(url), refs.map((r) => attachmentCaption(r) || fallbackCaption || '')); },
    }, frame, cap ? h('span.shot-cap', cap) : null));
  });
  row.appendChild(strip);
  return row;
}

function questionPanel(card, q, app) {
  const item = h('div.item');
  const panel = h('div.ask');
  panel.appendChild(h('p.ask-text', q.text || 'The agent is waiting on you.'));
  if (q.options && q.options.length) {
    const opts = h('div.ask-options');
    for (const opt of q.options) {
      opts.appendChild(h('button.ask-opt', {
        type: 'button',
        onclick: () => app.answer(card, q, opt.value),
      },
        h('span.ask-opt-label', opt.label),
        h('span.grow'),
        opt.hint ? h('span.ask-opt-hint', opt.hint) : null));
    }
    panel.appendChild(opts);
  } else {
    panel.appendChild(h('p.ask-free', 'Free text — type your answer below and it goes straight to the agent.'));
  }
  item.appendChild(panel);
  return item;
}

// ---- the evidence packet -------------------------------------------------

function evidencePacket(packet, card, state, app) {
  const p = packet || {};
  const item = h('div.item');
  const box = h('div.packet');

  box.appendChild(h('div',
    h('p.packet-label.good', 'The claim'),
    h('p.packet-claim', p.claim || 'No claim recorded — ask the agent what it thinks it did.')));

  const steps = Array.isArray(p.validate) ? p.validate.filter(Boolean)
    : (typeof p.validate === 'string' && p.validate ? [p.validate] : []);
  if (steps.length) {
    const list = h('div.steps');
    steps.forEach((s, i) => {
      const text = typeof s === 'string' ? s : (s.text || s.step || '');
      list.appendChild(h('div.step', h('span.step-n', String(i + 1)), h('p', text)));
    });
    box.appendChild(h('div', h('p.packet-label.accent', 'Check it yourself'), list));
  }

  const shots = Array.isArray(p.screenshots) ? p.screenshots : [];
  if (shots.length) box.appendChild(packetShots(shots, app));

  if (p.live_url) {
    box.appendChild(h('a.btn.packet-live', {
      href: p.live_url, target: '_blank', rel: 'noreferrer noopener', title: p.live_url,
    }, 'See it live ↗'));
  }

  const meta = [p.branch, p.diffstat, p.test_cmd && p.test_result ? `${p.test_cmd} → ${p.test_result}` : p.test_result]
    .filter(Boolean).join(' · ');
  if (meta) box.appendChild(h('p.packet-meta', meta));

  if (Array.isArray(p.per_card) && p.per_card.length) {
    const per = h('div');
    per.appendChild(h('p.packet-label.accent', `Batch — ${p.per_card.length} cards, one branch`));
    const list = h('div.steps');
    for (const entry of p.per_card) {
      list.appendChild(h('div.step',
        h('span.step-n', String(entry.card_num)),
        h('p', entry.claim || '')));
    }
    per.appendChild(list);
    box.appendChild(per);
  }

  if (state === 'ready') box.appendChild(verdictBar(card, app));
  item.appendChild(box);
  return item;
}

function packetShots(shots, app) {
  const urls = shots.map(attachmentUrl).filter(Boolean);
  const strip = h('div.shots', { style: { maxWidth: '100%' } });
  shots.forEach((ref) => {
    const url = attachmentUrl(ref);
    const cap = attachmentCaption(ref);
    const frame = h('div.shot-frame');
    if (url) {
      frame.appendChild(h('img', {
        src: url, alt: cap || 'screenshot', loading: 'lazy',
        onerror: (e) => { e.target.remove(); frame.appendChild(h('span.shot-slot', 'image unavailable')); },
      }));
    } else {
      frame.appendChild(h('span.shot-slot', typeof ref === 'string' ? firstLine(ref.split('/').pop(), 26) : 'screenshot'));
    }
    strip.appendChild(h('button.shot', {
      type: 'button', title: cap || 'screenshot',
      onclick: () => { if (url) app.lightbox(urls, urls.indexOf(url), shots.map(attachmentCaption)); },
    }, frame, cap ? h('span.shot-cap', cap) : null));
  });
  return strip;
}

function verdictBar(card, app) {
  const key = `bounce:${card.num}`;
  const wrap = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '9px' } });

  // Two bounces and the server tags `escalate`: stop offering a blind third try.
  if (card.bounce_count >= 2) {
    wrap.appendChild(h('p.escalate',
      'Bounced twice. The session stops retrying blind here and brings it to you to co-design.'));
  }

  const notes = h('textarea.bounce-notes', {
    rows: '2',
    placeholder: 'What has to change? (goes straight to the agent)',
    oninput: (e) => draft(key, e.target.value),
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendBounce(); }
    },
  });
  notes.value = draft(key);
  const notesWrap = h('div', { hidden: !draft(key) }, notes);

  function sendBounce() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    app.verdict(card, 'bounce', text);
  }

  const bounceBtn = h('button.btn.bounce', {
    type: 'button',
    onclick: () => {
      if (notesWrap.hidden) {
        notesWrap.hidden = false;
        bounceBtn.textContent = 'Send bounce';
        notes.focus();
        return;
      }
      sendBounce();
    },
  }, notesWrap.hidden ? 'Bounce' : 'Send bounce');

  wrap.appendChild(notesWrap);
  wrap.appendChild(h('div.verdicts',
    h('button.btn.approve', { type: 'button', onclick: () => app.verdict(card, 'approve') }, 'Approve'),
    bounceBtn,
    h('button.btn.reject', {
      type: 'button',
      title: 'this should not have been built — the branch is dropped',
      onclick: () => app.verdict(card, 'reject', draft(key) || undefined),
    }, 'Reject')));
  return wrap;
}
