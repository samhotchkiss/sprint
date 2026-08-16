// Who a line is FROM (card #63).
//
// User ruling, verbatim: "This is good, but I also meant that the session agent
// gave themselves a name. Like "Chuck"". So the board carries two names — the
// sprint's, and the session's own — and every message the session writes is
// signed with the second one. A session that never introduced itself is still
// "Session", and that fallback is the part that has to keep working: the label
// is on every line in the sidebar and every card thread.
//
// web/state.js is DOM-free at import time, so the label model can be tested
// without a browser. Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { store, applyBoard, actorLabel, sessionLabel } from '../web/state.js';

/** A board payload with just the fields these tests care about. */
function board(extra = {}) {
  return {
    sprint: { id: 1, title: 'Billing week', hold_mode: false },
    cards: [],
    sidebar: [],
    seq: 1,
    ...extra,
  };
}

test('nobody has a name until the session takes one', () => {
  applyBoard(board());
  assert.equal(store.agentName, '');
  assert.equal(sessionLabel(), 'Session');
  assert.equal(actorLabel('session'), 'Session');
});

test('a named session signs its lines with its name', () => {
  applyBoard(board({ agent_name: 'Chuck' }));
  assert.equal(store.agentName, 'Chuck');
  assert.equal(sessionLabel(), 'Chuck');
  assert.equal(actorLabel('session'), 'Chuck');
});

test('nobody else is renamed by it', () => {
  applyBoard(board({ agent_name: 'Chuck' }));
  assert.equal(actorLabel('user'), 'You');
  assert.equal(actorLabel('worker'), 'Agent');
  assert.equal(actorLabel('server'), 'Board');
  // an actor the board has never heard of reads as an agent, not as blank
  assert.equal(actorLabel('gremlin'), 'Agent');
});

test('a rename lands on the next board frame', () => {
  applyBoard(board({ agent_name: 'Chuck' }));
  applyBoard(board({ agent_name: 'Dolores' }));
  assert.equal(sessionLabel(), 'Dolores');
});

test('an emptied name falls straight back to the generic label', () => {
  applyBoard(board({ agent_name: 'Chuck' }));
  applyBoard(board({ agent_name: '' }));
  assert.equal(store.agentName, '');
  assert.equal(sessionLabel(), 'Session');
});

test('a board that does not send the field is not a session dropping its name', () => {
  // An older server, or a payload shape that predates card #63: silence is not
  // the same as "", and forgetting the name on every poll would make the
  // sidebar flicker between "Chuck" and "Session".
  applyBoard(board({ agent_name: 'Chuck' }));
  applyBoard(board());
  assert.equal(sessionLabel(), 'Chuck');
});

test('the sprint name and the session name are two different names', () => {
  applyBoard(board({ agent_name: 'Chuck' }));
  assert.equal(store.sprint.title, 'Billing week');
  assert.equal(store.agentName, 'Chuck');
});

test('surrounding whitespace never reaches a label', () => {
  applyBoard(board({ agent_name: '  Chuck ' }));
  assert.equal(sessionLabel(), 'Chuck');
});
