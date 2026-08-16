// HTTP surface. Every path here is defined in SPEC.md — nothing invented.
import { uid } from './util.js';

export class ApiError extends Error {
  constructor(status, body, path) {
    super((body && (body.error || body.message)) || `${status} on ${path}`);
    this.status = status;
    this.body = body || {};
    this.path = path;
    this.fields = (body && (body.missing || body.fields)) || null;
  }
}

let token = null;

/** Read ?t=TOKEN (the server also sets a cookie), remember it, strip it from the URL bar. */
export function initAuth() {
  const url = new URL(location.href);
  const t = url.searchParams.get('t');
  if (t) {
    token = t;
    try { sessionStorage.setItem('sprint.token', t); } catch {}
    url.searchParams.delete('t');
    history.replaceState(null, '', url.pathname + (url.search === '?' ? '' : url.search) + url.hash);
  } else {
    try { token = sessionStorage.getItem('sprint.token'); } catch {}
  }
  return token;
}

export function getToken() { return token; }

// No request may hang forever. A backend that went away mid-POST can leave the
// socket open with `fetch` never settling — which is exactly the bug this file
// was changed for: a "sending…" pill that stays "sending…" while nothing at all
// landed server-side. Every request has a deadline now, and blowing it is a
// failure the caller can see, say out loud, and retry.
const GET_TIMEOUT_MS = 15000;
const POST_TIMEOUT_MS = 20000;

async function req(method, path, body, opts = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (method !== 'GET') headers['Idempotency-Key'] = opts.idempotencyKey || uid();
  const limit = opts.timeoutMs != null ? opts.timeoutMs
    : (method === 'GET' ? GET_TIMEOUT_MS : POST_TIMEOUT_MS);
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl && limit ? setTimeout(() => ctrl.abort(), limit) : null;
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: opts.signal || (ctrl ? ctrl.signal : undefined),
    });
  } catch (err) {
    // Timed out, refused, or the server went away mid-flight. Either way the
    // caller is told something happened — never a promise that never settles.
    throw new NetworkError(path, !!(err && err.name === 'AbortError'));
  } finally {
    if (timer) clearTimeout(timer);
  }
  let payload = null;
  const text = await res.text();
  if (text) { try { payload = JSON.parse(text); } catch { payload = { raw: text }; } }
  noteGeneration(payload);
  if (!res.ok) throw new ApiError(res.status, payload, path);
  return payload;
}

/** The transport failed: timed out, refused, or the server went away mid-flight. */
export class NetworkError extends Error {
  constructor(path, timedOut) {
    super(timedOut ? `timed out on ${path}` : `could not reach ${path}`);
    this.status = 0;
    this.path = path;
    this.timedOut = !!timedOut;
    this.body = {};
    this.fields = null;
  }
}

// ---- which server are we talking to? -------------------------------------
//
// The server stamps a `generation` (one id per PROCESS) on /healthz, on the SSE
// hello and cursor frames, and on /api/board and /api/events. If it changes
// under an open tab, the backend that tab was talking to is gone: its stream is
// dead, anything it had in flight is lost, and its credentials may no longer be
// good. api.js only records what came back; live.js and app.js decide what to
// do about it.

let generation = null;
const genListeners = new Set();

function noteGeneration(payload) {
  if (payload && typeof payload === 'object' && payload.generation) {
    rememberGeneration(payload.generation);
  }
}

// ---- is this server as new as this page? ---------------------------------
//
// It is, or it is about to be, and the page no longer has an opinion about it.
//
// A board is a long-running process and web/ is read off disk per request, so a
// tab really can be running today's JS against a server that started weeks ago.
// This page used to answer that with a banner — "this board needs a restart to
// pick up new features — run `sprintd stop` then `sprintd start` in its
// project" — and the user's answer to the banner was "don't show me this
// notice". He is right: restarting a server is not a job you hand to the person
// reading the board. The server watches its own source file and re-execs itself
// when it changes (sprintd's `check_code_update`), so the gap closes by itself,
// usually within seconds, and the tab re-syncs off `generation` exactly as it
// does after any other restart.
//
// The server still publishes `api_version` — the log and the hub use it to say
// which build answered — but nothing on this page reads it, and no version
// number is ever allowed to turn into a chore on screen again.

/** Record a generation seen anywhere. Returns true if it is a NEW server. */
export function rememberGeneration(g) {
  if (!g) return false;
  if (generation == null) { generation = g; return false; }
  if (g === generation) return false;
  generation = g;
  for (const fn of genListeners) { try { fn(g); } catch {} }
  return true;
}

export function getGeneration() { return generation; }

/** Notified when the server answering us is provably a different process. */
export function onServerGeneration(fn) {
  genListeners.add(fn);
  return () => genListeners.delete(fn);
}

