// Reports: an agent's real output is a DOCUMENT, and a document does not fit in
// a one-liner.
//
// User's ask, verbatim: "Some way built into sprint to embed reports." His
// chosen shape, verbatim: "Yes, 1& 2, but not a rail. A link in the header",
// refined to "link should only appear once there's a report within the sprint".
//
// So there are exactly three surfaces and no rail:
//
//   in the thread   a report renders with the same skim/expand pattern every
//                   other long thing on this board uses — its title is the line
//                   you skim, the whole rendered document is behind the expand.
//   the library     a quiet "Reports" link in the header, drawn ONLY when this
//                   sprint has at least one report, opening a list of them.
//   the page        every report has a stable URL (#/report/<sha>.<ext>) that
//                   renders it on its own, so it can be linked to and returned
//                   to.
//
// Safety, restated here because this is the file that writes the HTML:
//   * Markdown is rendered SERVER-SIDE (`render_markdown` in bin/sprintd) out of
//     text that was escaped BEFORE any tag was generated. A `<script>` in a .md
//     comes back as the characters `<script>`. That is why `.report-doc`'s
//     innerHTML is set from `detail.html` and nothing else — it is our markup,
//     not the author's.
//   * Author .html is NEVER inlined. It loads into an iframe with a restrictive
//     `sandbox` attribute, from a URL the server already serves under
//     `Content-Security-Policy: sandbox`. Two independent walls.
import { h, clear, ageSuffix, firstLine, timeEl } from './util.js';
import { api, isReportRef, reportHash, reportTitle } from './api.js';

const DOC_LABEL = { md: 'MD', html: 'HTML' };

/** Split a mixed attachment list into [pictures, documents]. */
export function splitAttachments(refs) {
  const shots = [];
  const docs = [];
  for (const ref of refs || []) (isReportRef(ref) ? docs : shots).push(ref);
  return [shots, docs];
}

/** A stable version number for a run of report refs — see `reconcile`. */
export function docsVer(refs) {
  return refs.map((r) => String(r.sha256 || '').slice(0, 8)).join('.');
}

