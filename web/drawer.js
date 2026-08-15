// Card drawer: evidence above the fold, one interleaved timeline, chat, verdicts.
import { h, clear, timeEl, ageSuffix, richText, plural } from './util.js';
import { attachmentUrl, attachmentCaption } from './api.js';
import { STATE_LABEL, SYSTEM_KINDS, eventText, cardState, isSilent, draft } from './state.js';

const ACTOR_LABEL = { user: 'You', session: 'Session', worker: 'Agent', server: 'Board' };

export function renderDrawer(root, detail, app) {
  const keepScroll = root.querySelector('.drawer-body');
  const prevTop = keepScroll ? keepScroll.scrollTop : null;
  const atBottom = keepScroll ? (keepScroll.scrollHeight - keepScroll.scrollTop - keepScroll.clientHeight < 60) : true;
  clear(root);
  if (!detail) return;

  const card = detail.card;
  if (!card) {
    root.appendChild(h('div.drawer-head',
      h('div.drawer-heading', h('span.card-num', '#' + detail.num)),
      closeBtn(app)));
    root.appendChild(h('div.drawer-body',
      h('p.col-empty', detail.error ? detail.error : 'Loading card…')));
    return;
  }

  const state = cardState(card);
  root.appendChild(drawerHead(card, state, app));

  const body = h('div.drawer-body');
  if ((state === 'ready' || state === 'integrating') && (detail.evidence || card.evidence)) {
    if (state === 'integrating') {
      body.appendChild(h('div.panel.merging-panel',
        h('h4.panel-title', 'Merging'),
        h('p.panel-line', 'Approved. The session is rebasing on main, running the repo gate and merging.'),
        h('p.panel-note', 'It lands in Done only once the branch is actually in. If the gate fails it comes back to In progress with the reason — that is not a bounce.')));
    }
    body.appendChild(evidencePanel(detail.evidence || card.evidence, app));
  }
  if (state === 'blocked' && card.reason) {
    body.appendChild(h('div.panel.blocked-panel',
      h('h4.panel-title', 'Blocked'),
      h('p.panel-line', card.reason),
      h('p.panel-note', 'Nothing you type fixes this one — the session re-checks it.')));
  }
  if ((state === 'failed') && card.error) {
    body.appendChild(h('div.panel.error-panel',
      h('h4.panel-title', 'Agent failed'),
      h('p.panel-line', card.error),
      h('button.btn', { type: 'button', onclick: () => app.retryCard(card) }, 'Retry with a fresh agent')));
  }
  // The face carries a condensed title; the drawer always carries the user's
  // own words, verbatim and in full.
  if (card.body && card.body !== card.title) {
    body.appendChild(h('div.panel.body-panel',
      h('h4.panel-title', 'What you submitted'),
      h('p.card-body-text', richText(card.body, app.openCard))));
  }
  body.appendChild(timeline(detail, app));
  root.appendChild(body);

  root.appendChild(drawerFoot(card, state, detail, app));

  // Evidence sits above the fold on a ready card; everything else opens at the latest word.
  const evidenceFirst = state === 'ready' || state === 'integrating';
  const first = !detail._painted;
  detail._painted = true;
  requestAnimationFrame(() => {
    const b = root.querySelector('.drawer-body');
    if (!b) return;
    if (evidenceFirst) b.scrollTop = first ? 0 : (prevTop || 0);
    else if (prevTop == null || atBottom) b.scrollTop = b.scrollHeight;
    else b.scrollTop = prevTop;
  });
}

function closeBtn(app) {
  return h('button.btn.ghost.icon', { type: 'button', 'aria-label': 'close', onclick: () => app.closeCard() }, '✕');
}

