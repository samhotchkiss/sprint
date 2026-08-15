#!/usr/bin/env python3
"""Vendor the board's webfonts into web/fonts/ as local woff2 + fonts.css.

The board must make zero external requests (SPEC.md), so the design's four
families are checked in rather than linked. This script is how they got there;
run it only when a family or weight changes, then commit the result.

    python3 scripts/vendor-fonts.py

It talks to fonts.googleapis.com and fonts.gstatic.com — build time only.
Nothing at runtime ever does.
"""
import hashlib
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "web", "fonts")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# (family, css2 query, google/fonts ofl directory)
FAMILIES = [
    ("Instrument Serif", "Instrument+Serif", "instrumentserif"),
    ("IBM Plex Sans", "IBM+Plex+Sans:wght@400;450;500;600", "ibmplexsans"),
    ("IBM Plex Mono", "IBM+Plex+Mono:wght@400;500", "ibmplexmono"),
    ("Press Start 2P", "Press+Start+2P", "pressstart2p"),
]
WANT_SUBSETS = ("latin", "latin-ext")

HEADER = """/* Self-hosted webfonts. The board makes ZERO external requests (SPEC.md), so the
   four families the design calls for are vendored here as woff2 and declared with
   local @font-face rules — no Google Fonts link, no CDN, no preconnect.

     Instrument Serif  — display: h1, section heads, the evidence claim
     IBM Plex Sans     — body (variable file: one woff2 covers 400/450/500/600)
     IBM Plex Mono     — metadata, uppercase labels, machine reasons
     Press Start 2P    — the Chaos skin's display face

   All four are SIL Open Font License 1.1. The licence texts ship beside them as
   OFL-*.txt. Regenerate with scripts/vendor-fonts.py (network, build time only).

   Latin + latin-ext subsets only: this UI is English chrome plus whatever the
   user types, and anything outside those ranges falls through to the system
   stack rather than pulling a face we did not ship. */
"""


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as fh:
        data = fh.read()
    return data if binary else data.decode("utf-8")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def faces_for(family, spec):
    """Every (subset, weight, style, url, unicode-range) Google offers for a family."""
    css = get("https://fonts.googleapis.com/css2?family=%s&display=swap" % spec)
    blocks = re.findall(r"/\*\s*([a-z0-9\-\[\]]+)\s*\*/\s*(@font-face\s*\{.*?\})", css, re.S)
    if not blocks:
        blocks = [("latin", b) for b in re.findall(r"(@font-face\s*\{.*?\})", css, re.S)]
    out = []
    for subset, block in blocks:
        if subset not in WANT_SUBSETS:
            continue
        url = re.search(r"url\((https://[^)]+\.woff2)\)", block)
        if not url:
            continue
        weight = re.search(r"font-weight:\s*([0-9]+)", block)
        style = re.search(r"font-style:\s*(\w+)", block)
        urange = re.search(r"unicode-range:\s*([^;]+);", block)
        out.append({
            "subset": subset,
            "weight": weight.group(1) if weight else "400",
            "style": style.group(1) if style else "normal",
            "url": url.group(1),
            "range": urange.group(1).strip() if urange else None,
        })
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    css = [HEADER]

    for family, spec, ghdir in FAMILIES:
        faces = faces_for(family, spec)
        blobs = {}          # url -> (bytes, sha)
        for f in faces:
            if f["url"] not in blobs:
                data = get(f["url"], binary=True)
                blobs[f["url"]] = (data, hashlib.sha256(data).hexdigest())
            f["sha"] = blobs[f["url"]][1]

        # A family Google serves as a variable font hands back the SAME file for
        # every requested weight. Write it once and declare the whole axis, rather
        # than checking in four byte-identical copies.
        by_subset = {}
        for f in faces:
            by_subset.setdefault((f["subset"], f["style"]), []).append(f)

        wrote = 0
        for (subset, style), group in sorted(by_subset.items()):
            shas = {f["sha"] for f in group}
            variable = len(group) > 1 and len(shas) == 1
            emit = [group[0]] if variable else group
            for f in emit:
                name = "%s-%s-%s%s.woff2" % (
                    slug(family), f["weight"], subset,
                    "" if style == "normal" else "-" + style)
                data = blobs[f["url"]][0]
                with open(os.path.join(OUT, name), "wb") as fh:
                    fh.write(data)
                wrote += 1
                weight = "100 700" if variable else f["weight"]
                css.append("@font-face {")
                css.append("  font-family: '%s';" % family)
                css.append("  font-style: %s;" % style)
                css.append("  font-weight: %s;" % weight)
                css.append("  font-display: swap;")
                css.append("  src: url('%s') format('woff2');" % name)
                if f["range"]:
                    css.append("  unicode-range: %s;" % f["range"])
                css.append("}")
                print("  %-42s %6d bytes%s" % (name, len(data),
                                               "  (variable)" if variable else ""))
        print("%s: %d file(s)" % (family, wrote))

    with open(os.path.join(OUT, "fonts.css"), "w") as fh:
        fh.write("\n".join(css) + "\n")

    for family, _spec, ghdir in FAMILIES:
        try:
            txt = get("https://raw.githubusercontent.com/google/fonts/main/ofl/%s/OFL.txt" % ghdir)
        except Exception as exc:                                    # noqa: BLE001
            print("license fetch failed for %s: %s" % (family, exc), file=sys.stderr)
            continue
        with open(os.path.join(OUT, "OFL-%s.txt" % slug(family)), "w") as fh:
            fh.write(txt)
        print("license: OFL-%s.txt" % slug(family))


if __name__ == "__main__":
    main()
