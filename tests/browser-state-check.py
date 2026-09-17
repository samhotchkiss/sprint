#!/usr/bin/env python3
"""Browser regression for board-card flash and chat-message disappearance.

Serves this checkout's `web/` and drives the mock board with Playwright.

Invocation (from repo root, or pass the worktree path):

  /tmp/yunagi-ongoing-release-217/.venv/bin/python tests/browser-state-check.py . --expect-fixed

  /tmp/yunagi-ongoing-release-217/.venv/bin/python tests/browser-state-check.py \\
      /Users/sam/dev/sprint-browser-consistency --expect-fixed

Without --expect-fixed the script still prints the JSON report and exits 0.
With --expect-fixed it asserts:

  * a board card node is not removed across a window-focus refresh
  * a sent sidebar line is still on screen after a stale /api/board replay
  * a sent card-chat line is still on screen after a stale card GET
"""
import argparse, functools, http.server, json, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', help='sprint checkout (the directory that contains web/)')
p.add_argument('--expect-fixed', action='store_true')
a = p.parse_args()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(
    ('127.0.0.1', 0),
    functools.partial(Quiet, directory=str(Path(a.root) / 'web')),
)
threading.Thread(target=server.serve_forever, daemon=True).start()

with sync_playwright() as w:
    browser = w.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 950})
    page.goto(f'http://127.0.0.1:{server.server_port}/?mock=1')
    page.locator('#sidebar-text').wait_for()
    page.locator('[data-view="board"]').click()
    page.wait_for_timeout(400)
    report = {}
    page.evaluate('''() => { window.oldCard=document.querySelector('.card'); window.removed=0;
 window.obs=new MutationObserver(ms=>{for(const m of ms) for(const n of m.removedNodes)
 if(n===window.oldCard || (n.contains && n.contains(window.oldCard))) window.removed++});
 window.obs.observe(document.querySelector('main'),{childList:true,subtree:true});
 window.dispatchEvent(new Event('focus')); }''')
    page.wait_for_timeout(700)
    report['card_retained'] = page.evaluate('window.oldCard && window.oldCard.isConnected')
    report['card_removals'] = page.evaluate('window.removed')
    page.evaluate('''async()=>{window.stale=await (await fetch('/api/board')).json();
 const orig=window.fetch; window.fetch=(url,init)=>String(url).startsWith('/api/board') && window.useStale
 ? Promise.resolve(new Response(JSON.stringify(window.stale),{status:200,headers:{'Content-Type':'application/json'}}))
 :orig(url,init); }''')
    message = 'Browser acceptance: retained message 1709'
    page.locator('#sidebar-text').fill(message)
    page.locator('#sidebar-text').press('Enter')
    page.wait_for_timeout(600)
    report['message_after_send'] = page.get_by_text(message, exact=True).count()
    page.evaluate("""async()=>{const s=await import('/state.js');
 const events=await (await fetch('/api/events?after=0')).json();s.applyEvents(events.events);
 window.useStale=true; window.dispatchEvent(new Event('focus'));}""")
    page.wait_for_timeout(700)
    report['message_after_stale_refresh'] = page.get_by_text(message, exact=True).count()
    page.evaluate('window.useStale=false')
    page.locator('.card').first.click()
    composer = page.locator('textarea[id^="composer-"]').first
    composer.wait_for()
    num = composer.get_attribute('id').split('-')[-1]
    page.evaluate("""async(num)=>{window.cardPath='/api/cards/'+num;
 window.cardSnapshot=await (await fetch(window.cardPath)).json();const orig=window.fetch;
 window.fetch=(url,init)=>String(url)===window.cardPath && (!init || !init.method || init.method==='GET')
 ? Promise.resolve(new Response(JSON.stringify(window.cardSnapshot),{status:200,headers:{'Content-Type':'application/json'}}))
 :orig(url,init);}""", num)
    card_message = 'Card acceptance: retained reply 1709'
    composer.fill(card_message)
    composer.press('Enter')
    page.wait_for_timeout(700)
    report['card_message_after_stale_refresh'] = page.get_by_text(card_message, exact=True).count()
    print(json.dumps(report))
    if a.expect_fixed:
        assert report['card_retained'] and report['card_removals'] == 0, report
        assert report['message_after_send'] == 1 and report['message_after_stale_refresh'] == 1, report
        assert report['card_message_after_stale_refresh'] == 1, report
    browser.close()
server.shutdown()
