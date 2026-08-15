// Liveness transport: SSE first (with Last-Event-ID reconnect handled by the browser),
// degrading to polling /api/events if the stream keeps failing.
// The cursor drain is the truth — every (re)connect catches up from our last seq.
import { api, ApiError } from './api.js';

const SSE_FAIL_LIMIT = 3;       // consecutive stream failures before we give up on SSE
const POLL_MS = 3000;
const POLL_MAX_MS = 15000;
const SSE_RETRY_MS = 120000;    // while polling, occasionally re-try the stream

export class Live {
  constructor({ onEvents, onStatus, onAuthError, onCursor }) {
    this.onEvents = onEvents;
    this.onStatus = onStatus || (() => {});
    this.onAuthError = onAuthError || (() => {});
    // The session's drain cursor arrives out-of-band (a named `cursor` SSE frame,
    // or the `cursor` field on a poll) — it is not an event and never advances seq.
    this.onCursor = onCursor || (() => {});
    this.seq = 0;
    this.mode = 'idle';         // idle | sse | polling | error
    this.fails = 0;
    this.es = null;
    this.pollTimer = null;
    this.retryTimer = null;
    this.pollDelay = POLL_MS;
    this.stopped = false;
  }

  start(fromSeq = 0) {
    this.seq = Math.max(this.seq, fromSeq || 0);
    this.stopped = false;
    if (typeof EventSource === 'function') this.openStream();
    else this.startPolling();
  }

  stop() {
    this.stopped = true;
    this.closeStream();
    clearTimeout(this.pollTimer);
    clearTimeout(this.retryTimer);
    this.setMode('idle');
  }

  setMode(mode) {
    if (this.mode === mode) return;
    this.mode = mode;
    this.onStatus(mode);
  }

  // ---- SSE ---------------------------------------------------------------

  openStream() {
    this.closeStream();
    let es;
    try {
      es = new EventSource('/api/stream', { withCredentials: true });
    } catch {
      this.startPolling();
      return;
    }
    this.es = es;
    es.onopen = () => {
      this.fails = 0;
      this.setMode('sse');
      clearTimeout(this.pollTimer);
      this.catchUp();
    };
    const handle = (e) => this.ingestMessage(e);
    es.onmessage = handle;
    for (const kind of ['event', 'events', 'sprint']) es.addEventListener(kind, handle);
    es.addEventListener('cursor', (e) => this.ingestCursor(e));
    es.onerror = () => {
      // EventSource retries on its own; only count a failure when it truly closed,
      // or when it never opened.
      this.fails += 1;
      if (es.readyState === EventSource.CLOSED || this.fails >= SSE_FAIL_LIMIT) {
        this.closeStream();
        this.startPolling();
      }
    };
  }

  closeStream() {
    if (this.es) { try { this.es.close(); } catch {} this.es = null; }
  }

  ingestMessage(e) {
    if (!e || !e.data) return;                 // heartbeat comments never reach here
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    let list = Array.isArray(data) ? data : (Array.isArray(data.events) ? data.events : [data]);
    list = list.filter((ev) => ev && typeof ev === 'object');
    if (!list.length) return;
    if (list.length === 1 && list[0].seq == null && e.lastEventId) {
      const n = Number(e.lastEventId);
      if (!Number.isNaN(n)) list[0].seq = n;
    }
    this.deliver(list);
  }

  ingestCursor(e) {
    if (!e || !e.data) return;
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    const seq = Number(data && data.cursor != null ? data.cursor : data && data.seq);
    if (!Number.isNaN(seq)) this.onCursor(seq);
  }

  // ---- polling -----------------------------------------------------------

  startPolling() {
    if (this.stopped) return;
    this.setMode('polling');
    this.pollDelay = POLL_MS;
    clearTimeout(this.pollTimer);
    this.poll();
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => { if (!this.stopped) this.openStream(); }, SSE_RETRY_MS);
  }

  async poll() {
    if (this.stopped || this.mode === 'sse') return;
    try {
      await this.catchUp();
      this.pollDelay = POLL_MS;
      this.setMode('polling');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) { this.onAuthError(); return; }
      this.pollDelay = Math.min(POLL_MAX_MS, Math.round(this.pollDelay * 1.6));
      this.setMode('error');
    }
    if (this.stopped || this.mode === 'sse') return;
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => this.poll(), this.pollDelay);
  }

  // ---- shared ------------------------------------------------------------

  async catchUp() {
    const res = await api.events(this.seq, 500);
    const list = Array.isArray(res) ? res : (res && Array.isArray(res.events) ? res.events : []);
    if (res && !Array.isArray(res) && res.cursor != null) {
      const cur = Number(res.cursor);
      if (!Number.isNaN(cur)) this.onCursor(cur);
    }
    if (list.length) this.deliver(list);
    return list;
  }

  deliver(list) {
    const fresh = [];
    for (const ev of list) {
      const seq = Number(ev.seq != null ? ev.seq : ev.id);
      if (!Number.isNaN(seq)) {
        if (seq <= this.seq) continue;          // dedupe by seq (at-least-once transport)
        ev.seq = seq;
        this.seq = Math.max(this.seq, seq);
      }
      fresh.push(ev);
    }
    if (fresh.length) this.onEvents(fresh);
  }
}
