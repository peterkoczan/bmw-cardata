#!/usr/bin/env python3
"""Checkpoint for the long review, so an interruption only costs the step in flight.

Lives under data/ (gitignored) so it survives a lost session, a flat battery or a
new shell, and is not something anyone else inherits in a clone.

    python tools/review_progress.py            # show
    python tools/review_progress.py done B1 "note"
    python tools/review_progress.py next
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "data" / "review-progress.json"

STEPS = [
    ("A1", "repo description + homepage"),
    ("A2", "repo topics"),
    ("A3", "LICENSE"),
    ("A4", "docs/index.html landing page (Pages root 404s today)"),
    ("A5", "meta description + Open Graph on the published pages"),
    ("B1", "state-series query uses telemetry_vin_key_ts_desc (seq scan today)"),
    ("B2", "window the live payload instead of sending all history"),
    ("C1", "menu bar: heartbeat file for liveness, DB only on the slow tick"),
    ("C2", "page: stop polling while the tab is hidden"),
    ("C3", "page: stop the age timer when there is nothing to age"),
    ("D1", "docs/setup.html: linking a car, with slots for real screenshots"),
    ("D2", "menu bar links to the setup guide"),
    ("D3", "systemd units + Linux notes"),
    ("E1", "install.sh: scratch database/dir so it can be tested safely"),
    ("E2", "run install.sh end to end against the scratch database"),
]


def load():
    if FILE.exists():
        return json.loads(FILE.read_text())
    return {"done": {}}


def save(state):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(state, indent=2) + "\n")


def show(state):
    done = state["done"]
    for sid, label in STEPS:
        mark = "x" if sid in done else " "
        note = f"  -- {done[sid]}" if done.get(sid) else ""
        print(f"  [{mark}] {sid}  {label}{note}")
    left = [s for s, _ in STEPS if s not in done]
    print(f"\n  {len(STEPS) - len(left)}/{len(STEPS)} done" + (f", next: {left[0]}" if left else ", all complete"))


def main():
    state = load()
    args = sys.argv[1:]
    if not args:
        show(state)
    elif args[0] == "done":
        state["done"][args[1]] = " ".join(args[2:]) or "done"
        save(state)
        show(state)
    elif args[0] == "next":
        left = [s for s, _ in STEPS if s not in state["done"]]
        print(left[0] if left else "")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
