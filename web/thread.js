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
//
// Nothing in here is rebuilt on a whim. Every item carries a key and a version
// (see `reconcile`), so a frame that changed nothing changes no DOM — which is
// what keeps the rail still while the session's cursor ticks past underneath.
import { h, ageSuffix, richText, firstLine, reconcile, timeEl } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import {
  SYSTEM_KINDS, eventText, messageStatus, STATE_LABEL, draft, bounceComposing,
} from './state.js';
import { detailBlock } from './detail.js';
import { flowActive } from './review.js';
import { splitAttachments, docsVer, reportRow } from './reports.js';

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
  reconcile(root, threadItems(detail, app));
}

/** The whole stream as keyed descriptors — see `reconcile` in util.js. */
export function threadItems(detail, app) {
  const card = detail.card;
  const state = detail.state;
  const out = [];

  out.push({ key: 'opened', ver: detail.num,
    make: () => h('p.thread-opened', `Card #${detail.num} opened`) });

  const items = (detail.timeline || []).slice();
  const known = new Set(items.map((e) => e.seq).filter((s) => s != null));
  for (const p of detail.pendingLines || []) {
    if (p.seq != null && known.has(p.seq)) continue;
    items.push(p);
  }

  // The user's own words, verbatim, are the first thing in the thread — the face
  // carries a condensed title, and this is where the untouched submission lives.
  // On a fresh load the card arrives before its timeline does, so this stands in
  // for the `submitted` event for a beat. It carries the SAME key and version as
  // the real one, so when the timeline lands the node is reused and rebound
  // rather than swapped — a bubble that repaints itself with identical words is
  // exactly the flicker we are here to remove.
  if (card && card.body && !items.some((e) => e.kind === 'submitted')) {
    const ev = { actor: 'user', ts: card.created_at, kind: 'submitted',
      payload: { text: card.body } };
    out.push({ key: 'submitted', ver: bodyVer(card.body), data: ev,
      make: () => message(ev, app) });
  }

  // The question the card is actually waiting on renders as a panel at the
  // bottom, so its event would otherwise say the same words twice in a row.
  const liveQuestion = (state === 'needs_you' && card && card.question)
    ? lastIndexOfKind(items, 'question') : -1;

  const shown = new Set();
  items.forEach((ev, i) => {
    if (i === liveQuestion) return;
    const key = ev.kind === 'submitted' ? 'submitted' : eventKey(ev);
    if (SYSTEM_KINDS.has(ev.kind) && ev.kind !== 'submitted' && ev.kind !== 'evidence') {
      out.push({ key, ver: 1, data: ev, make: () => statusChange(ev, app) });
      return;
    }
    if (ev.kind === 'evidence') return;        // the packet itself renders below
    out.push({
      key,
      ver: ev.kind === 'submitted' ? bodyVer(eventText(ev)) : 1,
      data: ev,
      make: () => message(ev, app),
    });
    const atts = ev.payload && (ev.payload.attachments || ev.payload.images);
    if (Array.isArray(atts) && atts.length) {
      for (const a of atts) shown.add(attachmentUrl(a));
      pushAttachments(out, key, atts, app, ev.actor === 'user');
    }
  });

  // Anything attached to the card that no line in the thread already showed —
  // never a second copy of a screenshot you can already see above.
  const loose = (Array.isArray(card && card.attachments) ? card.attachments : [])
    .filter((a) => !shown.has(attachmentUrl(a)));
  if (loose.length) {
    pushAttachments(out, 'card-atts', loose, app, true, 'you attached this');
  }

  if (state === 'needs_you' && card && card.question) {
    out.push({ key: 'question', ver: card.question.id || card.question.text || 1,
      make: () => questionPanel(card, card.question, app) });
  } else if (detail.justAnswered) {
    out.push({ key: 'answered', ver: 1,
      make: () => statusLine('Delivered — the agent sees it next turn', 'now', 'good') });
  }

  const packet = detail.evidence || (card && card.evidence);
  if (packet && (state === 'ready' || state === 'integrating')) {
    if (state === 'integrating') {
      out.push({ key: 'merging', ver: card.state_since || 1,
        make: () => statusLine('Approved — merging', ageSuffix(card.state_since), 'good') });
    }
    // While the Review-next walkthrough is standing on this card, the verdict
    // lives in its pinned bar instead — one Approve on screen, in one place.
    const walking = !!card && flowActive(card.num);
    out.push({
      key: 'packet',
      // ...and whether you are mid-bounce, which is what swaps the three
      // verdict buttons for "Submit bounce / Cancel". A version that ignored it
      // would leave Approve on screen after you pressed Bounce.
      ver: `${state}:${card ? card.bounce_count : 0}:${(packet && packet.claim) || ''}`.length
        + ':' + state + ':' + (card ? card.bounce_count : 0) + ':' + (walking ? 'w' : '')
        + ':' + (card && bounceComposing(card.num) ? 'b' : ''),
      make: () => evidencePacket(packet, card, state, app, walking),
    });
  }
  return out;
}

