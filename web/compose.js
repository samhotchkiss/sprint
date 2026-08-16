// A composer: a text box with a send, wrapped around the shared attachment
// handler.
//
// It started as the "+ Drop work" sheet's box, and it is now also the rail's
// composer — card threads and the session chat — because "paste a screenshot
// into this conversation" should behave identically wherever you are.
//
// Card #66 moved the paste/drop/thumbnail half of it into `attach.js`, because
// "wherever you are" turned out to include places that are not composers at all:
// the bounce-notes box has no form and no Send, and it has to behave the same.
// What is left in here is what makes a composer a composer — Return sends,
// Shift+Return newlines, the box grows with the text, submit clears it.
import { initAttach, imageFiles, toBase64List, MAX_ATTACH_BYTES } from './attach.js';

export { imageFiles, toBase64List, MAX_ATTACH_BYTES };

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
  function grow() {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(maxHeight, Math.max(textarea.scrollHeight, minHeight)) + 'px';
  }

  const att = initAttach({
    zone: form, textarea, thumbsEl, errEl, fileInput, images: bag,
  });

  textarea.addEventListener('input', (e) => { grow(); att.setError(''); if (onInput) onInput(e); });
  // Return sends, Shift+Return makes a new line. Cmd/Ctrl+Return still sends,
  // because that is what the spec documented and muscle memory is real.
  textarea.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (e.shiftKey) return;
    e.preventDefault();
    form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true }));
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textarea.value.trim();
    const images = att.get();
    if (!text && !images.length) { att.setError('say something or drop an image'); textarea.focus(); return; }
    att.setError('');
    const payload = { text: text || undefined, images: images.slice() };
    textarea.value = '';
    att.set([]);
    att.render();
    grow();
    onSubmit(payload);
  });

  grow();
  return {
    addFiles: att.addFiles,
    focus: () => textarea.focus(),
    grow,
    isEmpty: () => !textarea.value.trim() && att.isEmpty(),
  };
}
