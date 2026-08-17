// Sprint settings — the board's dispatch policy, in a small panel.
//
// Everything here is the USER's call rather than the orchestrator's: what this
// sprint is CALLED, which model a worker gets by default, whether workers run
// as Claude subagents or as a command driven in its own tmux window, what those
// executors are called, how many run at once, whether an agent pre-reads a card
// that says it is done, and the standing instructions every brief carries.
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
import { modelTag } from './state.js';

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

// ---- presets -------------------------------------------------------------
//
// User verbatim: "make it easy to add executors." Adding one used to mean
// hand-writing a JSON object into a textarea, which is only easy if you
// already know the four field names and which two of them a tmux worker
// needs. So the common cases are buttons, and the buttons fill the form in.
//
// `spec` is EXACTLY what gets stored and exactly what the session reads at
// dispatch time (SKILL.md's tmux section: `{"kind":"tmux","command":"grok",
// "session":"sprint-workers"}`). No field is renamed on the way in or out —
// the test suite parses this block straight out of this file, PUTs it, GETs
// it back and resolves a dispatch label from it, so a preset that drifted
// from what the server accepts fails the build rather than 2am.
//
// Kept as strict JSON between the markers below so that test can read it.
/* PRESETS_JSON_START */
const PRESETS = {
  "grok": {
    "label": "grok via tmux",
    "blurb": "The grok CLI, driven in its own tmux window.",
    "name": "grok",
    "spec": {"kind": "tmux", "command": "grok", "session": "sprint-workers", "model": "grok-4"}
  },
  "codex": {
    "label": "codex via tmux",
    "blurb": "The codex CLI, driven in its own tmux window.",
    "name": "codex",
    "spec": {"kind": "tmux", "command": "codex", "session": "sprint-workers"}
  },
  "claude": {
    "label": "claude subagent",
    "blurb": "A Claude worker inside this session. No tmux, nothing to install.",
    "name": "claude",
    "spec": {"kind": "subagent"}
  },
  "cli": {
    "label": "another CLI via tmux",
    "blurb": "Any command-line agent. Give it a name and the command that starts it.",
    "name": "",
    "spec": {"kind": "tmux", "command": "", "session": "sprint-workers"}
  }
};
/* PRESETS_JSON_END */

const PRESET_ORDER = ['grok', 'codex', 'claude', 'cli'];

// What each field IS, in words, next to the field itself — the whole point of
// this card. Nobody should have to read SKILL.md to add an executor.
const FIELD_HINT = {
  name: 'What you call it on a card — the session dispatches with this name.'
    + ' Letters, digits, dots, dashes and underscores.',
  kind: 'A subagent is a Claude worker inside this session. A tmux worker is a'
    + ' command the session starts in its own tmux window and types the brief into.',
  command: 'The command that starts the agent in that window, exactly as you would'
    + ' type it yourself — "grok", "codex", "aider --model x".',
  session: 'Which tmux session its windows live in. One session is shared by every'
    + ' tmux worker on this board; sprint-workers is the usual answer.',
  model: 'Optional. The model to stamp on a card dispatched this way ("grok-4").'
    + ' Leave it blank to use the board\'s model policy above.',
  note: 'Optional. A reminder to yourself — it is stored and shown nowhere else.',
};

// A valid executor name, same rule the server enforces (clean_executor_name).
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
// The board's built-in name for "an ordinary Claude subagent". An executor of
// that name would read as the builtin everywhere and be a different thing.
const RESERVED_NAMES = ['subagent'];

