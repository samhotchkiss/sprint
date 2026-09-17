// Board/list paint reuse and out-of-order snapshot/message reconciliation.
//
// Run: node --test tests/browser-state.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  store, applyBoard, applyEvents, mergeSidebar, keepPendingLines, mergeTimeline,
  acknowledgeSidebarLine, cardPaintVer, normCard, eventText,
} from '../web/state.js';

function reset() {
  store.cards = new Map();
  store.patches = new Map();
  store.pending = [];
  store.sidebar = [];
  store.seq = 0;
  store.boardEpoch = 0;
  store.boardSeq = 0;
  store.detail = null;
  store.loaded = false;
  store.view = 'board';
  store.chatOpen = true;
  store.sprint = null;
  store.agentName = '';
  store.unit = null;
  store.doneMore = false;
  store.convoMore = false;
}

function work(num, extra = {}) {
  return { num, kind: 'work', state: extra.state || 'queued', title: extra.title || `Card #${num}`,
    last_activity_at: extra.at || 1, ...extra };
}

function echo(text, extra = {}) {
  return {
    actor: 'user', kind: 'chat', ts: '2026-01-01T00:00:00Z',
    payload: { text }, localEcho: true, local: true, localId: extra.localId || ('id-' + text + Math.random()),
    seq: extra.seq,
    ...extra,
  };
}

function board(cards, extra = {}) {
  return {
    sprint: { id: 1, title: 'Sprint' },
    cards,
    sidebar: extra.sidebar || [],
    seq: extra.seq != null ? extra.seq : 10,
  };
}

test('stale snapshot behind live seq does not drop a newer card', () => {
  reset();
  applyBoard(board([work(1), work(2), work(3)], { seq: 10 }), { epoch: 1 });
  store.seq = 15;
  applyBoard(board([work(1), work(2)], { seq: 10 }), { epoch: 2 });
  assert.ok(store.cards.has(3), 'a delayed board fetch must not un-see a card live events already passed');
});

test('authoritative snapshot at/past live seq may delete a card', () => {
  reset();
  applyBoard(board([work(1), work(2), work(3)], { seq: 10 }), { epoch: 1 });
  applyBoard(board([work(1), work(2)], { seq: 20 }), { epoch: 2 });
  assert.equal(store.cards.has(3), false, 'a current snapshot is allowed to remove cards');
});

test('older epoch does not replace a newer board', () => {
  reset();
  applyBoard(board([work(1), work(2), work(9)], { seq: 20 }), { epoch: 2 });
  applyBoard(board([work(1), work(2)], { seq: 10 }), { epoch: 1 });
  assert.ok(store.cards.has(9));
});

test('stale board snapshot does not drop a newer sidebar seq', () => {
  reset();
  applyBoard(board([], { seq: 10, sidebar: [] }), { epoch: 1 });
  store.sidebar = [{ seq: 50, actor: 'user', kind: 'chat', payload: { text: 'keep me' } }];
  store.seq = 50;
  applyBoard(board([], { seq: 10, sidebar: [] }), { epoch: 2 });
  assert.equal(store.sidebar.some((e) => e.seq === 50 && e.payload.text === 'keep me'), true);
});

test('two identical pending sidebar lines survive a snapshot that contains one copy', () => {
  reset();
  const a = echo('ok', { localId: 'a' });
  const b = echo('ok', { localId: 'b' });
  const merged = mergeSidebar([a, b], [
    { seq: 5, actor: 'user', kind: 'chat', payload: { text: 'ok' } },
  ], 5);
  assert.equal(merged.filter((e) => e.payload && e.payload.text === 'ok').length, 2,
    'a text Set must not swallow the second unconfirmed "ok"');
  assert.equal(merged.filter((e) => e.seq == null).length, 1);
});

test('SSE then POST replay does not eat a second identical pending line', () => {
  reset();
  const a = echo('ok', { localId: 'a' });
  const b = echo('ok', { localId: 'b' });
  store.sidebar = [a, b];
  applyEvents([{ seq: 10, actor: 'user', kind: 'chat', payload: { text: 'ok' } }]);
  assert.equal(a.seq, 10);
  assert.equal(b.seq, undefined);
  acknowledgeSidebarLine({ seq: 10, actor: 'user', kind: 'chat', payload: { text: 'ok' } }, a);
  const oks = store.sidebar.filter((e) => e.payload && e.payload.text === 'ok');
  assert.equal(oks.length, 2);
  assert.equal(b.seq, undefined, 'replay of seq 10 must not claim the second echo');
});

