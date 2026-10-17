#!/usr/bin/env python3
"""Structural check on the exported n8n workflows.

Catches the failure mode that matters: a workflow that imports into n8n but
has a connection pointing at a node that does not exist, which fails silently
at run time rather than at import.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / "n8n" / "workflows"


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        wf = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]

    for key in ("name", "nodes", "connections"):
        if key not in wf:
            errors.append(f"missing top-level key {key!r}")
    if errors:
        return errors

    names: list[str] = []
    for i, n in enumerate(wf["nodes"]):
        for key in ("name", "type", "position", "parameters"):
            if key not in n:
                errors.append(f"node[{i}] missing {key!r}")
        names.append(n.get("name", f"<node {i}>"))

    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        errors.append(f"duplicate node names: {', '.join(sorted(dupes))}")

    known = set(names)
    for src, conn in wf["connections"].items():
        if src not in known:
            errors.append(f"connection from unknown node {src!r}")
        for branch in conn.get("main", []):
            for link in branch or []:
                target = link.get("node")
                if target not in known:
                    errors.append(f"{src!r} -> unknown node {target!r}")

    triggers = [n for n in wf["nodes"] if "trigger" in n.get("type", "").lower()]
    if not triggers:
        errors.append("no trigger node: this workflow can never start")
    return errors


def main() -> int:
    files = sorted(WORKFLOWS.glob("*.json"))
    if not files:
        print(f"no workflows found in {WORKFLOWS}", file=sys.stderr)
        return 1

    failed = False
    for path in files:
        errors = validate(path)
        if errors:
            failed = True
            print(f"FAIL {path.name}")
            for e in errors:
                print(f"  - {e}")
        else:
            wf = json.loads(path.read_text())
            print(f"ok   {path.name} ({len(wf['nodes'])} nodes)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