let wrap = null;      // the sheet, built once on first open
let els = {};
let draft = null;     // what the panel is showing right now
let loaded = null;    // what the server last told us
let onSaved = null;
// The executor editor: null when the list is just a list, otherwise the row
// being added or edited. `original` is the name it had before (null = new), so
// a rename is a rename rather than a second executor.
let editing = null;
// Which row is asking "remove this?" — an inline confirmation, never the
// browser's own dialog (modal, unstyled, and always in the wrong skin).
let removing = null;
// Power users keep the raw object; everyone else never sees it.
let jsonMode = false;
// The sprint's NAME. Not part of the settings document — it is the open
// sprint's title on the server — but this is where a rename belongs, because
// it is the user's call and this is the panel of the user's calls.
let sprintName = { value: '', saved: '', max: 60, fallback: '' };
// ...and the SESSION's own name — the one it gave itself ("Chuck"). A
// different thing from the sprint's name and deliberately next to it, so the
// panel reads "this board is called X, the colleague running it is called Y".
let agentName = { value: '', saved: '', max: 24 };
// Standing instructions: the paragraph every agent this board sends is told,
// on top of the card it was given. Same live-in-a-variable shape as the two
// names above — typing writes here and repaints nothing (#46).
let standing = { value: '', saved: '', max: 4000 };

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
  // A sheet you re-open is a sheet you start over in: a half-typed executor
  // from last time would be a change you never asked for.
  editing = null;
  removing = null;
  jsonMode = false;
  setStatus('loading…');
  try {
    const res = await api.settings();
    loaded = res.settings;
    draft = clone(res.settings);
    sprintName = {
      value: res.name || '',
      saved: res.name || '',
      max: res.name_max || 60,
      fallback: res.name_default || '',
    };
    agentName = {
      value: res.agent_name || '',
      saved: res.agent_name || '',
      max: res.agent_name_max || 24,
    };
    standing = {
      value: res.special_instructions || '',
      saved: res.special_instructions || '',
      max: res.special_instructions_max || 4000,
    };
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

/**
 * Where the caret was, so a repaint can put it back (#46).
 *
 * This sheet rebuilds its whole body on any structural change — a preset, the
 * kind toggle, adding an executor. Every free-text field in it is therefore a
 * field you can be mid-word in when something else on the page repaints, and
 * the standing-instructions box is the worst case: it is a PARAGRAPH, so
 * losing the caret in it means hunting for where you were in four lines of
 * your own prose. The text itself was never at risk (it lives in a variable);
 * the caret was.
 */
function caretNow() {
  const el = document.activeElement;
  if (!el || !el.id || !els.body || !els.body.contains(el)) return null;
  if (el.selectionStart == null) return { id: el.id, start: null, end: null };
  return { id: el.id, start: el.selectionStart, end: el.selectionEnd };
}

function caretBack(was) {
  if (!was) return;
  const el = els.body && els.body.querySelector('#' + CSS.escape(was.id));
  if (!el) return;
  el.focus({ preventScroll: true });
  if (was.start != null && el.setSelectionRange) {
    try { el.setSelectionRange(was.start, was.end); } catch { /* not selectable */ }
  }
}

function paint() {
  if (!draft) return;
  const w = draft.worker;
  // The body scrolls, and a repaint that threw its scroll position away would
  // yank you back to the top of the sheet every time you touched a preset.
  const wasAt = els.body ? els.body.scrollTop : 0;
  const caret = caretNow();
  clear(els.body);

  // 0. the sprint's name — what the header, the switcher and the hub call it.
  const nameInput = h('input.settings-text', {
    id: 'settings-name',
    type: 'text',
    maxlength: String(sprintName.max),
    value: sprintName.value,
    placeholder: sprintName.fallback || 'this sprint',
    spellcheck: false,
    oninput: (e) => { sprintName.value = e.target.value; },
  });
  els.body.appendChild(field('Sprint name', nameInput,
    'What this sprint is called, everywhere it appears: the title above, the '
    + 'switcher, and the hub. Name it after the work, not the folder. Leave it '
    + `as “${sprintName.fallback || 'the folder name'}” and it stays the folder name.`));

  // 0b. who is running it. Normally the session sets this itself at launch;
  // this is the door for changing it, and for taking it back (clear the box).
  const agentInput = h('input.settings-text', {
    id: 'settings-agent-name',
    type: 'text',
    maxlength: String(agentName.max),
    value: agentName.value,
    placeholder: 'Session',
    spellcheck: false,
    oninput: (e) => { agentName.value = e.target.value; },
  });
  els.body.appendChild(field('Session name', agentInput,
    'What the session running this board calls itself — a first name, like '
    + '“Chuck”. It signs every line the session writes, in the sidebar and in '
    + 'card threads. The session picks one for itself at launch and keeps it '
    + 'across restarts; empty it and its lines go back to “Session”.'));

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
  els.body.appendChild(executorsField(w));

  // 5. who pre-reads a card that says it is done
  els.body.appendChild(reviewerField());

  // 6. what every agent is told on top of its card
  els.body.appendChild(standingField());

  els.body.scrollTop = wasAt;
  caretBack(caret);
}

// ---- standing instructions -----------------------------------------------

/**
 * The paragraph that rides along on every brief this board sends — worker and
 * reviewer alike. It is policy, not a card: the card says what to do, this
 * says how work is done here.
 *
 * It sits LAST because it is the field you visit least and read longest, and
 * because a four-line box at the top of a sheet pushes everything else below
 * the fold on a phone.
 */
function standingField() {
  const ta = h('textarea.settings-standing', {
    id: 'settings-standing',
    rows: 5,
    maxlength: String(standing.max),
    value: standing.value,
    spellcheck: true,
    placeholder: 'e.g. All UI work must be checked at the Fold width (980px).',
    // No repaint on input — this node has your caret in it, and it is the one
    // field on this sheet you write whole sentences into (#46).
    oninput: (e) => { standing.value = e.target.value; },
  });
  const box = field('Standing instructions', ta,
    'Included in every agent’s brief, worker and reviewer alike, under a '
    + 'heading of its own so nobody mistakes it for the card. Use it for how '
    + 'work is done on this board — the gate to run, a width to check, a rule '
    + 'you are tired of repeating. It applies from the next dispatch; agents '
    + 'already running keep the brief they started with.');
  box.classList.add('settings-standing-field');
  return box;
}

// ---- reviewer -------------------------------------------------------------

/**
 * Who reads a card that reaches Awaiting review BEFORE you do.
 *
 * The one sentence that must be on screen and not in a doc: it never approves.
 * Every verdict stays the user's — the reviewer reads the packet against the
 * card and writes down what it found.
 */
function reviewerField() {
  const r = draft.reviewer || (draft.reviewer = { enabled: false, executor: 'subagent', model: '' });
  const w = draft.worker;
  const box = h('div.settings-field.settings-reviewer');
  box.appendChild(h('div.settings-label-row',
    h('span.settings-label', 'Reviewer'),
    h('span.grow'),
    h('button.btn.tiny', {
      type: 'button',
      class: r.enabled ? 'btn tiny send' : 'btn tiny ghost',
      'aria-pressed': r.enabled ? 'true' : 'false',
      onclick: () => { r.enabled = !r.enabled; paint(); },
    }, r.enabled ? 'On' : 'Off')));

  if (r.enabled) {
    const names = ['subagent'].concat(
      Object.keys(w.executors || {}).filter((n) => n !== 'subagent'));
    const select = h('select.settings-select', {
      id: 'settings-reviewer-executor',
      onchange: (e) => { r.executor = e.target.value; paint(); },
    });
    for (const name of names) {
      const kind = name === 'subagent' ? 'subagent' : ((w.executors[name] || {}).kind || 'subagent');
      select.appendChild(h('option', {
        value: name, selected: r.executor === name,
      }, name === kind ? name : `${name} · ${kind}`));
    }
    box.appendChild(h('div.exec-field',
      h('label.exec-label', { for: 'settings-reviewer-executor' }, 'Runs as'),
      select,
      h('p.settings-hint', 'The same choices a worker has. A different agent '
        + 'from the one that did the work is the point — it reads with fresh eyes.')));

    const model = h('input.settings-text', {
      id: 'settings-reviewer-model',
      type: 'text', value: r.model || '', placeholder: 'opus',
      spellcheck: false, autocomplete: 'off', autocapitalize: 'off',
      oninput: (e) => { r.model = e.target.value; },
    });
    box.appendChild(h('div.exec-field',
      h('label.exec-label', { for: 'settings-reviewer-model' }, 'Model (optional)'),
      model,
      h('p.settings-hint', 'Leave it blank and it uses whatever that executor '
        + 'and the model policy above work out to.')));
  }

  box.appendChild(h('p.settings-hint.settings-never',
    r.enabled
      ? 'When a card reaches Awaiting review, this agent reads the evidence '
        + 'against the card first and writes what it found on the card. It '
        + 'never approves and never closes anything — every card still comes '
        + 'to you.'
      : 'Off: a card that says it is done comes straight to you. Turn this on '
        + 'and an agent reads the evidence first and leaves you its findings — '
        + 'it never approves, so you still see every card.'));
  return box;
}

// ---- executors -----------------------------------------------------------

/**
 * The list, the editor, and the escape hatch. Which of the three you see is a
 * small state machine: `jsonMode` swaps the whole thing for the raw object,
 * `editing` opens the form over the list, `removing` turns one row into its
 * own confirmation.
 */
function executorsField(w) {
  const box = h('div.settings-field.settings-execs');
  box.appendChild(h('div.settings-label-row',
    h('span.settings-label', 'Executors'),
    h('span.grow'),
    // Add sits WITH the label, not under the list: the sheet's body scrolls,
    // and a list of six executors would push the one button this whole panel
    // exists for below the fold.
    (jsonMode || editing) ? null : h('button.linkish.exec-add', {
      type: 'button',
      onclick: () => { removing = null; startAdd(); },
    }, 'Add executor'),
    h('button.linkish', {
      type: 'button',
      onclick: () => {
        // Leaving the form open behind a mode switch is how you lose a
        // half-typed executor without being told.
        if (!jsonMode) { editing = null; removing = null; }
        jsonMode = !jsonMode;
        paint();
      },
    }, jsonMode ? 'Back to the list' : 'Edit as JSON')));

  if (jsonMode) {
    const ta = h('textarea.settings-json', {
      id: 'settings-executors',
      rows: 8,
      spellcheck: false,
      value: JSON.stringify(w.executors || {}, null, 2),
      // Typed straight into the draft, so switching back to the list shows
      // what you just wrote rather than what you opened the sheet with.
      oninput: (e) => { readJson(e.target.value); },
    });
    box.appendChild(ta);
    box.appendChild(h('p.settings-hint',
      'name → definition. {"kind":"subagent"} for a Claude worker; '
      + '{"kind":"tmux","command":"grok","session":"sprint-workers"} for a CLI agent '
      + 'the session drives in its own tmux window.'));
    return box;
  }

  const names = Object.keys(w.executors || {}).sort();
  const list = h('div.exec-list');
  if (!names.length) {
    list.appendChild(h('p.exec-empty',
      'No executors yet — every card runs as an ordinary Claude subagent.'));
  }
  for (const name of names) list.appendChild(execRow(name, w.executors[name], w));
  box.appendChild(list);

  if (editing) box.appendChild(execEditor(w));
  else {
    box.appendChild(h('p.settings-hint',
      'An executor is a way to run a worker. Add one and you can send any card '
      + 'to it — the rest keep running as Claude subagents.'));
  }
  return box;
}

/** One stored executor: what it is, in words, plus Edit and Remove. */
function execRow(name, spec, w) {
  const row = h('div.exec-row', { 'data-exec': name });
  const isDefault = w.default_executor === name;
  row.appendChild(h('div.exec-main',
    h('span.exec-name', name),
    h('span.exec-what', execSentence(spec)),
    spec && spec.note ? h('span.exec-note', spec.note) : null));

  if (removing === name) {
    // Inline, in the row it is about, in this skin — the browser's own dialog
    // is a different application appearing on top of yours.
    row.classList.add('is-removing');
    row.appendChild(h('div.exec-confirm',
      h('span.exec-confirm-q', isDefault
        ? `Remove "${name}"? It is the default, so the board goes back to plain subagents.`
        : `Remove "${name}"? Cards already running keep it.`),
      h('button.btn.tiny.ghost', {
        type: 'button', onclick: () => { removing = null; paint(); },
      }, 'Keep it'),
      h('button.btn.tiny.danger', {
        type: 'button', onclick: () => removeExec(name),
      }, 'Remove')));
    return row;
  }

  row.appendChild(h('div.exec-acts',
    isDefault ? h('span.exec-default', 'default') : null,
    h('button.linkish', {
      type: 'button', onclick: () => { removing = null; startEdit(name, spec); },
    }, 'Edit'),
    h('button.linkish.danger', {
      type: 'button', onclick: () => { editing = null; removing = name; paint(); },
    }, 'Remove')));
  return row;
}

/** "a CLI agent in its own tmux window — runs `grok` in session sprint-workers" */
function execSentence(spec) {
  const s = spec || {};
  if (s.kind !== 'tmux') {
    return 'a Claude subagent' + (s.model ? ` on ${s.model}` : '');
  }
  return `runs \`${s.command || '?'}\` in tmux session ${s.session || 'sprint-workers'}`
    + (s.model ? ` on ${s.model}` : '');
}

function startAdd() {
  editing = {
    original: null, preset: null, name: '',
    kind: 'subagent', command: '', session: 'sprint-workers', model: '', note: '',
    err: null, errField: null,
  };
  paint();
  showEditor();
}

/**
 * The sheet's body scrolls, so a form that opened under a long list would open
 * off-screen. Bring it into view, and put the caret where the work starts.
 */
function showEditor() {
  const box = els.body && els.body.querySelector('.exec-editor');
  if (!box) return;
  box.scrollIntoView({ block: 'nearest' });
}

function startEdit(name, spec) {
  const s = spec || {};
  editing = {
    original: name, preset: null, name,
    kind: s.kind || 'subagent',
    command: s.command || '',
    session: s.session || 'sprint-workers',
    model: s.model || '',
    note: s.note || '',
    err: null, errField: null,
  };
  paint();
  showEditor();
}

function applyPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  const spec = p.spec || {};
  editing.preset = key;
  // A preset on a NEW executor names it too; on an existing one it only
  // changes how it runs, because the name is what cards already point at.
  if (editing.original == null) editing.name = p.name || '';
  editing.kind = spec.kind || 'subagent';
  editing.command = spec.command || '';
  editing.session = spec.session || 'sprint-workers';
  editing.model = spec.model || '';
  editing.err = null;
  editing.errField = null;
  paint();
}