test('SSE-before-POST stamps the first echo; POST reconciles onto that seq', () => {
  reset();
  const line = echo('hello', { localId: 'post-1' });
  store.sidebar = [line];
  applyEvents([{ seq: 7, actor: 'user', kind: 'chat', payload: { text: 'hello' } }]);
  const after = acknowledgeSidebarLine(
    { seq: 7, actor: 'user', kind: 'chat', payload: { text: 'hello' } }, line);
  assert.equal(after.seq, 7);
  assert.equal(store.sidebar.filter((e) => e.payload.text === 'hello').length, 1);
});

test('keepPendingLines retains a settled seq the stale timeline lacks', () => {
  const pending = [{ seq: 50, pending: false, actor: 'user', payload: { text: 'hi' } }];
  const kept = keepPendingLines(pending, [{ seq: 40, actor: 'worker', payload: { text: 'old' } }]);
  assert.equal(kept.length, 1);
  assert.equal(kept[0].seq, 50);
});

test('keepPendingLines claims one of two identical pending lines', () => {
  const p1 = { seq: null, pending: true, actor: 'user', payload: { text: 'hi' } };
  const p2 = { seq: null, pending: true, actor: 'user', payload: { text: 'hi' } };
  const kept = keepPendingLines([p1, p2],
    [{ seq: 9, actor: 'user', kind: 'chat', payload: { text: 'hi' } }]);
  assert.equal(kept.length, 1, 'one timeline event retires one echo, not both');
});

test('older timeline cannot drop a newer seq', () => {
  const merged = mergeTimeline(
    [{ seq: 12, payload: { text: 'new' } }],
    [{ seq: 10, payload: { text: 'old' } }],
  );
  assert.deepEqual(merged.map((e) => e.seq), [10, 12]);
});

test('cardPaintVer changes when question options, evidence, or model change', () => {
  reset();
  const base = { num: 1, state: 'needs_you', title: 'Decide' };
  const a = normCard({ ...base, question: { text: 'which', options: ['x'] } });
  const b = normCard({ ...base, question: { text: 'which', options: ['x', 'y'] } });
  const c = normCard({ ...base, question: { text: 'which', options: ['x'] }, evidence: { claim: 'done' } });
  const d = normCard({ ...base, question: { text: 'which', options: ['x'] }, model: 'opus' });
  assert.notEqual(cardPaintVer(a), cardPaintVer(b));
  assert.notEqual(cardPaintVer(a), cardPaintVer(c));
  assert.notEqual(cardPaintVer(a), cardPaintVer(d));
});

test('cardPaintVer changes when a done card title changes', () => {
  reset();
  const a = normCard({ num: 8, state: 'completed', title: 'Old name' });
  const b = normCard({ num: 8, state: 'completed', title: 'New name' });
  assert.notEqual(cardPaintVer(a), cardPaintVer(b));
});

test('firstLoad captures the request epoch before awaiting /api/board', () => {
  const src = readFileSync(fileURLToPath(new URL('../web/app.js', import.meta.url)), 'utf8');
  const start = src.indexOf('async function firstLoad');
  const fn = src.slice(start, start + 500);
  assert.match(fn, /const epoch = \+\+boardEpoch;[\s\S]*await api\.board\(\)/);
});

