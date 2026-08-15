#!/usr/bin/env python3
"""Fill a throwaway board with a representative sprint, through the real API.

Used to look at the UI with something real in it — every card here is created by
the same POSTs a session and its workers make, so what you see is the actual
render path, not a fixture the browser was handed.

    ./bin/sprintd --data-dir /tmp/demo start --port 8406 --token demo
    python3 scripts/seed-demo.py --server http://127.0.0.1:8406 --token demo

Never point this at a real board: it submits a couple of dozen cards.
"""
import argparse
import json
import os
import struct
import sys
import tempfile
import urllib.error
import urllib.request
import zlib

SERVER = ""
TOKEN = ""
TITLE = "Board polish + billing bugs"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SERVER + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            raw = fh.read().decode()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        print("%s %s -> %s %s" % (method, path, exc.code, exc.read().decode()[:400]),
              file=sys.stderr)
        raise


# ---- a placeholder screenshot, so packets have something to open ----------

def png(path, w, h, bands):
    """A tiny PNG: horizontal bands of solid colour. No dependencies."""
    rows = b""
    for y in range(h):
        r, g, b = bands[min(len(bands) - 1, y * len(bands) // h)]
        rows += b"\x00" + bytes([r, g, b]) * w
    def chunk(tag, payload):
        c = tag + payload
        return struct.pack(">I", len(payload)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(blob)
    return path


def submit(text, hold=False):
    res = call("POST", "/api/cards", {"text": text, "hold": hold})
    card = res.get("card", res)
    return card["num"]


def assign(num, agent, branch, title):
    call("POST", "/api/cards/%d/assign" % num,
         {"agent_name": agent, "worktree": "/tmp/wt/%d" % num, "branch": branch, "title": title})


def state(num, st, reason=None, title=None):
    body = {"state": st}
    if reason:
        body["reason"] = reason
    if title:
        body["title"] = title
    call("POST", "/api/cards/%d/state" % num, body)


def event(num, kind, text, detail=None, actor=None):
    payload = {"text": text}
    if detail:
        payload["detail"] = detail
    body = {"kind": kind, "payload": payload}
    if actor:
        body["actor"] = actor
    call("POST", "/api/cards/%d/events" % num, body)


def ask(num, text, options=None):
    body = {"text": text}
    if options:
        body["options"] = options
    call("POST", "/api/cards/%d/question" % num, body)


def ready(num, packet):
    call("POST", "/api/cards/%d/ready" % num, {"packet": packet})


def say(text, actor="session"):
    call("POST", "/api/sidebar", {"text": text, "actor": actor})


def main():
    global SERVER, TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("SPRINT_SERVER", "http://127.0.0.1:8406"))
    ap.add_argument("--token", default=os.environ.get("SPRINT_TOKEN", ""))
    args = ap.parse_args()
    SERVER, TOKEN = args.server.rstrip("/"), args.token

    # A fresh sprintd auto-opens an untitled sprint, and `open` is idempotent, so
    # it will not rename one that already exists — close it first on a virgin board.
    board = call("GET", "/api/board")
    if (board.get("sprint") or {}).get("title") != TITLE:
        if board.get("cards"):
            print("board already has cards — refusing to reseed", file=sys.stderr)
            return 1
        if board.get("sprint"):
            call("POST", "/api/sprint", {"action": "close"})
        call("POST", "/api/sprint", {"action": "open", "title": TITLE})

    shots = tempfile.mkdtemp(prefix="sprint-demo-shots-")
    light = png(os.path.join(shots, "toolbar-light.png"), 480, 300,
                [(244, 241, 236), (233, 230, 225), (216, 164, 92), (233, 230, 225)])
    dark = png(os.path.join(shots, "toolbar-dark.png"), 480, 300,
               [(16, 18, 22), (20, 22, 26), (127, 168, 138), (20, 22, 26)])
    filters = png(os.path.join(shots, "filters.png"), 480, 300,
                  [(16, 18, 22), (23, 26, 30), (143, 180, 216), (23, 26, 30)])
    empty = png(os.path.join(shots, "empty-state.png"), 480, 300,
                [(16, 18, 22), (18, 21, 26), (168, 201, 174), (18, 21, 26)])

    # ---- needs you: two questions -----------------------------------------
    n134 = submit("Add a CSV export to the tenants page.")
    assign(n134, "sprint-card-134", "sprint/134-tenant-export", "Tenant CSV export")
    state(n134, "in_progress")
    event(n134, "progress", "Export scaffolded; the column set is the open question.",
          "Route, streaming writer and the row query are done and tested. The only undecided "
          "piece is the identifier column, which changes the header row and the fixture files "
          "— cheap now, expensive after anyone scripts against it.")
    ask(n134,
        "The CSV export can key on the tenant slug (readable, can change) or the UUID "
        "(stable, ugly). Which do you want in the shipped file?",
        [{"label": "Slug", "value": "slug"},
         {"label": "UUID", "value": "uuid"},
         {"label": "Both columns", "value": "both"}])

    n135 = submit("Refund emails go out with no subject line.")
    assign(n135, "sprint-batch-6", "sprint/batch-6-copy", "Refund email subject line")
    state(n135, "in_progress")
    event(n135, "progress", "Folded into batch-6 with the other copy fixes.",
          "Batch-6 is one agent on sprint/batch-6-copy carrying #119 and this card — both "
          "copy-only edits in the same template directory, so one branch and one review.")
    ask(n135, "The refund email has no subject copy. Give me the line you want and I will "
              "ship it in both the HTML and text parts.")

    # ---- needs you: three signoffs ----------------------------------------
    n136 = submit("Toolbar contrast fails WCAG in light mode — the grey on grey is "
                  "unreadable in sunlight.")
    assign(n136, "sprint-card-136", "sprint/136-toolbar-contrast", "Toolbar contrast fails WCAG")
    state(n136, "in_progress")
    event(n136, "progress", "First pass: raised the token contrast.",
          "Bumped --toolbar-fg and --toolbar-muted in styles/tokens.css. Both themes read the "
          "same token — exactly what caused the regression you caught next.")
    event(n136, "note", "Bounced: dark theme regressed, toolbar text went muddy.",
          "Your note: fix light without touching dark. Bounce 1. After a second bounce the "
          "board stops retrying blind and brings it to you for co-design.")
    event(n136, "progress", "Scoped the change to the light palette only.",
          "Split the shared token into --toolbar-fg-light and --toolbar-fg-dark. The dark "
          "values are byte-identical to what shipped before.")
    ready(n136, {
        "claim": "Toolbar text now passes 4.5:1 in light mode without changing the dark palette.",
        "diffstat": "3 files changed, +24 −9",
        "branch": "sprint/136-toolbar-contrast",
        "test_cmd": "npm run test -- toolbar",
        "test_result": "38 pass, 0 fail",
        "validate": [
            "Open the live preview and look at the toolbar in light mode — the labels are "
            "near-black now, not grey.",
            "Flip your system to dark mode and reload: the toolbar should look exactly as it "
            "did before.",
            "Compare the two screenshots below.",
        ],
        "ui_change": True,
        "screenshots": [light, dark],
        "live_url": "http://127.0.0.1:8436/",
    })

    n137 = submit("A dump of small CSS complaints: filter row wraps at 1280, empty state "
                  "off-centre, table header overlaps the toolbar.")
    assign(n137, "sprint-batch-7", "sprint/batch-7-css", "Three CSS fixes on one branch")
    state(n137, "in_progress")
    event(n137, "progress", "Batched them onto one agent and one branch.",
          "Same three files, so parallel agents would have collided. Verdicts stay per-card: "
          "each item has its own claim and screenshots.")
    ready(n137, {
        "claim": "All three CSS complaints fixed on one branch; no shared file touched twice.",
        "diffstat": "5 files changed, +61 −33",
        "branch": "sprint/batch-7-css",
        "test_cmd": "npm run lint && npm run test -- styles",
        "test_result": "112 pass, 0 fail",
        "validate": [
            "Open the live preview at 1280px wide — the filter row stays on one line.",
            "Filter the invoices table to refunded: the empty state sits dead centre in light "
            "and dark.",
            "Scroll the table — the header sticks under the toolbar, not over it.",
        ],
        "ui_change": True,
        "screenshots": [filters, empty],
    })

    n138 = submit("The empty state on the invoices table sits left of centre.")
    assign(n138, "sprint-batch-7", "sprint/batch-7-css", "Invoices empty state off-centre")
    state(n138, "in_progress")
    ready(n138, {
        "claim": "Empty state is centred in both themes.",
        "diffstat": "1 file changed, +6 −4",
        "branch": "sprint/batch-7-css",
        "test_cmd": "npm run test -- styles",
        "test_result": "112 pass, 0 fail",
        "validate": ["Filter the invoices table to refunded and look at the empty state in "
                     "light and dark."],
        "ui_change": True,
        "screenshots": [empty],
    })

    # ---- in motion ---------------------------------------------------------
    n131 = submit("Toolbar contrast fails WCAG in light mode.")
    assign(n131, "sprint-batch-7", "sprint/batch-7-css", "Light-mode toolbar contrast")
    state(n131, "in_progress")
    event(n131, "progress", "I read this as: raise the light-mode toolbar contrast only.",
          "Restating it before I start so you can stop me early if I read it wrong. Dark mode "
          "is explicitly out of scope.")
    event(n131, "progress", "Found the tokens — three of them fail 4.5:1.",
          "styles/tokens.css: --toolbar-fg (3.8:1), --toolbar-muted (2.9:1), "
          "--toolbar-icon (4.1:1).")
    event(n131, "progress", "Swapped the toolbar tokens; re-running the contrast check.")

    n132 = submit("Stripe webhook retries double-charge when we answer 409.")
    assign(n132, "sprint-card-132", "sprint/132-webhook-409", "Webhook retries double-charge")
    state(n132, "in_progress")
    event(n132, "progress", "Reproducing the double-charge against the sandbox key.")

    n133 = submit("Run a full regression sweep before we cut the release.")
    assign(n133, "sprint-card-133", "sprint/133-regression", "Full regression sweep")
    state(n133, "in_progress")
    call("POST", "/api/cards/%d/events" % n133,
         {"kind": "note", "payload": {"text": "starting the full suite, ~45min"},
          "long_running": True})
    event(n133, "progress", "Suite at 780/1204 — no failures yet.",
          "The suite takes about 45 minutes end to end. I post a count every few hundred "
          "tests so the gap never looks like a hang.")

    n144 = submit("Dark mode: the drawer scrim is too dark to read through.")
    assign(n144, "sprint-batch-7", "sprint/batch-7-css", "Drawer scrim too dark")
    event(n144, "progress", "I read this as: lighten the drawer scrim in dark mode only.",
          "Folded into batch-7 because it touches the same stylesheet as #131. Correct me here "
          "and I will re-scope before I write anything.")

    # ---- blocked -----------------------------------------------------------
    n123 = submit("Land the billing migration.")
    assign(n123, "sprint-card-123", "sprint/123-billing-migration", "Merge the billing migration")
    state(n123, "in_progress")
    state(n123, "blocked", reason="ci_red: main has been red since 8f21e3 (migration collision)")
    event(n123, "note", "Re-checked CI at :05 and :20 — still red on the same job.",
          "The failure is a migration collision from another branch, not ours. I re-check "
          "every ten minutes and will merge without asking once it goes green.")

    n127 = submit("Rename the tenant slug column.")
    assign(n127, "sprint-card-127", "sprint/127-slug-rename", "Rename the tenant slug column")
    state(n127, "in_progress")
    state(n127, "blocked", reason="overlaps #131 — both edit backend/internal/tenant/store.go")

    n128 = submit("Point the settings page at the new tenant API.")
    assign(n128, "sprint-card-128", "sprint/128-settings-api", "Settings page on the new API")
    state(n128, "in_progress")
    state(n128, "blocked", reason="dependency: waiting on the contract landing in #127")

    n129 = submit("Rewrite the settings loader so it stops reading the file twice.")
    assign(n129, "sprint-card-129", "sprint/129-settings-loader", "Rewrite the settings loader")
    state(n129, "in_progress")
    event(n129, "error", "agent died mid-run: worktree .sprint/wt/129 vanished after the "
                         "power blip",
          "The rewrite is unrecoverable. Nothing was merged and nothing is half-applied — "
          "main is untouched. Retry dispatches a fresh agent with this whole timeline as "
          "its brief.")
    state(n129, "blocked", reason="failed: agent died mid-run — retry dispatches a fresh one")

    # ---- queued & held -----------------------------------------------------
    for text in ["Export button does nothing on the reports page.",
                 "Session picker forgets the last tenant.",
                 "Timestamps show UTC in the activity feed."]:
        submit(text)
    for text in ["Card shadows are too heavy on the dashboard.",
                 "Sidebar icons are 1px off the grid.",
                 "Tooltip arrow points the wrong way on the right edge.",
                 "Focus ring is invisible on the dark toolbar."]:
        submit(text, hold=True)

    # ---- done --------------------------------------------------------------
    n118 = submit("Login redirect loop when the session cookie expires.")
    assign(n118, "sprint-card-118", "sprint/118-login-loop", "Login redirect loop on expiry")
    state(n118, "in_progress")
    ready(n118, {
        "claim": "An expired session cookie now lands on the login page instead of looping.",
        "diffstat": "2 files changed, +18 −7", "branch": "sprint/118-login-loop",
        "test_cmd": "go test ./internal/auth/...", "test_result": "41 pass, 0 fail",
        "validate": ["Expire your session cookie and reload — you get the login page once."],
        "ui_change": False,
    })
    call("POST", "/api/cards/%d/verdict" % n118, {"verdict": "approve"})
    call("POST", "/api/cards/%d/integrated" % n118, {"ok": True})

    n121 = submit("Confetti animation when a card is approved.")
    assign(n121, "sprint-card-121", "sprint/121-confetti", "Confetti on approve")
    state(n121, "in_progress")
    ready(n121, {
        "claim": "Cards throw confetti when you approve them.",
        "diffstat": "1 file changed, +94 −0", "branch": "sprint/121-confetti",
        "test_cmd": "npm test", "test_result": "18 pass, 0 fail",
        "validate": ["Approve any card and watch the board."],
        "ui_change": False,
    })
    call("POST", "/api/cards/%d/verdict" % n121,
         {"verdict": "reject", "notes": "not the tone we want — calm, not confetti"})

    # ---- the session channel ----------------------------------------------
    say("Sprint open. Three agents running, cap is 3 — the rest are waiting their turn.")
    say("why have #%d, #%d and #%d been blocked for so long?" % (n123, n127, n128), actor="user")
    say("All three are downstream of one thing. #%d waits on main going green. #%d overlaps "
        "#%d on tenant/store.go, so I hold it until that merges. #%d depends on the contract "
        "#%d lands. I re-check every ten minutes — nothing for you to do unless you want #%d "
        "jumped ahead." % (n123, n127, n131, n128, n127, n127))
    say("keep the order. but batch the css ones.", actor="user")
    say("Done — #%d, #%d and #%d are one agent on sprint/batch-7-css. One worktree, one "
        "branch, one review." % (n137, n138, n144))

    # Park the session's drain cursor at the head: nothing is pending, so the
    # board reads "online" rather than showing an offline banner nobody caused.
    board = call("GET", "/api/board")
    call("POST", "/api/cursors/orchestrator", {"seq": int(board.get("seq", 0))})
    print("seeded %d cards; screenshots in %s" % (len(board.get("cards", [])), shots))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