/**
 * The form. Every field is labelled in plain words and says what it means; the
 * two that only exist for a tmux worker appear only for a tmux worker.
 *
 * Nothing in here repaints while you type (#46): an input's `oninput` writes to
 * `editing` and stops there. Only a structural change — a preset, the kind, an
 * error, save/cancel — repaints, and those are all clicks.
 */
function execEditor(w) {
  const e = editing;
  const box = h('div.exec-editor');
  box.appendChild(h('h3.exec-editor-head',
    e.original == null ? 'Add an executor' : `Edit "${e.original}"`));

  // 1. presets
  const row = h('div.exec-presets');
  for (const key of PRESET_ORDER) {
    const p = PRESETS[key];
    row.appendChild(h('button.exec-preset', {
      type: 'button',
      class: e.preset === key ? 'exec-preset is-on' : 'exec-preset',
      'aria-pressed': e.preset === key ? 'true' : 'false',
      onclick: () => applyPreset(key),
    }, h('span.exec-preset-name', p.label), h('span.exec-preset-blurb', p.blurb)));
  }
  box.appendChild(h('div.exec-field',
    h('label.exec-label', 'Start from'),
    row,
    h('p.settings-hint', 'Pick the closest one — it fills the fields in, and you '
      + 'can change any of them.')));

  // 2. name
  box.appendChild(textField(e, 'name', 'Name', 'grok', FIELD_HINT.name));

  // 3. kind
  const seg = h('div.seg.exec-seg', { role: 'group', 'aria-label': 'how it runs' });
  for (const [kind, label] of [['subagent', 'Claude subagent'], ['tmux', 'CLI in a tmux window']]) {
    seg.appendChild(h('button.seg-btn', {
      type: 'button',
      class: e.kind === kind ? 'seg-btn is-on' : 'seg-btn',
      'aria-pressed': e.kind === kind ? 'true' : 'false',
      onclick: () => { e.kind = kind; e.err = null; paint(); },
    }, label));
  }
  box.appendChild(h('div.exec-field',
    h('label.exec-label', 'How it runs'), seg, h('p.settings-hint', FIELD_HINT.kind)));

  // 4. the two tmux-only fields, plus what the window will be called
  if (e.kind === 'tmux') {
    box.appendChild(textField(e, 'command', 'Command', 'grok', FIELD_HINT.command));
    box.appendChild(textField(e, 'session', 'tmux session', 'sprint-workers', FIELD_HINT.session));
    // Not a field: the window name is derived from the card, every time, in
    // SKILL.md's dispatch block. Showing it stops anyone looking for the
    // setting that isn't there.
    box.appendChild(h('p.exec-derived',
      'Each card gets its own window in that session, named after the card: ',
      h('code', 'sprint-card-42'), '.'));
  }

  // 5. optional
  box.appendChild(textField(e, 'model', 'Model (optional)', 'grok-4', FIELD_HINT.model));
  box.appendChild(textField(e, 'note', 'Note to self (optional)', '', FIELD_HINT.note));

  if (e.err) box.appendChild(h('p.exec-err', e.err));
  box.appendChild(h('div.exec-editor-foot',
    h('button.btn.tiny.ghost', {
      type: 'button', onclick: () => { editing = null; paint(); },
    }, 'Cancel'),
    h('button.btn.tiny.send', {
      type: 'button', onclick: () => commitExec(w),
    }, e.original == null ? 'Add it' : 'Save changes')));
  return box;
}