/** Same number for the card's body and for the submitted event that carries it. */
function bodyVer(text) {
  return String(text == null ? '' : text).trim().length;
}

function refsVer(refs) {
  return refs.length + ':' + refs.map((r) => String(attachmentUrl(r) || '').length).join('.');
}

/** An event's identity in the log — stable across every re-render. */
function eventKey(ev) {
  if (ev.seq != null) return 's' + ev.seq;
  if (ev.localId) return 'l' + ev.localId;
  return 'x' + (ev.kind || '') + ':' + (ev.ts || '');
}

function lastIndexOfKind(items, kind) {
  for (let i = items.length - 1; i >= 0; i -= 1) if (items[i].kind === kind) return i;
  return -1;
}

/** The session chat: same bubbles, no card machinery. */
export function renderChat(root, lines, app) {
  reconcile(root, chatItems(lines, app));
}

export function chatItems(lines, app) {
  if (!lines.length) {
    return [{ key: 'empty', ver: 1, make: () => h('div.thread-empty',
      h('p', 'This is the session itself — same brain as the terminal.'),
      h('p', 'Ask it anything: “why have #123, #127 and #128 been blocked for so long?”')) }];
  }
  const out = [];
  lines.forEach((ev, i) => {
    const key = ev.seq != null ? 's' + ev.seq : 'echo' + i;
    out.push({ key, ver: 1, data: ev, make: () => message(ev, app) });
    const atts = ev.payload && (ev.payload.attachments || ev.payload.images);
    if (Array.isArray(atts) && atts.length) {
      pushAttachments(out, key, atts, app, ev.actor === 'user');
    }
  });
  return out;
}

/**
 * One event's attachments, as thread items. Pictures and documents arrive in
 * the SAME list (a report is an attachment, deliberately) and are told apart by
 * the server-set `doc` field, then rendered as the two different things they
 * are: a strip of thumbnails, and a skim line that expands into a document.
 */
function pushAttachments(out, key, atts, app, mine, fallbackCaption) {
  const [shots, docs] = splitAttachments(atts);
  if (shots.length) {
    // the version tracks the refs themselves (by length, not by value: a
    // pasted data: URL is enormous) so an optimistic local thumbnail is
    // swapped for the stored attachment exactly once
    out.push({ key: key + ':shots', ver: refsVer(shots),
      make: () => shotRow(shots, app, mine, fallbackCaption) });
  }
  if (docs.length) {
    out.push({ key: key + ':docs', ver: docsVer(docs),
      make: () => reportRow(docs, app, mine) });
  }
}

// ---- item types ----------------------------------------------------------

