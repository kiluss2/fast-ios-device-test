#!/usr/bin/env python3

"""File a flow run's screenshots and clip as PR evidence, one folder per ticket.

Both engines call `after_run` once a flow run has written its metrics.json. A run is filed
when it passed, or when it is marked "before" (`--before`, or `"evidence": {"role": "before"}`)
and failed at or after its outcome step (an action with `"outcome": true`, else the last action
that is not a screenshot, dump, motion or pause): a failure earlier than that is a broken flow,
not the bug.

    <FAST_EVIDENCE_DIR, default ~/Desktop>/<project> <ticket>/
      evidence/                    everything the PR attaches, and nothing else
        <sheet>.jpg                captioned sheet of every filed shot (the ticket by default)
        <case>.mp4, <case>-before.mp4
      source/sheet.json            fastEvidenceSheet spec; edit it and render again
      source/shots/<case>/<role>-<shot>.jpg   720 px wide, JPEG 80

The ticket comes from the flow's `evidence.ticket`, FAST_EVIDENCE_TICKET, or the ticket keys
in the git branch of `project_root`; a folder outside git with none of those files nothing.
A filed PASS run in the default artifacts root keeps only its metrics.json, whose `video` then
points at the filed clip. FAST_EVIDENCE=0 or `"evidence": false` turns filing off.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

SHOT_WIDTH = 720
SHOT_QUALITY = 80
# The PR attaches this folder whole, so it holds nothing else.
EVIDENCE_DIR = "evidence"
# Jira-style keys in upper case only: a lower-case `fix-crash-2` in a branch is not a ticket.
TICKET_KEY = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+-\d+(?!\d)")
CAPTURE_ACTIONS = {"screenshot", "dump", "motion", "pause"}


def settings(spec: dict[str, Any]) -> dict[str, Any] | None:
    """The flow's `evidence` object, or None when filing is turned off."""
    if os.environ.get("FAST_EVIDENCE") == "0":
        return None
    value = spec.get("evidence", {})
    if value is False:
        return None
    return dict(value) if isinstance(value, dict) else {}


def git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def tickets_in(branch: str) -> str | None:
    keys = list(dict.fromkeys(TICKET_KEY.findall(branch)))
    return " ".join(keys) or None


def safe(text: str) -> str:
    """A name that is safe in a file name and in a browser's upload dialog."""
    return re.sub(r"[^A-Za-z0-9._@-]+", "-", text).strip("-") or "case"


def folder_safe(text: str) -> str:
    return re.sub(r"[/:]+", "-", text).strip()


def ticket_name(project_root: Path, evidence: dict[str, Any]) -> str | None:
    explicit = evidence.get("ticket") or os.environ.get("FAST_EVIDENCE_TICKET")
    if explicit:
        return str(explicit)
    branch = git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return None
    if branch == "HEAD":
        return date.today().isoformat()
    return tickets_in(branch) or safe(branch.replace("/", "-"))


