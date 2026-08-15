// The right rail — 480px on the desktop, a 452px slide-over on the Fold.
//
// One thing occupies it at a time: the session chat, or one card's thread. That
// is the whole rule, and it is why the Chat button un-highlights the moment a
// card takes the rail — the rail belongs to the card now, and nothing on screen
// should suggest otherwise.
//
// The composer is pinned to the bottom with the line that explains what this
// place is: everything here appends, nothing is rewritten.
import { h, clear } from './util.js';
import { store, cardState, isSilent, draft } from './state.js';
import { renderThread, renderChat } from './thread.js';

export function renderRail(root, app) {
  const keep = root.querySelector('.thread');
  const prevTop = keep ? keep.scrollTop : null;
  const atBottom = keep ? (keep.scrollHeight - keep.scrollTop - keep.clientHeight < 80) : true;
  const sameCard = root.dataset.owner === railOwner();

  clear(root);
  root.dataset.owner = railOwner();
  if (!railOwner()) return;

  const thread = h('div.thread');
  if (store.detail) {
    const detail = store.detail;
    const card = detail.card;
    root.appendChild(cardHead(detail, card, app));
    if (!card) {
      thread.appendChild(h('p.thread-empty', detail.error || 'Loading card…'));
    } else {
      renderThread(thread, { ...detail, state: cardState(card) }, app);
    }
    root.appendChild(thread);
    root.appendChild(composer(card, app));
  } else {
    root.appendChild(chatHead());
    const lines = store.sidebar.slice().sort(byOrder);
    renderChat(thread, lines, app);
    root.appendChild(thread);
    root.appendChild(chatComposer(app));
  }

  // Open at the newest word; a re-render while you are reading history stays put.
  requestAnimationFrame(() => {
    const t = root.querySelector('.thread');
    if (!t) return;
    if (!sameCard || prevTop == null || atBottom) t.scrollTop = t.scrollHeight;
    else t.scrollTop = prevTop;
  });
}

function railOwner() {
  if (store.detail) return 'card:' + store.detail.num;
  if (store.chatOpen) return 'chat';
  return '';
}

const byOrder = (a, b) => {
  const o = (e) => (e.seq != null ? e.seq : (e.sortSeq != null ? e.sortSeq : Infinity));
  return o(a) - o(b);
};

// ---- heads ---------------------------------------------------------------

function chatHead() {
  const online = store.session.online;
  return h('div.rail-head',
    h('span.session-dot', { class: online ? 'session-dot' : 'session-dot off' }),
    h('span.rail-title', 'Session'),
    h('span.rail-note', online ? 'the manager channel' : 'not reading right now'));
}

function cardHead(detail, card, app) {
  const state = card ? cardState(card) : null;
  const head = h('div.rail-head');
  head.appendChild(h('span.rail-num', '#' + detail.num));
  head.appendChild(h('span.rail-title', { title: card ? card.title : '' }, card ? card.title : 'Loading…'));
  if (card) head.appendChild(cardMenu(card, state, app));
  head.appendChild(h('button.rail-close', {
    type: 'button', onclick: () => app.closeCard(),
  }, 'Close'));
  return head;
}

function cardMenu(card, state, app) {
  const menu = h('div.menu', { hidden: true });
  // A closed card is closed, not buried: the only thing on offer is getting it
  // back. Closing is the user's verb, and so is undoing it.
  const closed = ['completed', 'rejected', 'duplicate', 'canceled'].includes(state);
  const actions = closed ? [
    { label: 'Reopen — back to the queue', run: () => app.cardAction(card, 'reopen') },
    { label: card.pinned ? 'Unpin' : 'Pin to the top', run: () => app.cardAction(card, card.pinned ? 'unpin' : 'pin') },
  ] : [
    { label: card.pinned ? 'Unpin' : 'Pin to the top', run: () => app.cardAction(card, card.pinned ? 'unpin' : 'pin') },
    state === 'held'
      ? { label: 'Release — start work', run: () => app.cardAction(card, 'release') }
      : { label: 'Hold — stop work', run: () => app.cardAction(card, 'hold') },
    state === 'failed'
      ? { label: 'Retry with a fresh agent', run: () => app.retryCard(card) }
      : null,
    { label: 'Mark duplicate of…', run: () => app.markDuplicate(card) },
    { label: 'Cancel this card', run: () => app.cardAction(card, 'cancel'), danger: true },
  ].filter(Boolean);

  for (const a of actions) {
    menu.appendChild(h('button.menu-item', {
      type: 'button',
      class: a.danger ? 'menu-item danger' : 'menu-item',
      onclick: () => { menu.hidden = true; a.run(); },
    }, a.label));
  }

  return h('div.menu-wrap',
    h('button.icon-btn', {
      type: 'button', 'aria-label': 'card actions',
      onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; },
    }, '⋯'),
    menu);
}

