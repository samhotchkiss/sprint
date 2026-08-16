// Sprint settings — the board's dispatch policy, in a small panel.
//
// Four things live here, and all four are the USER's call rather than the
// orchestrator's: which model a worker gets by default, whether workers run as
// Claude subagents or as a command driven in its own tmux window, what those
// executors are called, and how many run at once.
//
// The panel is deliberately quiet: a ghost link in the header (the same
// restraint as the other header links), a sheet the width of the Drop-work
// sheet, and no chrome anywhere else on the page. Settings are something you
// visit twice a month.
//
// The one thing it must say out loud is WHEN a change bites: the server stores
// policy and the session reads it at dispatch, so nothing already running
// changes under you. That sentence is in the panel, not in a doc.
import { h, clear, $ } from './util.js';
import { api, ApiError } from './api.js';

const POLICY_LABEL = {
  lowest_feasible: 'Lowest feasible',
  always_sonnet: 'Always sonnet',
  always_opus: 'Always opus',
};

const POLICY_HINT = {
  lowest_feasible: 'Sonnet by default; the session stamps opus on a card that genuinely needs it.',
  always_sonnet: 'Every card is dispatched with sonnet, whatever the session thinks.',
  always_opus: 'Every card is dispatched with opus. Slower and dearer — for a sprint of hard cards.',
};

let wrap = null;      // the sheet, built once on first open
let els = {};
let draft = null;     // what the panel is showing right now
let loaded = null;    // what the server last told us
let onSaved = null;

/** Wire the header link. Called once at boot. */
export function installSettings(btn, afterSave) {
  onSaved = afterSave || null;
  if (!btn) return;
  btn.addEventListener('click', () => openSettings());
}

export function settingsOpen() {
  return !!(wrap && !wrap.hidden);
}

export function closeSettings() {
  if (!wrap || wrap.hidden) return false;
  wrap.hidden = true;
  return true;
}

export async function openSettings() {
  build();
  wrap.hidden = false;
  setStatus('loading…');
  try {
    const res = await api.settings();
    loaded = res.settings;
    draft = clone(res.settings);
    setStatus('');
    paint();
  } catch (err) {
    setStatus(message(err, 'could not read the settings'), true);
  }
}

// ---- the sheet -----------------------------------------------------------

function build() {
  if (wrap) return;
  wrap = h('div.sheet-wrap', { id: 'settings-wrap', hidden: true });
  const form = h('form.sheet.settings-sheet', {
    onsubmit: (e) => { e.preventDefault(); save(); },
  });

  form.appendChild(h('div.settings-head',
    h('h2', 'Sprint settings'),
    h('p.settings-when',
      'These are dispatch policy. A change takes effect for the ',
      h('strong', 'next dispatch'),
      ' — cards already running keep the executor and model they started with.')));

  els.body = h('div.settings-body');
  form.appendChild(els.body);

  els.err = h('p.settings-err', { hidden: true });
  els.status = h('span.settings-status');
  form.appendChild(els.err);
  form.appendChild(h('div.settings-foot',
    els.status,
    h('span.grow'),
    h('button.btn.ghost', { type: 'button', onclick: () => closeSettings() }, 'Cancel'),
    h('button.btn.send', { type: 'submit' }, 'Save')));

  wrap.appendChild(form);
  wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) closeSettings(); });
  document.body.appendChild(wrap);
}

