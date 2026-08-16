// The one place images get attached to anything you type.
//
// It started as the "+ Drop work" sheet's box, and it is now also the rail's
// composer — card threads and the session chat — because "paste a screenshot
// into this conversation" should behave identically wherever you are. One
// implementation, three mounts: paste, drop, file-picker, removable thumbnails,
// Return sends / Shift+Return newlines.
import { h, clear, uid } from './util.js';

const MAX_BYTES = 24 * 1024 * 1024;   // server caps the request at 25 MB

/**
 * @param images  optional {get, set} accessor so the attached images can live
 *                outside this closure. The rail is re-rendered whenever the
 *                board moves, and a thumbnail you pasted must not evaporate
 *                because an unrelated event arrived; the sheet, which is built
 *                once, keeps its list in here.
 */
export function initCompose({
  form, textarea, thumbsEl, fileInput, errEl, onSubmit, onInput,
  images: bag, minHeight = 40, maxHeight = 200,
}) {
  let own = [];   // {id, name, dataUrl, mime, bytes}
  const get = () => (bag ? bag.get() : own);
  const set = (v) => { if (bag) bag.set(v); else own = v; };

  function grow() {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(maxHeight, Math.max(textarea.scrollHeight, minHeight)) + 'px';
  }

  function totalBytes() { return get().reduce((n, i) => n + i.bytes, 0); }

  function setError(msg) {
    errEl.hidden = !msg;
    errEl.textContent = msg || '';
  }

  function renderThumbs() {
    const images = get();
    clear(thumbsEl);
    thumbsEl.hidden = images.length === 0;
    for (const img of images) {
      thumbsEl.appendChild(h('div.thumb',
        h('img', { src: img.dataUrl, alt: img.name || 'attachment' }),
        h('button.thumb-x', {
          type: 'button', 'aria-label': 'remove image',
          onclick: () => { set(get().filter((i) => i.id !== img.id)); renderThumbs(); setError(''); },
        }, '✕')));
    }
  }

  async function addFiles(files) {
    const accepted = Array.from(files || []).filter((f) => f && /^image\//.test(f.type));
    if (!accepted.length) return;
    for (const file of accepted) {
      try {
        const dataUrl = await readFile(file);
        const bytes = Math.round((dataUrl.length - dataUrl.indexOf(',') - 1) * 0.75);
        if (totalBytes() + bytes > MAX_BYTES) { setError('that would blow the 25 MB upload cap'); break; }
        set(get().concat([{ id: uid(), name: file.name || 'pasted image', dataUrl, mime: file.type, bytes }]));
      } catch { setError('could not read that image'); }
    }
    renderThumbs();
  }

  textarea.addEventListener('input', (e) => { grow(); setError(''); if (onInput) onInput(e); });
  textarea.addEventListener('paste', (e) => {
    const files = imageFiles(e.clipboardData);
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  // Return sends, Shift+Return makes a new line. Cmd/Ctrl+Return still sends,
  // because that is what the spec documented and muscle memory is real.
  textarea.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (e.shiftKey) return;
    e.preventDefault();
    form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true }));
  });
  if (fileInput) {
    fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
  }

  // drag & drop anywhere on the compose box
  for (const evt of ['dragover', 'drop']) {
    form.addEventListener(evt, (e) => {
      e.preventDefault();
      form.classList.toggle('dropping', evt === 'dragover');
      if (evt === 'drop' && e.dataTransfer) addFiles(e.dataTransfer.files);
    });
  }
  form.addEventListener('dragleave', () => form.classList.remove('dropping'));

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textarea.value.trim();
    const images = get();
    if (!text && !images.length) { setError('say something or drop an image'); textarea.focus(); return; }
    setError('');
    const payload = { text: text || undefined, images: images.slice() };
    textarea.value = '';
    set([]);
    renderThumbs();
    grow();
    onSubmit(payload);
  });

  renderThumbs();
  grow();
  return {
    addFiles,
    focus: () => textarea.focus(),
    grow,
    isEmpty: () => !textarea.value.trim() && !get().length,
  };
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

function readFile(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

/** SPEC: images is a list of base64 png/jpeg — we send the payload without the data: prefix. */
export function toBase64List(images) {
  return images.map((i) => i.dataUrl.slice(i.dataUrl.indexOf(',') + 1));
}