function drawerHead(card, state, app) {
  const menu = h('div.menu', { hidden: true });
  // A closed card is closed, not buried: the only thing on offer is getting it
  // back. Closing is the user's call, and so is undoing it.
  const closed = ['completed', 'rejected', 'duplicate', 'canceled'].includes(state);
  const actions = closed ? [
    { label: 'Reopen — back to Queued', run: () => app.cardAction(card, 'reopen') },
    { label: card.pinned ? 'Unpin' : 'Pin to top', run: () => app.cardAction(card, card.pinned ? 'unpin' : 'pin') },
  ] : [
    { label: card.pinned ? 'Unpin' : 'Pin to top', run: () => app.cardAction(card, card.pinned ? 'unpin' : 'pin') },
    state === 'held'
      ? { label: 'Release — start work', run: () => app.cardAction(card, 'release') }
      : { label: 'Hold — stop work', run: () => app.cardAction(card, 'hold') },
    { label: 'Cancel this card', run: () => app.cardAction(card, 'cancel'), danger: true },
    { label: 'Mark duplicate of…', run: () => app.markDuplicate(card) },
  ];
  for (const a of actions) {
    menu.appendChild(h('button.menu-item', {
      type: 'button', class: a.danger ? 'menu-item danger' : 'menu-item',
      onclick: () => { menu.hidden = true; a.run(); },
    }, a.label));
  }

  return h('div.drawer-head',
    h('div.drawer-heading',
      h('span.card-num', '#' + card.num),
      h('span.chip', { class: `chip state-${state}` }, STATE_LABEL[state] || state),
      card.agent_name ? h('span.badge.agent', (card.batch_id ? '⛓ ' : '') + card.agent_name) : null,
      card.branch ? h('span.badge.mono', card.branch) : null,
      h('span.grow'),
      h('div.menu-wrap',
        h('button.btn.ghost.icon', {
          type: 'button', 'aria-label': 'card actions',
          onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; },
        }, '⋯'),
        menu),
      closeBtn(app)),
    h('h2.drawer-title', card.title),
    h('p.drawer-sub',
      isSilent(card) ? h('span.foot-flag.is-silent', 'quiet ', timeEl(card.last_activity_at, { suffix: false }), ' — session is checking') : null,
      h('span.foot-quiet', 'last activity ', timeEl(card.last_activity_at)),
      card.bounce_count >= 2 ? h('span.foot-flag.is-silent', 'bounced twice — bring it to co-design') : null,
    ),
  );
}

// ---- evidence ------------------------------------------------------------

function evidencePanel(packet, app) {
  const p = packet || {};
  const panel = h('div.panel.evidence');
  panel.appendChild(h('h4.panel-title', 'Evidence'));
  if (p.claim) panel.appendChild(h('p.claim', p.claim));

  // The point of the packet: check it yourself without reading any code.
  const steps = Array.isArray(p.validate) ? p.validate.filter(Boolean)
    : (typeof p.validate === 'string' && p.validate ? [p.validate] : []);
  if (steps.length) {
    const list = h('ol.validate');
    for (const s of steps) list.appendChild(h('li', typeof s === 'string' ? s : (s.text || s.step || '')));
    panel.appendChild(h('div.validate-block',
      h('h5.validate-title', 'Check it yourself'),
      list));
  }
  if (p.live_url) {
    panel.appendChild(h('a.btn.live-btn', {
      href: p.live_url, target: '_blank', rel: 'noreferrer noopener',
      title: p.live_url,
    }, 'See it live ↗'));
  }

  const facts = h('dl.facts');
  const fact = (k, v, mono) => {
    if (!v) return;
    facts.appendChild(h('dt', k));
    facts.appendChild(h('dd', { class: mono ? 'mono' : '' }, v));
  };
  fact('tests', p.test_result, true);
  fact('command', p.test_cmd, true);
  fact('diff', p.diffstat, true);
  fact('branch', p.branch, true);
  if (facts.childElementCount) panel.appendChild(facts);

  if (p.live_url) panel.appendChild(h('p.live-url', p.live_url));
  const shots = Array.isArray(p.screenshots) ? p.screenshots : [];
  if (shots.length) panel.appendChild(shotStrip(shots, app));

  const per = Array.isArray(p.per_card) ? p.per_card : [];
  if (per.length) {
    const list = h('div.per-card');
    list.appendChild(h('h5.panel-subtitle', `Batch — ${plural(per.length, 'card')}`));
    for (const entry of per) {
      list.appendChild(h('div.per-card-row',
        h('div.per-card-head',
          h('button.cardlink', { type: 'button', onclick: () => app.openCard(entry.card_num) }, '#' + entry.card_num),
          h('span.per-claim', entry.claim || '')),
        Array.isArray(entry.screenshots) && entry.screenshots.length ? shotStrip(entry.screenshots, app, true) : null));
    }
    panel.appendChild(list);
  }
  return panel;
}

