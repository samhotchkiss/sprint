// The right rail — 480px on the desktop, a 452px slide-over on the Fold.
//
// One thing occupies it at a time: the session chat, or one card's thread. That
// is the whole rule, and it is why the Chat button un-highlights the moment a
// card takes the rail — the rail belongs to the card now, and nothing on screen
// should suggest otherwise.
//
// The composer is pinned to the bottom with the line that explains what this
// place is: everything here appends, nothing is rewritten.
//
// The rail is repainted a lot — every board event, every cursor move, every
// 30-second tick — so it is built to be repainted cheaply. Head, thread and
// composer each carry a signature; a paint that changes nothing replaces
// nothing, the thread reconciles item by item, and the composer (where you may
// be mid-sentence with a screenshot attached) is never thrown away unless what
// it is for actually changed.
import { h, clear, reconcile } from './util.js';
import { store, cardState, isSilent, draft, attachedImages } from './state.js';
import { phaseOf, phaseChip } from './phase.js';
import { renderThread, renderChat } from './thread.js';
import { initCompose } from './compose.js';
import { flowBarSig, reviewBar } from './review.js';

export function renderRail(root, app) {
  const owner = railOwner();
  if (!owner) {
    if (root.firstChild) clear(root);
    root.dataset.owner = '';
    return;
  }
  const same = root.dataset.owner === owner;
  if (!same) clear(root);            // a different card owns the rail: start clean
  root.dataset.owner = owner;

  const keep = same ? root.querySelector('.thread') : null;
  const prevTop = keep ? keep.scrollTop : null;
  const atBottom = keep ? (keep.scrollHeight - keep.scrollTop - keep.clientHeight < 80) : true;
  const thread = keep || h('div.thread');

  if (store.detail) {
    const detail = store.detail;
    const card = detail.card;
    syncPart(root, 'rail-head', headSig(detail, card), () => cardHead(detail, card, app));
    if (!thread.parentNode) root.appendChild(thread);
    if (!card) {
      reconcile(thread, [{ key: 'loading', ver: detail.error || 1,
        make: () => h('p.thread-empty', detail.error || 'Loading card…') }]);
    } else {
      renderThread(thread, { ...detail, state: cardState(card) }, app);
    }
    // The walkthrough's action bar, when this card is the one it is on. It sits
    // between the thread and the composer and it is the ONLY verdict on screen
    // while it is up — the packet drops its own buttons rather than showing you
    // two Approves that do the same thing.
    syncOptional(root, 'review-bar', barSig(card), () => reviewBar(card, app));
    // The composer is the ONE thing on this page you may be mid-sentence in, so
    // it is keyed on the card alone and never rebuilt for anything else: a state
    // flip, a silence, a question arriving all *tune* it in place. Rebuilding it
    // is what took the caret away mid-word (card #46).
    const box = syncPart(root, 'composer', composerKey(card), () => composer(card, app));
    if (box && box._tune) box._tune(card);
  } else {
    syncPart(root, 'rail-head', store.session.online ? 'on' : 'off', () => chatHead());
    if (!thread.parentNode) root.appendChild(thread);
    renderChat(thread, store.sidebar.slice().sort(byOrder), app);
    const box = syncPart(root, 'composer', 'sidebar', () => chatComposer(app));
    if (box && box._tune) box._tune();
  }

  // Open at the newest word; a re-render while you are reading history stays put.
  requestAnimationFrame(() => {
    const t = root.querySelector('.thread');
    if (!t) return;
    if (!same || prevTop == null) t.scrollTop = t.scrollHeight;
    else if (atBottom && t.scrollTop !== t.scrollHeight) t.scrollTop = t.scrollHeight;
  });
}

/**
 * Replace one fixed part of the rail only when its signature changed. The parts
 * are ordered head → thread → composer and each is built once, so replacing one
 * never disturbs the others (and never disturbs the thread's scroll position).
 */
function syncPart(root, cls, sig, build) {
  const found = root.querySelector('.' + cls);
  const want = String(sig);
  if (found && found.dataset.sig === want) return found;
  const node = build();
  node.dataset.sig = want;
  if (found) root.replaceChild(node, found);
  else root.appendChild(node);
  return node;
}

/**
 * A part that is sometimes not there at all. Same signature contract as
 * `syncPart`; a null signature removes it. It has to be placed before the
 * composer, so it is inserted rather than appended.
 */
function syncOptional(root, cls, sig, build) {
  const found = root.querySelector('.' + cls);
  if (sig == null) {
    if (found) root.removeChild(found);
    return null;
  }
  const want = String(sig);
  if (found && found.dataset.sig === want) return found;
  const node = build();
  node.dataset.sig = want;
  if (found) root.replaceChild(node, found);
  else root.insertBefore(node, root.querySelector('.composer') || null);
  return node;
}

/**
 * Null unless the walkthrough is standing on this exact card, ready for a
 * verdict. review.js owns the signature: a unit step's bar also changes while
 * its members are being approved one after another.
 */
function barSig(card) {
  return flowBarSig(card);
}

