// The rail's slots. A slot is a named place in the panel that is rebuilt only
// when its own signature changes — and the NAME is how the next paint finds
// what the last one put there.
//
// Card #53 shipped a verdict bar built as `.review-bar.is-verdict` into a slot
// called `verdict-bar`. Every paint looked for a node that had never existed,
// found nothing, and inserted another bar: two Approve buttons on a card with
// one decision in it. These tests are written against a builder whose element
// does NOT carry the slot's class, because that is the case that broke.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { syncPart, syncOptional } from '../web/slots.js';

// The smallest thing that behaves like the bits of the DOM these two touch.
function el(...classes) {
  const node = {
    classes: new Set(classes),
    dataset: {},
    children: [],
    classList: { add: (c) => node.classes.add(c) },
  };
  node.querySelector = (sel) => {
    const want = sel.replace(/^\./, '');
    return node.children.find((c) => c.classes.has(want)) || null;
  };
  node.appendChild = (c) => { node.children.push(c); return c; };
  node.insertBefore = (c, before) => {
    const at = before ? node.children.indexOf(before) : -1;
    if (at < 0) node.children.push(c); else node.children.splice(at, 0, c);
    return c;
  };
  node.replaceChild = (c, old) => { node.children[node.children.indexOf(old)] = c; return c; };
  node.removeChild = (c) => { node.children.splice(node.children.indexOf(c), 1); return c; };
  return node;
}

const countOf = (root, cls) => root.children.filter((c) => c.classes.has(cls)).length;

test('a slot holds ONE node however many times it is painted', () => {
  const root = el('rail');
  // the builder's own element does not carry the slot name — the #53 shape
  for (let i = 0; i < 5; i += 1) syncPart(root, 'verdict-bar', 'sig-a', () => el('review-bar', 'is-verdict'));
  assert.equal(countOf(root, 'verdict-bar'), 1);
  assert.equal(root.children.length, 1);
});

test('an optional slot holds ONE node however many times it is painted', () => {
  const root = el('rail');
  root.appendChild(el('composer'));
  for (let i = 0; i < 5; i += 1) {
    syncOptional(root, 'verdict-bar', 'sig-a', () => el('review-bar', 'is-verdict'));
  }
  assert.equal(countOf(root, 'verdict-bar'), 1);
  assert.equal(root.children.length, 2, 'the composer is still the only other thing there');
});

test('a rebuilt slot REPLACES rather than stacks when its signature changes', () => {
  const root = el('rail');
  syncOptional(root, 'verdict-bar', 'ready', () => el('review-bar', 'is-verdict'));
  syncOptional(root, 'verdict-bar', 'mid-bounce', () => el('review-bar', 'is-verdict'));
  assert.equal(countOf(root, 'verdict-bar'), 1);
  assert.equal(root.children[0].dataset.sig, 'mid-bounce');
});

test('a null signature takes the optional slot away again', () => {
  const root = el('rail');
  root.appendChild(el('composer'));
  syncOptional(root, 'verdict-bar', 'ready', () => el('review-bar', 'is-verdict'));
  assert.equal(countOf(root, 'verdict-bar'), 1);
  // a card that stops asking for a verdict must LOSE its bar, not keep a dead
  // one — which is only possible because the slot can be found by name
  syncOptional(root, 'verdict-bar', null, () => el('review-bar', 'is-verdict'));
  assert.equal(countOf(root, 'verdict-bar'), 0);
  assert.equal(root.children.length, 1);
});

test('an unchanged signature does not rebuild the node', () => {
  const root = el('rail');
  let built = 0;
  const make = () => { built += 1; return el('review-bar', 'is-verdict'); };
  syncPart(root, 'verdict-bar', 'same', make);
  syncPart(root, 'verdict-bar', 'same', make);
  assert.equal(built, 1, 'a slot that has not changed is left exactly where the caret is');
});

test('the optional slot goes ABOVE the composer, where the verdict is pinned', () => {
  const root = el('rail');
  root.appendChild(el('thread'));
  root.appendChild(el('composer'));
  syncOptional(root, 'verdict-bar', 'ready', () => el('review-bar', 'is-verdict'));
  assert.deepEqual(root.children.map((c) => [...c.classes][0]), ['thread', 'review-bar', 'composer']);
});