/** One labelled text input that writes straight into the editor's draft. */
function textField(e, key, label, placeholder, hint) {
  const id = 'exec-' + key;
  const input = h('input.settings-text', {
    id, type: 'text', value: e[key] || '', placeholder, spellcheck: false,
    autocomplete: 'off', autocapitalize: 'off',
    class: e.errField === key ? 'settings-text is-bad' : 'settings-text',
    // No repaint on input: this node has your caret in it.
    oninput: (ev) => { e[key] = ev.target.value; },
  });
  return h('div.exec-field',
    h('label.exec-label', { for: id }, label),
    input,
    h('p.settings-hint', hint));
}

/**
 * Validate, then write into the same `worker.executors` map the JSON textarea
 * writes into and `PUT /api/settings` sends. The server checks all of this
 * again — this is here so the answer arrives before the round trip, next to
 * the field it is about.
 */
function commitExec(w) {
  const e = editing;
  const name = (e.name || '').trim();
  const fail = (field, msg) => { e.errField = field; e.err = msg; paint(); return false; };

  if (!name) return fail('name', 'Give it a name — that is what you dispatch a card to.');
  if (!NAME_RE.test(name)) {
    return fail('name', 'A name is 1–40 characters: letters, digits, dots, dashes '
      + 'and underscores, starting with a letter or digit.');
  }
  if (RESERVED_NAMES.includes(name)) {
    return fail('name', `"${name}" is the board's own word for a plain Claude worker — pick another name.`);
  }
  const taken = Object.keys(w.executors || {});
  if (name !== e.original && taken.includes(name)) {
    return fail('name', `There is already an executor called "${name}".`);
  }
  const command = (e.command || '').trim();
  if (e.kind === 'tmux' && !command) {
    return fail('command', 'A tmux worker needs a command to start in the window — "grok", say.');
  }
  const session = (e.session || '').trim();
  if (e.kind === 'tmux' && !session) {
    return fail('session', 'A tmux worker needs a session to put its windows in.');
  }

  const spec = { kind: e.kind };
  if (e.kind === 'tmux') { spec.command = command; spec.session = session; }
  if ((e.model || '').trim()) spec.model = e.model.trim();
  if ((e.note || '').trim()) spec.note = e.note.trim();

  const next = { ...(w.executors || {}) };
  if (e.original != null && e.original !== name) {
    delete next[e.original];
    // A rename moves the default with it, otherwise saving would fail on a
    // default pointing at a name that no longer exists. Same for the reviewer.
    if (w.default_executor === e.original) w.default_executor = name;
    if (draft.reviewer && draft.reviewer.executor === e.original) {
      draft.reviewer.executor = name;
    }
  }
  next[name] = spec;
  w.executors = next;
  editing = null;
  setStatus('');
  paint();
  return true;
}

