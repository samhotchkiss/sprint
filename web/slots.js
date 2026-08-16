// The rail's SLOTS — the little bit of bookkeeping that lets one panel be
// rebuilt in pieces without the whole thing flickering, and without a piece
// quietly appearing twice.
//
// The rail is not a virtual DOM. It is a handful of named slots — head, thread,
// verdict bar, composer — each rebuilt only when its own signature changes, so
// the composer you are mid-sentence in survives a board frame landing under it
// (card #46). A slot is named by a CLASS, and the class is how the next paint
// finds the node it put there last time.
//
// The class is stamped on HERE rather than trusted to the builder. That is not
// tidiness: card #53's verdict bar was built as `.review-bar.is-verdict` while
// its slot was called `verdict-bar`, so every paint looked for a node that had
// never existed, found nothing, and inserted ANOTHER bar. Two Approve buttons,
// then three. Owning the class is what makes "replace what is there" mean it.
//
// No DOM APIs beyond querySelector / appendChild / insertBefore / replaceChild
// / removeChild and classList / dataset, so this file is directly testable.

/**
 * A slot that is always present. Rebuilds only when `sig` changes; returns the
 * node either way.
 */
export function syncPart(root, cls, sig, build) {
  const found = root.querySelector('.' + cls);
  const want = String(sig);
  if (found && found.dataset.sig === want) return found;
  const node = build();
  node.classList.add(cls);
  node.dataset.sig = want;
  if (found) root.replaceChild(node, found);
  else root.appendChild(node);
  return node;
}

/**
 * A slot that is sometimes not there at all. Same signature contract as
 * `syncPart`; a null signature REMOVES it, which is how a card that stops
 * asking for a verdict loses its bar rather than keeping a dead one. It has to
 * sit above the composer, so it is inserted rather than appended.
 */
export function syncOptional(root, cls, sig, build) {
  const found = root.querySelector('.' + cls);
  if (sig == null) {
    if (found) root.removeChild(found);
    return null;
  }
  const want = String(sig);
  if (found && found.dataset.sig === want) return found;
  const node = build();
  node.classList.add(cls);
  node.dataset.sig = want;
  if (found) root.replaceChild(node, found);
  else root.insertBefore(node, root.querySelector('.composer') || null);
  return node;
}