function message(first, app) {
  let ev = first;                    // rebound by _sync when a fresher copy lands
  const who = ACTOR[ev.actor] || ACTOR.worker;
  const mine = ev.actor === 'user';

  const item = h('div.item');
  // A button, always: a failed send has to be one tap from going again, and a
  // pill that is sometimes a button and sometimes a span is two nodes to keep
  // in sync. It only takes clicks when there is something to retry.
  const status = h('button.msg-status', {
    type: 'button',
    onclick: () => { if (ev && typeof ev.retry === 'function') ev.retry(); },
  });
  const when = h('span.msg-when');
  item.appendChild(h('div.msg-head', h('span.msg-who', who.label), status, when));

  // A message that is only pictures gets no bubble: the tiles underneath ARE
  // the message, and "sent a screenshot" over a screenshot is a caption nobody
  // asked for. The line still exists in the log — it is what the card face and
  // the session's relay read.
  const more = detailBlock(ev, app);
  if (!(ev.payload && ev.payload.images_only) || more) {
    const bubble = h('div.bubble');
    if (!(ev.payload && ev.payload.images_only)) {
      bubble.appendChild(h('p', richText(eventText(ev), app.openCard)));
    }
    // Only a line with real detail gets an affordance — a chevron over nothing
    // is a promise the history cannot keep.
    if (more) bubble.appendChild(more);
    item.appendChild(bubble);
  }

  // Delivery state moves under the message ("sending…" → "landed" → "session is
  // on it") while the message itself never changes. That transition is the most
  // frequent thing on the wire, so it is patched in place: the bubble, and any
  // image in it, is never re-created for it.
  item._sync = (next) => {
    if (next && next !== ev) ev = next;
    const st = mine ? messageStatus(ev) : null;
    item.className = `item ${who.cls}${mine ? ' mine' : ''}`
      + `${ev.pending || ev.local ? ' is-pending' : ''}${ev.failed ? ' is-failed' : ''}`;
    status.hidden = !st;
    if (st) {
      status.className = `msg-status is-${st.key}${st.retry ? ' is-retry' : ''}`;
      status.title = st.title;
      status.disabled = !st.retry;
      if (status.textContent !== st.label) status.textContent = st.label;
    }
    const hideWhen = st && (st.key === 'sending' || st.key === 'failed');
    when.hidden = !!hideWhen;
    const label = ageSuffix(ev.ts);
    if (!hideWhen && when.textContent !== label) when.textContent = label;
  };
  item._sync();
  return item;
}

function statusChange(ev, app) {
  // `stuck` is a nudge, not a fault: nothing broke, something is just owed.
  // Amber, not red — the same colour the card's age text goes.
  const tone = ev.kind === 'stuck' ? 'warn'
    : ev.kind === 'error' || ev.kind === 'agent_silent' ? 'bad'
      : (ev.kind === 'state' && (ev.payload.to === 'ready' || ev.payload.to === 'completed')) ? 'good'
        : ev.kind === 'verdict' && ev.payload.verdict === 'approve' ? 'good' : '';
  const label = statusLabel(ev);
  // a live time element, so the ticker keeps it honest without a rebuild
  const item = statusLine(label, timeEl(ev.ts), tone);
  // an error's stack, a silence note's findings — tucked under, closed
  const more = detailBlock(ev, app, { small: true });
  if (more) item.appendChild(more);
  return item;
}

function statusLabel(ev) {
  if (ev.kind === 'state') {
    const to = ev.payload.to;
    const label = STATE_LABEL[to] || to || 'updated';
    // The reason is a machine string that can run long; the thread wants a line,
    // and the whole reason is one click away on the card itself.
    return ev.payload.reason ? `${label} — ${firstLine(ev.payload.reason, 52)}` : label;
  }
  return firstLine(eventText(ev), 90);
}