function removeExec(name) {
  const w = draft.worker;
  const next = { ...(w.executors || {}) };
  delete next[name];
  w.executors = next;
  if (w.default_executor === name) w.default_executor = 'subagent';
  // Same for the reviewer: an executor nobody declares any more is a save the
  // server would refuse, and refusing it AFTER you removed the row is a dead
  // end you cannot get out of from inside the sheet.
  if (draft.reviewer && draft.reviewer.executor === name) {
    draft.reviewer.executor = 'subagent';
  }
  removing = null;
  paint();
}

/** The JSON textarea, parsed into the draft as you type. Bad JSON is left for
 *  `save` to complain about — nagging on every keystroke of a half-typed brace
 *  is noise. */
function readJson(raw) {
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      draft.worker.executors = parsed;
    }
  } catch { /* mid-edit; save() is the gate */ }
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
  // A form still open is a change you have not finished making. Saving around
  // it would silently drop it, so it is committed (and validated) first.
  if (editing && !commitExec(w)) return undefined;
  let executors = w.executors || {};
  if (jsonMode) {
    try {
      const raw = ($('#settings-executors') || {}).value || '{}';
      executors = JSON.parse(raw);
    } catch (err) {
      // A JSON typo is the one error we can name better than the server can,
      // because we know where the caret is.
      return setStatus('Executors is not valid JSON — check the brackets and commas.', true);
    }
  }
  if (!executors || typeof executors !== 'object' || Array.isArray(executors)) {
    return setStatus('Executors must be an object of name → definition.', true);
  }
  const wanted = (sprintName.value || '').trim();
  const rev = draft.reviewer || { enabled: false, executor: 'subagent', model: '' };
  const patch = {
    worker: {
      model_policy: w.model_policy,
      default_executor: w.default_executor,
      concurrency: Number(w.concurrency),
      executors,
    },
    reviewer: {
      enabled: !!rev.enabled,
      executor: rev.executor || 'subagent',
      model: (rev.model || '').trim(),
    },
  };
  // Standing instructions are sent whenever they differ, blank included: "" is
  // how you take them back off, the same way the session's name works.
  if (standing.value !== standing.saved) {
    patch.special_instructions = standing.value;
  }
  // Only send a name when it actually changed: a rename writes an event to the
  // board, and saving the model policy is not a rename.
  if (wanted && wanted !== sprintName.saved) patch.name = wanted;
  // The session's name, unlike the sprint's, CAN be emptied: "" is how you take
  // it back, so this one is sent whenever it differs, blank included.
  const wantedAgent = (agentName.value || '').trim();
  if (wantedAgent !== agentName.saved) patch.agent_name = wantedAgent;
  setStatus('saving…');
  try {
    const res = await api.saveSettings(patch);
    loaded = res.settings;
    draft = clone(res.settings);
    sprintName.value = res.name || wanted;
    sprintName.saved = sprintName.value;
    agentName.value = typeof res.agent_name === 'string' ? res.agent_name : wantedAgent;
    agentName.saved = agentName.value;
    standing.value = typeof res.settings.special_instructions === 'string'
      ? res.settings.special_instructions : standing.value;
    standing.saved = standing.value;
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
  //
  // `modelTag` is the arbiter of "worth saying" (#41): a card dispatched
  // explicitly ON the sprint default is not an exception, so it gets no badge.
  const model = modelTag(card);
  if (model && (!compact || !bits.length)) bits.push(d.model);
  if (!bits.length) return null;
  return h('span.exec-tag', {
    title: `dispatched as ${d.executor} (${d.kind}) with ${d.model}`,
  }, bits.join(' · '));
}
