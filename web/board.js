// The kanban surface: columns, card faces, inline answers.
import { h, clear, timeEl, age, firstLine, plural } from './util.js';
import { columns, cardState, isSilent, STATE_LABEL, store, draft } from './state.js';

const scrollMemo = new Map();

export function renderBoard(root, app) {
  for (const el of root.querySelectorAll('.col-body')) {
    scrollMemo.set(el.dataset.col, el.scrollTop);
  }
  clear(root);
  const cols = columns();
  for (const col of cols) {
    if (col.hideWhenEmpty && !col.cards.length) continue;
    root.appendChild(renderColumn(col, app));
  }
  for (const el of root.querySelectorAll('.col-body')) {
    const v = scrollMemo.get(el.dataset.col);
    if (v) el.scrollTop = v;
  }
}

function renderColumn(col, app) {
  const isDone = col.key === 'done';
  const open = !isDone || store.doneOpen;

  if (isDone && !open) {
    return h('section.col.col-done.is-rail', { 'data-col': 'done' },
      h('button.rail-btn', {
        type: 'button', title: 'show finished cards',
        onclick: () => { store.doneOpen = true; app.render(); },
      }, h('span.rail-label', 'Done'), col.cards.length ? h('span.col-count', String(col.cards.length)) : null));
  }

  const body = h('div.col-body', { 'data-col': col.key });

  if (isDone) {
    if (open) {
      if (!col.cards.length) body.appendChild(h('p.col-empty', 'Nothing shipped yet.'));
      for (const card of col.cards) body.appendChild(doneRow(card, app));
    }
  } else if (!col.cards.length) {
    body.appendChild(h('p.col-empty', emptyText(col.key)));
  } else {
    for (const card of col.cards) body.appendChild(renderCard(card, app));
  }

  const count = col.cards.length;
  // No count on Held — a held pile is not chrome.
  const showCount = col.key !== 'held' && count > 0;

  const head = h('div.col-head', { 'data-col': col.key },
    h('span.col-name', col.title),
    showCount ? h('span.col-count', String(count)) : null,
    isDone ? h('button.btn.ghost.tiny', {
      type: 'button',
      onclick: () => { store.doneOpen = false; app.render(); },
    }, 'Hide') : null,
  );

  return h('section.col', { 'data-col': col.key, class: `col col-${col.key}${isDone ? ' col-done' : ''}` }, head, body);
}

function emptyText(key) {
  return {
    queued: 'Nothing waiting.',
    in_progress: 'No agent is running.',
    needs_you: 'Nothing needs you.',
    blocked: 'Nothing is stuck.',
    ready: 'Nothing to review.',
    held: 'Nothing held.',
  }[key] || '—';
}

// ---- card face -----------------------------------------------------------

export function renderCard(card, app) {
  if (card.pendingSubmit) return pendingCard(card, app);
  const state = cardState(card);
  const silent = isSilent(card);
  const el = h('article.card', {
    'data-num': card.num,
    class: `card state-${state}${silent ? ' is-silent' : ''}${store.patches.has(card.num) ? ' is-optimistic' : ''}`,
    tabindex: '0',
    role: 'button',
    onclick: (e) => { if (!e.target.closest('.no-open')) app.openCard(card.num); },
    onkeydown: (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === el) { e.preventDefault(); app.openCard(card.num); }
    },
  });

  el.appendChild(h('div.card-head',
    h('span.card-num', '#' + card.num),
    card.pinned ? h('span.pin', { title: 'pinned' }, '★') : null,
    h('span.grow'),
    // Only batches get a face badge — a solo agent's name is just the card number.
    card.batch_id && card.agent_name
      ? h('span.badge.agent', { title: 'shared with the rest of this batch' }, '⛓ ' + shortAgent(card.agent_name))
      : null,
    card.bounce_count ? h('span.badge.bounce', { title: 'bounced ' + plural(card.bounce_count, 'time') },
      '↩ ' + card.bounce_count) : null,
    state === 'integrating'
      ? h('span.badge.merging', { title: 'the session is rebasing, gating and merging this branch' }, 'merging…')
      : null,
  ));

  el.appendChild(h('h3.card-title', card.title));

  if (state === 'needs_you' && card.question) {
    el.appendChild(questionBlock(card, app));
  }

  if (state === 'blocked' && card.reason) {
    el.appendChild(h('p.card-reason', h('span.reason-key', 'blocked:'), ' ' + card.reason));
  }
  if ((state === 'failed' || card.error) && state !== 'needs_you' && card.error) {
    el.appendChild(h('p.card-reason.is-error', h('span.reason-key', 'error:'), ' ' + firstLine(card.error, 120)));
  }
  if ((state === 'ready' || state === 'integrating') && card.evidence) {
    el.appendChild(evidenceTeaser(card, state));
  }

  const lastText = card.last_event ? app.eventText(card.last_event) : null;
  if (lastText && state !== 'needs_you') {
    el.appendChild(h('p.card-last', firstLine(lastText, 120)));
  }

  const foot = h('div.card-foot');
  foot.appendChild(h('span.state-age',
    h('span.state-dot'), STATE_LABEL[state] || state, ' ',
    timeEl(card.state_since || card.updated_at || card.created_at, { suffix: false })));
  if (state === 'queued' && card.queue_position != null) {
    foot.appendChild(h('span.qpos', 'next ' + ordinal(card.queue_position)));
  }
  if (card.long_running) foot.appendChild(h('span.foot-flag', 'long job'));
  if (silent) {
    foot.appendChild(h('span.foot-flag.is-silent', 'quiet ', timeEl(card.last_activity_at, { suffix: false })));
  } else if (card.last_activity_at && (state === 'in_progress' || state === 'triaging')) {
    foot.appendChild(h('span.foot-quiet', 'last word ', timeEl(card.last_activity_at, { suffix: false })));
  }
  el.appendChild(foot);
  return el;
}