export function statusLine(label, when, tone) {
  return h('div.item.centered',
    h('div.status-line', { class: `status-line${tone ? ' is-' + tone : ''}` },
      h('span.status-dot'),
      h('span.status-label', label),
      when ? h('span.status-when', when) : null));   // `when` may be a live timeEl
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

function evidencePacket(packet, card, state, app, walking) {
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

  // Ops work has no diff and no preview — what it has is a readback: the log
  // line or command output showing the thing actually happened. It is evidence,
  // so it renders verbatim and preformatted, never reflowed into prose.
  const readback = Array.isArray(p.readback) ? p.readback.join('\n')
    : (typeof p.readback === 'string' ? p.readback : '');
  if (readback.trim()) {
    box.appendChild(h('div',
      h('p.packet-label.accent', 'What came back'),
      h('pre.packet-readback', readback.trim())));
  }

  const shots = Array.isArray(p.screenshots) ? p.screenshots : [];
  if (shots.length) box.appendChild(packetShots(shots, app));

  // A packet may ship DOCUMENTS as well as pictures. They read exactly as they
  // do in the thread — a title you skim, the whole document behind the expand —
  // so a findings write-up is evidence without becoming a wall of text you have
  // to scroll past to reach Approve.
  const [, docs] = splitAttachments(Array.isArray(p.reports) ? p.reports : []);
  if (docs.length) {
    const wrap = h('div');
    wrap.appendChild(h('p.packet-label.accent', docs.length === 1 ? 'Report' : `Reports · ${docs.length}`));
    wrap.appendChild(reportRow(docs, app, false));
    box.appendChild(wrap);
  }

  if (p.live_url) {
    box.appendChild(h('a.btn.packet-live', {
      href: p.live_url, target: '_blank', rel: 'noreferrer noopener', title: p.live_url,
    }, 'See it live ↗'));
  }

  const meta = [p.work_kind === 'ops' ? 'ops — no branch, no diff' : null,
    p.branch, p.diffstat,
    p.test_cmd && p.test_result ? `${p.test_cmd} → ${p.test_result}` : p.test_result]
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

  if (state === 'ready' && !walking) box.appendChild(verdictBar(card, app));
  else if (state === 'ready') {
    box.appendChild(h('p.packet-walking',
      'Approve, Bounce or Skip are pinned at the bottom of this panel while you are walking the queue.'));
  }
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

  // Card #44. Hitting Bounce is already the decision; from that moment the row
  // offers exactly two things — send it, or back out. Approve and Reject are
  // not dimmed, they are GONE, because the failure being prevented is hitting
  // Approve with a half-written bounce in the box under it.
  const composing = bounceComposing(card.num) || !!draft(key);

  const notes = h('textarea.bounce-notes', {
    rows: '2',
    placeholder: 'What has to change? (goes straight to the agent)',
    oninput: (e) => draft(key, e.target.value),
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendBounce(); }
      if (e.key === 'Escape') { e.preventDefault(); cancelBounce(); }
    },
  });
  notes.value = draft(key);

  function sendBounce() {
    const text = notes.value.trim();
    if (!text) { notes.focus(); return; }
    draft(key, null);
    bounceComposing(card.num, false);
    app.verdict(card, 'bounce', text);
  }

  function cancelBounce() {
    // Cancel puts the three buttons back. It drops only what you typed HERE —
    // every other composer on the page keeps its draft.
    draft(key, null);
    bounceComposing(card.num, false);
    app.render();
  }

  if (composing) {
    wrap.appendChild(h('div', notes));
    wrap.appendChild(h('div.verdicts.is-bouncing',
      h('button.btn.bounce', { type: 'button', onclick: () => sendBounce() }, 'Submit bounce'),
      h('button.btn.ghost', { type: 'button', onclick: () => cancelBounce() }, 'Cancel')));
    setTimeout(() => notes.focus(), 0);
    return wrap;
  }

  wrap.appendChild(h('div.verdicts',
    h('button.btn.approve', { type: 'button', onclick: () => app.verdict(card, 'approve') }, 'Approve'),
    h('button.btn.bounce', {
      type: 'button',
      title: 'send it back with notes',
      onclick: () => { bounceComposing(card.num, true); app.render(); },
    }, 'Bounce'),
    h('button.btn.reject', {
      type: 'button',
      title: 'this should not have been built — the branch is dropped',
      onclick: () => app.verdict(card, 'reject', draft(key) || undefined),
    }, 'Reject')));
  return wrap;
}
