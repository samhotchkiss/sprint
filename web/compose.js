// Submit box: text + pasted/attached images (multiple, removable) + hold toggle.
import { h, clear, uid } from './util.js';

const MAX_BYTES = 24 * 1024 * 1024;   // server caps the request at 25 MB

export function initCompose({ form, textarea, thumbsEl, fileInput, errEl, onSubmit }) {
  let images = [];   // {id, name, dataUrl, mime, bytes}

  function grow() {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(200, Math.max(textarea.scrollHeight, 40)) + 'px';
  }

  function totalBytes() { return images.reduce((n, i) => n + i.bytes, 0); }

  function setError(msg) {
    errEl.hidden = !msg;
    errEl.textContent = msg || '';
  }

  function renderThumbs() {
    clear(thumbsEl);
    thumbsEl.hidden = images.length === 0;
    for (const img of images) {
      thumbsEl.appendChild(h('div.thumb',
        h('img', { src: img.dataUrl, alt: img.name || 'attachment' }),
        h('button.thumb-x', {
          type: 'button', 'aria-label': 'remove image',
          onclick: () => { images = images.filter((i) => i.id !== img.id); renderThumbs(); setError(''); },
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
        images.push({ id: uid(), name: file.name || 'pasted image', dataUrl, mime: file.type, bytes });
      } catch { setError('could not read that image'); }
    }
    renderThumbs();
  }

  textarea.addEventListener('input', () => { grow(); setError(''); });
  textarea.addEventListener('paste', (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    const files = [];
    for (const it of items) {
      if (it.kind === 'file' && /^image\//.test(it.type)) {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); }
  });
  fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

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
    if (!text && !images.length) { setError('say something or drop an image'); textarea.focus(); return; }
    setError('');
    const payload = { text: text || undefined, images: images.slice() };
    textarea.value = '';
    images = [];
    renderThumbs();
    grow();
    onSubmit(payload);
  });

  grow();
  return { addFiles, focus: () => textarea.focus() };
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