function installDom() {
  if (globalThis.document && globalThis.document._sprintFake) return;
  class FakeNode {
    constructor() {
      this.childNodes = [];
      this.parentNode = null;
      this.nodeType = 1;
    }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    appendChild(c) {
      if (!c) return c;
      if (c.nodeType === 11) {
        while (c.childNodes.length) this.appendChild(c.childNodes[0]);
        return c;
      }
      if (c.parentNode) c.parentNode.removeChild(c);
      this.childNodes.push(c);
      c.parentNode = this;
      return c;
    }
    removeChild(c) {
      const i = this.childNodes.indexOf(c);
      if (i >= 0) this.childNodes.splice(i, 1);
      c.parentNode = null;
      return c;
    }
    replaceChild(n, o) {
      const i = this.childNodes.indexOf(o);
      if (n.parentNode) n.parentNode.removeChild(n);
      this.childNodes[i] = n;
      n.parentNode = this;
      o.parentNode = null;
      return n;
    }
    insertBefore(n, ref) {
      if (n.parentNode) n.parentNode.removeChild(n);
      if (!ref) return this.appendChild(n);
      const i = this.childNodes.indexOf(ref);
      this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, n);
      n.parentNode = this;
      return n;
    }
  }
  class FakeEl extends FakeNode {
    constructor(tag) {
      super();
      this.tagName = String(tag).toUpperCase();
      this.className = '';
      this.attributes = {};
      this.style = {};
      this.dataset = {};
      this.hidden = false;
      const self = this;
      this.classList = {
        add(...cs) {
          const set = new Set(self.className.split(/\s+/).filter(Boolean));
          cs.forEach((c) => set.add(c));
          self.className = [...set].join(' ');
        },
        toggle(c, on) {
          const set = new Set(self.className.split(/\s+/).filter(Boolean));
          if (on === false) set.delete(c);
          else if (on === true) set.add(c);
          else if (set.has(c)) set.delete(c);
          else set.add(c);
          self.className = [...set].join(' ');
        },
      };
    }
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k.startsWith('data-')) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_, a) => a.toUpperCase());
        this.dataset[camel] = String(v);
      }
    }
    getAttribute(k) { return this.attributes[k]; }
    addEventListener() {}
    querySelector(sel) { return qsa(this, sel)[0] || null; }
    querySelectorAll(sel) { return qsa(this, sel); }
    focus() {}
    set textContent(v) {
      this.childNodes = [];
      if (v != null && v !== '') this.appendChild(document.createTextNode(String(v)));
    }
    get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  }
  function qsa(root, sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.children || []) {
        if (matchSel(c, sel)) out.push(c);
        walk(c);
      }
    };
    walk(root);
    return out;
  }
  function matchSel(el, sel) {
    if (sel.startsWith('.')) {
      return sel.slice(1).split('.').every((c) => (el.className || '').split(/\s+/).includes(c));
    }
    const data = sel.match(/^\[data-([^=]+)="([^"]+)"\]$/);
    if (data) {
      const camel = data[1].replace(/-([a-z])/g, (_, a) => a.toUpperCase());
      return el.dataset && String(el.dataset[camel]) === data[2];
    }
    return false;
  }
  globalThis.Node = FakeNode;
  globalThis.HTMLElement = FakeEl;
  globalThis.document = {
    _sprintFake: true,
    createElement: (t) => new FakeEl(t),
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t), parentNode: null, childNodes: [], children: [] }),
    createDocumentFragment: () => {
      const f = new FakeNode();
      f.nodeType = 11;
      return f;
    },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  globalThis.window = globalThis.window || globalThis;
}

function findByNum(root, num) {
  const out = [];
  const walk = (n) => {
    if (n && n.dataset && String(n.dataset.num) === String(num)) out.push(n);
    for (const c of (n && n.children) || []) walk(c);
  };
  walk(root);
  return out[0] || null;
}

const app = { openCard() {}, eventText, render() {} };

test('regular board refresh does not recreate unchanged card DOM', async () => {
  installDom();
  const { renderBoard } = await import('../web/board.js');
  reset();
  applyBoard(board([
    work(1, { state: 'in_progress', title: 'Running' }),
    work(2, { state: 'queued', title: 'Waiting' }),
  ], { seq: 3 }));
  const root = document.createElement('main');
  renderBoard(root, app);
  const first = findByNum(root, 1);
  const second = findByNum(root, 2);
  assert.ok(first && second);
  renderBoard(root, app);
  assert.equal(findByNum(root, 1), first);
  assert.equal(findByNum(root, 2), second);
});

test('regular list refresh does not recreate unchanged row DOM', async () => {
  installDom();
  const { renderList } = await import('../web/list.js');
  reset();
  store.view = 'list';
  applyBoard(board([
    work(1, { state: 'in_progress', title: 'Running' }),
    work(2, { state: 'queued', title: 'Waiting' }),
    work(3, { state: 'blocked', title: 'Stuck', reason: 'ci_red' }),
  ], { seq: 4 }));
  const root = document.createElement('main');
  renderList(root, app);
  const motion = findByNum(root, 1);
  const queued = findByNum(root, 2);
  const blocked = findByNum(root, 3);
  assert.ok(motion && queued && blocked);
  renderList(root, app);
  assert.equal(findByNum(root, 1), motion);
  assert.equal(findByNum(root, 2), queued);
  assert.equal(findByNum(root, 3), blocked);
});

test('list keeps unchanged rows when a sibling card changes', async () => {
  installDom();
  const { renderList } = await import('../web/list.js');
  reset();
  store.view = 'list';
  applyBoard(board([
    work(1, { state: 'in_progress', title: 'One' }),
    work(2, { state: 'in_progress', title: 'Two' }),
  ], { seq: 4 }));
  const root = document.createElement('main');
  renderList(root, app);
  const one = findByNum(root, 1);
  applyBoard(board([
    work(1, { state: 'in_progress', title: 'One' }),
    work(2, { state: 'in_progress', title: 'Two renamed' }),
  ], { seq: 5 }), { epoch: 1 });
  renderList(root, app);
  assert.equal(findByNum(root, 1), one, 'an unchanged row must survive a sibling edit');
  assert.equal(findByNum(root, 2).textContent.includes('Two renamed'), true);
});