function shortAgent(name) {
  return String(name).replace(/^sprint-/, '').replace(/^card-/, '#');
}

function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function questionBlock(card, app) {
  const q = card.question;
  const key = `answer:${card.num}`;
  const box = h('div.question.no-open');
  box.appendChild(h('p.question-text', q.text));
  if (q.options && q.options.length) {
    const opts = h('div.quick-replies');
    for (const opt of q.options) {
      opts.appendChild(h('button.btn.quick', {
        type: 'button',
        onclick: () => app.answer(card, q, opt.value),
      }, opt.label));
    }
    box.appendChild(opts);
  }
  const ta = h('textarea.answer-box', {
    id: `answer-${card.num}`,
    rows: '1',
    placeholder: q.options && q.options.length ? 'or answer in your own words…' : 'Answer…',
    oninput: (e) => { draft(key, e.target.value); autogrow(e.target); },
    onkeydown: (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
      else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    },
  });
  ta.value = draft(key);
  const btn = h('button.btn.primary.small', { type: 'button', onclick: () => send() }, 'Answer');
  function send() {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    draft(key, null);
    app.answer(card, q, text);
  }
  box.appendChild(h('div.answer-row', ta, btn));
  return box;
}

export function autogrow(ta, max = 160) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(max, ta.scrollHeight) + 'px';
}

function evidenceTeaser(card, state) {
  const p = card.evidence || {};
  const bits = [];
  if (p.test_result) bits.push(p.test_result);
  if (p.diffstat) bits.push(firstLine(p.diffstat, 40));
  const shots = Array.isArray(p.screenshots) ? p.screenshots.length : 0;
  if (shots) bits.push(plural(shots, 'shot'));
  const steps = Array.isArray(p.validate) ? p.validate.length : 0;
  return h('div.teaser', { class: state === 'integrating' ? 'teaser merging' : 'teaser' },
    p.claim ? h('p.teaser-claim', firstLine(p.claim, 140)) : null,
    bits.length ? h('p.teaser-bits', bits.join(' · ')) : null,
    state === 'integrating'
      ? h('p.teaser-cta', 'merging — waiting for the branch to land')
      : h('p.teaser-cta', steps ? `Check it yourself — ${plural(steps, 'step')} →` : 'Review evidence →'),
  );
}

function doneRow(card, app) {
  const state = cardState(card);
  return h('button.done-row', {
    type: 'button',
    class: `done-row state-${state}`,
    onclick: () => app.openCard(card.num),
  },
    h('span.done-mark', state === 'completed' ? '✓' : state === 'rejected' ? '✕' : '·'),
    h('span.card-num', '#' + card.num),
    h('span.done-title', card.title),
    h('span.done-age', age(card.updated_at)),
  );
}

function pendingCard(card, app) {
  return h('article.card.is-pending',
    h('div.card-head', h('span.card-num', '#…'), h('span.grow'),
      h('span.badge', card.error ? 'not sent' : 'sending')),
    h('h3.card-title', card.title || firstLine(card.text || '', 90) || 'New item'),
    card.images && card.images.length ? h('p.card-last', plural(card.images.length, 'image')) : null,
    card.error
      ? h('div.card-foot', h('span.foot-flag.is-silent', card.error),
        h('button.btn.tiny.no-open', { type: 'button', onclick: () => app.retrySubmit(card) }, 'Retry'))
      : h('div.card-foot', h('span.foot-quiet', 'sending…')),
  );
}
