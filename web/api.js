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

async function req(method, path, body, opts = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (method !== 'GET') headers['Idempotency-Key'] = opts.idempotencyKey || uid();
  const res = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: opts.signal,
  });
  let payload = null;
  const text = await res.text();
  if (text) { try { payload = JSON.parse(text); } catch { payload = { raw: text }; } }
  if (!res.ok) throw new ApiError(res.status, payload, path);
  return payload;
}

export const api = {
  board: () => req('GET', '/api/board'),
  card: (num) => req('GET', `/api/cards/${num}`),
  events: (after, limit = 500) => req('GET', `/api/events?after=${after || 0}&limit=${limit}`),

  submit: (payload, key) => req('POST', '/api/cards', payload, { idempotencyKey: key }),
  chat: (num, text) => req('POST', `/api/cards/${num}/chat`, { text }),
  answer: (num, question_id, text) => req('POST', `/api/cards/${num}/answer`, { question_id, text }),
  action: (num, action, extra) => req('POST', `/api/cards/${num}/action`, { action, ...(extra || {}) }),
  retry: (num) => req('POST', `/api/cards/${num}/action`, { action: 'retry' }),
  verdict: (num, verdict, notes) =>
    req('POST', `/api/cards/${num}/verdict`, notes ? { verdict, notes } : { verdict }),

  sidebar: (text) => req('POST', '/api/sidebar', { text, actor: 'user' }),
  holdMode: (on) => req('POST', '/api/sprint', { action: 'set_hold_mode', hold_mode: !!on }),
};

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