function shotStrip(shots, app, small) {
  const strip = h('div.shots', { class: small ? 'shots small' : 'shots' });
  const urls = shots.map(attachmentUrl).filter(Boolean);
  shots.forEach((ref, i) => {
    const url = attachmentUrl(ref);
    if (!url) return;
    const cap = attachmentCaption(ref);
    strip.appendChild(h('button.shot', {
      type: 'button', title: cap || 'screenshot',
      onclick: () => app.lightbox(urls, i, shots.map(attachmentCaption)),
    },
      h('img', { src: url, alt: cap || 'screenshot', loading: 'lazy', onerror: (e) => e.target.closest('.shot').classList.add('broken') }),
      cap ? h('span.shot-cap', cap) : null));
  });
  return strip;
}

// ---- timeline ------------------------------------------------------------

function timeline(detail, app) {
  const wrap = h('div.timeline');
  const items = (detail.timeline || []).slice();
  for (const p of detail.pendingLines || []) items.push(p);
  if (!items.length) {
    wrap.appendChild(h('p.col-empty', 'Nothing yet.'));
    return wrap;
  }
  for (const ev of items) {
    if (SYSTEM_KINDS.has(ev.kind) && ev.kind !== 'submitted') {
      wrap.appendChild(h('div.sysline', { class: `sysline kind-${ev.kind}` },
        h('span.sysline-text', eventText(ev)),
        h('span.sysline-time', timeEl(ev.ts, { suffix: false }))));
      continue;
    }
    const mine = ev.actor === 'user';
    const row = h('div.msg', { class: `msg actor-${ev.actor}${mine ? ' mine' : ''}${ev.pending ? ' pending' : ''}` });
    row.appendChild(h('div.msg-head',
      h('span.msg-actor', ACTOR_LABEL[ev.actor] || ev.actor),
      ev.kind === 'question' ? h('span.msg-kind', 'question') : null,
      ev.kind === 'progress' ? h('span.msg-kind', 'progress') : null,
      h('span.grow'),
      h('span.msg-time', ev.pending ? 'sending…' : ageSuffix(ev.ts))));
    row.appendChild(h('div.msg-text', richText(eventText(ev), app.openCard)));
    const atts = ev.payload && (ev.payload.attachments || ev.payload.images);
    if (Array.isArray(atts) && atts.length) row.appendChild(shotStrip(atts, app, true));
    wrap.appendChild(row);
  }
  return wrap;
}

// ---- footer: verdicts + chat --------------------------------------------