// Every send takes a `key`. Retrying a failed send re-POSTs with the SAME
// Idempotency-Key, so a send that was actually slow-but-landed (the server got
// it, the answer never made it back) replays instead of duplicating.
export const api = {
  board: () => req('GET', '/api/board'),
  card: (num) => req('GET', `/api/cards/${num}`),
  events: (after, limit = 500) => req('GET', `/api/events?after=${after || 0}&limit=${limit}`),
  // Every live sprint on this machine, this one flagged `self` — the title
  // switcher. Server-cached; the browser polls it every 30s, never SSE.
  siblings: () => req('GET', '/api/siblings', undefined, { timeoutMs: 8000 }),
  // No auth, tiny deadline: this is the "is anyone home, and is it still the
  // same anyone" probe we run when the transport falls over.
  health: () => req('GET', '/healthz', undefined, { timeoutMs: 5000 }),

  submit: (payload, key) => req('POST', '/api/cards', payload, { idempotencyKey: key }),
  // text, images, or both — exactly like dropping work on the board
  chat: (num, text, images, key) => req('POST', `/api/cards/${num}/chat`,
    images && images.length ? { text, images } : { text }, { idempotencyKey: key }),
  // An answer takes screenshots exactly like a chat line does (card #66): the
  // picture rides the answer event itself, so the agent that reads the answer
  // sees what you were pointing at without hunting the thread for it.
  answer: (num, question_id, text, images, key) =>
    req('POST', `/api/cards/${num}/answer`,
      images && images.length ? { question_id, text, images } : { question_id, text },
      { idempotencyKey: key }),
  action: (num, action, extra, key) =>
    req('POST', `/api/cards/${num}/action`, { action, ...(extra || {}) }, { idempotencyKey: key }),
  retry: (num, key) =>
    req('POST', `/api/cards/${num}/action`, { action: 'retry' }, { idempotencyKey: key }),
  // A bounce carries the screenshot that shows what is wrong (card #66) — it
  // lands on the verdict event, so the agent picking the card back up sees it
  // in the timeline next to the notes.
  verdict: (num, verdict, notes, key, images) =>
    req('POST', `/api/cards/${num}/verdict`, {
      verdict,
      ...(notes ? { notes } : {}),
      ...(images && images.length ? { images } : {}),
    }, { idempotencyKey: key }),

  sidebar: (text, images, key) => req('POST', '/api/sidebar',
    images && images.length ? { text, images, actor: 'user' } : { text, actor: 'user' },
    { idempotencyKey: key }),
  holdMode: (on) => req('POST', '/api/sprint', { action: 'set_hold_mode', hold_mode: !!on }),
  // The Resume button in the account-limit banner. Server-side this is the
  // same single-writer clear the clock uses, so pressing it twice (or pressing
  // it on two boards) still emits exactly one `limit_cleared` per board.
  clearLimit: (id, key) => req('POST', `/api/limits/${id}/clear`, {},
    { idempotencyKey: key }),

  // Dispatch policy: model policy, executors, concurrency. A document you
  // replace, not an event you append — hence PUT.
  settings: () => req('GET', '/api/settings'),
  saveSettings: (patch) => req('PUT', '/api/settings', patch),

  // The report library. `scope` defaults to the OPEN sprint server-side — the
  // header link exists only when THIS sprint has a report, so the default is
  // the number that decides it. `scope: 'all'` is history, never the condition.
  reports: (scope) => req('GET', `/api/reports${scope === 'all' ? '?scope=all' : ''}`),
  // One report, rendered: `.html` for markdown (rendered by the server out of
  // escaped text), `raw_url` + `sandboxed` for author HTML.
  report: (sha, ext) => req('GET', `/api/reports/${sha}.${ext}`),
};

// ---- reports -------------------------------------------------------------
//
// A report is an attachment like a screenshot is, so it arrives in the same
// `payload.attachments` list. It is told apart by ONE field the server sets and
// a worker cannot forge into existence: `doc` ∈ {md, html}.

/** Is this attachment ref a report document rather than a picture? */
export function isReportRef(ref) {
  return !!(ref && typeof ref === 'object'
    && (ref.doc === 'md' || ref.doc === 'html')
    && /^[0-9a-f]{64}$/.test(String(ref.sha256 || '')));
}

/** The stable in-app URL for one report — the thing a link can point at. */
export function reportHash(ref) {
  if (!isReportRef(ref)) return null;
  return `#/report/${ref.sha256}.${ref.doc === 'html' ? 'html' : 'md'}`;
}

/** What this document calls itself, falling back to its filename. */
export function reportTitle(ref) {
  if (!ref || typeof ref !== 'object') return 'report';
  return ref.title || ref.name || 'report';
}

/**
 * Attachment refs. The server hands every ref a ready-to-use `url`
 * (`/api/attachments/<sha256>.<ext>`); the rest of this is belt-and-braces for a
 * raw sha or an already-absolute URL. A bare filesystem path is NOT a URL — the
 * browser cannot load it, so it resolves to null and the thumb is skipped.
 */
export function attachmentUrl(ref) {
  if (!ref) return null;
  if (typeof ref === 'object') {
    if (ref.url) return ref.url;
    const sha = ref.sha256 || ref.sha;
    if (sha && /^[0-9a-f]{64}$/.test(sha)) {
      const ext = /jpe?g/.test(ref.mime || ref.ext || '') ? 'jpg' : 'png';
      return `/api/attachments/${sha}.${ext}`;
    }
    return null;
  }
  const s = String(ref);
  if (/^(https?:|data:|blob:)/.test(s)) return s;
  if (/^\/api\/attachments\/[0-9a-f]{64}\.(png|jpe?g)$/.test(s)) return s;
  const m = s.match(/^([0-9a-f]{64})\.(png|jpe?g)$/);
  if (m) return `/api/attachments/${m[1]}.${m[2] === 'png' ? 'png' : 'jpg'}`;
  return null;
}

export function attachmentCaption(ref) {
  if (ref && typeof ref === 'object') return ref.caption || ref.label || ref.name || '';
  return '';
}
