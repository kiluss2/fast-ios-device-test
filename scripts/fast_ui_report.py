#!/usr/bin/env python3

"""Collate fastUITest runs into one flow × simulator table (appearance / Dynamic Type variants
run with --appearance / --content-size appear as their own `flow@dark@xxl` rows).

    fast_ui_report.py --root <artifacts-root> --session <name>
    fast_ui_report.py --root <artifacts-root> --since 20260908-140000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_runs(root: Path, session: str, since: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for metrics in root.glob("*/*/metrics.json"):
        try:
            payload = json.loads(metrics.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stamp = metrics.parent.name.split("-")[0] + "-" + metrics.parent.name.split("-")[1]
        if session and payload.get("session") != session:
            continue
        if since and stamp < since:
            continue
        payload["_stamp"] = stamp
        payload["_dir"] = str(metrics.parent)
        runs.append(payload)
    return runs


def target(run: dict[str, Any]) -> str:
    """The simulator or, for a fastDeviceTest run, the iPhone the run used (first 8 chars)."""
    return str(run.get("simulator") or run.get("udid") or "?")[:8]


def latest_per_cell(runs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for run in sorted(runs, key=lambda item: item["_stamp"]):
        cells[(run.get("task", "?"), target(run))] = run
    return cells


def render(cells: dict[tuple[str, str], dict[str, Any]], attempts: dict[tuple[str, str], int]) -> str:
    simulators = sorted({sim for _, sim in cells})
    # Matrix variants (`flow@dark@xxl`) are their own rows, kept directly under their base flow.
    tasks = sorted({task for task, _ in cells}, key=lambda task: (task.split("@")[0], task))
    lines = ["| flow | " + " | ".join(simulators) + " |", "|---|" + "---|" * len(simulators)]
    for task in tasks:
        row = [task]
        for sim in simulators:
            run = cells.get((task, sim))
            if run is None:
                row.append("—")
                continue
            label = f"{run['status'].upper()} {run['total_seconds']:.0f}s"
            if attempts[(task, sim)] > 1:
                label += f" ×{attempts[(task, sim)]}"
            if run["status"] != "pass":
                label += f" @{run.get('step', '?')}"
            row.append(label)
        lines.append("| " + " | ".join(row) + " |")
    failures = [run for run in cells.values() if run["status"] != "pass"]
    if failures:
        lines.append("")
        lines.append("Failures:")
        for run in failures:
            lines.append(f"- {run['task']} [{target(run)}] step={run.get('step')}: {run.get('error')}")
            lines.append(f"  artifacts: {Path(run['_dir']).resolve().as_uri()}")
    videos = [run["video"] for run in cells.values() if run.get("video")]
    if videos:
        lines.append("")
        lines.append("Videos:")
        lines.extend(f"- {Path(video).resolve().as_uri()}" for video in videos)
    total = sum(run["total_seconds"] for run in cells.values())
    passed = sum(1 for run in cells.values() if run["status"] == "pass")
    lines.append("")
    lines.append(f"{passed}/{len(cells)} PASS, device-seconds={total:.0f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="artifacts root of the runs (the one fastSimulatorTest printed or `clean --list` shows)")
    parser.add_argument("--session", default="", help="Only runs tagged with this --session")
    parser.add_argument("--since", default="", help="Only runs at/after YYYYmmdd-HHMMSS")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.session and not args.since:
        parser.error("pass --session or --since")
    runs = load_runs(args.root.expanduser().resolve(), args.session, args.since)
    if not runs:
        print("no runs matched", file=sys.stderr)
        return 1
    cells = latest_per_cell(runs)
    attempts: dict[tuple[str, str], int] = {}
    for run in runs:
        key = (run.get("task", "?"), target(run))
        attempts[key] = attempts.get(key, 0) + 1
    if args.json:
        print(json.dumps({f"{task}@{sim}": run for (task, sim), run in cells.items()}, indent=2))
    else:
        print(render(cells, attempts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