function drawerFoot(card, state, detail, app) {
  const foot = h('footer.drawer-foot');

  if (state === 'needs_you' && card.question) {
    foot.appendChild(questionFoot(card, app));
  }

  if (state === 'ready') {
    const notesKey = `bounce:${card.num}`;
    const notes = h('textarea.bounce-notes', {
      rows: '2', placeholder: 'What has to change? (sent to the agent)',
      oninput: (e) => draft(notesKey, e.target.value),
    });
    notes.value = draft(notesKey);
    const notesWrap = h('div.bounce-wrap', { hidden: !draft(notesKey) }, notes);
    const bounceBtn = h('button.btn.bounce', {
      type: 'button',
      onclick: () => {
        if (notesWrap.hidden) { notesWrap.hidden = false; bounceBtn.textContent = 'Send bounce'; notes.focus(); return; }
        const text = notes.value.trim();
        if (!text) { notes.focus(); return; }
        draft(notesKey, null);
        app.verdict(card, 'bounce', text);
      },
    }, notesWrap.hidden ? 'Bounce with notes' : 'Send bounce');
    const bar = h('div.verdicts',
      h('button.btn.approve', { type: 'button', onclick: () => app.verdict(card, 'approve') }, 'Approve'),
      bounceBtn,
      h('button.btn.reject', { type: 'button', onclick: () => app.verdict(card, 'reject', draft(notesKey) || undefined) }, 'Reject'),
    );
    foot.appendChild(h('div.verdict-block', notesWrap, bar));
  }

  const key = `chat:${card.num}`;
  const ta = h('textarea.chat-input', {
    id: `chat-${card.num}`, rows: '1',
    placeholder: state === 'needs_you' ? 'Add context…' : 'Say something to the agent…',
    oninput: (e) => { draft(key, e.target.value); grow(e.target); },
    onkeydown: (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
    },
  });
  ta.value = draft(key);
  function send() {
    const text = ta.value.trim();
    if (!text) return;
    draft(key, null);
    ta.value = '';
    grow(ta);
    app.chat(card, text);
  }
  foot.appendChild(h('form.chat-row', {
    onsubmit: (e) => { e.preventDefault(); send(); },
  }, ta, h('button.btn.primary.icon', { type: 'submit', 'aria-label': 'send' }, '↑')));
  return foot;
}

function questionFoot(card, app) {
  const q = card.question;
  const key = `answer:${card.num}`;
  const block = h('div.panel.question-panel');
  block.appendChild(h('h4.panel-title', 'Needs you'));
  block.appendChild(h('p.question-text', q.text));
  if (q.options && q.options.length) {
    const opts = h('div.quick-replies');
    for (const opt of q.options) {
      opts.appendChild(h('button.btn.quick', { type: 'button', onclick: () => app.answer(card, q, opt.value) }, opt.label));
    }
    block.appendChild(opts);
  }
  const ta = h('textarea.answer-box', {
    rows: '1', placeholder: 'Answer…',
    oninput: (e) => { draft(key, e.target.value); grow(e.target); },
    onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } },
  });
  ta.value = draft(key);
  function go() {
    const text = ta.value.trim();
    if (!text) return;
    draft(key, null);
    app.answer(card, q, text);
  }
  block.appendChild(h('div.answer-row', ta, h('button.btn.primary.small', { type: 'button', onclick: go }, 'Answer')));
  return block;
}

function grow(ta, max = 160) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(max, ta.scrollHeight) + 'px';
}

// ---- lightbox ------------------------------------------------------------

export function openLightbox(root, urls, index, captions) {
  let i = index;
  const img = h('img.lightbox-img', { src: urls[i], alt: (captions && captions[i]) || 'screenshot' });
  const cap = h('div.lightbox-cap', (captions && captions[i]) || '');
  const counter = h('span.lightbox-count', urls.length > 1 ? `${i + 1} / ${urls.length}` : '');
  function show(n) {
    i = (n + urls.length) % urls.length;
    img.src = urls[i];
    cap.textContent = (captions && captions[i]) || '';
    counter.textContent = urls.length > 1 ? `${i + 1} / ${urls.length}` : '';
  }
  clear(root);
  root.hidden = false;
  root.appendChild(h('div.lightbox-bar',
    counter, h('span.grow'),
    urls.length > 1 ? h('button.btn.ghost.icon', { type: 'button', onclick: () => show(i - 1), 'aria-label': 'previous' }, '‹') : null,
    urls.length > 1 ? h('button.btn.ghost.icon', { type: 'button', onclick: () => show(i + 1), 'aria-label': 'next' }, '›') : null,
    h('button.btn.ghost.icon', { type: 'button', onclick: () => closeLightbox(root), 'aria-label': 'close' }, '✕')));
  root.appendChild(img);
  root.appendChild(cap);
  root.onclick = (e) => { if (e.target === root || e.target === img) closeLightbox(root); };
  root._nav = (dir) => show(i + dir);
}

export function closeLightbox(root) {
  root.hidden = true;
  clear(root);
  root._nav = null;
}
