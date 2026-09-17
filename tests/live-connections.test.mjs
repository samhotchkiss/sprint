import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Live } from '../web/live.js';
import { api } from '../web/api.js';

test('many board tabs leave connections free and resync does not reopen streams', async () => {
  const previous = globalThis.EventSource;
  const events = api.events;
  let streams = 0, requests = 0;
  globalThis.EventSource = class { constructor() { streams++; } close() {} addEventListener() {} };
  api.events = async () => { requests++; return { events: [] }; };
  const tabs = Array.from({ length: 8 }, () => new Live({ onEvents() {} }));
  try {
    tabs.forEach(tab => tab.start());
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(streams, 0);
    assert.equal(requests, 8);
    tabs.forEach(tab => tab.resync());
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(streams, 0);
    assert.equal(requests, 16);
    assert.ok(tabs.every(tab => tab.mode === 'polling' && !tab.retryTimer));
  } finally {
    tabs.forEach(tab => tab.stop());
    api.events = events;
    globalThis.EventSource = previous;
  }
});

test('stopping during an outstanding poll leaves transport stopped', async () => {
  const events = api.events;
  let resolve;
  api.events = () => new Promise(done => { resolve = done; });
  const tab = new Live({ onEvents() {} });
  try {
    tab.start();
    tab.stop();
    resolve({ events: [] });
    await new Promise(done => setImmediate(done));
    assert.equal(tab.mode, 'idle');
    assert.equal(tab.pollTimer, null);
  } finally { tab.stop(); api.events = events; }
});
