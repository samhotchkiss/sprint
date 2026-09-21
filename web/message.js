// Safe Markdown subset for thread bubbles. DOM nodes only — never innerHTML.
//
// Blocks: paragraphs, bullet/numbered lists, fenced code.
// Inline: bold, emphasis, inline code, [label](url), bare http(s) URLs, #N cards.
// Anything else, including HTML, is a text node.

import { h } from './util.js';

const SAFE_SCHEME = /^(https?)$/i;
const BARE_URL = /https?:\/\/[^\s<>"'`\]]+/;
const CARD = /#(\d{1,7})\b/;

export function safeHref(raw) {
  const s = String(raw == null ? '' : raw).trim();
  if (!s) return null;
  const scheme = s.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/);
  if (!scheme) return null;
  if (!SAFE_SCHEME.test(scheme[1])) return null;
  if (/[\x00-\x1f\x7f]/.test(s)) return null;
  return trimUrlPunctuation(s);
}

function trimUrlPunctuation(url) {
  let end = url.length;
  const closers = { ')': '(', ']': '[', '}': '{' };
  while (end > 0) {
    const ch = url[end - 1];
    if ('.,;:!?'.includes(ch)) { end -= 1; continue; }
    if (closers[ch]) {
      const body = url.slice(0, end);
      const opens = (body.match(new RegExp('\\' + closers[ch], 'g')) || []).length;
      const shuts = (body.match(new RegExp('\\' + ch, 'g')) || []).length;
      if (shuts > opens) { end -= 1; continue; }
    }
    break;
  }
  return end ? url.slice(0, end) : null;
}

export function renderMarkdown(text, onCard) {
  const frag = document.createDocumentFragment();
  const source = String(text == null ? '' : text);
  const blocks = parseBlocks(source);
  for (const block of blocks) frag.appendChild(renderBlock(block, onCard));
  return frag;
}

function parseBlocks(src) {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const fence = lines[i].match(/^```([\w+-]*)[ \t]*$/);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^```[ \t]*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      blocks.push({ type: 'fence', text: body.join('\n') });
      continue;
    }
    const bullet = lines[i].match(/^(\s*)([-*+])[ \t]+(.*)$/);
    const numbered = lines[i].match(/^(\s*)(\d+)\.[ \t]+(.*)$/);
    if (bullet || numbered) {
      const ordered = !!numbered;
      const items = [];
      while (i < lines.length) {
        const m = ordered
          ? lines[i].match(/^\s*\d+\.[ \t]+(.*)$/)
          : lines[i].match(/^\s*[-*+][ \t]+(.*)$/);
        if (!m) break;
        items.push(m[1]);
        i += 1;
      }
      blocks.push({ type: ordered ? 'ol' : 'ul', items });
      continue;
    }
    if (/^\s*$/.test(lines[i])) { i += 1; continue; }
    const para = [];
    while (i < lines.length) {
      if (/^\s*$/.test(lines[i])) break;
      if (/^```/.test(lines[i])) break;
      if (/^\s*[-*+][ \t]+\S/.test(lines[i])) break;
      if (/^\s*\d+\.[ \t]+\S/.test(lines[i])) break;
      para.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: 'p', text: para.join('\n') });
  }
  if (!blocks.length) blocks.push({ type: 'p', text: source });
  return blocks;
}

function renderBlock(block, onCard) {
  if (block.type === 'fence') {
    const pre = h('pre.md-fence');
    pre.appendChild(h('code', block.text));
    return pre;
  }
  if (block.type === 'ul' || block.type === 'ol') {
    const list = h(block.type);
    for (const item of block.items) {
      const li = h('li');
      appendInline(li, item, onCard);
      list.appendChild(li);
    }
    return list;
  }
  const p = h('p');
  appendInline(p, block.text, onCard);
  return p;
}

function appendInline(parent, text, onCard) {
  const s = String(text == null ? '' : text);
  let i = 0;
  while (i < s.length) {
    if (s[i] === '`') {
      const end = s.indexOf('`', i + 1);
      if (end > i) {
        parent.appendChild(h('code.md-inline', s.slice(i + 1, end)));
        i = end + 1;
        continue;
      }
    }
    if (s.startsWith('**', i) || s.startsWith('__', i)) {
      const mark = s.slice(i, i + 2);
      const end = s.indexOf(mark, i + 2);
      if (end > i + 2) {
        const el = h('strong');
        appendInline(el, s.slice(i + 2, end), onCard);
        parent.appendChild(el);
        i = end + 2;
        continue;
      }
    }
    if ((s[i] === '*' || s[i] === '_') && s[i + 1] !== s[i] && s[i + 1]) {
      const mark = s[i];
      const end = findCloser(s, mark, i + 1);
      if (end > i + 1) {
        const el = h('em');
        appendInline(el, s.slice(i + 1, end), onCard);
        parent.appendChild(el);
        i = end + 1;
        continue;
      }
    }
    if (s[i] === '[') {
      const mid = s.indexOf('](', i + 1);
      const close = mid >= 0 ? s.indexOf(')', mid + 2) : -1;
      if (mid > i && close > mid) {
        const href = safeHref(s.slice(mid + 2, close));
        if (href) {
          const a = outbound(href);
          appendInline(a, s.slice(i + 1, mid), onCard);
          parent.appendChild(a);
          i = close + 1;
          continue;
        }
      }
    }
    const rest = s.slice(i);
    const url = rest.match(BARE_URL);
    const card = rest.match(CARD);
    const urlAt = url && url.index === 0 ? url[0] : null;
    const cardAt = card && card.index === 0 ? card : null;
    if (urlAt) {
      const href = safeHref(urlAt);
      if (href) {
        parent.appendChild(outbound(href, href));
        i += href.length;
        continue;
      }
    }
    if (cardAt) {
      const num = Number(cardAt[1]);
      parent.appendChild(h('button.cardlink', {
        type: 'button',
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); onCard && onCard(num); },
      }, '#' + num));
      i += cardAt[0].length;
      continue;
    }
    let next = s.length;
    if (url && url.index > 0) next = Math.min(next, i + url.index);
    if (card && card.index > 0) next = Math.min(next, i + card.index);
    const tick = s.indexOf('`', i);
    if (tick >= 0) next = Math.min(next, tick);
    const star = s.indexOf('*', i);
    if (star >= 0) next = Math.min(next, star);
    const under = s.indexOf('_', i);
    if (under >= 0) next = Math.min(next, under);
    const brack = s.indexOf('[', i);
    if (brack >= 0) next = Math.min(next, brack);
    if (next === i) next = i + 1;
    parent.appendChild(document.createTextNode(s.slice(i, next)));
    i = next;
  }
}

function findCloser(s, mark, from) {
  let j = from;
  while (j < s.length) {
    if (s[j] === '`') {
      const end = s.indexOf('`', j + 1);
      if (end < 0) return -1;
      j = end + 1;
      continue;
    }
    if (s[j] === mark) return j;
    j += 1;
  }
  return -1;
}

function outbound(href, label) {
  return h('a.extlink', {
    href,
    target: '_blank',
    rel: 'noopener noreferrer',
    title: href,
    onclick: (e) => e.stopPropagation(),
  }, label || href);
}

export function isMarkdownDetail(ev) {
  const fmt = ev && ev.payload ? ev.payload.detail_format : null;
  return fmt === 'markdown';
}
