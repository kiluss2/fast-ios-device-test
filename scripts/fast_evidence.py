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
      source/runs.json             per case and role: the code it was filed from, when, which flow

Each filed run records a fingerprint of the code it ran (the content of the flow's
`app.build_inputs`, else of the whole checkout; commits that keep the content keep it), so
`fastEvidenceSheet --stale <folder>` can name the cases to retake. A failing run of a case that
has filed evidence marks that case too: its old pictures no longer prove the code.

The ticket comes from the flow's `evidence.ticket`, FAST_EVIDENCE_TICKET, or the ticket keys
in the git branch of `project_root`; a folder outside git with none of those files nothing.
A filed PASS run in the default artifacts root keeps only its metrics.json, whose `video` then
points at the filed clip. FAST_EVIDENCE=0 or `"evidence": false` turns filing off.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime
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


RUNS_FILE = "runs.json"


def git_bytes(root: Path, *args: str, stdin: bytes | None = None) -> bytes | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def code_fingerprint(project_root: Path, inputs: list[str]) -> str | None:
    """A hash of the content of `inputs` (paths in the checkout, else the whole checkout) as it is
    on disk now: blob ids of tracked files, worktree edits and untracked files included, ignored
    files left out. None outside git."""
    top = git(project_root, "rev-parse", "--show-toplevel")
    if not top:
        return None
    specs = [str(Path(path)) for path in inputs] or ["."]
    listed = git_bytes(Path(top), "ls-files", "-s", "-z", "--", *specs)
    changed = git_bytes(Path(top), "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", *specs)
    if listed is None or changed is None:
        return None
    blobs: dict[str, str] = {}
    for entry in listed.split(b"\0"):
        if b"\t" in entry:
            meta, path = entry.split(b"\t", 1)
            blobs[path.decode("utf-8", "surrogateescape")] = meta.split()[1].decode()
    dirty = []
    for entry in changed.split(b"\0"):
        if len(entry) > 3:
            path = entry[3:].decode("utf-8", "surrogateescape")
            if (Path(top) / path).is_file():
                dirty.append(path)
            else:
                blobs.pop(path, None)
    if dirty:
        hashed = git_bytes(Path(top), "hash-object", "--stdin-paths", stdin="\n".join(dirty).encode("utf-8", "surrogateescape"))
        if hashed is None:
            return None
        blobs.update(zip(dirty, hashed.decode().split()))
    digest = hashlib.sha1()
    for path in sorted(blobs):
        digest.update(f"{path}\0{blobs[path]}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:16]


def build_inputs(spec: dict[str, Any], project_root: Path, flow: Path | None) -> list[str]:
    base = flow.parent if flow else project_root
    return [str((base / Path(path).expanduser()).resolve()) for path in (spec.get("app") or {}).get("build_inputs") or []]


def case_name(evidence: dict[str, Any], task: str, device: str) -> str:
    return safe(str(evidence.get("case") or f"{task}-{device}"))


def read_runs(folder: Path) -> dict[str, Any]:
    path = folder / "source" / RUNS_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_runs(folder: Path, runs: dict[str, Any]) -> None:
    path = folder / "source" / RUNS_FILE
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(runs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(partial, path)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
    device: str, role: str, status: str, artifacts: Path, video: Path | None, run_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    before = role == "before"
    shots = shot_files(spec.get("actions") or [], artifacts, list(evidence.get("shots") or []), status != "pass")
    clip = video if video and video.is_file() else None
    if not shots and not clip:
        return None
    case = case_name(evidence, task, device)
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
        runs = read_runs(folder)
        runs[f"{case}/{role}"] = {
            "filed": now(), "task": task, "device": device, "sheet": sheet_name or None,
            "shots": [f"{case}/{role}/{stem}" for stem in stored], "clip": filed_clip.name if filed_clip else None,
            **(run_info or {}),
        }
        write_runs(folder, runs)
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
    flow: Path | None = None, engine: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """File the run if it counts; returns (what was filed, None) or (None, the error). A failing
    run of a case with filed evidence returns ({"dir", "stale", "filed"}, None) and marks the case
    in runs.json. A filing problem never changes the run's own result."""
    evidence = settings(spec)
    if evidence is None:
        return None, None
    try:
        metrics_path = artifacts / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        status = str(metrics.get("status", ""))
        role = "before" if (role or evidence.get("role")) == "before" else "after"
        step = str(metrics.get("step", ""))
        filing = should_file(status, role, step, spec.get("actions") or [])
        if not filing and (status == "pass" or role != "after"):
            return None, None
        named = folder_for(project_root, evidence)
        if named is None:
            return None, None
        folder, project, ticket = named
        device_name = device() if callable(device) else device
        inputs = build_inputs(spec, project_root, flow)
        build = code_fingerprint(project_root, inputs)
        if not filing:
            return mark_failed(folder, case_name(evidence, task, device_name), step, build), None
        run_info = {
            "build": build, "project_root": str(project_root), "inputs": inputs,
            "flow": str(flow) if flow else None, "engine": engine or None,
        }
        filed = file_run(
            spec, evidence, folder, f"{project} · {ticket}", ticket, task, device_name,
            role, status, artifacts, video, run_info,
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


def mark_failed(folder: Path, case: str, step: str, build: str | None) -> dict[str, Any] | None:
    """A failing run of a case whose evidence is filed: its pictures were taken from code that no
    longer passes, so the case is marked until a passing run files it again."""
    if not (folder / "source" / RUNS_FILE).exists():
        return None
    with folder_lock(folder):
        runs = read_runs(folder)
        entry = runs.get(f"{case}/after")
        if entry is None:
            return None
        entry["failed"] = {"at": now(), "step": step, "build": build}
        write_runs(folder, runs)
    return {"dir": folder, "stale": case, "filed": entry.get("filed")}


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
