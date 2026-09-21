// Safe Markdown subset: structure, #card links, and no unsafe URLs/HTML.
//
// Run: node --test tests/test_message_markdown.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { renderMarkdown, safeHref, isMarkdownDetail } from '../web/message.js';
import { detailBlock } from '../web/detail.js';

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
  }
  class FakeEl extends FakeNode {
    constructor(tag) {
      super();
      this.tagName = String(tag).toUpperCase();
      this.className = '';
      this.attributes = {};
      this.style = {};
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
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return this.attributes[k]; }
    addEventListener() {}
    set textContent(v) {
      this.childNodes = [];
      if (v != null && v !== '') this.appendChild(document.createTextNode(String(v)));
    }
    get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
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
  };
}

function tags(node, name) {
  const out = [];
  const walk = (n) => {
    if (n && n.tagName === name) out.push(n);
    for (const c of (n && n.childNodes) || []) walk(c);
  };
  walk(node);
  return out;
}

function wrap(frag) {
  const root = document.createElement('div');
  root.appendChild(frag);
  return root;
}

installDom();

test('paragraphs, lists, fences, bold, code, and card links', () => {
  const opened = [];
  const src = [
    'Hello **board** and `inline`.',
    '',
    '- one',
    '- two with #44',
    '',
    '1. first',
    '1. second',
    '',
    '```',
    'const x = 1;',
    '```',
    '',
    'See https://example.com/docs and [label](https://example.com/a).',
  ].join('\n');
  const root = wrap(renderMarkdown(src, (n) => opened.push(n)));
  assert.equal(tags(root, 'P').length, 2);
  assert.equal(tags(root, 'STRONG').length, 1);
  assert.equal(tags(root, 'STRONG')[0].textContent, 'board');
  assert.equal(tags(root, 'CODE').filter((c) => (c.className || '').includes('md-inline')).length, 1);
  assert.equal(tags(root, 'UL').length, 1);
  assert.equal(tags(root, 'OL').length, 1);
  assert.equal(tags(root, 'LI').length, 4);
  assert.equal(tags(root, 'PRE').length, 1);
  assert.match(tags(root, 'PRE')[0].textContent, /const x = 1;/);
  const cards = tags(root, 'BUTTON').filter((b) => (b.className || '').includes('cardlink'));
  assert.equal(cards.length, 1);
  assert.equal(cards[0].textContent, '#44');
  const links = tags(root, 'A');
  assert.ok(links.some((a) => a.getAttribute('href') === 'https://example.com/docs'));
  assert.ok(links.some((a) => a.getAttribute('href') === 'https://example.com/a'));
  assert.equal(root.textContent.includes('Hello'), true);
  assert.equal(root.textContent.includes('second'), true);
});

test('HTML and javascript: URLs stay inert text', () => {
  const src = 'Click [x](javascript:alert(1)) and <img src=x onerror=alert(1)> plus [ok](https://ok.example).';
  const root = wrap(renderMarkdown(src));
  assert.equal(tags(root, 'IMG').length, 0);
  assert.equal(tags(root, 'SCRIPT').length, 0);
  const hrefs = tags(root, 'A').map((a) => a.getAttribute('href'));
  assert.deepEqual(hrefs, ['https://ok.example']);
  assert.equal(root.textContent.includes('<img src=x onerror=alert(1)>'), true);
  assert.equal(root.textContent.includes('javascript:alert(1)'), true);
});

test('safeHref rejects non-http schemes', () => {
  assert.equal(safeHref('javascript:alert(1)'), null);
  assert.equal(safeHref('data:text/html,hi'), null);
  assert.equal(safeHref('vbscript:x'), null);
  assert.equal(safeHref('file:///etc/passwd'), null);
  assert.equal(safeHref('https://ok.example/a'), 'https://ok.example/a');
  assert.equal(safeHref('http://ok.example/a.'), 'http://ok.example/a');
});

test('unclosed fence keeps the remaining source', () => {
  const src = 'before\n```\nstill here\nand here';
  const root = wrap(renderMarkdown(src));
  assert.match(root.textContent, /still here/);
  assert.match(root.textContent, /and here/);
  assert.match(root.textContent, /before/);
});

test('long visible text is not truncated', () => {
  const body = 'word '.repeat(200).trim();
  const root = wrap(renderMarkdown(body));
  assert.equal(root.textContent.includes(body), true);
  assert.ok(root.textContent.length >= body.length);
});

test('collapsed detail stays preformatted unless detail_format is markdown', () => {
  const log = 'line 1\n  indented\nline 3';
  const pre = detailBlock({ seq: 1, payload: { detail: log } }, { openCard() {} });
  assert.equal((pre.className || '').includes('is-markdown'), false);
  const body = pre.childNodes.find((n) => n.className && n.className.includes('detail-body'));
  assert.ok(body);
  assert.equal((body.className || '').includes('is-markdown'), false);
  assert.match(body.textContent, /  indented/);

  const md = detailBlock({
    seq: 2,
    payload: { detail: '- a\n- b', detail_format: 'markdown' },
  }, { openCard() {} });
  const mdBody = md.childNodes.find((n) => n.className && String(n.className).includes('detail-body'));
  assert.ok(String(mdBody.className).includes('is-markdown'));
  assert.equal(tags(mdBody, 'LI').length, 2);
});

test('isMarkdownDetail is exact', () => {
  assert.equal(isMarkdownDetail({ payload: { detail_format: 'markdown' } }), true);
  assert.equal(isMarkdownDetail({ payload: { detail_format: 'text' } }), false);
  assert.equal(isMarkdownDetail({ payload: {} }), false);
});
