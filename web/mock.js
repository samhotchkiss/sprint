// Offline demo data. Loaded ONLY when the page is opened with ?mock=1 — app.js
// dynamically imports this module behind that flag, so production never fetches it.
//   ?mock=1            representative board
//   &offline=1         session reported offline (banner)
//   &live=1            drip a few scripted events (chime + badge + reconciliation)
//   &theme=dark|light  force a theme for verification
// It stubs window.fetch (for /api/* only) and window.EventSource.

const now = Date.now();
const iso = (msAgo) => new Date(now - msAgo).toISOString();
const MIN = 60000, HOUR = 3600000;

function shot(title, dark, accent = '#3a6ea5') {
  const bg = dark ? '#14171b' : '#ffffff';
  const bar = dark ? '#1d2126' : '#f1f1ee';
  const line = dark ? '#2b3138' : '#e4e4de';
  const ink = dark ? '#e6e8ea' : '#22262b';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">
<rect width="640" height="400" fill="${bg}"/>
<rect width="640" height="44" fill="${bar}"/><rect y="43" width="640" height="1" fill="${line}"/>
<circle cx="24" cy="22" r="7" fill="${accent}"/>
<rect x="42" y="16" width="120" height="12" rx="4" fill="${ink}" opacity=".8"/>
<rect x="0" y="44" width="170" height="356" fill="${bar}"/><rect x="169" y="44" width="1" height="356" fill="${line}"/>
${[0, 1, 2, 3, 4].map((i) => `<rect x="18" y="${72 + i * 34}" width="${120 - i * 12}" height="10" rx="4" fill="${ink}" opacity="${0.5 - i * 0.06}"/>`).join('')}
<rect x="196" y="72" width="300" height="18" rx="5" fill="${ink}" opacity=".85"/>
<rect x="196" y="104" width="410" height="10" rx="4" fill="${ink}" opacity=".35"/>
<rect x="196" y="122" width="380" height="10" rx="4" fill="${ink}" opacity=".35"/>
<rect x="196" y="160" width="410" height="94" rx="10" fill="none" stroke="${line}" stroke-width="2"/>
<rect x="214" y="182" width="150" height="12" rx="4" fill="${ink}" opacity=".6"/>
<rect x="214" y="206" width="240" height="10" rx="4" fill="${ink}" opacity=".3"/>
<rect x="214" y="226" width="96" height="14" rx="7" fill="${accent}"/>
<rect x="196" y="278" width="410" height="76" rx="10" fill="${accent}" opacity=".08"/>
<text x="212" y="312" font-family="ui-sans-serif,system-ui" font-size="15" fill="${ink}" opacity=".85">${title}</text>
<text x="212" y="334" font-family="ui-monospace,monospace" font-size="11" fill="${ink}" opacity=".5">${dark ? 'dark' : 'light'} · 1440×900</text>
</svg>`;
  return 'data:image/svg+xml,' + encodeURIComponent(svg);
}

const SHOTS = {
  toolbarLight: { url: shot('Toolbar contrast — after', false), caption: 'toolbar · light' },
  toolbarDark: { url: shot('Toolbar contrast — after', true), caption: 'toolbar · dark' },
  emptyLight: { url: shot('Empty state — after', false, '#2b7a52'), caption: 'empty state · light' },
  emptyDark: { url: shot('Empty state — after', true, '#6cc490'), caption: 'empty state · dark' },
  filterLight: { url: shot('Filter row — after', false, '#5f4aa0'), caption: 'filters · light' },
};

let seq = 508;
const nextSeq = () => ++seq;

const state = {
  sprint: { id: 3, title: 'Board polish + billing bugs', opened_at: iso(5 * HOUR), closed_at: null, hold_mode: false },
  sessionOnline: true,
  cards: [],
  timelines: {},
  evidence: {},
  sidebar: [],
  queue: [],       // undelivered events
};

function card(c) {
  const full = {
    body: c.title, batch_id: null, agent_name: null, worktree: null, branch: null,
    bounce_count: 0, pinned: 0, dup_of: null, long_running: false,
    created_at: iso(4 * HOUR), updated_at: iso(10 * MIN), queue_position: null,
    last_event: null, question: null, reason: null, error: null, ...c,
  };
  full.state_since = full.state_since || full.updated_at;
  full.last_activity_at = full.last_activity_at || full.updated_at;
  state.cards.push(full);
  return full;
}

function ev(card_num, actor, kind, payload, tsAgo) {
  return { seq: nextSeq(), card_num, ts: iso(tsAgo), actor, kind, payload };
}

function timeline(num, lines) { state.timelines[num] = lines; }

// ---- the board -----------------------------------------------------------

card({
  num: 118, state: 'completed', title: 'Login redirect loop when the session cookie expires',
  updated_at: iso(52 * MIN), agent_name: 'sprint-card-118', branch: 'sprint/118-login-loop',
});
card({
  num: 119, state: 'completed', title: 'Empty state copy on the invoices table',
  updated_at: iso(38 * MIN), agent_name: 'sprint-batch-6',
  batch_id: 'b6', branch: 'sprint/batch-6-copy',
});
card({
  num: 121, state: 'rejected', title: 'Confetti animation when a card is approved',
  updated_at: iso(2 * HOUR),
});
card({
  num: 122, state: 'duplicate', title: 'Invoice total is wrong on refunds', dup_of: 118,
  updated_at: iso(3 * HOUR),
});

card({
  num: 123, state: 'blocked', title: 'Merge the billing migration',
  reason: 'ci_red: main has been red since 8f21e3 (migration collision)',
  updated_at: iso(97 * MIN), last_activity_at: iso(41 * MIN),
  agent_name: 'sprint-card-123', branch: 'sprint/123-billing-migration',
});
card({
  num: 127, state: 'blocked', title: 'Rename the tenant slug column',
  reason: 'overlaps #131 — both edit backend/internal/tenant/store.go',
  updated_at: iso(88 * MIN), last_activity_at: iso(35 * MIN),
});
card({
  num: 128, state: 'blocked', title: 'Point the settings page at the new tenant API',
  reason: 'dependency: waiting on the contract landing in #127',
  updated_at: iso(84 * MIN), last_activity_at: iso(33 * MIN),
});
card({
  num: 129, state: 'failed', title: 'Rewrite the settings loader',
  error: 'agent died mid-run: worktree .sprint/wt/129 vanished after the power blip',
  updated_at: iso(26 * MIN), last_activity_at: iso(26 * MIN), agent_name: 'sprint-card-129',
});

card({
  num: 131, state: 'in_progress', title: 'Toolbar contrast fails WCAG in light mode',
  agent_name: 'sprint-batch-7', batch_id: 'b7', branch: 'sprint/batch-7-css',
  updated_at: iso(22 * MIN), last_activity_at: iso(40000),
  last_event: { seq: 470, ts: iso(40000), actor: 'worker', kind: 'progress', payload: { text: 'Swapped the toolbar tokens; re-running the contrast check.' } },
});
card({
  num: 132, state: 'in_progress', title: 'Stripe webhook retries double-charge on 409',
  agent_name: 'sprint-card-132', branch: 'sprint/132-webhook-retry',
  updated_at: iso(51 * MIN), last_activity_at: iso(14 * MIN),
  last_event: { seq: 462, ts: iso(14 * MIN), actor: 'worker', kind: 'progress', payload: { text: 'Reproducing the double-charge against the sandbox key.' } },
});
card({
  num: 133, state: 'in_progress', title: 'Full regression sweep before the release cut',
  agent_name: 'sprint-card-133', long_running: true, branch: 'sprint/133-regression',
  updated_at: iso(70 * MIN), last_activity_at: iso(24 * MIN),
  last_event: { seq: 455, ts: iso(24 * MIN), actor: 'worker', kind: 'note', payload: { text: 'Long job: the full suite takes ~45 minutes. Silence is expected.' } },
});
card({
  num: 144, state: 'triaging', title: 'Dark mode: the drawer scrim is too dark to read through',
  agent_name: 'sprint-batch-7', batch_id: 'b7',
  updated_at: iso(3 * MIN), last_activity_at: iso(3 * MIN),
  last_event: { seq: 494, ts: iso(3 * MIN), actor: 'worker', kind: 'progress', payload: { text: 'I read this as: lighten the drawer scrim in dark mode only.' } },
});

card({
  num: 134, state: 'needs_you', title: 'Which tenant identifier should the export use?',
  agent_name: 'sprint-card-134', updated_at: iso(9 * MIN), last_activity_at: iso(9 * MIN),
  question: {
    id: 'q-134-1',
    text: 'The CSV export can key on the tenant slug (readable, can change) or the UUID (stable, ugly). Which do you want in the shipped file?',
    options: [{ label: 'Slug', value: 'Use the slug' }, { label: 'UUID', value: 'Use the UUID' }, { label: 'Both columns', value: 'Ship both columns' }],
  },
});
card({
  num: 135, state: 'needs_you', title: 'Refund emails: what should the subject line say?',
  agent_name: 'sprint-batch-6', batch_id: 'b6',
  updated_at: iso(34 * MIN), last_activity_at: iso(34 * MIN),
  question: { id: 'q-135-1', text: 'The refund email has no subject copy. Give me the line you want and I will ship it in both the HTML and text parts.', options: null },
});

card({
  num: 136, state: 'ready', title: 'Toolbar contrast fails WCAG in light mode',
  agent_name: 'sprint-card-136', branch: 'sprint/136-toolbar-contrast',
  updated_at: iso(6 * MIN), last_activity_at: iso(6 * MIN), bounce_count: 1,
  evidence: {
    claim: 'Toolbar text now passes 4.5:1 in light mode without changing the dark palette.',
    validate: [
      'Open the live preview and look at the toolbar in light mode — the labels are near-black now, not grey.',
      'Flip your system to dark mode and reload: the toolbar should look exactly as it did before.',
      'Compare the two screenshots below (before is in the timeline).',
    ],
    ui_change: true,
    diffstat: '3 files changed, 24 insertions(+), 9 deletions(-)',
    branch: 'sprint/136-toolbar-contrast',
    test_cmd: 'npm run test -- toolbar',
    test_result: '38 pass, 0 fail',
    live_url: 'http://100.100.10.10:8436/settings',
    screenshots: [SHOTS.toolbarLight, SHOTS.toolbarDark],
  },
});
card({
  num: 137, state: 'ready', title: 'Twelve small CSS fixes (batch)',
  agent_name: 'sprint-batch-7', batch_id: 'b7', branch: 'sprint/batch-7-css',
  updated_at: iso(17 * MIN), last_activity_at: iso(17 * MIN),
  evidence: {
    claim: 'All three CSS complaints fixed on one branch; no shared file touched twice.',
    validate: [
      'Open the live preview at 1280px wide — the filter row stays on one line.',
      'Empty the invoices table (filter to “refunded”): the empty state sits dead centre in light and dark.',
      'Scroll the table — the header sticks under the toolbar, not over it.',
    ],
    ui_change: true,
    live_url: 'http://100.100.10.10:8407/invoices',
    diffstat: '5 files changed, 61 insertions(+), 33 deletions(-)',
    branch: 'sprint/batch-7-css',
    test_cmd: 'npm run lint && npm run test -- styles',
    test_result: '112 pass, 0 fail',
    screenshots: [SHOTS.emptyLight, SHOTS.emptyDark, SHOTS.filterLight],
    per_card: [
      { card_num: 137, claim: 'Filter row no longer wraps at 1280px.', screenshots: [SHOTS.filterLight] },
      { card_num: 138, claim: 'Empty state is centred in both themes.', screenshots: [SHOTS.emptyLight, SHOTS.emptyDark] },
      { card_num: 139, claim: 'Table header sticks under the toolbar instead of over it.' },
    ],
  },
});
card({
  num: 138, state: 'ready', title: 'Empty state is off-centre on the invoices table',
  agent_name: 'sprint-batch-7', batch_id: 'b7', branch: 'sprint/batch-7-css',
  updated_at: iso(17 * MIN), last_activity_at: iso(17 * MIN),
});

card({
  num: 143, state: 'integrating', title: 'Invoice PDF job retries forever on a 500',
  agent_name: 'sprint-card-143', branch: 'sprint/143-pdf-retry',
  updated_at: iso(2 * MIN), last_activity_at: iso(2 * MIN),
  evidence: {
    claim: 'The PDF job now gives up after 5 attempts and records the failure instead of looping.',
    validate: [
      'Run `sprint-demo pdf --fail` — it stops after 5 tries instead of spinning (watch the attempt counter).',
      'Check `SELECT status FROM pdf_jobs ORDER BY id DESC LIMIT 1` — it reads `failed`, not `pending`.',
    ],
    ui_change: false,
    diffstat: '2 files changed, 31 insertions(+), 6 deletions(-)',
    branch: 'sprint/143-pdf-retry',
    test_cmd: 'go test ./internal/pdf/...',
    test_result: '54 pass, 0 fail',
  },
});

card({ num: 140, state: 'queued', title: 'Export button does nothing on the reports page', queue_position: 1, updated_at: iso(12 * MIN) });
card({ num: 141, state: 'queued', title: 'Session picker forgets the last tenant', queue_position: 2, updated_at: iso(11 * MIN), pinned: 1 });
card({ num: 142, state: 'queued', title: 'Timestamps show UTC in the activity feed', queue_position: 3, updated_at: iso(9 * MIN) });

for (const [num, title] of [
  [145, 'Card shadows are too heavy on the dashboard'],
  [146, 'Sidebar icons are 1px off the grid'],
  [147, 'Tooltip arrow points the wrong way on the right edge'],
  [148, 'Focus ring is invisible on the dark toolbar'],
]) card({ num, state: 'held', title, updated_at: iso(4 * MIN), created_at: iso(4 * MIN) });

// ---- timelines -----------------------------------------------------------

timeline(131, [
  ev(131, 'user', 'submitted', { text: 'Toolbar contrast fails WCAG in light mode — the grey on grey is unreadable in sunlight.' }, 22 * MIN),
  ev(131, 'server', 'state', { to: 'triaging' }, 21 * MIN),
  ev(131, 'worker', 'chat', { text: 'I read this as: raise the toolbar foreground contrast in light mode only, without touching dark.' }, 21 * MIN),
  ev(131, 'server', 'state', { to: 'in_progress' }, 20 * MIN),
  ev(131, 'worker', 'progress', { text: 'Found the tokens in styles/tokens.css — three of them fail 4.5:1.' }, 12 * MIN),
  ev(131, 'worker', 'progress', { text: 'Swapped the toolbar tokens; re-running the contrast check.' }, 40000),
]);

timeline(132, [
  ev(132, 'user', 'submitted', { text: 'Stripe webhook retries double-charge when we answer 409.' }, 51 * MIN),
  ev(132, 'server', 'state', { to: 'in_progress' }, 50 * MIN),
  ev(132, 'worker', 'progress', { text: 'Reproducing the double-charge against the sandbox key.' }, 14 * MIN),
  ev(132, 'server', 'agent_silent', { text: 'No worker event for 5 minutes — the session is checking on sprint-card-132.' }, 8 * MIN),
  ev(132, 'session', 'note', { text: 'Pinged the agent: it is stuck waiting on a sandbox rate limit, not wedged. Giving it ten more minutes before I restart it.' }, 7 * MIN),
]);

timeline(134, [
  ev(134, 'user', 'submitted', { text: 'Add a CSV export to the tenants page.' }, 40 * MIN),
  ev(134, 'server', 'state', { to: 'in_progress' }, 39 * MIN),
  ev(134, 'worker', 'progress', { text: 'Export scaffolded; the column set is the open question.' }, 12 * MIN),
  ev(134, 'worker', 'question', { text: 'The CSV export can key on the tenant slug (readable, can change) or the UUID (stable, ugly). Which do you want in the shipped file?' }, 9 * MIN),
  ev(134, 'server', 'state', { to: 'needs_you' }, 9 * MIN),
]);

timeline(123, [
  ev(123, 'user', 'submitted', { text: 'Land the billing migration.' }, 100 * MIN),
  ev(123, 'server', 'state', { to: 'in_progress' }, 99 * MIN),
  ev(123, 'worker', 'progress', { text: 'Migration written as 000418_billing_periods.up.sql.' }, 80 * MIN),
  ev(123, 'server', 'state', { to: 'blocked', reason: 'ci_red: main has been red since 8f21e3 (migration collision)' }, 74 * MIN),
  ev(123, 'session', 'note', { text: 'Re-checked CI at :05 and :20 — still red on the same job. Nothing you type unblocks this one; #123 lands the moment main is green.' }, 41 * MIN),
]);

timeline(136, [
  ev(136, 'user', 'submitted', { text: 'Toolbar contrast fails WCAG in light mode.' }, 3 * HOUR),
  ev(136, 'server', 'state', { to: 'in_progress' }, 3 * HOUR),
  ev(136, 'worker', 'evidence', { claim: 'First pass: raised the token contrast.' }, 90 * MIN),
  ev(136, 'user', 'verdict', { verdict: 'bounce', notes: 'The dark theme regressed — the toolbar text went muddy. Fix light without touching dark.' }, 80 * MIN),
  ev(136, 'server', 'state', { to: 'in_progress' }, 80 * MIN),
  ev(136, 'worker', 'progress', { text: 'Scoped the change to the light palette only; dark tokens untouched.' }, 20 * MIN),
  ev(136, 'worker', 'progress', { text: 'Screenshotted light and dark from my own preview at 1440×900.' }, 8 * MIN),
  ev(136, 'server', 'state', { to: 'ready' }, 6 * MIN),
]);

timeline(143, [
  ev(143, 'user', 'submitted', { text: 'The invoice PDF job retries forever when the renderer 500s.' }, 2 * HOUR),
  ev(143, 'server', 'state', { to: 'in_progress' }, 2 * HOUR),
  ev(143, 'worker', 'progress', { text: 'Reproduced: the retry loop has no attempt ceiling.' }, 70 * MIN),
  ev(143, 'worker', 'evidence', { claim: 'The PDF job now gives up after 5 attempts and records the failure instead of looping.' }, 14 * MIN),
  ev(143, 'server', 'state', { to: 'ready' }, 14 * MIN),
  ev(143, 'user', 'verdict', { verdict: 'approve' }, 2 * MIN),
  ev(143, 'server', 'state', { to: 'integrating' }, 2 * MIN),
  ev(143, 'session', 'note', { text: 'Rebasing sprint/143-pdf-retry on main and running the repo gate before I merge.' }, 100000),
]);

timeline(129, [
  ev(129, 'user', 'submitted', { text: 'Rewrite the settings loader so it stops reading the file twice.' }, 2 * HOUR),
  ev(129, 'server', 'state', { to: 'in_progress' }, 2 * HOUR),
  ev(129, 'worker', 'progress', { text: 'Loader rewritten; wiring the callers.' }, 40 * MIN),
  ev(129, 'server', 'error', { text: 'agent died mid-run: worktree .sprint/wt/129 vanished after the power blip' }, 26 * MIN),
  ev(129, 'server', 'state', { to: 'failed' }, 26 * MIN),
]);

state.sidebar = [
  { seq: 401, card_num: null, ts: iso(3 * HOUR), actor: 'session', kind: 'chat', payload: { text: 'Sprint open. Three agents running, cap is 3 — #140–#142 are waiting their turn.' } },
  { seq: 428, card_num: null, ts: iso(46 * MIN), actor: 'user', kind: 'chat', payload: { text: 'hey, why have #123, #127 and #128 been blocked for so long?' } },
  {
    seq: 429, card_num: null, ts: iso(45 * MIN), actor: 'session', kind: 'chat', payload: {
      text: 'All three are downstream of one thing. #123 is waiting on main going green (migration collision, CI red since 8f21e3). #127 overlaps #131 on tenant/store.go so I am holding it until that branch merges. #128 depends on the contract #127 lands. I re-check them every ten minutes — nothing for you to do unless you want me to jump #127 ahead of #131.',
    },
  },
  { seq: 430, card_num: null, ts: iso(44 * MIN), actor: 'user', kind: 'chat', payload: { text: 'no, keep the order. but batch the css ones.' } },
  { seq: 431, card_num: null, ts: iso(43 * MIN), actor: 'session', kind: 'chat', payload: { text: 'Done — #137, #138 and #144 are one agent on sprint/batch-7-css. One worktree, one branch, one review.' } },
];

// ---- server behaviour ----------------------------------------------------

function findCard(num) { return state.cards.find((c) => c.num === Number(num)); }

function boardPayload() {
  return {
    sprint: state.sprint,
    session: state.sessionOnline ? { status: 'online', last_seen: iso(4000) } : { status: 'offline', last_seen: iso(3 * MIN) },
    seq,
    cards: state.cards.map((c) => ({ ...c })),
    sidebar: state.sidebar,
  };
}

function detailPayload(num) {
  const c = findCard(num);
  if (!c) return null;
  const tl = state.timelines[c.num] || [
    ev(c.num, 'user', 'submitted', { text: c.body || c.title }, 40 * MIN),
    ev(c.num, 'server', 'state', { to: c.state }, 30 * MIN),
  ];
  state.timelines[c.num] = tl;
  return { card: { ...c }, timeline: tl, evidence: c.evidence || null, attachments: [] };
}

function push(event) {
  event.seq = nextSeq();
  event.ts = event.ts || new Date().toISOString();
  state.queue.push(event);
  for (const es of openStreams) es._emit(event);
  return event;
}

function pushTimeline(num, event) {
  const tl = state.timelines[num] || (state.timelines[num] = []);
  tl.push(event);
}

const openStreams = [];

class MockEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.listeners = {};
    openStreams.push(this);
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen({});
    }, 30);
  }
  addEventListener(kind, fn) { (this.listeners[kind] = this.listeners[kind] || []).push(fn); }
  removeEventListener(kind, fn) {
    this.listeners[kind] = (this.listeners[kind] || []).filter((f) => f !== fn);
  }
  close() {
    this.readyState = 2;
    const i = openStreams.indexOf(this);
    if (i >= 0) openStreams.splice(i, 1);
  }
  _emit(event) {
    const msg = { data: JSON.stringify(event), lastEventId: String(event.seq) };
    if (this.onmessage) this.onmessage(msg);
    for (const fn of this.listeners.message || []) fn(msg);
  }
}
MockEventSource.CONNECTING = 0;
MockEventSource.OPEN = 1;
MockEventSource.CLOSED = 2;

class BrokenEventSource {
  constructor() {
    this.readyState = 2;
    setTimeout(() => { if (this.onerror) this.onerror({}); }, 40);
  }
  addEventListener() {}
  removeEventListener() {}
  close() {}
}
BrokenEventSource.CONNECTING = 0;
BrokenEventSource.OPEN = 1;
BrokenEventSource.CLOSED = 2;

function json(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  }));
}

let nextNum = 150;

function route(method, path, query, body) {
  if (unauth) return json({ error: 'bad or missing token' }, 401);
  if (method === 'GET' && path === '/api/board') return json(boardPayload());
  if (method === 'GET' && path === '/api/events') {
    const after = Number(query.get('after') || 0);
    return json({ events: state.queue.filter((e) => e.seq > after) });
  }
  const m = path.match(/^\/api\/cards\/(\d+)(?:\/(\w+))?$/);
  if (m) {
    const num = Number(m[1]), sub = m[2], c = findCard(num);
    if (!c) return json({ error: 'no such card' }, 404);
    if (method === 'GET' && !sub) return json(detailPayload(num));
    if (sub === 'chat') {
      const e = push({ card_num: num, actor: 'user', kind: 'chat', payload: { text: body.text } });
      pushTimeline(num, e);
      c.last_activity_at = e.ts;
      setTimeout(() => {
        const reply = push({ card_num: num, actor: 'worker', kind: 'chat', payload: { text: 'Got it — folding that in now.' } });
        pushTimeline(num, reply);
      }, 1400);
      return json({ ok: true });
    }
    if (sub === 'answer') {
      if (!c.question || c.question.answered_at) return json({ error: 'already answered' }, 409);
      c.question.answered_at = new Date().toISOString();
      const e = push({ card_num: num, actor: 'user', kind: 'answer', payload: { text: body.text, question_id: body.question_id } });
      pushTimeline(num, e);
      c.question = null;
      c.state = 'in_progress';
      c.state_since = e.ts;
      c.last_activity_at = e.ts;
      pushTimeline(num, push({ card_num: num, actor: 'server', kind: 'state', payload: { state: 'in_progress' } }));
      return json({ ok: true });
    }
    if (sub === 'verdict') {
      const v = body.verdict;
      // approve → integrating; the session flips it to completed once the branch lands
      c.state = v === 'approve' ? 'integrating' : v === 'bounce' ? 'in_progress' : 'rejected';
      if (v === 'bounce') c.bounce_count += 1;
      c.updated_at = new Date().toISOString();
      c.state_since = c.updated_at;
      pushTimeline(num, push({ card_num: num, actor: 'user', kind: 'verdict', payload: { verdict: v, notes: body.notes } }));
      pushTimeline(num, push({ card_num: num, actor: 'server', kind: 'state', payload: { state: c.state } }));
      if (v === 'approve') {
        setTimeout(() => {
          c.state = 'completed';
          c.updated_at = new Date().toISOString();
          c.state_since = c.updated_at;
          pushTimeline(num, push({ card_num: num, actor: 'session', kind: 'note', payload: { text: 'Rebased on main, gate green, merged as 4c1a09e. Worktree pruned.' } }));
          pushTimeline(num, push({ card_num: num, actor: 'server', kind: 'state', payload: { state: 'completed' } }));
        }, 6000);
      }
      return json({ ok: true });
    }
    if (sub === 'action') {
      const a = body.action;
      if (a === 'pin') c.pinned = body.pinned === false ? 0 : 1;
      else if (a === 'hold') c.state = 'held';
      else if (a === 'release') c.state = 'queued';
      else if (a === 'cancel') c.state = 'canceled';
      else if (a === 'duplicate_of') { c.state = 'duplicate'; c.dup_of = body.dup_of; }
      c.updated_at = new Date().toISOString();
      c.state_since = c.updated_at;
      push({ card_num: num, actor: 'user', kind: 'state', payload: { state: c.state } });
      return json({ ok: true });
    }
  }
  if (method === 'POST' && path === '/api/cards') {
    const num = nextNum++;
    const created = card({
      num,
      state: body.hold || state.sprint.hold_mode ? 'held' : 'queued',
      title: (body.text || 'Screenshot').split('\n')[0].slice(0, 90),
      body: body.text || '',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      queue_position: body.hold ? null : 4,
    });
    push({ card_num: num, actor: 'user', kind: 'submitted', payload: { text: body.text, images: (body.images || []).length } });
    return json(created);
  }
  if (method === 'POST' && path === '/api/sidebar') {
    const e = { seq: nextSeq(), card_num: null, ts: new Date().toISOString(), actor: 'user', kind: 'chat', payload: { text: body.text } };
    state.sidebar.push(e);
    state.queue.push(e);
    setTimeout(() => {
      const reply = { card_num: null, actor: 'session', kind: 'chat', payload: { text: 'Heard. I will fold that into the next dispatch pass — watch #140 and #141.' } };
      state.sidebar.push(push(reply));
    }, 1500);
    return json({ ok: true });
  }
  if (method === 'POST' && path === '/api/sprint') {
    if (body.action === 'set_hold_mode') state.sprint.hold_mode = !!body.hold_mode;
    return json({ sprint: state.sprint });
  }
  return json({ error: 'mock has no route for ' + method + ' ' + path }, 404);
}

let unauth = false;

export function installMock(params) {
  if (params.get('offline')) state.sessionOnline = false;
  if (params.get('unauth')) unauth = true;   // &unauth=1 exercises the 401 re-auth wall

  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (!url.pathname.startsWith('/api/')) return realFetch(input, init);
    const method = (init.method || 'GET').toUpperCase();
    let body = {};
    if (init.body) { try { body = JSON.parse(init.body); } catch { body = {}; } }
    return new Promise((resolve) => {
      setTimeout(() => resolve(route(method, url.pathname, url.searchParams, body)), 90);
    });
  };
  // &nosse=1 makes the stream fail so you can watch the polling fallback take over
  window.EventSource = params.get('nosse') ? BrokenEventSource : MockEventSource;

  if (params.get('live')) scriptedEvents();
}

function scriptedEvents() {
  const beats = [
    [6000, () => {
      const c = findCard(133);
      const e = push({ card_num: 133, actor: 'worker', kind: 'progress', payload: { text: 'Suite at 780/1204 — no failures yet.' } });
      c.last_activity_at = e.ts;
      pushTimeline(133, e);
    }],
    [12000, () => {
      const c = findCard(132);
      c.state = 'needs_you';
      c.question = {
        id: 'q-132-1',
        text: 'The sandbox refuses the 409 replay. Do you want me to stub the retry in tests, or wait for a real sandbox key?',
        options: [{ label: 'Stub it', value: 'Stub the retry in tests' }, { label: 'Wait for the key', value: 'Wait for a real key' }],
      };
      c.state_since = new Date().toISOString();
      pushTimeline(132, push({ card_num: 132, actor: 'worker', kind: 'question', payload: { question: c.question } }));
      push({ card_num: 132, actor: 'server', kind: 'state', payload: { state: 'needs_you' } });
    }],
  ];
  for (const [delay, fn] of beats) setTimeout(fn, delay);
}
