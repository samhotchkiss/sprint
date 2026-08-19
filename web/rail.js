// The right rail — 480px on the desktop, a 452px slide-over on the Fold.
//
// One thing occupies it at a time: the session chat, one card's thread, or one
// work unit's outline (card #55). That is the whole rule, and it is why the Chat
// button un-highlights the moment a card takes the rail — the rail belongs to
// the card now, and nothing on screen should suggest otherwise.
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
import { h, clear, reconcile, autolink } from './util.js';
import {
  store, cardState, isSilent, draft, attachedImages, cardComposerKey,
  isConversation, conversationAnswered, sessionLabel,
} from './state.js';
import { phaseOf, phaseChip } from './phase.js';
import { executorTag } from './settings.js';
import { renderThread, renderChat } from './thread.js';
import { initCompose } from './compose.js';
import { paperclip } from './attach.js';
import { syncPart, syncOptional } from './slots.js';
import {
  unitInReview, unitSig, unitHead, unitOutline, unitBar, verdictBarSig, packetVerdictBar,
} from './review.js';

export function renderRail(root, app) {
  // A work unit takes the rail as a whole page — the outline, then one Approve
  // for the lot. It is resolved fresh every paint: the unit is a projection over
  // whatever is still under review, so a unit that has dissolved (everything
  // merged, everything bounced) closes itself rather than hanging around.
  const unit = store.unit ? unitInReview(store.unit.lead) : null;
  if (store.unit && !unit) store.unit = null;

  const owner = railOwner(unit);
  if (!owner) {
    if (root.firstChild) clear(root);
    root.dataset.owner = '';
    return;
  }
  const same = root.dataset.owner === owner;
  if (!same) clear(root);            // a different card owns the rail: start clean
  root.dataset.owner = owner;

  if (unit && !store.detail) {
    syncPart(root, 'rail-head', unit.key + '|' + unit.size, () => unitHead(unit, app));
    syncPart(root, 'unit-outline', unitSig(unit), () => unitOutline(unit, app));
    syncPart(root, 'unit-bar', unitSig(unit), () => unitBar(unit, app));
    return;
  }

  const keep = same ? root.querySelector('.thread') : null;
  const prevTop = keep ? keep.scrollTop : null;
  const atBottom = keep ? (keep.scrollHeight - keep.scrollTop - keep.clientHeight < 80) : true;
  const thread = keep || h('div.thread');

  if (store.detail) {
    const detail = store.detail;
    const card = detail.card;
    syncPart(root, 'rail-head', headSig(detail, card), () => cardHead(detail, card, app));
    if (!thread.parentNode) root.appendChild(thread);
    // "Blocked by #58 — needs the endpoint first", directly under the head, so
    // the answer to "why is this sitting here" is the first thing in the card's
    // details rather than something you have to find in the thread (#61).
    syncBlockedLine(root, card, app);
    // What this card's agent was told on top of the card itself (#71). Under
    // the blocked line for the same reason that one is under the head: both
    // are facts about the card that the thread would bury.
    syncStandingLine(root);
    if (!card) {
      reconcile(thread, [{ key: 'loading', ver: detail.error || 1,
        make: () => h('p.thread-empty', detail.error || 'Loading card…') }]);
    } else {
      renderThread(thread, { ...detail, state: cardState(card) }, app);
    }
    // The card's own verdict. Card #53: every verdict lives in the rail, pinned
    // here rather than at the bottom of the packet, so a packet with six
    // screenshots in it can never push Approve below the fold. A work unit's
    // Approve is pinned in exactly this spot, under its outline (card #55) —
    // whichever the rail is showing, the decision is in the same place.
    syncOptional(root, 'verdict-bar', card ? verdictBarSig(card) : null,
      () => packetVerdictBar(card, app));
    // A thread's decision, in the same pinned spot a work card's verdict uses:
    // whichever the rail is showing, the thing you do about it is in one place.
    syncOptional(root, 'convo-bar', card ? convoBarSig(card) : null,
      () => convoBar(card, app));
    // The composer is the ONE thing on this page you may be mid-sentence in, so
    // it is keyed on the card alone and never rebuilt for anything else: a state
    // flip, a silence, a question arriving all *tune* it in place. Rebuilding it
    // is what took the caret away mid-word (card #46).
    //
    // A RESOLVED thread is the one card with no box at all (card #80): it is
    // finished and lives in Done, so a message typed into it would land where
    // nobody is looking. The bar above says to reopen it, which is the honest
    // way back to a thread you want to keep talking in.
    if (card && isConversation(card) && cardState(card) === 'resolved') {
      const old = root.querySelector('.composer');
      if (old) root.removeChild(old);
    } else {
      const box = syncPart(root, 'composer', composerKey(card), () => composer(card, app));
      if (box && box._tune) box._tune(card);
    }
  } else {
    // The name is part of the key: an introduction has to repaint the head,
    // and nothing else about the head changes often enough to care.
    syncPart(root, 'rail-head',
      `${store.session.online ? 'on' : 'off'}|${store.agentName}`, () => chatHead());
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
 * The card's wall, when the wall is another card. It sits between the head and
 * the thread and it is the only thing in the rail that is neither an event nor
 * a control: a fact about this card that the thread would bury.
 *
 * Same signature contract as the other parts — a card whose blocker changed
 * repaints one line, and a card with nothing in its way carries no line at all.
 */
function syncBlockedLine(root, card, app) {
  const found = root.querySelector('.rail-blocked');
  const sig = (card && card.blocked_by != null)
    ? `${card.blocked_by}|${card.blocked_reason || ''}` : null;
  if (sig == null) {
    if (found) root.removeChild(found);
    return;
  }
  if (found && found.dataset.sig === sig) return;
  const node = blockedLine(card, app);
  node.dataset.sig = sig;
  if (found) root.replaceChild(node, found);
  else root.insertBefore(node, root.querySelector('.thread') || null);
}

/**
 * The standing instructions this board adds to every brief, shown as the brief
 * itself carries them — the same heading, the same words, straight off the
 * server (`board.standing_instructions` → `store.standing`).
 *
 * Why it is on the card and not only in Settings: when an agent does something
 * you did not ask for on this card, the first question is "what was it told?",
 * and the honest answer is the card's text PLUS this. A board with no standing
 * instructions draws nothing at all, which is most boards.
 *
 * Collapsed by default — it is the same paragraph on every card, so it earns a
 * line, not a wall. Native <details>, so the browser owns the toggle.
 */
function syncStandingLine(root) {
  const found = root.querySelector('.rail-standing');
  const text = store.standing || '';
  if (!text) {
    if (found) root.removeChild(found);
    return;
  }
  if (found && found.dataset.sig === text) return;
  const node = standingLine(text);
  node.dataset.sig = text;
  if (found) root.replaceChild(node, found);
  else root.insertBefore(node, root.querySelector('.thread') || null);
}

function standingLine(block) {
  // The block arrives as "## <heading>\n\n<body>" — the exact string the
  // session pastes into the brief. Split it once for display; the body is
  // rendered preformatted so the user's own line breaks survive.
  const nl = block.indexOf('\n');
  const heading = block.slice(0, nl < 0 ? block.length : nl).replace(/^#+\s*/, '');
  const body = block.slice(nl < 0 ? block.length : nl + 1).trim();
  const box = h('details.rail-standing');
  box.appendChild(h('summary.rail-standing-head',
    h('span.rail-standing-label', 'Standing instructions'),
    h('span.rail-standing-what', 'in every brief on this board')));
  box.appendChild(h('p.rail-standing-heading', heading));
  box.appendChild(h('pre.rail-standing-body', body));
  return box;
}

function blockedLine(card, app) {
  const line = h('div.rail-blocked');
  line.appendChild(h('span.rail-blocked-label', 'Blocked by'));
  // The ordinary in-app card link (#54's autolink), so #58 here behaves
  // exactly like #58 anywhere else on the board: one click, same rail.
  line.appendChild(h('span.rail-blocked-num',
    autolink('#' + card.blocked_by, app && app.openCard)));
  if (card.blocked_reason) {
    line.appendChild(h('span.rail-blocked-why', '— ' + card.blocked_reason));
  }
  line.appendChild(h('span.rail-blocked-note',
    'It clears itself when that card lands.'));
  return line;
}

function headSig(detail, card) {
  if (!card) return 'loading:' + detail.num;
  // The phase (and whether it has run past what it claimed) is part of the head
  // now, so it has to be part of what makes the head repaint.
  const ph = phaseOf(card);
  return [detail.num, card.title, cardState(card), card.pinned ? 'p' : '',
    detail.fromUnit != null ? 'u' + detail.fromUnit : '',
    ph ? `${ph.name}@${ph.since}${ph.overdue ? '!' : ''}` : '',
    // the executor tag lives in the head too, so a re-dispatch on grok repaints it
    card.executor || '', card.model || ''].join('|');
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
  return cardComposerKey(card.num);
}

function railOwner(unit) {
  if (store.detail) return 'card:' + store.detail.num;
  if (unit) return 'unit:' + unit.key;
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
  // Once the session has a name, the header says WHO you are talking to and the
  // note underneath keeps saying WHAT the channel is — you should never have to
  // work out that "Chuck" is the session. Nameless, this is exactly as it was.
  const named = !!store.agentName;
  const note = online ? 'the manager channel' : 'not reading right now';
  return h('div.rail-head',
    h('span.session-dot', { class: online ? 'session-dot' : 'session-dot off' }),
    h('span.rail-title', sessionLabel()),
    h('span.rail-note', named ? `the session · ${note}` : note));
}

function cardHead(detail, card, app) {
  const state = card ? cardState(card) : null;
  const head = h('div.rail-head');
  // You came here from a unit's outline to read one member's own history; the
  // way back is where you left from, not the whole board.
  if (detail.fromUnit != null) {
    head.appendChild(h('button.rail-back', {
      type: 'button', title: 'back to the outline', onclick: () => app.openUnit(detail.fromUnit),
    }, '‹ Unit'));
  }
  head.appendChild(h('span.rail-num', '#' + detail.num));
  head.appendChild(h('span.rail-title', { title: card ? card.title : '' }, card ? card.title : 'Loading…'));
  // The same chip the card face carries: opening a card should not cost you the
  // one line that says what its agent is doing right now.
  const chip = card ? phaseChip(card) : null;
  if (chip) head.appendChild(chip);
  // A conversation has no phase to show, so the head says what it is instead —
  // otherwise a thread and a work card are indistinguishable once open.
  else if (card && isConversation(card)) head.appendChild(h('span.rail-note', 'conversation'));
  // ...and, when this card is not running on the board's defaults, what it was
  // dispatched as: "grok · tmux".
  const exec = card ? executorTag(card) : null;
  if (exec) head.appendChild(exec);
  if (card) head.appendChild(cardMenu(card, state, app));
  head.appendChild(h('button.rail-close', {
    type: 'button', onclick: () => app.closeCard(),
  }, 'Close'));
  return head;
}

// ---- the conversation bar (card #80) --------------------------------------
//
// The bug this fixes, in one sentence: a thread whose question you had already
// ANSWERED sat in Needs you looking exactly like one nobody had touched,
// because the only exit a conversation had was `cancel` — and cancel means
// "discard, this did not happen", which no session may write on a card anyway.
//
// So the rail says which of the three a thread is, out loud:
//
//   answered  you replied and it is finished → the Resolve button, right here
//   waiting   somebody is still owed an answer → says so, and offers nothing
//   resolved  you ended it → says so, and offers the undo
//
// Resolve is a BUTTON and not a menu item on purpose: the user has to be able
// to notice that a thread has become resolvable without going looking for it.

function convoBarSig(card) {
  if (!card || !isConversation(card)) return null;
  const state = cardState(card);
  if (state !== 'conversation' && state !== 'resolved') return null;
  return [card.num, state, conversationAnswered(card) ? 'answered' : 'open'].join('|');
}

function convoBar(card, app) {
  const state = cardState(card);
  const bar = h('div.review-bar.is-convo');

  if (state === 'resolved') {
    bar.appendChild(h('p.convo-bar-line',
      'Resolved — you ended this thread. It is kept, and you can read it any time.'));
    bar.appendChild(h('div.convo-bar-acts',
      h('button.btn', {
        type: 'button', onclick: () => app.cardAction(card, 'reopen'),
      }, 'Reopen this conversation')));
    return bar;
  }

  if (!conversationAnswered(card)) {
    // Deliberately no button: nothing is finished, so there is nothing to
    // agree is finished. This line exists so the answered one reads as
    // different at a glance rather than as the same card in a different mood.
    bar.classList.add('is-waiting');
    bar.appendChild(h('p.convo-bar-line',
      'Still needs your answer — reply below and this thread is done.'));
    return bar;
  }

  bar.classList.add('is-answered');
  bar.appendChild(h('p.convo-bar-line',
    'Answered — you had the last word here. Resolve it to close it out and keep it.'));
  bar.appendChild(h('div.convo-bar-acts',
    h('button.btn.resolve', {
      type: 'button', onclick: () => app.cardAction(card, 'resolve'),
    }, 'Resolve — keep this thread'),
    h('span.convo-bar-note', 'Only you can do this. Nothing on the board resolves a thread for you.')));
  return bar;
}

function cardMenu(card, state, app) {
  const menu = h('div.menu', { hidden: true });
  // A closed card is closed, not buried: the only thing on offer is getting it
  // back. Closing is the user's verb, and so is undoing it.
  const closed = ['completed', 'rejected', 'duplicate', 'canceled', 'resolved'].includes(state);
  // A conversation has no queue, no agent and no branch, so none of the verbs
  // that push work around mean anything on one. What is left is keeping it at
  // the top and ending it — and ending it is the user's word for "this
  // discussion is done", never a work state.
  const actions = isConversation(card) ? [
    { label: card.pinned ? 'Unpin' : 'Pin to the top', run: () => app.cardAction(card, card.pinned ? 'unpin' : 'pin') },
    // Two endings, and the words say which is which: resolving KEEPS the
    // thread, discarding says it never happened. Resolve is in the bar as a
    // button too — this is the copy of it for a thread you want to end early.
    closed
      ? { label: 'Reopen this conversation', run: () => app.cardAction(card, 'reopen') }
      : { label: 'Resolve — the discussion is done', run: () => app.cardAction(card, 'resolve') },
    closed
      ? null
      : { label: 'Discard this conversation', run: () => app.cardAction(card, 'cancel'), danger: true },
  ].filter(Boolean) : closed ? [
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
    // Not every thing you drop on the board turns out to be work. This turns
    // one into an ongoing thread instead — same number, same body, same
    // timeline; only what the board expects of it changes.
    { label: 'Make this a conversation', run: () => app.cardAction(card, 'make_conversation') },
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
    // A conversation is a thread, not a job: nothing here is waiting on an
    // agent, and replying is the whole point rather than an interruption. The
    // wording is set on the LIVE node, never by rebuilding it — rebuilding is
    // what used to move your caret when the highlight changed under you.
    const convo = isConversation(c);
    // Card #76: named the same way the sidebar names the session ("Ask the
    // session anything…") — the card's own number, right in the placeholder,
    // so which surface you are typing into is never a guess.
    setText(box, 'textarea', 'placeholder', convo
      ? `Say something in #${c.num}'s thread…`
      : answering
        ? `Answer #${c.num} in your own words… (paste a screenshot too)`
        : (state === 'ready' ? `Reply to #${c.num}, or bounce with notes…`
          : `Reply to card #${c.num} — paste a screenshot if it is easier`));
    setText(box, '.composer-hint', 'textContent', convo
      ? 'An ongoing thread — replying is what clears its highlight.'
      : isSilent(c)
        ? 'Quiet for five minutes — the session is already checking on the agent.'
        : 'Everything here appends — nothing is rewritten.');
    box.classList.toggle('is-answering', answering);
    live.send = (text, images) => {
      // A screenshot pasted while answering rides the ANSWER (card #66), not a
      // separate line before it: the picture belongs to the words it came with.
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
    // Named, the box asks you to talk to a person; nameless, it is exactly the
    // sentence it always was.
    placeholder: store.agentName
      ? `Ask ${store.agentName} anything… (paste a screenshot too)`
      : 'Ask the session anything… (paste a screenshot too)',
    hint: '',
    send: (text, images) => app.sessionChat(text, images),
  });
  // The session going offline changes one sentence under the box, and it used
  // to change the whole box — with your half-written question inside it. The
  // name it wears can arrive late too, and it must not cost a half-written
  // question either.
  box._tune = () => {
    setText(box, 'textarea', 'placeholder', store.agentName
      ? `Ask ${store.agentName} anything… (paste a screenshot too)`
      : 'Ask the session anything… (paste a screenshot too)');
    setText(box, '.composer-hint', 'textContent', store.session.online
      ? 'Everything here appends — nothing is rewritten.'
      : `${store.agentName || 'The session'} is not reading right now — what you `
        + 'send waits in the queue.');
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