function paint() {
  if (!draft) return;
  const w = draft.worker;
  clear(els.body);

  // 1. model policy
  const seg = h('div.seg.settings-seg', { role: 'group', 'aria-label': 'model policy' });
  for (const key of ['lowest_feasible', 'always_sonnet', 'always_opus']) {
    seg.appendChild(h('button.seg-btn', {
      type: 'button',
      class: w.model_policy === key ? 'seg-btn is-on' : 'seg-btn',
      'aria-pressed': w.model_policy === key ? 'true' : 'false',
      onclick: () => { w.model_policy = key; paint(); },
    }, POLICY_LABEL[key]));
  }
  els.body.appendChild(field('Model', seg, POLICY_HINT[w.model_policy]));

  // 2. default executor
  const names = ['subagent'].concat(Object.keys(w.executors || {}).filter((n) => n !== 'subagent'));
  const select = h('select.settings-select', {
    id: 'settings-default-executor',
    onchange: (e) => { w.default_executor = e.target.value; paint(); },
  });
  for (const name of names) {
    const kind = name === 'subagent' ? 'subagent' : ((w.executors[name] || {}).kind || 'subagent');
    select.appendChild(h('option', {
      value: name,
      selected: w.default_executor === name,
    }, name === kind ? name : `${name} · ${kind}`));
  }
  els.body.appendChild(field('Default executor', select,
    'What a card runs on when nothing on the card says otherwise. A tmux executor '
    + 'gets its own tmux window and is driven with tmux-send; a subagent is a Claude worker.'));

  // 3. concurrency
  const conc = h('input.settings-num', {
    id: 'settings-concurrency',
    type: 'number', min: '1', max: '20', step: '1',
    value: String(w.concurrency),
    oninput: (e) => { w.concurrency = Number(e.target.value); },
  });
  els.body.appendChild(field('Agents at once', conc,
    'A batch counts as one, however many cards it carries.'));

  // 4. the executors themselves
  const ta = h('textarea.settings-json', {
    id: 'settings-executors',
    rows: 6,
    spellcheck: false,
    value: JSON.stringify(w.executors || {}, null, 2),
  });
  els.body.appendChild(field('Executors', ta,
    'name → definition. {"kind":"subagent"} for a Claude worker; '
    + '{"kind":"tmux","command":"grok","session":"sprint-workers"} for a CLI agent '
    + 'the session drives in its own tmux window.'));
}

function field(label, control, hint) {
  const id = control.id || null;
  return h('div.settings-field',
    h('label.settings-label', id ? { for: id } : null, label),
    control,
    hint ? h('p.settings-hint', hint) : null);
}

// ---- save ----------------------------------------------------------------

async function save() {
  if (!draft) return;
  const w = draft.worker;
  let executors;
  try {
    const raw = ($('#settings-executors') || {}).value || '{}';
    executors = JSON.parse(raw);
  } catch (err) {
    // A JSON typo is the one error we can name better than the server can,
    // because we know where the caret is.
    return setStatus('Executors is not valid JSON — check the brackets and commas.', true);
  }
  if (!executors || typeof executors !== 'object' || Array.isArray(executors)) {
    return setStatus('Executors must be an object of name → definition.', true);
  }
  const patch = {
    worker: {
      model_policy: w.model_policy,
      default_executor: w.default_executor,
      concurrency: Number(w.concurrency),
      executors,
    },
  };
  setStatus('saving…');
  try {
    const res = await api.saveSettings(patch);
    loaded = res.settings;
    draft = clone(res.settings);
    setStatus('');
    closeSettings();
    if (onSaved) onSaved(res.settings);
  } catch (err) {
    // The server names the exact key it choked on — say that, not "invalid".
    setStatus(message(err, 'the board would not take that'), true);
    paint();
  }
  return undefined;
}

function message(err, fallback) {
  if (err instanceof ApiError) {
    const field_ = err.body && err.body.field;
    const msg = (err.body && (err.body.message || err.body.error)) || fallback;
    return field_ ? `${field_}: ${msg}` : msg;
  }
  return fallback;
}

function setStatus(text, bad) {
  if (!els.err) return;
  els.err.hidden = !(bad && text);
  if (bad && text) els.err.textContent = text;
  els.status.textContent = bad ? '' : (text || '');
}

function clone(obj) { return JSON.parse(JSON.stringify(obj)); }

// ---- the card face's executor tag ----------------------------------------

/**
 * "grok · tmux" — but only when this card is NOT running on the board's
 * defaults. A board where everything runs the same way carries no executor
 * chrome at all, which is the whole point: the tag means "this one is
 * different", and a tag on every card would mean nothing.
 */
export function executorTag(card, { compact = false } = {}) {
  const d = card && card.dispatch;
  if (!d || d.is_default) return null;
  const bits = [];
  if (card.executor) {
    bits.push(d.executor);
    if (d.kind && d.kind !== 'subagent' && d.kind !== d.executor) bits.push(d.kind);
  }
  // A card face has one line for this and the agent's name has to fit on it
  // too, so the model drops off there — "grok · tmux" is what makes this card
  // different, and the full "grok · tmux · grok-4" is on the tag's tooltip and
  // in the rail head. A card whose ONLY difference is the model still says so.
  if (card.model && (!compact || !bits.length)) bits.push(d.model);
  if (!bits.length) return null;
  return h('span.exec-tag', {
    title: `dispatched as ${d.executor} (${d.kind}) with ${d.model}`,
  }, bits.join(' · '));
}
