// Paste a screenshot into ANY box you can type in.
//
// User verbatim (card #66): "i need to be able to paste images into ANY text
// entry area-- I just tried to bounce a card with a screenshot showing the
// issue, it wouldn't let me paste an image in."
//
// This is the one implementation of that behaviour, and every text box on the
// board mounts it: the Drop-work sheet, the rail's card composer, the session
// chat, the answer box, and the bounce-notes box that was the failing case. It
// is deliberately NOT tied to a <form> or to a send button — a bounce box has
// neither — so the only thing it needs is a zone to watch, a textarea to catch
// pastes from, and somewhere to draw the thumbnails.
//
// What it gives every box, identically:
//   paste            an image on the clipboard becomes a thumbnail
//   drop             the same, dragged in from the desktop
//   pick             the paperclip, for the times paste is not how you got it
//   thumbnails       above the box, each with its own ✕ to take it back off
//   one size cap     the server's 25 MB, refused here with words not a 413
//   images only      anything that is not a picture is ignored, exactly as the
//                    server refuses it — no silent half-attachments
//
// Focus is the thing this file is most careful about (card #46). A thumbnail
// strip appearing must never take the caret out of the sentence you are typing,
// so every render happens inside `preserveCaret`: whatever was focused, with
// whatever it had selected, is still focused with the same selection afterwards.
import { h, clear, uid } from './util.js';

/** The server caps the request at 25 MB; we refuse just under it, in words. */
export const MAX_ATTACH_BYTES = 24 * 1024 * 1024;

/**
 * Run `fn`, then put the caret back exactly where it was.
 *
 * Card #46 is the rule this keeps: the composer must not lose the caret when
 * the thumbnail strip renders. Removing and re-adding nodes elsewhere in the
 * form usually leaves focus alone — usually is not a guarantee, and this is.
 */
export function preserveCaret(fn) {
  const a = document.activeElement;
  const text = a && (a instanceof HTMLTextAreaElement || a instanceof HTMLInputElement);
  const start = text ? a.selectionStart : null;
  const end = text ? a.selectionEnd : null;
  try {
    fn();
  } finally {
    if (text && document.activeElement !== a && a.isConnected) {
      a.focus({ preventScroll: true });
      try { a.setSelectionRange(start, end); } catch { /* number inputs and friends */ }
    }
  }
}

/** The image files on a paste/drop, or []. */
export function imageFiles(dt) {
  const out = [];
  const items = dt && dt.items;
  if (items) {
    for (const it of items) {
      if (it.kind === 'file' && /^image\//.test(it.type)) {
        const f = it.getAsFile();
        if (f) out.push(f);
      }
    }
  }
  if (!out.length && dt && dt.files) {
    for (const f of dt.files) if (f && /^image\//.test(f.type)) out.push(f);
  }
  return out;
}

/** SPEC: images is a list of base64 png/jpeg — we send the payload without the data: prefix. */
export function toBase64List(images) {
  return (images || []).map((i) => i.dataUrl.slice(i.dataUrl.indexOf(',') + 1));
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

/**
 * Mount paste/drop/pick on one text entry surface.
 *
 * @param zone      the element a drop counts on (a form, a div — anything)
 * @param textarea  where a paste is caught. Optional only in theory.
 * @param thumbsEl  where the thumbnails are drawn; hidden while there are none
 * @param errEl     one line of plain English when something is refused
 * @param fileInput optional <input type=file> behind a paperclip
 * @param images    optional {get, set} so the pending list can live outside this
 *                  closure — the rail and the verdict bar are rebuilt whenever
 *                  the board moves, and a screenshot you just pasted must not
 *                  evaporate because an unrelated event arrived.
 * @param onChange  called after anything is added or removed
 * @returns {{addFiles, get, set, clear, render, isEmpty}}
 */
export function initAttach({
  zone, textarea, thumbsEl, errEl, fileInput, images: bag, onChange,
}) {
  let own = [];   // {id, name, dataUrl, mime, bytes}
  const get = () => (bag ? bag.get() : own);
  const set = (v) => { if (bag) bag.set(v); else own = v; };

  function totalBytes() { return get().reduce((n, i) => n + i.bytes, 0); }

  function setError(msg) {
    if (!errEl) return;
    errEl.hidden = !msg;
    errEl.textContent = msg || '';
  }

  function render() {
    if (!thumbsEl) return;
    preserveCaret(() => {
      const list = get();
      clear(thumbsEl);
      thumbsEl.hidden = list.length === 0;
      for (const img of list) {
        thumbsEl.appendChild(h('div.thumb',
          h('img', { src: img.dataUrl, alt: img.name || 'attachment' }),
          h('button.thumb-x', {
            type: 'button', 'aria-label': 'remove image',
            onclick: () => {
              set(get().filter((i) => i.id !== img.id));
              render();
              setError('');
              if (onChange) onChange(get());
            },
          }, '✕')));
      }
    });
  }

  async function addFiles(files) {
    const accepted = Array.from(files || []).filter((f) => f && /^image\//.test(f.type));
    if (!accepted.length) return;
    for (const file of accepted) {
      try {
        // eslint-disable-next-line no-await-in-loop
        const dataUrl = await readFile(file);
        const bytes = Math.round((dataUrl.length - dataUrl.indexOf(',') - 1) * 0.75);
        if (totalBytes() + bytes > MAX_ATTACH_BYTES) {
          setError('that would blow the 25 MB upload cap');
          break;
        }
        set(get().concat([{
          id: uid(), name: file.name || 'pasted image', dataUrl, mime: file.type, bytes,
        }]));
      } catch { setError('could not read that image'); }
    }
    render();
    if (onChange) onChange(get());
  }

  if (textarea) {
    textarea.addEventListener('paste', (e) => {
      const files = imageFiles(e.clipboardData);
      if (files.length) { e.preventDefault(); addFiles(files); }
    });
  }
  if (fileInput) {
    fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
  }
  if (zone) {
    for (const evt of ['dragover', 'drop']) {
      zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.toggle('dropping', evt === 'dragover');
        if (evt === 'drop' && e.dataTransfer) addFiles(e.dataTransfer.files);
      });
    }
    zone.addEventListener('dragleave', () => zone.classList.remove('dropping'));
  }

  render();

  return {
    addFiles,
    get,
    set,
    render,
    setError,
    clear: () => { set([]); render(); setError(''); },
    isEmpty: () => !get().length,
  };
}

/** Drawn, not typed: an emoji paperclip renders differently on every machine. */
export function paperclip(size = 17) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M20 11.5 12.2 19.3a5 5 0 0 1-7.1-7.1l8-8a3.4 3.4 0 1 1 4.8 4.8l-8 8a1.8 1.8 0 0 1-2.5-2.5l7.2-7.2');
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.6');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(path);
  return svg;
}
