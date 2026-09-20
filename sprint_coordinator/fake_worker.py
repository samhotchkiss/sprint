"""Stdin-JSON/stdout-JSON test worker. Not a production agent."""

from __future__ import annotations

import json
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tier = argv[0] if argv else "low"
    job = json.load(sys.stdin)
    payload = {
        "ok": True,
        "kind": "reply",
        "text": "fake-%s:%s" % (tier, job.get("obligation_id")),
        "addresses_obligation": True,
        "tier": tier,
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
