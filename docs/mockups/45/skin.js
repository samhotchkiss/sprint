/* ---------------------------------------------------------------------------
   Card #45 mockups — the skin switch.

   CHAOS IS THE DEFAULT. Calm is the second opinion. The choice rides in the URL
   (?skin=) so a link you send someone opens in the skin you were looking at,
   and it is mirrored into localStorage so clicking around does not lose it.

   No framework, no build, no external request — the same rules as the pages.
   --------------------------------------------------------------------------- */
(function () {
  var KEY = 'mock45-skin';

  var q = new URLSearchParams(location.search);
  var skin = q.get('skin');
  if (skin !== 'calm' && skin !== 'chaos') {
    try { skin = localStorage.getItem(KEY); } catch (e) { skin = null; }
  }
  if (skin !== 'calm') skin = 'chaos';

  /* set before first paint so no page flashes the wrong skin */
  document.documentElement.className = 'skin-' + skin;
  try { localStorage.setItem(KEY, skin); } catch (e) {}
  window.MOCK_SKIN = skin;

  /* every internal link and framed page carries the skin forward */
  function withSkin(href) {
    if (!href || /^(https?:|mailto:|#)/.test(href)) return href;
    var hashAt = href.indexOf('#');
    var hash = hashAt >= 0 ? href.slice(hashAt) : '';
    var base = hashAt >= 0 ? href.slice(0, hashAt) : href;
    if (!/\.html/.test(base)) return href;
    base = base.replace(/([?&])skin=[a-z]*(&|$)/g, '$1').replace(/[?&]$/, '');
    base += (base.indexOf('?') >= 0 ? '&' : '?') + 'skin=' + skin;
    return base + hash;
  }
  window.MOCK_WITH_SKIN = withSkin;

  function go(name) {
    /* an option page framed inside the viewer switches the WHOLE viewer, not
       just its own frame — otherwise the bar and the mockup disagree */
    var w = window;
    try { if (window.top && window.top !== window && window.top.location.href) w = window.top; } catch (e) { w = window; }
    var u = new URL(w.location.href);
    u.searchParams.set('skin', name);
    w.location.href = u.toString();
  }

  function paint() {
    var i;
    var links = document.querySelectorAll('a[href]');
    for (i = 0; i < links.length; i++) links[i].setAttribute('href', withSkin(links[i].getAttribute('href')));
    var frames = document.querySelectorAll('iframe[src]');
    for (i = 0; i < frames.length; i++) frames[i].setAttribute('src', withSkin(frames[i].getAttribute('src')));

    if (!document.querySelector('.scanlines')) {
      var s = document.createElement('div');
      s.className = 'scanlines';
      document.body.appendChild(s);
    }

    var hosts = document.querySelectorAll('[data-skin-toggle]');
    for (i = 0; i < hosts.length; i++) {
      var box = document.createElement('span');
      box.className = 'skin-toggle';
      ['chaos', 'calm'].forEach(function (name) {
        var b = document.createElement('button');
        b.type = 'button';
        b.textContent = name;
        if (name === skin) b.className = 'is-on';
        b.addEventListener('click', function () { go(name); });
        box.appendChild(b);
      });
      hosts[i].innerHTML = '';
      hosts[i].appendChild(box);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', paint);
  else paint();
})();