function bytesLabel(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// ---- in a thread ---------------------------------------------------------

/**
 * One thread item holding every report on a single event. Closed, it is a line
 * you skim; open, it is the whole document. Nothing is fetched until you ask.
 */
export function reportRow(refs, app, mine) {
  const row = h('div.item', { class: `item${mine ? ' mine' : ''}` });
  for (const ref of refs) row.appendChild(reportCard(ref, app));
  return row;
}

export function reportCard(ref, app, opts = {}) {
  const kind = ref.doc === 'html' ? 'html' : 'md';
  const card = h('div.report', { class: `report is-${kind}` });
  const body = h('div.report-body', { hidden: true });
  let loaded = false;
  let open = false;

  const chevron = h('span.report-chev', { 'aria-hidden': 'true' }, '▸');
  const head = h('button.report-head', {
    type: 'button',
    'aria-expanded': 'false',
    onclick: () => toggle(),
  },
    h('span.report-kind', DOC_LABEL[kind]),
    h('span.report-title', firstLine(reportTitle(ref), 90)),
    h('span.grow'),
    ref.bytes ? h('span.report-size', bytesLabel(ref.bytes)) : null,
    chevron);

  card.appendChild(head);
  // The stable link lives on the row, not behind the expand: a report you want
  // to send someone should never require opening it first.
  if (!opts.noLink) {
    card.appendChild(h('a.report-open', {
      href: reportHash(ref),
      title: 'open this report on its own page',
    }, 'Open full page →'));
  }
  card.appendChild(body);

  function toggle() {
    open = !open;
    body.hidden = !open;
    card.classList.toggle('is-open', open);
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    chevron.textContent = open ? '▾' : '▸';
    if (open && !loaded) {
      loaded = true;
      fillReport(body, ref, app);
    }
  }

  card._expand = () => { if (!open) toggle(); };
  return card;
}

/**
 * Fetch and paint one rendered report into `target`. The only place in the app
 * that turns a report into DOM, so the two safety rules live in one function.
 */
export async function fillReport(target, ref, app) {
  clear(target);
  target.appendChild(h('p.report-loading', 'opening the report…'));
  let detail;
  try {
    detail = await api.report(ref.sha256, ref.doc === 'html' ? 'html' : 'md');
  } catch (err) {
    clear(target);
    target.appendChild(h('p.report-err', 'Could not open this report.'));
    if (app && app.toast) app.toast('could not open that report');
    return;
  }
  clear(target);
  target.appendChild(reportContent(detail));
}

/**
 * The rendered document itself, for a thread expand and for the full page
 * alike — they must never disagree about what a report looks like.
 */
export function reportContent(detail) {
  if (detail && detail.doc === 'html') {
    // Author HTML: sandboxed, and the sandbox attribute is deliberately EMPTY.
    // No allow-scripts, no allow-same-origin, no allow-forms, no allow-popups —
    // an empty sandbox is every restriction switched on at once. The response
    // itself also carries `Content-Security-Policy: sandbox`, so a direct
    // navigation to the raw URL is inert too.
    const frame = h('iframe.report-frame', {
      src: detail.raw_url || detail.url,
      sandbox: '',
      referrerpolicy: 'no-referrer',
      loading: 'lazy',
      title: detail.title || 'report',
    });
    return h('div.report-doc.is-html',
      h('p.report-note', 'This report is author-written HTML — it renders in a sandbox with scripts and network access switched off.'),
      frame);
  }
  // Markdown: `detail.html` is OUR markup, generated server-side out of text
  // that was escaped before a single tag existed. Nothing the author wrote is
  // markup here — a `<script>` in the source is the characters `<script>`.
  const doc = h('div.report-doc.is-md');
  doc.innerHTML = (detail && detail.html) || '';
  // The document's title is already the page heading (and the skim line), and a
  // report that opens by repeating its own name reads as a mistake. If the very
  // first thing in the body IS that title, drop it — once, and only on an exact
  // match, so a document whose first heading says something else keeps it.
  const first = doc.firstElementChild;
  const title = String((detail && detail.title) || '').trim();
  if (first && first.tagName === 'H1' && title
      && first.textContent.trim() === title) {
    first.remove();
  }
  return doc;
}

// ---- the library page ----------------------------------------------------

/**
 * Every report in THIS sprint: title, source card, author, date. Deliberately a
 * page and not a rail — the user ruled the rail out by name.
 */
export function renderReportsPage(root, app, state) {
  clear(root);
  const page = h('section.page.reports-page');
  page.appendChild(h('div.page-head',
    h('a.page-back', { href: '#', onclick: (e) => { e.preventDefault(); app.goBoard(); } }, '← Board'),
    h('h2.page-title', 'Reports'),
    h('p.page-sub', 'Documents agents wrote during this sprint. Each one opens on its own page.')));

  if (state.error) {
    page.appendChild(h('p.page-empty', 'Could not load the report library.'));
  } else if (!state.data) {
    page.appendChild(h('p.page-empty', 'loading…'));
  } else if (!state.data.reports.length) {
    page.appendChild(h('p.page-empty', 'No reports in this sprint yet. An agent attaches one with `sprint-post <num> chat "…" --report path.md`.'));
  } else {
    const list = h('ul.report-list');
    for (const r of state.data.reports) list.appendChild(reportListRow(r, app));
    page.appendChild(list);
  }
  root.appendChild(page);
}

function reportListRow(r, app) {
  const meta = h('div.report-meta');
  if (r.card_num != null) {
    meta.appendChild(h('a.report-card-link', {
      href: `#/c/${r.card_num}`,
      onclick: (e) => { e.preventDefault(); app.openCard(r.card_num); },
      title: r.card_title || `card #${r.card_num}`,
    }, `#${r.card_num}`));
    if (r.card_title) meta.appendChild(h('span.report-card-title', firstLine(r.card_title, 44)));
  } else {
    meta.appendChild(h('span.report-card-link.is-none', 'sidebar'));
  }
  meta.appendChild(h('span.report-dot', '·'));
  meta.appendChild(h('span.report-actor', ACTOR_LABEL[r.actor] || r.actor || 'agent'));
  meta.appendChild(h('span.report-dot', '·'));
  meta.appendChild(timeEl(r.ts));

  return h('li.report-row',
    h('a.report-row-main', { href: `#/report/${r.sha256}.${r.doc === 'html' ? 'html' : 'md'}` },
      h('span.report-kind', DOC_LABEL[r.doc === 'html' ? 'html' : 'md']),
      h('span.report-row-title', firstLine(r.title || r.name || 'report', 90))),
    meta);
}

const ACTOR_LABEL = { user: 'You', session: 'Session', worker: 'Agent', server: 'Board' };

// ---- one report on its own page -----------------------------------------

export function renderReportPage(root, app, state) {
  clear(root);
  const page = h('section.page.report-page');
  const d = state.data;
  page.appendChild(h('div.page-head',
    h('a.page-back', { href: '#/reports', onclick: (e) => { e.preventDefault(); app.goReports(); } }, '← Reports'),
    h('h2.page-title', d ? (d.title || d.name || 'Report') : 'Report'),
    d ? h('p.page-sub',
      d.card_num != null
        ? h('a.report-card-link', {
          href: `#/c/${d.card_num}`,
          onclick: (e) => { e.preventDefault(); app.openCard(d.card_num); },
        }, `#${d.card_num}`)
        : h('span.report-card-link.is-none', 'sidebar'),
      h('span.report-dot', '·'),
      h('span.report-actor', ACTOR_LABEL[d.actor] || d.actor || 'agent'),
      h('span.report-dot', '·'),
      h('span', ageSuffix(d.ts)),
      h('span.report-dot', '·'),
      h('a.report-raw', { href: d.url, target: '_blank', rel: 'noreferrer noopener' }, 'raw')) : null));

  if (state.error) page.appendChild(h('p.page-empty', 'Could not open this report — it may not exist on this board.'));
  else if (!d) page.appendChild(h('p.page-empty', 'loading…'));
  else page.appendChild(reportContent(d));
  root.appendChild(page);
}