function headSig(detail, card) {
  if (!card) return 'loading:' + detail.num;
  // The phase (and whether it has run past what it claimed) is part of the head
  // now, so it has to be part of what makes the head repaint.
  const ph = phaseOf(card);
  return [detail.num, card.title, cardState(card), card.pinned ? 'p' : '',
    ph ? `${ph.name}@${ph.since}${ph.overdue ? '!' : ''}` : ''].join('|');
}

/**
 * One composer per card, and that is the whole signature. Everything that used
 * to be in here — the state, the open question, whether the agent has gone
 * quiet — changes what the box SAYS, not what it IS, so it is tuned rather than
 * replaced (see `_tune` below).
 */
function composerKey(card) {
  return card ? 'card:' + card.num : 'none';
}

/**
 * One draft per card, whichever thing the box is currently for. It used to be
 * `answer:N` and `chat:N`, which meant a half-typed reply evaporated the moment
 * the agent asked a question (or the answer landed) underneath you.
 */
function draftKey(card) {
  return 'card:' + card.num;
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
  // The same chip the card face carries: opening a card should not cost you the
  // one line that says what its agent is doing right now.
  const chip = card ? phaseChip(card) : null;
  if (chip) head.appendChild(chip);
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
  if (!card) return h('form.composer');

  // What the box does is re-bound on every tune, so the closure never holds a
  // stale card: the node is long-lived, the card object is not.
  const live = { send: () => {} };

  const box = composerBox({
    id: `composer-${card.num}`,
    key: draftKey(card),
    placeholder: '',
    hint: '',
    send: (text, images) => live.send(text, images),
  });

  box._tune = (c) => {
    if (!c) return;
    const state = cardState(c);
    const answering = state === 'needs_you' && !!c.question;
    setText(box, 'textarea', 'placeholder', answering
      ? 'Answer in your own words… (paste a screenshot too)'
      : (state === 'ready' ? 'Reply, or bounce with notes…'
        : 'Reply to this card — paste a screenshot if it is easier'));
    setText(box, '.composer-hint', 'textContent', isSilent(c)
      ? 'Quiet for five minutes — the session is already checking on the agent.'
      : 'Everything here appends — nothing is rewritten.');
    box.classList.toggle('is-answering', answering);
    live.send = (text, images) => {
      // An answer is a question's answer, not an attachment carrier — so a
      // screenshot pasted while answering goes to the agent as its own line
      // first, and the answer follows and unblocks the card.
      if (answering) app.answer(c, c.question, text, images);
      else app.chat(c, text, images);
    };
  };
  box._tune(card);
  return box;
}

function chatComposer(app) {
  const box = composerBox({
    id: 'sidebar-text',
    key: 'sidebar',
    placeholder: 'Ask the session anything… (paste a screenshot too)',
    hint: '',
    send: (text, images) => app.sessionChat(text, images),
  });
  // The session going offline changes one sentence under the box, and it used
  // to change the whole box — with your half-written question inside it.
  box._tune = () => {
    setText(box, '.composer-hint', 'textContent', store.session.online
      ? 'Everything here appends — nothing is rewritten.'
      : 'The session is not reading right now — what you send waits in the queue.');
  };
  box._tune();
  return box;
}

/** Patch one word of a long-lived node, and only when it actually changed. */
function setText(root, sel, prop, value) {
  const node = root.querySelector(sel);
  if (node && node[prop] !== value) node[prop] = value;
}

/**
 * The rail's composer: the Drop-work sheet's box, minus the hold toggle. Paste
 * or drop a screenshot and it becomes a removable thumbnail above the line you
 * are typing; Return sends both together.
 */
function composerBox({ id, key, placeholder, hint, send }) {
  const foot = h('form.composer', { autocomplete: 'off' });
  const ta = h('textarea', { id, rows: '1', placeholder });
  ta.value = draft(key);

  const thumbs = h('div.thumbs.composer-thumbs', { hidden: true });
  const err = h('span.composer-err', { hidden: true });
  const fileId = `${id}-file`;
  const file = h('input', { type: 'file', id: fileId, multiple: true, accept: 'image/*', hidden: true });

  foot.appendChild(thumbs);
  foot.appendChild(h('div.composer-row',
    ta,
    h('label.icon-btn.attach', { for: fileId, title: 'attach an image' }, paperclip()),
    file,
    h('button.btn.send', { type: 'submit' }, 'Send')));
  foot.appendChild(err);
  foot.appendChild(h('span.composer-hint', hint));

  initCompose({
    form: foot,
    textarea: ta,
    thumbsEl: thumbs,
    fileInput: file,
    errEl: err,
    minHeight: 44,
    maxHeight: 160,
    // Both halves of a half-written message survive a re-render: the words in
    // `drafts`, the screenshots in `attached`.
    images: { get: () => attachedImages(key), set: (v) => attachedImages(key, v) },
    onInput: (e) => draft(key, e.target.value),
    onSubmit: ({ text, images }) => { draft(key, null); send(text || '', images); },
  });
  return foot;
}

/** Drawn, not typed: an emoji paperclip renders differently on every machine. */
function paperclip() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '17');
  svg.setAttribute('height', '17');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M20 11.5 12.2 19.3a5 5 0 0 1-7.1-7.1l8-8a3.4 3.4 0 1 1 4.8 4.8l-8 8a1.8 1.8 0 0 1-2.5-2.5l7.2-7.2');
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.6');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(path);
  return svg;
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
