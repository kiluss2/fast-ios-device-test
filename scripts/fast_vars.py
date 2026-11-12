"""Flow variables and the `host` step: a command on the Mac (another phone's server calls, the Mac's
Wi-Fi), the JSON fields it prints kept as `${name}` for later steps, and its `undo` run when the flow
ends. Simulator only: an iPhone run has no Mac step in the middle of its XCUITest request."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
HOST_LOG_LIMIT = 500
HOST_TIMEOUT = 30.0
HOST_TYPES = ("host", "undo")


class VarError(ValueError):
    """A step that cannot run: an unknown variable, or a host command that failed."""


def substitute(value: Any, variables: dict[str, str], where: str) -> Any:
    """Every `${name}` in every string, once: a value that itself holds `${…}` stays as it is."""
    if isinstance(value, str):
        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise VarError(f"{where}: unknown variable ${{{name}}}")
            return str(variables[name])
        return VAR.sub(one, value)
    if isinstance(value, list):
        return [substitute(item, variables, where) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables, where) for key, item in value.items()}
    return value


def json_path(data: Any, path: str) -> Any:
    """`data.members.0.id`: dots between keys, digits index a list."""
    current = data
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def spawn(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """The one place a host command starts (tests replace it)."""
    return subprocess.run(argv, **kwargs)


def run_host(argv: list[str], cwd: Path, timeout: float, save: dict[str, str] | None) -> tuple[dict[str, str], str]:
    """Runs a command (no shell) and returns the fields `save` names and its stdout, cut for the log."""
    name = Path(argv[0]).name
    try:
        done = spawn(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise VarError(f"{name} timed out after {timeout:g} s") from error
    except OSError as error:
        raise VarError(f"{name} could not start: {error}") from error
    stdout = done.stdout or ""
    if done.returncode != 0:
        raise VarError(f"{name} exited {done.returncode}: {(done.stderr or stdout).strip()[-300:]}")
    saved: dict[str, str] = {}
    if save:
        try:
            data = json.loads(stdout)
        except ValueError as error:
            raise VarError(f"{name} printed no JSON for save: {stdout.strip()[:120]}") from error
        for variable, path in save.items():
            try:
                value = json_path(data, path)
            except KeyError:
                raise VarError(f"no '{path}' in {name}'s output") from None
            saved[variable] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return saved, stdout[:HOST_LOG_LIMIT]


def run_undo(argv: list[str], cwd: Path) -> str | None:
    """An undo never fails the flow: a warning line when it did not work, else None."""
    try:
        run_host(argv, cwd, HOST_TIMEOUT, None)
    except VarError as error:
        return f"WARN undo {Path(argv[0]).name}: {error}"
    return None


class FlowVars:
    """The variables of one flow run (or one `do` request) and the undos its host steps declared."""

    def __init__(self, variables: dict[str, str], cwd: Path) -> None:
        self.variables = {key: str(value) for key, value in variables.items()}
        self.cwd = cwd
        self.undo: list[dict[str, Any]] = []

    def expand(self, action: dict[str, Any], index: int) -> dict[str, Any]:
        """The action with its variables filled in; a host step's `undo` waits until its own run saved."""
        where = f"step {index}"
        if action.get("type") == "host":
            later = {key: value for key, value in action.items() if key != "undo"}
            return {**substitute(later, self.variables, where), **({"undo": action["undo"]} if "undo" in action else {})}
        return substitute(action, self.variables, where)

    def step(self, action: dict[str, Any], index: int) -> dict[str, Any] | None:
        """Runs a `host` or `undo` action (already expanded); returns its result for the run's log."""
        kind = action.get("type")
        if kind not in HOST_TYPES:
            return None
        argv = [str(part) for part in action["run"]]
        if kind == "host" and action.get("undo"):
            # Checked before the command runs: a step that turns the Wi-Fi off must not run when its way back cannot.
            known = set(self.variables) | set(action.get("save") or {})
            unknown = [name for part in action["undo"] for name in VAR.findall(str(part)) if name not in known]
            if unknown:
                raise VarError(f"step {index} undo: unknown variable ${{{unknown[0]}}}")
        try:
            saved, stdout = run_host(argv, self.cwd, float(action.get("timeout", HOST_TIMEOUT)),
                                     action.get("save") if kind == "host" else None)
        except VarError as error:
            raise VarError(f"step {index}: {error}") from None
        self.variables.update(saved)
        if kind == "host" and action.get("undo"):
            undo = substitute(action["undo"], self.variables, f"step {index} undo")
            self.undo.append({"run": [str(part) for part in undo], "cwd": str(self.cwd)})
        return {"host": Path(argv[0]).name, "stdout": stdout, "saved": sorted(saved)}

    def pending(self) -> list[list[str]]:
        return [entry["run"] for entry in self.undo]

    def run_undos(self) -> list[str]:
        """Every declared undo, last first; the warnings of those that failed."""
        warnings = [warning for entry in reversed(self.undo) if (warning := run_undo(entry["run"], Path(entry["cwd"])))]
        self.undo = []
        return warnings
