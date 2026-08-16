// Small DOM + formatting helpers. No dependencies.

/** Create an element. h('div.card', {onclick}, 'text', childEl) */
export function h(spec, props, ...kids) {
  const [tag, ...classes] = String(spec).split('.');
  const el = document.createElement(tag || 'div');
  if (classes.length) el.className = classes.join(' ');
  if (props && (typeof props !== 'object' || props instanceof Node)) {
    kids.unshift(props);
    props = null;
  }
  for (const k in props || {}) {
    const v = props[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = el.className ? el.className + ' ' + v : v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k in el && k !== 'list' && typeof v !== 'object') { try { el[k] = v; } catch { el.setAttribute(k, v); } }
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}

function add(el, kids) {
  for (const kid of kids) {
    if (kid == null || kid === false || kid === '') continue;
    if (Array.isArray(kid)) add(el, kid);
    else if (kid instanceof Node) el.appendChild(kid);
    else el.appendChild(document.createTextNode(String(kid)));
  }
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

/**
 * Keyed reconcile: bring `root`'s children in line with `items` while touching
 * as little DOM as possible.
 *
 * This exists because the rail used to be rebuilt from scratch on every frame,
 * and frames arrive constantly — a live session moves its drain cursor every
 * second or so. Rebuilding threw away every <img> in the thread and the browser
 * visibly re-painted it: the rail blinked. Now an item is rebuilt only when its
 * own content changed (`ver`), everything else is left exactly where it is, and
 * a node that just needs a small update (a message's delivery pill) gets it via
 * the `_sync` hook it hung on itself.
 *
 *   items: [{key, ver, make() -> Node}]
 */
export function reconcile(root, items) {
  const existing = new Map();
  for (const node of Array.from(root.children)) {
    const k = node.dataset ? node.dataset.k : null;
    if (k && !existing.has(k)) existing.set(k, node);
    else root.removeChild(node);
  }
  const wanted = new Set();
  items.forEach((item, i) => {
    const ver = String(item.ver == null ? '1' : item.ver);
    wanted.add(item.key);
    let node = existing.get(item.key);
    if (node && node.dataset.v !== ver) {
      const fresh = item.make();
      stamp(fresh, item.key, ver);
      root.replaceChild(fresh, node);
      existing.set(item.key, fresh);
      node = fresh;
    } else if (!node) {
      node = item.make();
      stamp(node, item.key, ver);
      existing.set(item.key, node);
    } else if (typeof node._sync === 'function') {
      node._sync();                    // in place: no replacement, no repaint
    }
    const at = root.children[i];
    if (at !== node) root.insertBefore(node, at || null);
  });
  for (const [k, node] of existing) {
    if (!wanted.has(k) && node.parentNode === root) root.removeChild(node);
  }
}

function stamp(node, key, ver) {
  node.dataset.k = key;
  node.dataset.v = ver;
}

export function debounce(fn, ms) {
  let t = null;
  const wrapped = (...a) => { clearTimeout(t); t = setTimeout(() => { t = null; fn(...a); }, ms); };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

export function uid() {
  if (crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'x' + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

// ---- time ----------------------------------------------------------------

export function ms(ts) {
  if (ts == null) return null;
  if (typeof ts === 'number') return ts < 1e12 ? ts * 1000 : ts; // seconds or millis
  const t = Date.parse(ts);
  return Number.isNaN(t) ? null : t;
}

/** "just now" / "4m" / "2h 10m" / "3d" — compact, no clock times. */
export function age(ts, now = Date.now()) {
  const t = ms(ts);
  if (t == null) return '';
  let s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 45) return 'just now';
  const m = Math.max(1, Math.floor(s / 60));   // never render "0m"
  if (m < 60) return m + 'm';
  const hrs = Math.floor(m / 60), rem = m % 60;
  if (hrs < 24) return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
  const d = Math.floor(hrs / 24);
  return d < 7 ? `${d}d` : `${Math.floor(d / 7)}w`;
}

export function ageSuffix(ts, now = Date.now()) {
  const a = age(ts, now);
  return a === 'just now' || a === '' ? a : a + ' ago';
}

export function elapsedMs(ts, now = Date.now()) {
  const t = ms(ts);
  return t == null ? Infinity : now - t;
}

/** Elements carrying data-ts get their text refreshed by the ticker. */
export function timeEl(ts, { suffix = true, prefix = '' } = {}) {
  const el = h('span.t', { 'data-ts': ts == null ? '' : ts, 'data-suffix': suffix ? '1' : '' });
  el.textContent = prefix + (suffix ? ageSuffix(ts) : age(ts));
  if (prefix) el.dataset.prefix = prefix;
  return el;
}

export function tickTimes(root = document) {
  const now = Date.now();
  for (const el of root.querySelectorAll('.t[data-ts]')) {
    const ts = el.dataset.ts;
    if (!ts) continue;
    el.textContent = (el.dataset.prefix || '') + (el.dataset.suffix ? ageSuffix(ts, now) : age(ts, now));
  }
}

// ---- text ----------------------------------------------------------------

/** Split text into nodes, turning #123 into clickable card links. */
export function autolink(text, onCard) {
  const frag = document.createDocumentFragment();
  const s = String(text == null ? '' : text);
  const re = /#(\d{1,7})\b/g;
  let last = 0, m;
  while ((m = re.exec(s))) {
    if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
    const num = Number(m[1]);
    frag.appendChild(h('button.cardlink', {
      type: 'button',
      onclick: (e) => { e.preventDefault(); e.stopPropagation(); onCard && onCard(num); },
    }, '#' + num));
    last = m.index + m[0].length;
  }
  if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
  return frag;
}

/** Multi-line text with #N autolinks, preserving newlines. */
export function richText(text, onCard) {
  const frag = document.createDocumentFragment();
  const lines = String(text == null ? '' : text).split('\n');
  lines.forEach((line, i) => {
    if (i) frag.appendChild(document.createElement('br'));
    frag.appendChild(autolink(line, onCard));
  });
  return frag;
}

export function firstLine(text, max = 140) {
  const s = String(text == null ? '' : text).trim().split('\n')[0];
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

export function plural(n, one, many) {
  return `${n} ${n === 1 ? one : (many || one + 's')}`;
}
