#!/usr/bin/env python3
"""Resumable checkpoint for long, multi-step work.

Unlike review_progress.py, the steps are not hardcoded -- a plan is created at
runtime and stored alongside its progress, so this survives work whose shape is
not known until something else (a review, a scan) has produced it.

State lives in data/checkpoints.json, which is gitignored: it survives a lost
session, a flat battery, a new shell or a fresh clone-free machine, and nobody
else inherits it.

    python tools/checkpoint.py                       show the active plan
    python tools/checkpoint.py brief                 everything a new session needs
    python tools/checkpoint.py plans                 list plans
    python tools/checkpoint.py show <plan>           show one plan

    python tools/checkpoint.py start <plan> <file>   create a plan from a JSON file
    python tools/checkpoint.py begin <id>            mark in flight (see below)
    python tools/checkpoint.py done <id> [note]      mark finished
    python tools/checkpoint.py block <id> [why]      mark blocked, keep going
    python tools/checkpoint.py reset <id>            back to pending
    python tools/checkpoint.py next                  print the next actionable id

Why `begin` matters: a step marked in_progress when the session dies is the one
that may be half-applied. On resume it needs redoing from a known state, whereas
a pending step was never touched. Without that distinction a resumed session has
to re-verify everything it thought it had finished.

Read and write are separate commands on purpose. A bare invocation only ever
prints -- during the last review, running the write command to *look* at progress
silently completed a step that had not been started.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "data" / "checkpoints.json"

PENDING, IN_PROGRESS, DONE, BLOCKED = "pending", "in_progress", "done", "blocked"
MARK = {PENDING: " ", IN_PROGRESS: ">", DONE: "x", BLOCKED: "!"}


def load():
    if FILE.exists():
        return json.loads(FILE.read_text())
    return {"active": None, "plans": {}}


def save(state):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(FILE)  # atomic: a crash mid-write must not lose the plan


def active_plan(state, name=None):
    name = name or state.get("active")
    if not name:
        raise SystemExit("no active plan. Create one: checkpoint.py start <plan> <file>")
    if name not in state["plans"]:
        raise SystemExit(f"no such plan: {name}. Known: {', '.join(state['plans']) or '(none)'}")
    return name, state["plans"][name]


def find(plan, sid):
    for step in plan["steps"]:
        if step["id"] == sid:
            return step
    known = ", ".join(s["id"] for s in plan["steps"])
    raise SystemExit(f"no step {sid!r} in plan {plan['name']!r}. Known: {known}")


def counts(plan):
    out = {PENDING: 0, IN_PROGRESS: 0, DONE: 0, BLOCKED: 0}
    for step in plan["steps"]:
        out[step["status"]] += 1
    return out


def show(plan, verbose=False):
    print(f"\n  {plan['name']} -- {plan.get('description', '')}\n")
    for step in plan["steps"]:
        note = f"  -- {step['note']}" if step.get("note") else ""
        print(f"  [{MARK[step['status']]}] {step['id']:<6} {step['title']}{note}")
        if verbose and step.get("detail"):
            for line in step["detail"].splitlines():
                print(f"           {line}")
    c = counts(plan)
    total = len(plan["steps"])
    line = f"\n  {c[DONE]}/{total} done"
    if c[IN_PROGRESS]:
        stuck = [s["id"] for s in plan["steps"] if s["status"] == IN_PROGRESS]
        line += f", IN FLIGHT (redo from a known state): {', '.join(stuck)}"
    if c[BLOCKED]:
        line += f", {c[BLOCKED]} blocked"
    nxt = next_id(plan)
    line += f", next: {nxt}" if nxt else ", nothing left"
    print(line)


def next_id(plan):
    # An in-flight step outranks a pending one: finish what was interrupted first.
    for want in (IN_PROGRESS, PENDING):
        for step in plan["steps"]:
            if step["status"] == want:
                return step["id"]
    return None


def set_status(state, sid, status, note):
    name, plan = active_plan(state)
    step = find(plan, sid)
    step["status"] = status
    if note:
        step["note"] = note
    save(state)
    show(plan)


def main():
    state = load()
    args = sys.argv[1:]
    cmd = args[0] if args else "show"

    if cmd == "show":
        _, plan = active_plan(state, args[1] if len(args) > 1 else None)
        show(plan)

    elif cmd == "brief":
        # One blob a fresh session can read to pick up without re-deriving anything.
        name, plan = active_plan(state)
        show(plan, verbose=True)
        nxt = next_id(plan)
        if nxt:
            step = find(plan, nxt)
            print(f"\n  RESUME AT {step['id']}: {step['title']}")
            if step["status"] == IN_PROGRESS:
                print("  This was in flight when the session ended -- verify the current")
                print("  state of the file before assuming any of it landed.")
            if step.get("detail"):
                print(f"\n{step['detail']}")

    elif cmd == "plans":
        for name, plan in state["plans"].items():
            c = counts(plan)
            flag = " (active)" if name == state.get("active") else ""
            print(f"  {name}{flag}: {c[DONE]}/{len(plan['steps'])} done")

    elif cmd == "start":
        name, path = args[1], Path(args[2])
        spec = json.loads(path.read_text())
        steps = [
            {
                "id": s["id"],
                "title": s["title"],
                "detail": s.get("detail", ""),
                "status": PENDING,
                "note": "",
            }
            for s in spec["steps"]
        ]
        state["plans"][name] = {
            "name": name,
            "description": spec.get("description", ""),
            "steps": steps,
        }
        state["active"] = name
        save(state)
        show(state["plans"][name])

    elif cmd == "begin":
        set_status(state, args[1], IN_PROGRESS, " ".join(args[2:]))
    elif cmd == "done":
        set_status(state, args[1], DONE, " ".join(args[2:]))
    elif cmd == "block":
        set_status(state, args[1], BLOCKED, " ".join(args[2:]))
    elif cmd == "reset":
        set_status(state, args[1], PENDING, "")

    elif cmd == "next":
        _, plan = active_plan(state)
        print(next_id(plan) or "")

    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