def project_name(project_root: Path, evidence: dict[str, Any]) -> str:
    explicit = evidence.get("project") or os.environ.get("FAST_EVIDENCE_PROJECT")
    if explicit:
        return str(explicit)
    # The main checkout's name, so a worktree of the base branch files beside the ticket's runs.
    common = git(project_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and Path(common).name == ".git":
        return Path(common).parent.name
    top = git(project_root, "rev-parse", "--show-toplevel")
    return Path(top).name if top else project_root.resolve().name


def folder_for(project_root: Path, evidence: dict[str, Any]) -> tuple[Path, str, str] | None:
    """(evidence folder, project, ticket) for runs of this checkout, or None when it has no ticket."""
    ticket = ticket_name(project_root, evidence)
    if ticket is None:
        return None
    project = project_name(project_root, evidence)
    root = Path(os.environ.get("FAST_EVIDENCE_DIR") or Path.home() / "Desktop").expanduser()
    return root / folder_safe(f"{project} {ticket}"), project, ticket


def outcome_step(actions: list[dict[str, Any]]) -> int:
    """The action that checks the result: the one marked `"outcome": true`, else the last one that
    can fail for the app's sake (screenshots, dumps and pauses after it only capture)."""
    marked = [index for index, action in enumerate(actions, start=1) if action.get("outcome")]
    if marked:
        return marked[0]
    checks = [index for index, action in enumerate(actions, start=1) if action.get("type") not in CAPTURE_ACTIONS]
    return checks[-1] if checks else len(actions)


def should_file(status: str, role: str, step: str, actions: list[dict[str, Any]]) -> bool:
    if status == "pass":
        return True
    if role != "before":
        return False
    # Both engines name a failing action "<NN>_<type>"; prepare, install and launch have no number.
    match = re.match(r"(\d+)_", str(step))
    return bool(match) and int(match.group(1)) >= outcome_step(actions)


def shot_files(actions: list[dict[str, Any]], artifacts: Path, wanted: list[str], with_failure: bool) -> list[tuple[str, Path]]:
    """(shot name, file) for each screenshot the run saved, in step order."""
    found: dict[str, Path] = {}
    for index, action in enumerate(actions, start=1):
        if action.get("type") != "screenshot":
            continue
        name = str(action.get("name") or f"step-{index:02d}")
        stem = name[:-4] if name.lower().endswith(".png") else name
        # The Simulator saves the name as given; the iPhone engine adds .png to it.
        for candidate in (name, f"{name}.png"):
            if (artifacts / candidate).is_file():
                found.setdefault(stem, artifacts / candidate)
                break
    if with_failure and (artifacts / "failure.png").is_file():
        found.setdefault("failure", artifacts / "failure.png")
    if wanted:
        return [(stem, found[stem]) for stem in wanted if stem in found]
    return list(found.items())


def store_shot(source: Path, target: Path) -> None:
    from PIL import Image

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    with Image.open(source) as image:
        image = image.convert("RGB")
        if image.width > SHOT_WIDTH:
            image = image.resize((SHOT_WIDTH, round(image.height * SHOT_WIDTH / image.width)), Image.LANCZOS)
        image.save(partial, "JPEG", quality=SHOT_QUALITY, optimize=True, progressive=True)
    os.replace(partial, target)


def store_clip(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    shutil.copyfile(source, partial)
    os.replace(partial, target)


@contextmanager
def folder_lock(folder: Path) -> Iterator[None]:
    """Sessions filing the same ticket at once take turns on its sheet and shots."""
    source = folder / "source"
    source.mkdir(parents=True, exist_ok=True)
    with (source / ".lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def key_parts(cell: dict[str, Any]) -> tuple[str, str]:
    parts = str(cell.get("key", "")).split("/")
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", "")


def merge_cells(cells: list[dict[str, Any]], removed: list[str], new_cells: list[dict[str, Any]]) -> None:
    """A known cell gets only its new image (the agent may have edited its text or moved it); a new
    one goes beside its case: before cells ahead of the case's after cells, after cells behind them."""
    for cell in new_cells:
        if cell["key"] in removed:
            continue
        existing = next((known for known in cells if known.get("key") == cell["key"]), None)
        if existing is not None:
            existing["image"] = cell["image"]
            continue
        case, role = key_parts(cell)
        same = [index for index, known in enumerate(cells) if key_parts(known) == (case, role)]
        other = [index for index, known in enumerate(cells) if key_parts(known)[0] == case and key_parts(known)[1] != role]
        if same:
            position = same[-1] + 1
        elif other:
            position = other[0] if role == "before" else other[-1] + 1
        else:
            position = len(cells)
        cells.insert(position, cell)


def file_run(
    spec: dict[str, Any], evidence: dict[str, Any], folder: Path, title: str, ticket: str, task: str,
    device: str, role: str, status: str, artifacts: Path, video: Path | None,
) -> dict[str, Any] | None:
    before = role == "before"
    shots = shot_files(spec.get("actions") or [], artifacts, list(evidence.get("shots") or []), status != "pass")
    clip = video if video and video.is_file() else None
    if not shots and not clip:
        return None
    case = safe(str(evidence.get("case") or f"{task}-{device}"))
    label = str(evidence.get("label") or f"{task} · {device}") + (" · before" if before else "")
    caption = str(evidence.get("before_caption") or "") if before else str(evidence.get("caption") or spec.get("expect") or "")
    sheet_name = str(evidence.get("sheet") or "")
    with folder_lock(folder):
        stored: dict[str, str] = {}
        cells = []
        for stem, source in shots:
            target = folder / "source/shots" / case / f"{role}-{safe(stem)}.jpg"
            store_shot(source, target)
            stored[stem] = str(target)
            cells.append({
                "key": f"{case}/{role}/{stem}", "image": f"shots/{case}/{role}-{safe(stem)}.jpg",
                "label": label + (f" · {stem}" if len(shots) > 1 else ""), "caption": caption,
            })
        filed_clip = None
        if clip:
            filed_clip = folder / EVIDENCE_DIR / f"{case}{'-before' if before else ''}{clip.suffix or '.mp4'}"
            store_clip(clip, filed_clip)
        if cells:
            spec_path = folder / "source" / (f"sheet-{safe(sheet_name)}.json" if sheet_name else "sheet.json")
            sheet = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.exists() else {}
            sheet.setdefault("title", title)
            sheet.setdefault("out", f"../{EVIDENCE_DIR}/{folder_safe(sheet_name or ticket)}.jpg")
            sheet.setdefault("cells", [])
            merge_cells(sheet["cells"], list(sheet.get("removed") or []), cells)
            spec_path.write_text(json.dumps(sheet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if sheet["cells"]:
                import fast_evidence_sheet

                fast_evidence_sheet.build(spec_path)
    return {"dir": folder, "shots": stored, "clip": filed_clip}


def tidy_run(artifacts: Path, video: Path | None) -> None:
    """Everything of a filed PASS run but its metrics.json, and its task-level clip."""
    for entry in artifacts.iterdir():
        if entry.name == "metrics.json":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    if video and video.parent != artifacts:
        video.unlink(missing_ok=True)


def after_run(
    spec: dict[str, Any], project_root: Path, task: str, device: str | Callable[[], str],
    role: str | None, artifacts: Path, video: Path | None, tidy: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """File the run if it counts; returns (what was filed, None) or (None, the error). A filing
    problem never changes the run's own result."""
    evidence = settings(spec)
    if evidence is None:
        return None, None
    try:
        metrics_path = artifacts / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        status = str(metrics.get("status", ""))
        role = "before" if (role or evidence.get("role")) == "before" else "after"
        if not should_file(status, role, str(metrics.get("step", "")), spec.get("actions") or []):
            return None, None
        named = folder_for(project_root, evidence)
        if named is None:
            return None, None
        folder, project, ticket = named
        filed = file_run(
            spec, evidence, folder, f"{project} · {ticket}", ticket, task, device() if callable(device) else device,
            role, status, artifacts, video,
        )
        if filed is None:
            return None, None
        metrics["evidence"] = str(filed["dir"])
        metrics["evidence_shots"] = filed["shots"]
        emptied = tidy and status == "pass"
        if emptied:
            if filed["clip"]:
                metrics["video"] = str(filed["clip"])
            else:
                metrics.pop("video", None)
        metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if emptied:
            tidy_run(artifacts, video)
        return filed, None
    except Exception as error:  # noqa: BLE001 - evidence is a by-product, the run's result stands
        return None, f"{type(error).__name__}: {error}"


def simulator_name(udid: str) -> str:
    try:
        listed = json.loads(subprocess.run(
            ["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True, timeout=20,
        ).stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return udid[:8]
    for devices in listed.get("devices", {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return str(device.get("name") or udid[:8])
    return udid[:8]