// ---- composers -----------------------------------------------------------

/**
 * One box at the bottom of a card thread. What it sends depends on what the card
 * is waiting for: an open question makes it an answer, everything else makes it
 * a message to the agent. The placeholder says which, so nothing is a surprise.
 */
function composer(card, app) {
  const foot = h('footer.composer');
  if (!card) return foot;

  const state = cardState(card);
  const answering = state === 'needs_you' && !!card.question;
  const key = (answering ? 'answer:' : 'chat:') + card.num;

  const ta = h('textarea', {
    id: `composer-${card.num}`,
    rows: '1',
    placeholder: answering ? 'Answer in your own words…'
      : (state === 'ready' ? 'Reply, or bounce with notes…' : 'Reply to this card…'),
    oninput: (e) => { draft(key, e.target.value); grow(e.target); },
    onkeydown: (e) => {
      // Return sends, Shift+Return makes a new line.
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    },
  });
  ta.value = draft(key);

  function send() {
    const text = ta.value.trim();
    if (!text) return;
    draft(key, null);
    ta.value = '';
    grow(ta);
    if (answering) app.answer(card, card.question, text);
    else app.chat(card, text);
  }

  foot.appendChild(h('form.composer-row', { onsubmit: (e) => { e.preventDefault(); send(); } },
    ta,
    h('button.btn.send', { type: 'submit' }, 'Send')));
  foot.appendChild(h('span.composer-hint',
    isSilent(card)
      ? 'Quiet for five minutes — the session is already checking on the agent.'
      : 'Everything here appends — nothing is rewritten.'));
  return foot;
}

function chatComposer(app) {
  const key = 'sidebar';
  const ta = h('textarea', {
    id: 'sidebar-text',
    rows: '1',
    placeholder: 'Ask the session anything…',
    oninput: (e) => { draft(key, e.target.value); grow(e.target); },
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    },
  });
  ta.value = draft(key);

  function send() {
    const text = ta.value.trim();
    if (!text) return;
    draft(key, null);
    ta.value = '';
    grow(ta);
    app.sessionChat(text);
  }

  const foot = h('footer.composer');
  foot.appendChild(h('form.composer-row', { onsubmit: (e) => { e.preventDefault(); send(); } },
    ta,
    h('button.btn.send', { type: 'submit' }, 'Send')));
  foot.appendChild(h('span.composer-hint',
    store.session.online
      ? 'Everything here appends — nothing is rewritten.'
      : 'The session is not reading right now — what you send waits in the queue.'));
  return foot;
}

function grow(ta, max = 160) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(max, Math.max(ta.scrollHeight, 44)) + 'px';
}

// ---- lightbox ------------------------------------------------------------

export function openLightbox(root, urls, index, captions) {
  let i = Math.max(0, index);
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
    urls.length > 1 ? h('button.icon-btn', { type: 'button', onclick: () => show(i - 1), 'aria-label': 'previous' }, '‹') : null,
    urls.length > 1 ? h('button.icon-btn', { type: 'button', onclick: () => show(i + 1), 'aria-label': 'next' }, '›') : null,
    h('button.icon-btn', { type: 'button', onclick: () => closeLightbox(root), 'aria-label': 'close' }, '✕')));
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
