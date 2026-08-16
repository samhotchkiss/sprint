// The shared paste/drop handler's pure half (card #66).
//
// `initAttach` needs a DOM, and the browser pass covers it. These two do not:
// picking the images out of a clipboard payload, and stripping the data: prefix
// off them on the way to the server. They are the two places a paste can go
// wrong silently — a screenshot dropped because the clipboard called it
// something unexpected, or a body the server refuses because the prefix rode
// along — so they get a test with no browser in it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { imageFiles, toBase64List, MAX_ATTACH_BYTES } from '../web/attach.js';

const file = (name, type) => ({ name, type });

/** A clipboard/drag payload shaped the way the browser hands it over. */
function dt({ items, files }) {
  return {
    items: items && items.map((it) => ({ ...it, getAsFile: () => it.file })),
    files,
  };
}

test('a pasted screenshot is found in the clipboard items', () => {
  const png = file('shot.png', 'image/png');
  const found = imageFiles(dt({ items: [{ kind: 'file', type: 'image/png', file: png }] }));
  assert.deepEqual(found, [png]);
});

test('the text that rides along with a pasted image is ignored', () => {
  const png = file('shot.png', 'image/png');
  const found = imageFiles(dt({
    items: [
      { kind: 'string', type: 'text/html', file: null },
      { kind: 'string', type: 'text/plain', file: null },
      { kind: 'file', type: 'image/png', file: png },
    ],
  }));
  assert.deepEqual(found, [png]);
});

test('anything that is not a picture is left alone', () => {
  const pdf = file('notes.pdf', 'application/pdf');
  assert.deepEqual(imageFiles(dt({ items: [{ kind: 'file', type: 'application/pdf', file: pdf }] })), []);
  assert.deepEqual(imageFiles(dt({ files: [pdf] })), []);
});

test('a drop falls back to the file list when there are no items', () => {
  const jpg = file('photo.jpg', 'image/jpeg');
  const pdf = file('notes.pdf', 'application/pdf');
  assert.deepEqual(imageFiles(dt({ files: [jpg, pdf] })), [jpg]);
});

test('an empty or missing clipboard is not an error', () => {
  assert.deepEqual(imageFiles(null), []);
  assert.deepEqual(imageFiles(undefined), []);
  assert.deepEqual(imageFiles(dt({})), []);
});

test('the data: prefix is stripped before the images are sent', () => {
  const out = toBase64List([
    { dataUrl: 'data:image/png;base64,AAAA' },
    { dataUrl: 'data:image/jpeg;base64,BBBB' },
  ]);
  assert.deepEqual(out, ['AAAA', 'BBBB']);
});

test('no images is an empty list, never a crash', () => {
  assert.deepEqual(toBase64List([]), []);
  assert.deepEqual(toBase64List(null), []);
  assert.deepEqual(toBase64List(undefined), []);
});

test('the client cap sits under the server 25 MB limit', () => {
  assert.ok(MAX_ATTACH_BYTES < 25 * 1024 * 1024);
});
