#!/usr/bin/env python3

"""Run compact, data-driven iOS Simulator UI flows."""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

# bin/ holds symlinks in a git checkout but copies in the zip, so look beside scripts/ too.
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "scripts")]
from fast_media import publish_video, shrink_video  # noqa: E402


BUILD_SUFFIXES = {
    ".c", ".cc", ".cpp", ".entitlements", ".gif", ".h", ".jpeg", ".jpg",
    ".json", ".m", ".mm", ".mp3", ".mp4", ".otf", ".pbxproj", ".pdf",
    ".plist", ".png", ".strings", ".swift", ".ttf", ".wav", ".xcconfig",
    ".xcprivacy", ".xcstrings", ".xib",
}


class FastUIError(RuntimeError):
    # The hierarchy seen when a wait gave up, so callers can explain the miss.
    nodes: list[dict[str, Any]] = []


class Metrics:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.steps: dict[str, float] = {}

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.steps[name] = round(time.perf_counter() - started, 3)

    @property
    def total(self) -> float:
        return round(time.perf_counter() - self.started, 3)


def command(
    args: list[str], *, check: bool = True, timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise FastUIError(f"command failed ({result.returncode}) {' '.join(args)}{suffix}")
    return result


def required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise FastUIError(f"missing '{key}' in {context}")
    return mapping[key]


def resolved_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


# AXe links SimulatorKit from the selected Xcode; AXe older than 1.8.0 cannot
# find it under Xcode 27 (getsentry/XcodeBuildMCP#453), which surfaces as these strings.
AXE_XCODE_MARKERS = ("simulatorkit", "privateframeworks", "developer dir", "xcode-select")
AXE_XCODE_HINT = (
    "AXe could not load the simulator frameworks of the selected Xcode; Xcode 27 needs AXe 1.8.0 or newer "
    "(check `axe --version`), or rerun with DEVELOPER_DIR pointing at an Xcode 26 install"
)


# Right after a boot AXe's remote automation session sometimes fails to come up once;
# the next attempt a second later succeeds, so one retry turns a flaky FAIL into a pass.
AXE_SESSION_MARKER = "automation session"
AXE_SESSION_RETRIES = 1


def axe(udid: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    arguments = ["axe", *args, "--udid", udid]
    for attempt in range(AXE_SESSION_RETRIES + 1):
        result = command(arguments, check=False)
        transient = result.returncode != 0 and AXE_SESSION_MARKER in f"{result.stderr}\n{result.stdout}".lower()
        if not transient or attempt == AXE_SESSION_RETRIES:
            break
        time.sleep(1.0)
    if check and result.returncode != 0:
        output = f"{result.stderr}\n{result.stdout}"
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = f"command failed ({result.returncode}) {' '.join(arguments)}"
        message += f": {detail[-1]}" if detail else ""
        if any(marker in output.lower() for marker in AXE_XCODE_MARKERS):
            message += f" — {AXE_XCODE_HINT}"
        raise FastUIError(message)
    return result


def file_link(path: Path | str) -> str:
    """Absolute `file://` URL: terminals open it on click, a bare or relative path they do not."""
    return Path(path).expanduser().resolve().as_uri()


NODE_KEYS = ("type", "AXLabel", "children")


def flatten(value: Any) -> Iterator[dict[str, Any]]:
    """Accessibility nodes of a describe-ui tree, parents first. Only `children`
    is descended into: an AXe node's other dict values (`frame`, …) are attributes."""
    if isinstance(value, dict):
        if any(key in value for key in NODE_KEYS):
            yield value
        yield from flatten(value.get("children") or [])
    elif isinstance(value, list):
        for child in value:
            yield from flatten(child)


def compact_node(node: dict[str, Any]) -> dict[str, Any]:
    """Label, type, id, value, frame and tap centre only: what a flow author needs,
    without the nested children that made a dump grow with the depth of the tree."""
    compact: dict[str, Any] = {}
    for key, source in (("label", "AXLabel"), ("type", "type"), ("id", "AXUniqueId"), ("value", "AXValue")):
        value = node.get(source)
        if value not in (None, ""):
            compact[key] = str(value) if key != "type" else value
    if node.get("enabled") is False:
        compact["enabled"] = False
    frame = node.get("frame") or {}
    try:
        box = {name: round(float(frame[name]), 1) for name in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return compact
    compact["frame"] = box
    compact["center"] = [round(box["x"] + box["width"] / 2, 1), round(box["y"] + box["height"] / 2, 1)]
    return compact


def write_dump(path: Path, nodes: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps([compact_node(node) for node in nodes], indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


DESCRIBE_UI_ATTEMPTS = 3
# A describe-ui call costs about a second of AXe session setup; a tree younger than
# this, taken by the wait that just succeeded, is still the screen the tap lands on.
HIERARCHY_REUSE = 0.3
_HIERARCHY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def invalidate_hierarchy(udid: str) -> None:
    _HIERARCHY_CACHE.pop(udid, None)


def hierarchy(udid: str, max_age: float = 0.0) -> list[dict[str, Any]]:
    cached = _HIERARCHY_CACHE.get(udid)
    if cached is not None and max_age > 0 and time.perf_counter() - cached[0] <= max_age:
        return cached[1]
    # AXe occasionally fails once right after launch while the accessibility
    # server is still coming up; one failed poll must not fail the whole flow.
    last_error: Exception | None = None
    for attempt in range(DESCRIBE_UI_ATTEMPTS):
        try:
            result = axe(udid, "describe-ui")
            nodes = list(flatten(json.loads(result.stdout)))
            _HIERARCHY_CACHE[udid] = (time.perf_counter(), nodes)
            note_system_alert(nodes)
            return nodes
        except (FastUIError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(0.4 * (attempt + 1))
    raise FastUIError(f"describe-ui failed {DESCRIBE_UI_ATTEMPTS} times: {last_error}")


NOISE_LABEL = re.compile(r"^[\d:.,%\s]*$")
SCROLL_BAR_LABEL = re.compile(r"^(Vertical|Horizontal) scroll bar, \d+ pages?$")


def visible_labels(nodes: list[dict[str, Any]], limit: int = 40) -> list[str]:
    # Screen order (top-left first), without clock/battery/widget digits that
    # otherwise crowd out the labels a flow author actually needs.
    labels: list[str] = []
    for node in sort_nodes(nodes, ["y", "x"]):
        label = str(node.get("AXLabel") or "").strip()
        if (
            label and label not in labels
            and not NOISE_LABEL.match(label) and not SCROLL_BAR_LABEL.match(label)
        ):
            labels.append(label)
    return labels[:limit]


def node_type(node: dict[str, Any]) -> str | None:
    return node.get("type") or node.get("AXRole")


ALERT_TYPES = ("Sheet", "Alert")


def inside(node: dict[str, Any], container: dict[str, Any]) -> bool:
    inner, outer = node.get("frame") or {}, container.get("frame") or {}
    try:
        return (
            float(outer["x"]) <= float(inner["x"]) and float(outer["y"]) <= float(inner["y"])
            and float(inner["x"]) + float(inner["width"]) <= float(outer["x"]) + float(outer["width"])
            and float(inner["y"]) + float(inner["height"]) <= float(outer["y"]) + float(outer["height"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def alert_sheets(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Alert containers on screen. AXe on iOS 27 reports both a UIAlertController and a
    SwiftUI `.alert` as a Sheet whose buttons sit inside its frame."""
    return [node for node in nodes if node_type(node) in ALERT_TYPES and tappable(node)]


def system_alert(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The system alert (permission prompt, SpringBoard alert) covering the app, if any.
    Measured on iOS 27 / AXe: while one is up, describe-ui returns SpringBoard's tree, whose
    root Application has a blank label (" ") and SpringBoard's pid, and the app's own nodes
    are absent; an in-app alert has the same Sheet > … > Button shape but sits under the
    app's root (label = app name, the app's pid)."""
    if not nodes or node_type(nodes[0]) != "Application" or str(nodes[0].get("AXLabel") or "").strip():
        return None
    sheets = alert_sheets(nodes)
    return sheets[0] if sheets else None


# Alerts the flow saw but did not dismiss: {title: first step that saw it}.
_SYSTEM_ALERTS: dict[str, str] = {}
_CURRENT_STEP = ["prepare"]
# Permission prompts the optional_permission_labels auto-tap dismissed, for the report.
AUTO_TAPPED: list[dict[str, str]] = []


def note_system_alert(nodes: list[dict[str, Any]]) -> None:
    sheet = system_alert(nodes)
    if sheet is not None:
        _SYSTEM_ALERTS.setdefault(str(sheet.get("AXLabel") or "system alert"), _CURRENT_STEP[0])


def forget_system_alert(sheet: dict[str, Any]) -> None:
    _SYSTEM_ALERTS.pop(str(sheet.get("AXLabel") or "system alert"), None)


def alert_button(
    nodes: list[dict[str, Any]], label: str, system_only: bool
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(button, its alert) for a Button labelled `label` inside an alert; a same-named
    button in the app's own layout (no alert around it) never matches."""
    sheets = [sheet for sheet in [system_alert(nodes)] if sheet] if system_only else alert_sheets(nodes)
    for sheet in sheets:
        for node in nodes:
            if node.get("AXLabel") == label and node_type(node) == "Button" and tappable(node) and inside(node, sheet):
                return node, sheet
    return None


def typed_labels(nodes: list[dict[str, Any]], limit: int = 25) -> list[str]:
    """`visible_labels` with the element type beside each label: a flow author sees at
    once that "Add Widget" is a StaticText, which `tap_label` (Button) never matches."""
    by_label: dict[str, str | None] = {}
    for node in sort_nodes(nodes, ["y", "x"]):
        label = str(node.get("AXLabel") or "").strip()
        if label and label not in by_label:
            by_label[label] = node_type(node)
    return [
        f"{label} ({by_label.get(label)})" if by_label.get(label) else label
        for label in visible_labels(nodes, limit=limit)
    ]


def selector_hints(selector: dict[str, Any], nodes: list[dict[str, Any]]) -> str:
    """Why a selector may have missed: same label under another type, or a near-miss label."""
    wanted = selector.get("label") or selector.get("label_contains")
    if not wanted:
        return ""
    expected_type = selector.get("element_type")
    if selector.get("label") is not None:
        other_types = sorted({
            str(node_type(node)) for node in nodes
            if node.get("AXLabel") == wanted and node_type(node) and node_type(node) != expected_type
        })
        if other_types and expected_type:
            return (
                f"; '{wanted}' exists as {', '.join(other_types)}, not {expected_type}: "
                "add element_type or use tap_first"
            )
    labels = visible_labels(nodes, limit=200)
    close = difflib.get_close_matches(str(wanted), labels, n=3, cutoff=0.6)
    close += [label for label in labels if str(wanted).lower() in label.lower() and label not in close]
    return f"; closest labels: [{', '.join(close[:3])}]" if close else ""


SELECTOR_KEYS = ("label", "label_contains", "label_is_empty", "id", "value", "element_type", "enabled", "frame")


def selector_of(action: dict[str, Any], default_type: str | None = None) -> dict[str, Any]:
    """The selector keys an action carries; `default_type` fills in `element_type` when absent."""
    selector = {key: action[key] for key in SELECTOR_KEYS if key in action}
    if default_type is not None and "element_type" not in selector:
        selector["element_type"] = default_type
    return selector


def matches(node: dict[str, Any], selector: dict[str, Any]) -> bool:
    label = selector.get("label")
    if label is not None and node.get("AXLabel") != label:
        return False
    identifier = selector.get("id")
    if identifier is not None and node.get("AXUniqueId") != identifier:
        return False
    value = selector.get("value")
    if value is not None and str(node.get("AXValue")) != str(value):
        return False
    if selector.get("label_is_empty") and node.get("AXLabel"):
        return False
    fragment = selector.get("label_contains")
    if fragment is not None and fragment not in str(node.get("AXLabel") or ""):
        return False
    expected_type = selector.get("element_type")
    if expected_type is not None and node_type(node) != expected_type:
        return False
    enabled = selector.get("enabled")
    if enabled is not None and bool(node.get("enabled", True)) != bool(enabled):
        return False
    frame = node.get("frame") or {}
    limits = selector.get("frame") or {}
    properties = {
        "x": float(frame.get("x", -1)),
        "y": float(frame.get("y", -1)),
        "width": float(frame.get("width", 0)),
        "height": float(frame.get("height", 0)),
    }
    for property_name, actual in properties.items():
        minimum = limits.get(f"min_{property_name}")
        maximum = limits.get(f"max_{property_name}")
        if minimum is not None and actual < float(minimum):
            return False
        if maximum is not None and actual > float(maximum):
            return False
    return True


PROMPT_GRACE = 4.0


def wait_nodes(
    udid: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    timeout: float,
    optional_prompt_labels: list[str] | None = None,
    first_poll_max_age: float = 0.0,
) -> list[dict[str, Any]]:
    deadline = time.perf_counter() + timeout
    nodes: list[dict[str, Any]] = []
    max_age = first_poll_max_age
    while time.perf_counter() < deadline:
        nodes = hierarchy(udid, max_age)
        max_age = 0.0
        # Only a button inside a system alert: an in-app "Allow"/"OK" is the app's own UI.
        prompt = next(
            (
                found
                for label in (optional_prompt_labels or [])
                for found in [alert_button(nodes, label, system_only=True)]
                if found is not None
            ),
            None,
        )
        if prompt is not None:
            tap_point(udid, *frame_center(prompt[0]), 0.08)
            forget_system_alert(prompt[1])
            AUTO_TAPPED.append({"step": _CURRENT_STEP[0], "alert": str(prompt[1].get("AXLabel") or ""),
                                "button": str(prompt[0].get("AXLabel"))})
            invalidate_hierarchy(udid)
            # Dismissing a permission alert is not progress toward the selector: under load
            # one describe-ui can take 10 s+, so keep a few seconds to see the screen behind it.
            deadline = max(deadline, time.perf_counter() + PROMPT_GRACE)
            continue
        if predicate(nodes):
            return nodes
        time.sleep(0.12)
    labels = ", ".join(typed_labels(nodes, limit=25))
    error = FastUIError(f"timed out waiting for UI selector; visible labels: [{labels}]")
    error.nodes = nodes
    raise error


CONTAINER_TYPES = ("Application", "Window")


def tappable(node: dict[str, Any]) -> bool:
    frame = node.get("frame") or {}
    try:
        return float(frame["width"]) > 0 and float(frame["height"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def resolve_tap_target(
    nodes: list[dict[str, Any]], selector: dict[str, Any], explicit_type: bool
) -> dict[str, Any] | None:
    """The node `tap_label` taps: the first match of the full selector; else, when the
    flow did not pin `element_type`, the one node of any type with that label (a SwiftUI
    text or image that is tappable but not exposed as a Button)."""
    typed = sort_nodes([node for node in nodes if matches(node, selector)], ["y", "x"])
    if typed:
        return typed[0]
    if explicit_type:
        return None
    loose = {key: value for key, value in selector.items() if key != "element_type"}
    # The app's root node carries the app name as its label; "tapping" it would hit the screen centre.
    candidates = [
        node for node in nodes
        if matches(node, loose) and tappable(node) and node_type(node) not in CONTAINER_TYPES
    ]
    return candidates[0] if len(candidates) == 1 else None


def wait_selector(
    udid: str,
    selector: dict[str, Any],
    timeout: float,
    optional_prompt_labels: list[str],
    predicate: Callable[[list[dict[str, Any]]], bool] | None = None,
    first_poll_max_age: float = 0.0,
) -> list[dict[str, Any]]:
    """Wait until `selector` matches (or `predicate` holds); a timeout explains the miss."""
    try:
        return wait_nodes(
            udid,
            predicate or (lambda nodes: any(matches(node, selector) for node in nodes)),
            timeout,
            optional_prompt_labels,
            first_poll_max_age,
        )
    except FastUIError as error:
        explained = FastUIError(f"{error}{selector_hints(selector, error.nodes)}")
        explained.nodes = error.nodes
        raise explained from None


def wait_absent(
    udid: str,
    selector: dict[str, Any],
    timeout: float,
    optional_prompt_labels: list[str],
) -> None:
    """Succeed as soon as nothing matches the selector; fail if it is still there at the deadline."""
    try:
        wait_nodes(
            udid,
            lambda nodes: not any(matches(node, selector) for node in nodes),
            timeout,
            optional_prompt_labels,
        )
    except FastUIError as error:
        labels = str(error).split("visible labels:")[-1].strip()
        raise FastUIError(
            f"'{selector.get('label')}' is still on screen after {timeout:g}s; visible labels: {labels}"
        ) from None


def assert_absent(
    udid: str,
    selector: dict[str, Any],
    seconds: float,
    optional_prompt_labels: list[str],
) -> None:
    """Fail the moment the selector appears during the whole window; used to prove a gate held."""
    deadline = time.perf_counter() + seconds
    while True:
        nodes = wait_nodes(udid, lambda _: True, seconds, optional_prompt_labels)
        if any(matches(node, selector) for node in nodes):
            labels = ", ".join(visible_labels(nodes, limit=25))
            raise FastUIError(
                f"'{selector.get('label')}' appeared while asserting absence; visible labels: [{labels}]"
            )
        if time.perf_counter() >= deadline:
            return
        time.sleep(0.3)


SETTLE_TIMEOUT = 2.5


def tree_signature(nodes: list[dict[str, Any]]) -> str:
    """Labels, types and whole-point frames in screen order: equal signatures mean nothing moved."""
    parts = []
    for node in sort_nodes(nodes, ["y", "x"]):
        frame = node.get("frame") or {}
        box = ",".join(f"{float(frame.get(key, 0) or 0):.0f}" for key in ("x", "y", "width", "height"))
        parts.append(f"{node.get('AXLabel') or ''}/{node_type(node) or ''}/{box}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def wait_settled(
    udid: str, timeout: float = SETTLE_TIMEOUT, interval: float = 0.1, stable_for: float = 0.1
) -> bool:
    """Poll until two consecutive trees match and have for `stable_for`; False after `timeout`.
    Opt-in per action: a screen that animates forever would otherwise pay the whole timeout."""
    deadline = time.perf_counter() + timeout
    previous: str | None = None
    stable_since = 0.0
    while True:
        try:
            signature = tree_signature(hierarchy(udid))
        except FastUIError:
            return False
        now = time.perf_counter()
        if signature == previous:
            if now - stable_since >= stable_for:
                return True
        else:
            previous, stable_since = signature, now
        if now >= deadline:
            return False
        time.sleep(interval)


def swipe_points(
    action: dict[str, Any], nodes: list[dict[str, Any]], index: int, area: dict[str, Any] | None = None,
) -> tuple[list[float], list[float]]:
    """`from`/`to` points, or a `direction` the finger moves in ("up" scrolls content down)
    across `area` (an element's frame, e.g. a row to swipe-delete) or else the whole screen."""
    if "direction" not in action:
        return required(action, "from", f"action {index}"), required(action, "to", f"action {index}")
    screen = area or screen_frame(nodes)
    x0, y0 = float(screen.get("x", 0)), float(screen.get("y", 0))
    width, height = float(screen.get("width", 0)), float(screen.get("height", 0))
    mid_x, mid_y = x0 + width / 2, y0 + height / 2
    top, bottom = y0 + height * 0.3, y0 + height * 0.7
    left, right = x0 + width * 0.2, x0 + width * 0.8
    points = {
        "up": ([mid_x, bottom], [mid_x, top]),
        "down": ([mid_x, top], [mid_x, bottom]),
        "left": ([right, mid_y], [left, mid_y]),
        "right": ([left, mid_y], [right, mid_y]),
    }
    if action["direction"] not in points or not width or not height:
        raise FastUIError(f"action {index}: swipe needs from/to or direction up|down|left|right")
    return points[action["direction"]]


def swipe(udid: str, start: list[float], end: list[float], duration: float, post_delay: float) -> None:
    invalidate_hierarchy(udid)
    axe(
        udid, "swipe", "--start-x", str(start[0]), "--start-y", str(start[1]),
        "--end-x", str(end[0]), "--end-y", str(end[1]),
        "--duration", str(duration),
        "--post-delay", str(post_delay),
    )


def frame_center(node: dict[str, Any]) -> tuple[float, float]:
    frame = node.get("frame") or {}
    try:
        return (
            float(frame["x"]) + float(frame["width"]) / 2,
            float(frame["y"]) + float(frame["height"]) / 2,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise FastUIError("selected node has no tappable frame") from error


AXE_MAX_POST_DELAY = 10.0


def split_post_delay(post_delay: float) -> tuple[float, float]:
    capped = min(float(post_delay), AXE_MAX_POST_DELAY)
    return capped, max(0.0, float(post_delay) - capped)


def tap_point(udid: str, x: float, y: float, post_delay: float) -> None:
    invalidate_hierarchy(udid)
    capped, remainder = split_post_delay(max(post_delay, TAP_SETTLE))
    axe(
        udid, "tap", "-x", f"{x:.2f}", "-y", f"{y:.2f}",
        "--post-delay", str(capped),
    )
    if remainder:
        time.sleep(remainder)


def screenshot(udid: str, path: Path) -> None:
    image_type = path.suffix.removeprefix(".").lower() or "png"
    if image_type == "jpg":
        image_type = "jpeg"
    command(["xcrun", "simctl", "io", udid, "screenshot", f"--type={image_type}", str(path)])


UI_SETTINGS: dict[str, dict[str, str]] = {
    # simctl value -> short tag used in the task name of a matrix variant
    "appearance": {"light": "light", "dark": "dark"},
    "content_size": {
        "extra-small": "xs", "small": "s", "medium": "m", "large": "l",
        "extra-large": "xl", "extra-extra-large": "xxl", "extra-extra-extra-large": "xxxl",
        "accessibility-medium": "ax-m", "accessibility-large": "ax-l",
        "accessibility-extra-large": "ax-xl", "accessibility-extra-extra-large": "ax-xxl",
        "accessibility-extra-extra-extra-large": "ax-xxxl",
    },
}


def ui_variant(setup: dict[str, Any], appearance: str | None, content_size: str | None) -> dict[str, str]:
    """Effective simulator UI settings: CLI overrides win over `setup`."""
    variant: dict[str, str] = {}
    for key, override in (("appearance", appearance), ("content_size", content_size)):
        value = override or setup.get(key)
        if not value:
            continue
        if value not in UI_SETTINGS[key]:
            raise FastUIError(f"unsupported {key} '{value}'; use one of {', '.join(UI_SETTINGS[key])}")
        variant[key] = value
    return variant


def variant_name(name: str, variant: dict[str, str]) -> str:
    return name + "".join(f"@{UI_SETTINGS[key][variant[key]]}" for key in UI_SETTINGS if key in variant)


def apply_ui_settings(udid: str, variant: dict[str, str], previous: dict[str, str]) -> None:
    """Set the variant, recording each replaced value in `previous` before it changes so a
    failure halfway still restores what was already switched."""
    for key, value in variant.items():
        current = command(["xcrun", "simctl", "ui", udid, key]).stdout.strip()
        if current == value:
            continue
        if current in UI_SETTINGS[key]:
            previous[key] = current
        command(["xcrun", "simctl", "ui", udid, key, value])


def restore_ui_settings(udid: str, previous: dict[str, str]) -> None:
    for key, value in previous.items():
        command(["xcrun", "simctl", "ui", udid, key, value], check=False)


def step_labels(udid: str, step: str, at: float, result: dict[str, Any] | None) -> dict[str, Any]:
    """Visible text after a step, so a reviewer checks copy without OCR on the video frames."""
    if result and "labels" in result:
        labels = result["labels"]
    else:
        try:
            labels = visible_labels(hierarchy(udid, HIERARCHY_REUSE))
        except FastUIError:
            labels = []
    return {"step": step, "at": round(at, 2), "labels": labels}


def stop_stale_recorder(udid: str) -> None:
    # A recorder left behind by an aborted run keeps the device locked and
    # makes the next start write a 0-byte file.
    command(["pkill", "-INT", "-f", f"simctl io {udid} recordVideo"], check=False)


class Interrupted(Exception):
    """SIGTERM/SIGHUP arrived; raised from the handler so teardown still runs."""


def raise_interrupted(signum: int, _frame: Any) -> None:
    # A second signal during teardown must not cut the undo short.
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted(f"interrupted by {signal.Signals(signum).name}")


LOCK_DIR = Path(os.environ.get("FAST_UI_LOCK_DIR") or Path.home() / "Library/Caches/fast-ui/locks")
DISPOSABLE_PREFIXES = ("fastwf-", "fastui-")


def simulator_name(udid: str) -> str:
    try:
        with (Path.home() / "Library/Developer/CoreSimulator/Devices" / udid / "device.plist").open("rb") as stream:
            return str(plistlib.load(stream).get("name", ""))
    except (OSError, plistlib.InvalidFileException):
        return ""


class SimulatorLease:
    """Per-UDID flock plus a journal of what this run changed on the simulator.

    The journal is rewritten whenever the run touches simulator-wide state, and
    `dirty` is cleared only after teardown finished. A run killed with SIGKILL keeps
    dirty=1, so the next run on that simulator repairs from the journal first."""

    def __init__(self, udid: str, flow: Path) -> None:
        self.udid = udid
        self.flow = flow
        self.path = LOCK_DIR / f"{udid}.lock"
        self.handle: Any = None
        self.journal: dict[str, Any] = {}
        self.repaired: list[str] = []

    def acquire(self, wait: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.perf_counter() + wait
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.perf_counter() >= deadline:
                    holder = self.read()
                    self.handle.close()
                    self.handle = None
                    # The holder may not have written its journal yet (it just took the lock).
                    who = (
                        f"{holder['flow']} (pid {holder.get('pid')}, since {holder.get('started')})"
                        if holder.get("pid") and not holder.get("dirty") == 0 else "another fast_ui run"
                    )
                    raise FastUIError(
                        f"simulator busy: {self.udid} is running {who}; "
                        "wait for it, set FAST_UI_LOCK_WAIT=<seconds>, or use another simulator"
                    )
                time.sleep(0.5)
        previous = self.read()
        if previous.get("dirty"):
            self.repair(previous)
        self.journal = {
            "pid": os.getpid(), "flow": str(self.flow),
            "started": datetime.now().isoformat(timespec="seconds"), "dirty": 1,
        }
        self.save()

    def read(self) -> dict[str, Any]:
        try:
            self.handle.seek(0)
            return json.loads(self.handle.read() or "{}")
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps(self.journal) + "\n")
        self.handle.flush()

    def touch(self, key: str, value: Any = True) -> None:
        if self.journal.get(key) != value:
            self.journal[key] = value
            self.save()

    def repair(self, previous: dict[str, Any]) -> None:
        """Undo what a killed run recorded; best effort, every step independent."""
        udid = self.udid
        stop_stale_recorder(udid)
        if previous.get("status_bar"):
            command(["xcrun", "simctl", "status_bar", udid, "clear"], check=False)
        if previous.get("location"):
            command(["xcrun", "simctl", "location", udid, "clear"], check=False)
        restore_ui_settings(udid, previous.get("ui") or {})
        for service, app in previous.get("privacy") or []:
            command(["xcrun", "simctl", "privacy", udid, "reset", service, app], check=False)
        bundle_id = previous.get("bundle_id")
        if bundle_id:
            command(["xcrun", "simctl", "terminate", udid, bundle_id], check=False)
            for key in previous.get("preferences") or []:
                command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id, key], check=False)
        self.repaired = [key for key in ("status_bar", "location", "ui", "privacy", "bundle_id", "preferences") if previous.get(key)]
        print(
            f"WARN dirty=1: previous run pid {previous.get('pid', '?')} ({previous.get('flow', '?')}) "
            f"was killed before teardown; repaired {', '.join(self.repaired) or 'recorder'}",
            file=sys.stderr,
        )

    def release(self, clean: bool) -> None:
        if self.handle is None:
            return
        if clean:
            self.journal["dirty"] = 0
            self.save()
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


# Set by main() so actions can journal what they change; None outside a run.
LEASE: SimulatorLease | None = None
PREVIOUS_UI: dict[str, str] = {}


def journal(key: str, value: Any = True) -> None:
    if LEASE is not None:
        LEASE.touch(key, value)


class Recorder:
    """Records the simulator screen with `simctl io recordVideo` for one run."""

    def __init__(self, udid: str, path: Path) -> None:
        self.udid = udid
        self.path = path
        self.process: subprocess.Popen[bytes] | None = None
        self.started: float | None = None

    def elapsed(self) -> float:
        return time.perf_counter() - self.started if self.started is not None else 0.0

    def start(self) -> None:
        stop_stale_recorder(self.udid)
        self.path.unlink(missing_ok=True)
        self.started = time.perf_counter()
        self.process = subprocess.Popen(
            [
                "xcrun", "simctl", "io", self.udid, "recordVideo",
                "--codec", "hevc", "--force", str(self.path),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)

    def stop(self) -> None:
        if self.process is None:
            return
        time.sleep(1.0)
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
        for _ in range(20):
            partial = list(self.path.parent.glob(self.path.name + ".sb-*"))
            if self.path.exists() and not partial:
                break
            time.sleep(1.0)
        self.process = None


def prune_artifacts(root: Path, keep_days: float) -> int:
    """Delete run folders and final videos untouched for `keep_days`; empty task dirs go too."""
    if keep_days <= 0 or not root.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for task_dir in root.iterdir():
        if not task_dir.is_dir():
            continue
        for entry in task_dir.iterdir():
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                continue
        if not any(task_dir.iterdir()):
            task_dir.rmdir()
    return removed


def build_input_files(app: dict[str, Any], base: Path) -> list[Path]:
    files: list[Path] = []
    for raw_root in app.get("build_inputs", []):
        root = resolved_path(raw_root, base)
        if not root.exists():
            continue
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if path.is_file() and path.suffix.lower() in BUILD_SUFFIXES:
                files.append(path)
    return sorted(set(files), key=lambda path: str(path))


def build_fingerprint(app: dict[str, Any], base: Path) -> str:
    digest = hashlib.sha256()
    # The simulator UDID is deliberately excluded: one simulator .app serves
    # every booted simulator, and keying on it forced a full rebuild whenever
    # flows alternated between devices.
    settings = {
        key: app.get(key)
        for key in ("project", "scheme", "configuration", "bundle_id", "app_path")
    }
    digest.update(json.dumps(settings, sort_keys=True).encode("utf-8"))
    for path in build_input_files(app, base):
        stat = path.stat()
        digest.update(str(path).encode("utf-8"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("ascii"))
    return digest.hexdigest()


def read_build_fingerprint(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("fingerprint")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def write_build_fingerprint(path: Path, fingerprint: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fingerprint": fingerprint, "updated_at": datetime.now().isoformat()}, indent=2)
        + "\n",
        encoding="utf-8",
    )


def verify_app(path: Path, bundle_id: str) -> None:
    info = path / "Info.plist"
    if not info.exists():
        raise FastUIError(f"invalid app bundle: {path}")
    with info.open("rb") as stream:
        actual = plistlib.load(stream).get("CFBundleIdentifier")
    if actual != bundle_id:
        raise FastUIError(f"bundle mismatch: expected {bundle_id}, found {actual}")


def ensure_booted(udid: str) -> None:
    # Installing into a shut-down simulator fails only after the build has
    # already spent a minute, so boot (or confirm boot) up front.
    command(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=120)
    ensure_device_hub(udid)


DEVICE_HUB_WAIT = 30.0
# Under Device Hub a tap is dropped when `axe` exits right after sending it:
# measured on Xcode 27.0, 0 s lost every tap, 0.15 s one in three, 0.25 s none.
DEVICE_HUB_TAP_SETTLE = 0.4
TAP_SETTLE = 0.0


def ensure_device_hub(udid: str) -> None:
    # Xcode 27 replaced Simulator.app with Device Hub, and AXe's automation
    # session times out unless Device Hub is running. Older Xcodes ship no
    # Device Hub, so this does nothing there.
    developer_dir = os.environ.get("DEVELOPER_DIR") or command(["xcode-select", "-p"], check=False).stdout.strip()
    if not developer_dir:
        return
    device_hub = Path(developer_dir).parent / "Applications" / "DeviceHub.app"
    if not device_hub.exists():
        return
    global TAP_SETTLE
    TAP_SETTLE = DEVICE_HUB_TAP_SETTLE
    if command(["pgrep", "-f", str(device_hub)], check=False).returncode == 0:
        return
    command(["open", "-g", "-a", str(device_hub)])
    deadline = time.perf_counter() + DEVICE_HUB_WAIT
    while time.perf_counter() < deadline:
        if axe(udid, "describe-ui", check=False).returncode == 0:
            return
        time.sleep(1)


@contextmanager
def build_lock(derived_data: Path) -> Iterator[None]:
    # Parallel flows share one DerivedData; only one xcodebuild may run in it.
    derived_data.mkdir(parents=True, exist_ok=True)
    with (derived_data / ".fastui-build.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare_app(app: dict[str, Any], base: Path, udid: str, reuse_app: bool) -> tuple[Path | None, bool]:
    ensure_booted(udid)
    if app.get("prebuilt"):
        # Drive what is already on the simulator (a system app, a build Xcode installed):
        # nothing to build, so no project or DerivedData is needed.
        if app.get("app_path"):
            app_path = resolved_path(app["app_path"], base)
            verify_app(app_path, required(app, "bundle_id", "app"))
            return app_path, False
        return None, False
    app_path = resolved_path(required(app, "app_path", "app"), base)
    derived_data = resolved_path(required(app, "derived_data", "app"), base)
    if reuse_app:
        verify_app(app_path, required(app, "bundle_id", "app"))
        return app_path, False
    with build_lock(derived_data):
        return build_if_needed(app, base, udid, app_path, derived_data)


SHIM_DIR = Path(__file__).resolve().parent / "shims"
SCRIPT_PHASE = re.compile(
    r"isa = PBXShellScriptBuildPhase;(?P<body>.*?)\n\t\t\};", re.S
)
# A PATH assignment whose value does not start with $PATH puts its own directory
# ahead of the shim (SwiftLint's README form: export PATH="/opt/homebrew/bin:$PATH").
PATH_PREPEND = re.compile(r"\bPATH=(?![\"']?\$\{?PATH\b)")
EXPLICIT_SWIFTLINT = re.compile(r"[^\s;&|\"'`]*/swiftlint\b")


def project_files(project: Path) -> list[Path]:
    if project.suffix == ".xcodeproj":
        return [project / "project.pbxproj"] if (project / "project.pbxproj").exists() else []
    contents = project / "contents.xcworkspacedata"
    found: list[Path] = []
    if contents.exists():
        for location in re.findall(r'location = "(?:group|container|absolute):([^"]+\.xcodeproj)"', contents.read_text(errors="replace")):
            path = Path(location) if location.startswith("/") else project.parent / location
            if (path / "project.pbxproj").exists():
                found.append(path / "project.pbxproj")
    return found


def pbx_string(body: str, key: str) -> str:
    match = re.search(key + r' = (?:"((?:[^"\\]|\\.)*)"|([^;\s]+));', body)
    if not match:
        return ""
    if match.group(2) is not None:
        return match.group(2)
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), match.group(1))


def lint_phase_names(project: Path) -> list[str]:
    names: list[str] = []
    for pbxproj in project_files(project):
        for match in SCRIPT_PHASE.finditer(pbxproj.read_text(errors="replace")):
            if re.search(r"\bswiftlint\b", pbx_string(match.group("body"), "shellScript"), re.I):
                names.append(pbx_string(match.group("body"), "name") or "Run Script")
    return names


def lint_report(project: Path) -> tuple[str, list[str]]:
    """Classify SwiftLint in the project: none, shimmed (the PATH shim neutralises
    every lint phase) or not-shimmable (some phase or plugin bypasses PATH)."""
    shimmed: list[str] = []
    blocked: list[str] = []
    for pbxproj in project_files(project):
        text = pbxproj.read_text(errors="replace")
        if re.search(r"SwiftLint(?:BuildTool)?Plugin", text):
            blocked.append(f"{pbxproj.parent.stem}: SwiftLint build-tool plugin")
        for match in SCRIPT_PHASE.finditer(text):
            script = pbx_string(match.group("body"), "shellScript")
            code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
            if not re.search(r"\bswiftlint\b", code, re.I):
                continue
            name = pbx_string(match.group("body"), "name") or "Run Script"
            explicit = EXPLICIT_SWIFTLINT.search(code)
            if explicit:
                blocked.append(f"'{name}': runs {explicit.group(0)} by path")
            elif re.search(r"\bmint\s+run\b", code):
                blocked.append(f"'{name}': runs swiftlint through mint")
            elif PATH_PREPEND.search(code):
                blocked.append(f"'{name}': prepends to PATH before swiftlint")
            else:
                shimmed.append(name)
    if blocked:
        return "not-shimmable", blocked
    return ("shimmed", shimmed) if shimmed else ("none", [])


def build_failure(output: str, lint_state: str, lint_phases: list[str]) -> str:
    """Name the failing build command and its first error: line instead of the
    last lines of the log (which are only the command list and BUILD FAILED)."""
    lines = output.splitlines()
    failed: list[str] = []
    if "The following build commands failed:" in output:
        start = lines.index(next(l for l in lines if "The following build commands failed:" in l)) + 1
        for line in lines[start:]:
            line = line.strip()
            if not line or line.startswith("(") and "failure" in line:
                break
            phase = re.match(r"PhaseScriptExecution (.+?) /", line)
            failed.append(f"run-script phase '{phase.group(1).replace(chr(92) + ' ', ' ')}'" if phase else line.split(" /")[0])
    errors = [l.strip() for l in lines if re.search(r"(^|: )error: ", l) and "The following build commands failed" not in l]
    errors = [e for e in errors if not e.startswith("error: Command PhaseScriptExecution failed")] or errors
    # xcodebuild -quiet drops the "build commands failed" list; SwiftLint's own
    # summary line and its "Violation: ... (rule_id)" errors still identify it.
    serious = re.search(r"Done linting! Found \d+ violations?, (\d+) serious", output)
    if not failed and ((serious and int(serious.group(1)) > 0) or any(re.search(r"Violation: .*\(\w+\)$", e) for e in errors)):
        failed.append("SwiftLint" + (f" (run-script phase {', '.join(repr(n) for n in lint_phases)})" if lint_phases else ""))
    parts = ["build failed"]
    if failed:
        parts.append("in " + ", ".join(dict.fromkeys(failed)))
    if errors:
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        parts.append(f"first error: {errors[0][:300]}{more}")
    else:
        parts.append("| " + " | ".join(l.strip() for l in lines[-6:]))
    if any("swiftlint" in f.lower() for f in failed) or any("swiftlint" in e.lower() for e in errors):
        parts.append(
            "hint: the lint phase calls swiftlint by path or prepends to PATH, so skip_lint cannot bypass it; "
            "fix the violation or make the phase call plain `swiftlint`" if lint_state == "not-shimmable"
            else "hint: remove \"skip_lint\": false / FAST_UI_SKIP_LINT=0 to skip SwiftLint in test builds"
        )
    return " ".join(parts)


def skip_lint_enabled(app: dict[str, Any]) -> bool:
    override = os.environ.get("FAST_UI_SKIP_LINT")
    if override is not None:
        return override not in ("0", "false", "no", "")
    return bool(app.get("skip_lint", True))


def build_if_needed(
    app: dict[str, Any], base: Path, udid: str, app_path: Path, derived_data: Path
) -> tuple[Path, bool]:
    state_path = derived_data / ".fastui-build-state.json"
    fingerprint_before = build_fingerprint(app, base)
    # Without build_inputs the fingerprint cannot see a source edit, so the cached
    # app would be reused forever; let xcodebuild's own incremental check decide.
    untracked = not build_input_files(app, base)
    stale = untracked or not app_path.exists() or read_build_fingerprint(state_path) != fingerprint_before
    if not stale:
        verify_app(app_path, required(app, "bundle_id", "app"))
        return app_path, False

    project = resolved_path(required(app, "project", "app"), base)
    project_flag = "-workspace" if project.suffix == ".xcworkspace" else "-project"
    build = [
        "xcodebuild", "build", project_flag, str(project),
        "-scheme", required(app, "scheme", "app"),
        "-destination", f"platform=iOS Simulator,id={udid}",
        "-derivedDataPath", str(derived_data),
        "-skipPackagePluginValidation", "-disableAutomaticPackageResolution",
        "COMPILER_INDEX_STORE_ENABLE=NO", "ONLY_ACTIVE_ARCH=YES", "-quiet",
    ]
    if app.get("configuration"):
        build[6:6] = ["-configuration", app["configuration"]]
    env = dict(os.environ)
    lint_state, lint_detail = lint_report(project)
    lint_names = lint_phase_names(project)
    if skip_lint_enabled(app):
        # Xcode keeps the caller's PATH for run-script phases, so a phase that calls
        # plain `swiftlint` finds the no-op shim first.
        env["PATH"] = f"{SHIM_DIR}{os.pathsep}{env.get('PATH', '')}"
    else:
        lint_state = "on" if lint_state != "none" else "none"
    if lint_state != "none":
        print(f"lint={lint_state}" + (f" ({'; '.join(lint_detail)})" if lint_detail else ""), file=sys.stderr)
    result = command(build, check=False, timeout=float(app.get("build_timeout", 300)), env=env)
    if result.returncode != 0:
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        (derived_data / ".fastui-build.log").write_text(output)
        raise FastUIError(build_failure(output, lint_state, lint_names) + f" log={derived_data / '.fastui-build.log'}")
    verify_app(app_path, required(app, "bundle_id", "app"))
    fingerprint_after = build_fingerprint(app, base)
    if fingerprint_after == fingerprint_before:
        write_build_fingerprint(state_path, fingerprint_after)
    return app_path, True


def write_preference(udid: str, domain: str, key: str, value: Any) -> None:
    args = ["xcrun", "simctl", "spawn", udid, "defaults", "write", domain, key]
    if isinstance(value, bool):
        args += ["-bool", "true" if value else "false"]
    elif isinstance(value, int):
        args += ["-int", str(value)]
    elif isinstance(value, float):
        args += ["-float", str(value)]
    elif isinstance(value, str):
        args += ["-string", value]
    elif isinstance(value, dict) and set(value) == {"$json_data"}:
        encoded = json.dumps(
            value["$json_data"], separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        args += ["-data", encoded.hex()]
    else:
        raise FastUIError(f"unsupported preference type for '{key}'")
    command(args)


def set_preferences(udid: str, bundle_id: str, preferences: dict[str, Any]) -> None:
    if not preferences:
        return
    for key, value in preferences.items():
        write_preference(udid, bundle_id, key, value)


def reset_preferences(udid: str, bundle_id: str, preferences: dict[str, Any], fresh_install: bool) -> None:
    # Seeded keys (e.g. isPremium to hide the paywall) live in the simulator's user
    # domain and would otherwise stay active for ordinary app runs after the test.
    if not preferences:
        return
    command(["xcrun", "simctl", "terminate", udid, bundle_id], check=False)
    if fresh_install:
        command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id], check=False)
        return
    for key in preferences:
        command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id, key], check=False)


def sort_nodes(nodes: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    return sorted(
        nodes,
        key=lambda node: tuple(float((node.get("frame") or {}).get(key, 0)) for key in keys),
    )


def motion_ratio(first: Path, second: Path, region: list[float], threshold: int) -> float:
    try:
        # Only `motion` needs Pillow; every other action runs on a stock python3.
        from PIL import Image, ImageChops
    except ImportError as error:
        raise FastUIError("motion needs Pillow: python3 -m pip install --user pillow") from error
    image_a = Image.open(first).convert("RGB")
    image_b = Image.open(second).convert("RGB")
    if image_a.size != image_b.size:
        raise FastUIError("motion frames have different dimensions")
    width, height = image_a.size
    box = (
        round(width * float(region[0])), round(height * float(region[1])),
        round(width * float(region[2])), round(height * float(region[3])),
    )
    difference = ImageChops.difference(image_a.crop(box), image_b.crop(box))
    # Max over channels, then a threshold mask, all in C — a Python loop over a
    # 3x screenshot took seconds per frame pair.
    red, green, blue = difference.split()
    peak = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = peak.point(lambda value: 255 if value >= threshold else 0)
    changed = mask.histogram()[255]
    return changed / (difference.width * difference.height)


GESTURE_KINDS = {
    "tap_label", "tap_first", "tap_nearest", "tap_point", "swipe", "scroll_to",
    "long_press", "double_tap", "drag", "edge_swipe", "back", "pull_to_refresh",
    "set_switch", "set_slider", "set_picker", "tap_alert",
}

# Gestures AXe cannot synthesize: it sends one touch at a time.
DEVICE_ONLY = {
    "pinch": "a two-finger pinch",
    "rotate_gesture": "a two-finger rotation",
    "two_finger_tap": "a two-finger tap",
    "set_orientation": "rotating the device",
}

# USB HID usage ids AXe sends for `press_key`.
HID_KEYS = {
    "return": 40, "enter": 40, "escape": 41, "delete": 42, "backspace": 42, "tab": 43, "space": 44,
    "right": 79, "left": 80, "down": 81, "up": 82,
}
HID_COMMAND, HID_A = 227, 4

# `axe button` names; a press shorter than ~0.2 s is dropped under Device Hub.
SIMULATOR_BUTTONS = {"home": "home", "lock": "lock", "side": "side-button", "siri": "siri", "apple_pay": "apple-pay"}
BUTTON_PRESS = 0.3

# Start and end of an edge swipe as fractions of the screen. Measured on Xcode 27.0: neither
# `axe swipe` nor `axe gesture swipe-from-left-edge` triggers the back gesture; `axe drag`,
# which sends every touch move, does.
EDGE_FRACTIONS = {
    "left": ((0.005, 0.5), (0.8, 0.5)), "right": ((0.995, 0.5), (0.2, 0.5)),
    "top": ((0.5, 0.003), (0.5, 0.6)), "bottom": ((0.5, 0.997), (0.5, 0.5)),
}

STATUS_BAR_FLAGS = {
    "time": "--time", "data_network": "--dataNetwork", "wifi_mode": "--wifiMode", "wifi_bars": "--wifiBars",
    "cellular_mode": "--cellularMode", "cellular_bars": "--cellularBars", "operator_name": "--operatorName",
    "battery_state": "--batteryState", "battery_level": "--batteryLevel",
}


def ax_text(node: dict[str, Any], key: str = "AXValue") -> str:
    """An AX attribute as text. AXe sends numbers as numbers, and `value or ""` would turn
    a wheel's row 0 or a switch's 0 into an empty string."""
    value = node.get(key)
    return "" if value is None else str(value)


def has_selector(action: dict[str, Any]) -> bool:
    return any(key in action for key in SELECTOR_KEYS)


def screen_frame(nodes: list[dict[str, Any]]) -> dict[str, float]:
    """The application node's frame, which spans the screen. Only without one, the largest
    frame starting on screen: a scroll view's content can be taller than the screen and
    start far above it (y -1580 in a long Settings list)."""
    frames = [node.get("frame") or {} for node in nodes if node_type(node) == "Application"]
    frames = frames or [
        node.get("frame") or {} for node in nodes
        if float((node.get("frame") or {}).get("x", 0)) >= 0 and float((node.get("frame") or {}).get("y", 0)) >= 0
    ]
    frame = max(
        frames,
        key=lambda value: float(value.get("width", 0)) * float(value.get("height", 0)),
        default={},
    )
    return {key: float(frame.get(key, 0)) for key in ("x", "y", "width", "height")}


def resolve_node(
    udid: str, selector: dict[str, Any], timeout: float, optional_prompt_labels: list[str],
    accept: Callable[[dict[str, Any]], bool] = lambda node: True,
) -> dict[str, Any]:
    """The top-left node matching `selector` (and `accept`), waiting for it like `wait` does."""
    def found(node: dict[str, Any]) -> bool:
        return matches(node, selector) and tappable(node) and accept(node)

    nodes = wait_selector(
        udid, selector, timeout, optional_prompt_labels,
        predicate=lambda value: any(found(node) for node in value),
        first_poll_max_age=HIERARCHY_REUSE,
    )
    return sort_nodes([node for node in nodes if found(node)], ["y", "x"])[0]


# AXe reports a UISwitch as CheckBox on iOS 27 and as Switch before it.
SWITCH_TYPES = ("Switch", "CheckBox")


SWITCH_PRESS = 0.3
PICKER_TYPES = ("PickerWheel", "Picker", "Slider")


def is_picker(node: dict[str, Any]) -> bool:
    """A picker wheel; AXe reports a SwiftUI wheel as a Slider whose value is the row index."""
    return node_type(node) in PICKER_TYPES and not SCROLL_BAR_LABEL.match(str(node.get("AXLabel") or ""))
SLIDER_CORRECTIONS = 3
# A UISlider thumb is ~28 pt wide, so its centre travels inside the track by about half that.
SLIDER_THUMB_INSET = 14.0


def slider_percent(node: dict[str, Any]) -> float | None:
    """A slider's AXValue as a percentage: UIKit reports "50%", SwiftUI the fraction "0.5".
    None for anything else, such as the step number of a stepped slider."""
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(%?)\s*", ax_text(node))
    if not match:
        return None
    number = float(match.group(1))
    if match.group(2):
        return number
    return number * 100 if 0 <= number <= 1 else None


def is_switch(node: dict[str, Any]) -> bool:
    return node_type(node) in SWITCH_TYPES and str(node.get("AXValue")) in ("0", "1")


def is_slider(node: dict[str, Any]) -> bool:
    """A Slider that is not a scroll view's scroll bar, which AX also reports as Slider."""
    return node_type(node) == "Slider" and not SCROLL_BAR_LABEL.match(str(node.get("AXLabel") or ""))


def resolve_point(
    udid: str, spec: Any, timeout: float, optional_prompt_labels: list[str], context: str,
) -> tuple[float, float]:
    """A gesture target: `[x, y]`, a mapping with `x`/`y`, or a selector mapping."""
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        return float(spec[0]), float(spec[1])
    if isinstance(spec, dict):
        if "x" in spec and "y" in spec:
            return float(spec["x"]), float(spec["y"])
        if has_selector(spec):
            return frame_center(resolve_node(udid, selector_of(spec), timeout, optional_prompt_labels))
    raise FastUIError(f"{context} needs x/y, an [x, y] point or a selector (label, id, …)")


def deduped(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One node per on-screen box: AX trees nest a text in containers with the same frame."""
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for node in nodes:
        frame = node.get("frame") or {}
        key = tuple(round(float(frame.get(name, 0) or 0)) for name in ("x", "y", "width", "height"))
        if key not in seen:
            seen.add(key)
            unique.append(node)
    return unique


def check_text(actual: str, action: dict[str, Any], context: str) -> str | None:
    """Why `actual` fails the action's `equals`/`not_equals`/`contains`/`matches`; None when it passes."""
    checks = [key for key in ("equals", "not_equals", "contains", "matches") if key in action]
    if not checks:
        raise FastUIError(f"{context} needs equals, not_equals, contains or matches")
    for key in checks:
        expected = str(action[key])
        if key == "equals" and actual != expected:
            return f"is '{actual}', expected '{expected}'"
        if key == "not_equals" and actual == expected:
            return f"is still '{actual}'"
        if key == "contains" and expected not in actual:
            return f"is '{actual}', expected it to contain '{expected}'"
        if key == "matches" and not re.search(expected, actual):
            return f"is '{actual}', expected it to match /{expected}/"
    return None


def count_failure(count: int, action: dict[str, Any]) -> str | None:
    if "count" in action and count != int(action["count"]):
        return f"expected {int(action['count'])}"
    if "min" in action and count < int(action["min"]):
        return f"expected at least {int(action['min'])}"
    if "max" in action and count > int(action["max"]):
        return f"expected at most {int(action['max'])}"
    return None


def poll_until(udid: str, check: Callable[[list[dict[str, Any]]], str | None], timeout: float,
               optional_prompt_labels: list[str]) -> None:
    """Re-read the tree until `check` returns None; its last complaint becomes the failure."""
    complaint: list[str | None] = [None]

    def passes(nodes: list[dict[str, Any]]) -> bool:
        complaint[0] = check(nodes)
        return complaint[0] is None

    try:
        wait_nodes(udid, passes, timeout, optional_prompt_labels)
    except FastUIError as error:
        labels = str(error).split("visible labels:")[-1].strip()
        reason = complaint[0] or "a declared permission prompt kept covering the screen"
        raise FastUIError(f"{reason} after {timeout:g}s; visible labels: {labels}") from None


def settle_tap(post_delay: float) -> None:
    capped, remainder = split_post_delay(max(post_delay, TAP_SETTLE))
    time.sleep(capped + remainder)


def long_press(udid: str, x: float, y: float, duration: float, post_delay: float) -> None:
    invalidate_hierarchy(udid)
    axe(udid, "touch", "-x", f"{x:.2f}", "-y", f"{y:.2f}", "--down", "--up", "--delay", str(duration))
    settle_tap(post_delay)


def double_tap(udid: str, x: float, y: float, post_delay: float) -> None:
    # One AXe session for both taps: two `axe tap` processes land too far apart to be a double tap.
    invalidate_hierarchy(udid)
    point = f"tap -x {x:.2f} -y {y:.2f}"
    axe(udid, "batch", *KEY_LEAD, "--step", point, "--step", "sleep 0.08", "--step", point, "--step", f"sleep {KEY_SETTLE}")
    settle_tap(post_delay)


def hold_and_drag(
    udid: str, start: tuple[float, float], end: tuple[float, float], hold: float, duration: float, post_delay: float,
) -> None:
    """Press, stay still for `hold` (long enough for a reorder or drag-and-drop to pick the item
    up), then move: a touch-down followed by a swipe from the same point in one AXe session
    keeps the finger down throughout."""
    invalidate_hierarchy(udid)
    (x0, y0), (x1, y1) = start, end
    axe(
        udid, "batch",
        "--step", f"touch -x {x0:.2f} -y {y0:.2f} --down",
        "--step", f"sleep {hold}",
        "--step", f"swipe --start-x {x0:.2f} --start-y {y0:.2f} --end-x {x1:.2f} --end-y {y1:.2f} --duration {duration}",
    )
    settle_tap(post_delay)


def drag(udid: str, start: tuple[float, float], end: tuple[float, float], duration: float, post_delay: float) -> None:
    """A finger moved without a fling: no momentum scroll, and edge gestures recognise it."""
    invalidate_hierarchy(udid)
    capped, remainder = split_post_delay(post_delay)
    axe(
        udid, "drag", "--start-x", f"{start[0]:.2f}", "--start-y", f"{start[1]:.2f}",
        "--end-x", f"{end[0]:.2f}", "--end-y", f"{end[1]:.2f}", "--duration", str(duration),
        "--post-delay", str(capped),
    )
    time.sleep(remainder)


# Like a tap, a key is dropped when AXe exits right after sending it, and also when it is the
# first thing a new AXe session sends after another one typed. Measured on Xcode 27.0 (two
# Backspaces after `axe type`): a lone `axe key` or `axe key-sequence` deleted nothing, a batch
# with a trailing sleep worked 2/6 times, with a leading sleep as well 5/5.
KEY_SETTLE = 0.3
KEY_LEAD = ["--step", "sleep 0.3"]


DISMISS_ATTEMPTS = 3
KEYBOARD_KEY_IDS = ("Return", "delete", "shift", "space")


def is_keyboard_key(node: dict[str, Any]) -> bool:
    return node_type(node) == "Button" and node.get("AXUniqueId") in KEYBOARD_KEY_IDS


def press_keys(udid: str, codes: list[int]) -> None:
    invalidate_hierarchy(udid)
    steps: list[str] = []
    for code in codes:
        steps += ["--step", f"key {code}", "--step", "sleep 0.05"]
    axe(udid, "batch", *KEY_LEAD, *steps, "--step", f"sleep {KEY_SETTLE}")


def clear_focused_text(udid: str) -> None:
    """Select all, then delete: works whatever the cursor position and text length."""
    invalidate_hierarchy(udid)
    axe(
        udid, "batch", *KEY_LEAD,
        "--step", f"key-combo --modifiers {HID_COMMAND} --key {HID_A}",
        "--step", "sleep 0.2",
        "--step", f"key {HID_KEYS['delete']}",
        "--step", f"sleep {KEY_SETTLE}",
    )


def verify_cleared(
    udid: str, selector: dict[str, Any], timeout: float, optional_prompt_labels: list[str],
) -> None:
    """Cmd+A is sometimes eaten by the edit menu that a tap on a focused field opens, so
    whatever value the field still reports is deleted from the end: Cmd+Down moves the
    cursor there, then one Backspace per character. AX reports an empty field's placeholder
    as its value, which cannot be told apart from text; Backspace on an empty field does nothing."""
    time.sleep(0.2)
    # A field picked by its old text no longer has it: find it again without `value`.
    selector = {key: value for key, value in selector.items() if key != "value"}
    remaining = ax_text(resolve_node(udid, selector, timeout, optional_prompt_labels))
    if not remaining:
        return
    invalidate_hierarchy(udid)
    steps = ["--step", f"key-combo --modifiers {HID_COMMAND} --key {HID_KEYS['down']}", "--step", "sleep 0.1"]
    for _ in range(len(remaining) + 2):
        steps += ["--step", f"key {HID_KEYS['delete']}", "--step", "sleep 0.03"]
    axe(udid, "batch", *KEY_LEAD, *steps, "--step", f"sleep {KEY_SETTLE}")


def focus_field(
    udid: str, action: dict[str, Any], timeout: float, optional_prompt_labels: list[str], post_delay: float,
) -> None:
    """Tap the field a typing action names, if it names one; keyboards need a moment to come up."""
    if has_selector(action):
        node = resolve_node(udid, selector_of(action), timeout, optional_prompt_labels)
        tap_point(udid, *frame_center(node), max(post_delay, 0.5))


def switch_point(node: dict[str, Any]) -> tuple[float, float]:
    """A SwiftUI Toggle spans its whole row and only flips when the switch itself is hit,
    so a wide switch is tapped near its trailing edge."""
    frame = node.get("frame") or {}
    x, y = frame_center(node)
    width = float(frame.get("width", 0))
    if width > 120:
        x = float(frame["x"]) + width - 36
    return x, y


def simctl_with_input(args: list[str], data: str) -> None:
    result = subprocess.run(args, input=data, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise FastUIError(f"command failed ({result.returncode}) {' '.join(args)}" + (f": {detail[-1]}" if detail else ""))


def execute_action(
    action: dict[str, Any],
    udid: str,
    artifacts: Path,
    index: int,
    optional_prompt_labels: list[str],
    defaults: dict[str, Any] | None = None,
    bundle_id: str = "",
) -> dict[str, Any] | None:
    """Run one action; `defaults` carries flow-wide keys such as `settle` from `setup`,
    `bundle_id` is the app under test for `launch`/`terminate` without an `app` key."""
    kind = required(action, "type", f"action {index}")
    result = run_action(action, udid, artifacts, index, optional_prompt_labels, bundle_id)
    settle = action.get("settle", (defaults or {}).get("settle"))
    if kind in GESTURE_KINDS and settle:
        timeout = SETTLE_TIMEOUT if isinstance(settle, bool) else float(settle)
        result = {**(result or {}), "settled": wait_settled(udid, timeout=timeout)}
    return result


def run_action(
    action: dict[str, Any],
    udid: str,
    artifacts: Path,
    index: int,
    optional_prompt_labels: list[str],
    bundle_id: str = "",
) -> dict[str, Any] | None:
    kind = required(action, "type", f"action {index}")
    timeout = float(action.get("timeout", 6))
    post_delay = float(action.get("post_delay", 0.15))

    if kind == "wait":
        wait_selector(udid, action, timeout, optional_prompt_labels)
    elif kind == "tap_label":
        if not any(key in action for key in ("label", "label_contains", "id")):
            raise FastUIError(f"missing 'label' or 'id' in action {index}")
        selector = selector_of(action, "Button")
        explicit_type = "element_type" in action
        if action.get("optional", False):
            target = resolve_tap_target(hierarchy(udid), selector, explicit_type)
        else:
            # One hierarchy poll resolves the control (and dismisses any
            # permission alert in the way); tapping its frame avoids a second
            # label lookup inside `axe tap --label`.
            nodes = wait_selector(
                udid, selector, timeout, optional_prompt_labels,
                predicate=lambda value: resolve_tap_target(value, selector, explicit_type) is not None,
                first_poll_max_age=HIERARCHY_REUSE,
            )
            target = resolve_tap_target(nodes, selector, explicit_type)
        if target is not None:
            tap_point(udid, *frame_center(target), post_delay)
            if node_type(target) != selector["element_type"]:
                return {"fallback_type": node_type(target)}
    elif kind == "wait_absent":
        wait_absent(udid, action, timeout, optional_prompt_labels)
    elif kind == "assert_absent":
        assert_absent(udid, action, float(action.get("seconds", 3)), optional_prompt_labels)
    elif kind == "dump":
        settle = action.get("settle_seconds")
        settled = None
        if settle:
            # Bounded: two equal trees in a row, or give up after `settle_seconds`.
            settled = wait_settled(udid, timeout=float(settle))
        nodes = wait_nodes(
            udid, lambda _: True, timeout, optional_prompt_labels,
            first_poll_max_age=HIERARCHY_REUSE if settle else 0.0,
        )
        name = action.get("name", f"step-{index:02d}")
        path = artifacts / f"{name}-ui.json"
        write_dump(path, nodes)
        return {
            "path": str(path), "labels": visible_labels(nodes),
            **({"settled": settled} if settled is not None else {}),
        }
    elif kind == "pause":
        time.sleep(float(action.get("seconds", 1)))
    elif kind == "tap_point":
        tap_point(udid, float(action["x"]), float(action["y"]), post_delay)
    elif kind == "swipe":
        area = None
        if has_selector(action):
            area = resolve_node(udid, selector_of(action), timeout, optional_prompt_labels).get("frame")
        nodes = hierarchy(udid) if "direction" in action and area is None else []
        start, end = swipe_points(action, nodes, index, area)
        if area is None:
            swipe(udid, start, end, float(action.get("duration", 0.3)), post_delay)
        else:
            # A row's swipe actions ignore `axe swipe` (measured on Xcode 27.0); a drag reveals them.
            drag(udid, tuple(start), tuple(end), float(action.get("duration", 0.3)), max(post_delay, 0.4))
    elif kind == "scroll_to":
        limit = int(action.get("max_swipes", 6))
        for attempt in range(limit + 1):
            # Through wait_nodes so a permission alert listed in optional_permission_labels
            # is dismissed instead of swallowing the swipe.
            nodes = wait_nodes(udid, lambda _: True, timeout, optional_prompt_labels)
            if any(matches(node, action) for node in nodes):
                break
            covering = system_alert(nodes)
            if covering is not None:
                # Swiping a SpringBoard alert only burns the swipe budget (measured: 22 s).
                raise FastUIError(
                    f"a system alert covers the app: '{covering.get('AXLabel')}'; add its button to "
                    "setup.optional_permission_labels or a tap_alert step before this one"
                )
            if attempt == limit:
                labels = ", ".join(visible_labels(nodes, limit=25))
                target = action.get("label") or action.get("label_contains") or "selector"
                raise FastUIError(f"'{target}' not found after {limit} swipes; visible labels: [{labels}]")
            direction = {"direction": action.get("direction", "up")}
            swipe(udid, *swipe_points(direction, nodes, index), 0.3, 0.4)
    elif kind == "tap_first":
        nodes = wait_selector(udid, action, timeout, optional_prompt_labels, first_poll_max_age=HIERARCHY_REUSE)
        candidates = [node for node in nodes if matches(node, action)]
        candidates = sort_nodes(candidates, action.get("sort", ["y", "x"]))
        if not candidates:
            raise FastUIError(f"no node matched tap_first at action {index}")
        tap_point(udid, *frame_center(candidates[0]), post_delay)
    elif kind == "tap_nearest":
        target_selector = required(action, "target", f"action {index}")
        anchor_selector = required(action, "anchor", f"action {index}")
        axis = action.get("axis", "y")

        def resolve_and_tap() -> None:
            nodes = wait_nodes(
                udid,
                lambda value: any(matches(node, target_selector) for node in value)
                and any(matches(node, anchor_selector) for node in value),
                timeout,
                optional_prompt_labels,
                HIERARCHY_REUSE,
            )
            targets = [node for node in nodes if matches(node, target_selector)]
            anchors = [node for node in nodes if matches(node, anchor_selector)]
            if len(anchors) != 1 or not targets:
                raise FastUIError(
                    "tap_nearest requires one anchor and at least one target; "
                    f"got {len(anchors)}/{len(targets)}"
                )
            anchor_value = float((anchors[0].get("frame") or {})[axis])
            target = min(
                targets,
                key=lambda node: abs(
                    float((node.get("frame") or {})[axis]) - anchor_value
                ),
            )
            tap_point(udid, *frame_center(target), post_delay)

        resolve_and_tap()
        success = action.get("success")
        dismiss_selectors = action.get("dismiss", [])
        if isinstance(dismiss_selectors, dict):
            dismiss_selectors = [dismiss_selectors]
        if success:
            nodes = wait_nodes(
                udid,
                lambda value: any(matches(node, success) for node in value)
                or any(
                    matches(node, selector)
                    for selector in dismiss_selectors
                    for node in value
                ),
                timeout,
                optional_prompt_labels,
            )
            if not any(matches(node, success) for node in nodes):
                dismissals = [
                    node
                    for selector in dismiss_selectors
                    for node in nodes
                    if matches(node, selector)
                ]
                if not dismissals:
                    raise FastUIError("tap_nearest reached neither success nor dismiss selector")
                tap_point(udid, *frame_center(sort_nodes(dismissals, ["y", "x"])[0]), post_delay)
                resolve_and_tap()
                wait_selector(udid, success, timeout, optional_prompt_labels)
    elif kind == "screenshot":
        path = artifacts / action.get("name", f"step-{index:02d}.png")
        screenshot(udid, path)
        return {"path": str(path)}
    elif kind == "motion":
        frame_count = int(action.get("frames", 3))
        if frame_count < 2:
            raise FastUIError("motion action needs at least two frames")
        region = action.get("region", [0, 0, 1, 1])
        if len(region) != 4:
            raise FastUIError("motion region must be [left, top, right, bottom]")
        paths: list[Path] = []
        for frame_index in range(frame_count):
            path = artifacts / f"{action.get('name', 'motion')}-{frame_index + 1}.png"
            screenshot(udid, path)
            paths.append(path)
            if frame_index + 1 < frame_count:
                time.sleep(float(action.get("interval", 0.35)))
        ratios = [
            motion_ratio(paths[position], paths[position + 1], region, int(action.get("pixel_threshold", 12)))
            for position in range(len(paths) - 1)
        ]
        measured = min(ratios)
        threshold = float(action.get("ratio_threshold", 0.001))
        if measured < threshold:
            raise FastUIError(f"motion {measured:.6f} is below {threshold:.6f}")
        keep_frames = bool(action.get("keep_frames", False))
        if not keep_frames:
            for path in paths:
                path.unlink(missing_ok=True)
        return {
            "motion_ratio": round(measured, 6),
            "motion_ratios": [round(value, 6) for value in ratios],
            **({"frames": [str(path) for path in paths]} if keep_frames else {}),
        }
    elif kind == "type_text":
        text = required(action, "text", f"action {index}")
        focus_field(udid, action, timeout, optional_prompt_labels, post_delay)
        if action.get("clear", False):
            clear_focused_text(udid)
            if has_selector(action):
                verify_cleared(udid, selector_of(action), timeout, optional_prompt_labels)
        # A lone `axe type` lost its last character ("hello worl") when AXe exited right after
        # it, like a key; inside a batch with sleeps around it nothing was lost.
        invalidate_hierarchy(udid)
        axe(udid, "batch", *KEY_LEAD, "--step", f"type {shlex.quote(text)}", "--step", f"sleep {KEY_SETTLE}")
    elif kind == "clear_text":
        focus_field(udid, action, timeout, optional_prompt_labels, post_delay)
        clear_focused_text(udid)
        if has_selector(action):
            verify_cleared(udid, selector_of(action), timeout, optional_prompt_labels)
    elif kind == "press_key":
        key = str(required(action, "key", f"action {index}")).lower()
        if key not in HID_KEYS:
            raise FastUIError(f"action {index}: press_key key must be one of {', '.join(HID_KEYS)}")
        press_keys(udid, [HID_KEYS[key]] * int(action.get("count", 1)))
        time.sleep(post_delay)
    elif kind == "dismiss_keyboard":
        # Simulator keeps the software keyboard out of the app's AX tree, so there is no key to
        # find: tap the app's own button (a toolbar "Done") when named, else send Return.
        if "button" in action:
            node = resolve_node(udid, {"label": action["button"]}, timeout, optional_prompt_labels)
            tap_point(udid, *frame_center(node), post_delay)
        else:
            # The first Return can be spent accepting a QuickType/autocorrect suggestion; when the
            # keyboard's keys are still in the tree (an app's keyboard often is), send another.
            for _ in range(DISMISS_ATTEMPTS):
                press_keys(udid, [HID_KEYS["return"]])
                time.sleep(max(post_delay, 0.4))
                if not any(is_keyboard_key(node) for node in hierarchy(udid)):
                    break
    elif kind == "long_press":
        point = resolve_point(udid, action, timeout, optional_prompt_labels, f"action {index}: long_press")
        long_press(udid, *point, float(action.get("duration", 1.0)), post_delay)
    elif kind == "double_tap":
        point = resolve_point(udid, action, timeout, optional_prompt_labels, f"action {index}: double_tap")
        double_tap(udid, *point, post_delay)
    elif kind == "drag":
        start = resolve_point(udid, required(action, "from", f"action {index}"), timeout, optional_prompt_labels,
                              f"action {index}: drag 'from'")
        end = resolve_point(udid, required(action, "to", f"action {index}"), timeout, optional_prompt_labels,
                            f"action {index}: drag 'to'")
        hold_and_drag(udid, start, end, float(action.get("hold", 0.6)), float(action.get("duration", 0.8)), post_delay)
    elif kind in ("edge_swipe", "back"):
        edge = "left" if kind == "back" else action.get("edge", "left")
        if edge not in EDGE_FRACTIONS:
            raise FastUIError(f"action {index}: edge must be left, right, top or bottom")
        screen = screen_frame(hierarchy(udid))
        (fx0, fy0), (fx1, fy1) = EDGE_FRACTIONS[edge]
        drag(
            udid,
            (screen["x"] + screen["width"] * fx0, screen["y"] + screen["height"] * fy0),
            (screen["x"] + screen["width"] * fx1, screen["y"] + screen["height"] * fy1),
            float(action.get("duration", 0.5)), max(post_delay, 0.6),
        )
    elif kind == "pull_to_refresh":
        if has_selector(action):
            area = resolve_node(udid, selector_of(action), timeout, optional_prompt_labels).get("frame") or {}
            frame = {key: float(area.get(key, 0)) for key in ("x", "y", "width", "height")}
        else:
            frame = screen_frame(hierarchy(udid))
        x = frame["x"] + frame["width"] / 2
        drag(
            udid, (x, frame["y"] + frame["height"] * 0.25), (x, frame["y"] + frame["height"] * 0.75),
            float(action.get("duration", 0.8)), max(post_delay, 1.0),
        )
    elif kind == "set_switch":
        selector = selector_of(action)
        wanted = "1" if bool(required(action, "on", f"action {index}")) else "0"
        node = resolve_node(udid, selector, timeout, optional_prompt_labels, accept=is_switch)
        if str(node.get("AXValue")) != wanted:
            invalidate_hierarchy(udid)
            frame = node.get("frame") or {}
            x, y = switch_point(node)
            # The iOS 27 switch ignores AXe's taps, even `--tap-style physical`; measured on Xcode 27.0,
            # it flips only for a touch held about SWITCH_PRESS seconds (0.1 s did nothing).
            axe(udid, "touch", "-x", f"{x:.2f}", "-y", f"{y:.2f}", "--down", "--up", "--delay", str(SWITCH_PRESS))
            settle_tap(post_delay)
            # The same switch, found by where it was: its value is what changes.
            moved = {**{k: v for k, v in selector.items() if k != "value"}, "frame": {
                "min_x": float(frame.get("x", 0)) - 4, "max_x": float(frame.get("x", 0)) + 4,
                "min_y": float(frame.get("y", 0)) - 4, "max_y": float(frame.get("y", 0)) + 4,
            }}
            poll_until(
                udid,
                lambda nodes: None if any(matches(n, moved) and is_switch(n) and str(n.get("AXValue")) == wanted
                                          for n in nodes)
                else f"switch did not turn {'on' if wanted == '1' else 'off'}",
                timeout, optional_prompt_labels,
            )
            return {"toggled": True}
        return {"toggled": False}
    elif kind == "set_slider":
        percent = float(required(action, "percent", f"action {index}"))
        if not 0 <= percent <= 100:
            raise FastUIError(f"action {index}: percent must be 0-100")
        selector = selector_of(action)
        node = resolve_node(udid, selector, timeout, optional_prompt_labels, accept=is_slider)
        target = ["--id", node["AXUniqueId"]] if node.get("AXUniqueId") else ["--label", str(node.get("AXLabel") or "")]
        invalidate_hierarchy(udid)
        # AXe fails when a stepped slider snaps to a neighbouring step; correct by dragging the thumb.
        axe(udid, "slider", *target, "--element-type", "Slider", "--value", f"{percent:g}", check=False)
        tolerance = float(action.get("tolerance", 2))
        # Up to SLIDER_CORRECTIONS drags, each followed by a fresh read.
        for attempt in range(SLIDER_CORRECTIONS + 1):
            node = resolve_node(udid, selector, timeout, optional_prompt_labels, accept=is_slider)
            current = slider_percent(node)
            if current is None or abs(current - percent) <= tolerance or attempt == SLIDER_CORRECTIONS:
                break
            frame = node.get("frame") or {}
            left = float(frame.get("x", 0)) + SLIDER_THUMB_INSET
            travel = float(frame.get("width", 0)) - 2 * SLIDER_THUMB_INSET
            y = frame_center(node)[1]
            drag(udid, (left + travel * current / 100, y), (left + travel * percent / 100, y), 0.5, 0.4)
        if current is not None and abs(current - percent) > tolerance:
            raise FastUIError(f"slider is at {current:g}%, expected {percent:g}% (±{tolerance:g})")
        time.sleep(post_delay)
        return {"percent": current}
    elif kind == "set_picker":
        if "option" not in action and "index" not in action:
            raise FastUIError(f"action {index}: set_picker needs option (the row text) or index")
        selector = selector_of(action)
        limit = int(action.get("max_steps", 15))
        row = float(action.get("row_height", 34))
        # The wheel exposes only its current value: the row text for a UIPickerView, the row
        # index for a SwiftUI wheel (reported as a Slider). Step it one row at a time; for an
        # index the direction is known, for text search forwards, then back.
        wanted_index = int(action["index"]) if "index" in action else None
        directions = [None] * (2 * limit) if wanted_index is not None else ["up"] * limit + ["down"] * (2 * limit)
        for step, direction in enumerate([*directions, "stop"]):
            node = resolve_node(
                udid, selector, timeout, optional_prompt_labels,
                # An index wheel shows a whole number; that keeps a "0.5" slider out of the match.
                accept=lambda value: is_picker(value)
                and (wanted_index is None or ax_text(value).isdigit()),
            )
            current = ax_text(node)
            if wanted_index is not None:
                if current == str(wanted_index):
                    return {"steps": step}
                if current.isdigit() and direction != "stop":
                    direction = "up" if int(current) < wanted_index else "down"
            elif str(action["option"]) in current:
                return {"steps": step}
            if direction in (None, "stop"):
                wanted = action.get("option", action.get("index"))
                raise FastUIError(f"picker never showed '{wanted}'; it shows '{current}'")
            x, y = frame_center(node)
            drag(udid, (x, y), (x, y - row if direction == "up" else y + row), 0.4, 0.6)
    elif kind == "tap_alert":
        label = str(required(action, "button", f"action {index}"))
        system_only = bool(action.get("system", False))
        found: list[Any] = [None]
        auto_before = len(AUTO_TAPPED)

        def alert_ready(nodes: list[dict[str, Any]]) -> bool:
            if any(item["button"] == label for item in AUTO_TAPPED[auto_before:]):
                return True  # the optional_permission_labels auto-tap already pressed it
            hit = alert_button(nodes, label, system_only)
            found[0] = None if hit is None else (*hit, system_alert(nodes) is hit[1])
            return hit is not None

        try:
            wait_nodes(udid, alert_ready, timeout, optional_prompt_labels, first_poll_max_age=HIERARCHY_REUSE)
        except FastUIError as error:
            if any(item["button"] == label for item in AUTO_TAPPED[auto_before:]):
                # Pressed by the auto-tap on the last poll before the deadline.
                return {"alert": AUTO_TAPPED[-1]["alert"], "system": True, "auto_tapped": True}
            if action.get("optional", False):
                # A permission prompt that iOS chose not to show (already answered, or
                # dropped right after a boot) is not a failure of the flow.
                return {"skipped": True}
            raise FastUIError(f"no alert with a '{label}' button: {error}") from error
        if found[0] is None:
            return {"alert": AUTO_TAPPED[-1]["alert"], "system": True, "auto_tapped": True}
        button, sheet, is_system = found[0]
        tap_point(udid, *frame_center(button), post_delay)
        invalidate_hierarchy(udid)
        if is_system:
            forget_system_alert(sheet)
        return {"alert": str(sheet.get("AXLabel") or ""), "system": is_system}
    elif kind == "assert_value":
        selector = selector_of(action)
        attribute = {"value": "AXValue", "label": "AXLabel"}.get(action.get("attribute", "value"))
        if attribute is None:
            raise FastUIError(f"action {index}: attribute must be value or label")
        check_text("", action, f"action {index}: assert_value")  # validates the keys up front

        def value_check(nodes: list[dict[str, Any]]) -> str | None:
            found = sort_nodes([node for node in nodes if matches(node, selector) and tappable(node)], ["y", "x"])
            if not found:
                return "no node matched the selector"
            failure = check_text(ax_text(found[0], attribute), action, "assert_value")
            return None if failure is None else f"{action.get('attribute', 'value')} {failure}"

        poll_until(udid, value_check, timeout, optional_prompt_labels)
    elif kind == "assert_count":
        selector = selector_of(action)
        if not any(key in action for key in ("count", "min", "max")):
            raise FastUIError(f"action {index}: assert_count needs count, min or max")
        seen: list[int] = [0]

        def count_check(nodes: list[dict[str, Any]]) -> str | None:
            seen[0] = len(deduped([node for node in nodes if matches(node, selector) and tappable(node)]))
            failure = count_failure(seen[0], action)
            return None if failure is None else f"{seen[0]} nodes matched, {failure}"

        poll_until(udid, count_check, timeout, optional_prompt_labels)
        return {"count": seen[0]}
    elif kind == "press_button":
        button = str(required(action, "button", f"action {index}"))
        if button not in SIMULATOR_BUTTONS:
            raise FastUIError(
                f"action {index}: Simulator buttons are {', '.join(SIMULATOR_BUTTONS)}"
                + ("; volume buttons need a physical iPhone" if button.startswith("volume") else "")
            )
        invalidate_hierarchy(udid)
        axe(udid, "button", SIMULATOR_BUTTONS[button], "--duration", str(action.get("duration", BUTTON_PRESS)))
        time.sleep(max(post_delay, 1.0))
    elif kind == "activate":
        app = action.get("app") or bundle_id
        if not app:
            raise FastUIError(f"action {index}: activate needs 'app'")
        # `simctl launch` brings a running app to the front without restarting it.
        command(["xcrun", "simctl", "launch", udid, app])
        invalidate_hierarchy(udid)
    elif kind == "set_appearance":
        mode = required(action, "appearance", f"action {index}")
        if "appearance" not in PREVIOUS_UI:
            current = command(["xcrun", "simctl", "ui", udid, "appearance"], check=False).stdout.strip()
            if current in UI_SETTINGS["appearance"] and current != mode:
                PREVIOUS_UI["appearance"] = current
                journal("ui", dict(PREVIOUS_UI))
        command(["xcrun", "simctl", "ui", udid, "appearance", mode])
        invalidate_hierarchy(udid)
    elif kind == "set_permission":
        verb = action.get("action")
        if verb not in ("grant", "revoke", "reset"):
            raise FastUIError(f"action {index}: set_permission needs action: grant, revoke or reset")
        service = required(action, "service", f"action {index}")
        app = action.get("app") or bundle_id
        if app and verb != "reset" and LEASE is not None:
            # TCC grants can outlive an uninstall (seen 1 of 3 dirty->clean runs), so undo them.
            granted = [entry for entry in LEASE.journal.get("privacy", []) if entry != [service, app]]
            journal("privacy", granted + [[service, app]])
        command(["xcrun", "simctl", "privacy", udid, verb, service, *([app] if app else [])])
        invalidate_hierarchy(udid)
    elif kind == "set_location":
        if action.get("clear"):
            command(["xcrun", "simctl", "location", udid, "clear"])
        else:
            journal("location")
            latitude = float(required(action, "latitude", f"action {index}"))
            longitude = float(required(action, "longitude", f"action {index}"))
            command(["xcrun", "simctl", "location", udid, "set", f"{latitude},{longitude}"])
    elif kind == "push_notification":
        app = action.get("app") or bundle_id
        if not app:
            raise FastUIError(f"action {index}: push_notification needs 'app'")
        payload = action.get("payload") or {"aps": {"alert": {
            "title": action.get("title", "Test"), "body": action.get("body", "")}}}
        try:
            simctl_with_input(["xcrun", "simctl", "push", udid, app, "-"], json.dumps(payload))
        except FastUIError as error:
            if "not authorized" not in str(error):
                raise
            raise FastUIError(
                f"{error} — the app has not been granted notification permission yet: let it ask "
                "(and add \"Allow\" to setup.optional_permission_labels) before pushing"
            ) from None
        invalidate_hierarchy(udid)
    elif kind == "status_bar":
        if action.get("clear"):
            command(["xcrun", "simctl", "status_bar", udid, "clear"])
        else:
            flags = [part for key, flag in STATUS_BAR_FLAGS.items() if key in action for part in (flag, str(action[key]))]
            if not flags:
                raise FastUIError(f"action {index}: status_bar needs clear or one of {', '.join(STATUS_BAR_FLAGS)}")
            journal("status_bar")
            command(["xcrun", "simctl", "status_bar", udid, "override", *flags])
    elif kind == "set_clipboard":
        text = str(required(action, "text", f"action {index}"))
        simctl_with_input(["xcrun", "simctl", "pbcopy", udid], text)
        # On Xcode 27.0 / iOS 27 `simctl pbcopy` exits 0 and the device pasteboard stays empty
        # (`pbsync host` too); say so instead of letting a later paste fail obscurely.
        deadline = time.perf_counter() + min(timeout, 3.0)
        while command(["xcrun", "simctl", "pbpaste", udid]).stdout != text:
            if time.perf_counter() >= deadline:
                raise FastUIError(
                    "simctl pbcopy did not reach the simulator's pasteboard (seen on Xcode 27.0 / iOS 27); "
                    "set_clipboard works on Xcode 26 simulators and on a physical iPhone"
                )
            time.sleep(0.3)
    elif kind == "assert_clipboard":
        # The device pasteboard syncs a moment after `pbcopy` or an in-app copy; poll it.
        deadline = time.perf_counter() + timeout
        while True:
            actual = command(["xcrun", "simctl", "pbpaste", udid]).stdout
            failure = check_text(actual, action, f"action {index}: assert_clipboard")
            if not failure:
                break
            if time.perf_counter() >= deadline:
                raise FastUIError(f"clipboard {failure} after {timeout:g}s")
            time.sleep(0.3)
    elif kind in DEVICE_ONLY:
        raise FastUIError(
            f"action {index}: {kind} needs {DEVICE_ONLY[kind]}, which Simulator cannot do through AXe; "
            "run it with fastDeviceTest on a physical iPhone"
        )
    elif kind in ("launch", "terminate"):
        app = action.get("app") or bundle_id
        if not app:
            raise FastUIError(f"action {index}: {kind} needs 'app'")
        if kind == "launch":
            arguments = [str(value) for value in action.get("launch_arguments", [])]
            command(["xcrun", "simctl", "launch", udid, app, *arguments])
        else:
            command(["xcrun", "simctl", "terminate", udid, app], check=False)
        invalidate_hierarchy(udid)
    elif kind == "open_url":
        command(["xcrun", "simctl", "openurl", udid, required(action, "url", f"action {index}")])
        invalidate_hierarchy(udid)
    else:
        raise FastUIError(f"unsupported action type '{kind}'")
    return None


# ---------------------------------------------------------------------------
# Interactive session: `start <flow>` / `do '<json>'` / `save <flow>`.
# The app is built, fresh-installed and launched once; each `do` runs one action
# through execute_action (the same code a full run uses) against the live app and
# prints the labels on screen afterwards; `save` writes the recorded actions into
# a flow and verifies it with one ordinary fresh-install run.
# ---------------------------------------------------------------------------

SESSION_COMMANDS = ("start", "do", "save", "labels")
NOT_RECORDED = {"dump", "screenshot", "labels"}


def session_pointer(udid: str | None) -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / f"fast-ui-session-{udid or 'current'}"


def session_root(spec: dict[str, Any], base: Path) -> Path:
    app = spec.get("app", {})
    if os.environ.get("FAST_UI_ROOT"):
        return Path(os.environ["FAST_UI_ROOT"]).expanduser().resolve()
    return resolved_path(app.get("project_root", "."), base) / "build/fast-ui"


def load_session(udid: str | None) -> tuple[Path, dict[str, Any]]:
    pointer = session_pointer(udid)
    if not pointer.exists():
        raise FastUIError("no session: run `fast_ui.py start <flow.json>` first")
    path = Path(pointer.read_text(encoding="utf-8").strip())
    return path, json.loads(path.read_text(encoding="utf-8"))


def ensure_not_busy(udid: str) -> None:
    """A session step must not drive a simulator that a full run currently holds."""
    lease = SimulatorLease(udid, Path("session"))
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    with lease.path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FastUIError(f"simulator busy: {udid} is running a fastSimulatorTest flow; wait for it") from None
        fcntl.flock(handle, fcntl.LOCK_UN)


def print_screen(nodes: list[dict[str, Any]], prefix: str) -> None:
    print(f"{prefix} labels={json.dumps(typed_labels(nodes, limit=40), ensure_ascii=False)}")


def session_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fast_ui.py " + argv[0])
    parser.add_argument("target", nargs="?", help="start/save: flow path; do: action JSON")
    parser.add_argument("--udid", help="Pick the session of this simulator (default: the last one started)")
    parser.add_argument("--reuse-app", action="store_true", help="start: skip the freshness check")
    parser.add_argument("--settle", type=float, default=0.0, help="do/labels: settle up to N s before listing")
    parser.add_argument("--no-verify", action="store_true", help="save: write the flow without running it")
    args = parser.parse_args(argv[1:])
    verb = argv[0]
    started = time.perf_counter()

    if verb == "start":
        if not args.target:
            raise FastUIError("start needs a flow path")
        flow_path = Path(args.target).expanduser().resolve()
        spec = json.loads(flow_path.read_text(encoding="utf-8"))
        base = flow_path.parent
        app = required(spec, "app", "flow")
        setup = spec.get("setup", {})
        udid = required(app, "simulator", "app")
        bundle_id = required(app, "bundle_id", "app")
        ensure_not_busy(udid)
        phases: dict[str, float] = {}
        app_path, built = prepare_app(app, base, udid, args.reuse_app)
        phases["prepare"] = round(time.perf_counter() - started, 2)
        if bool(setup.get("fresh_install", not app.get("prebuilt"))) and app_path is not None:
            command(["xcrun", "simctl", "uninstall", udid, bundle_id], check=False)
            command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id], check=False)
            command(["xcrun", "simctl", "privacy", udid, "reset", "all", bundle_id], check=False)
            command(["xcrun", "simctl", "install", udid, str(app_path)])
        else:
            command(["xcrun", "simctl", "terminate", udid, bundle_id], check=False)
        set_preferences(udid, bundle_id, setup.get("preferences", {}))
        command(["xcrun", "simctl", "launch", udid, bundle_id, *map(str, setup.get("launch_arguments", []))])
        phases["install_launch"] = round(time.perf_counter() - started - phases["prepare"], 2)
        root = session_root(spec, base)
        artifacts = root / f"session-{udid}"
        artifacts.mkdir(parents=True, exist_ok=True)
        path = root / f"session-{udid}.json"
        session = {
            "flow": str(flow_path), "udid": udid, "bundle_id": bundle_id,
            "setup": setup, "artifacts": str(artifacts), "actions": [],
        }
        path.write_text(json.dumps(session, indent=1) + "\n", encoding="utf-8")
        for pointer in (session_pointer(udid), session_pointer(None)):
            pointer.write_text(str(path), encoding="utf-8")
        nodes = wait_nodes(udid, lambda _: True, 6, setup.get("optional_permission_labels", []))
        print_screen(nodes, f"STARTED udid={udid} build={'yes' if built else 'skip'} engine={time.perf_counter() - started:.2f}s phases={json.dumps(phases)}")
        return 0

    path, session = load_session(args.udid)
    udid, setup = session["udid"], session.get("setup", {})
    prompts = setup.get("optional_permission_labels", [])

    if verb in ("do", "labels"):
        ensure_not_busy(udid)
        ensure_booted(udid)
        actions = [] if verb == "labels" else json.loads(args.target or "{}")
        actions = actions if isinstance(actions, list) else [actions]
        result: dict[str, Any] | None = None
        for action in actions:
            index = len(session["actions"]) + 1
            try:
                result = execute_action(
                    action, udid, Path(session["artifacts"]), index, prompts,
                    {"settle": True} if setup.get("settle_after_actions") else {}, session["bundle_id"],
                )
            except FastUIError as error:
                print(f"FAIL step={index} engine={time.perf_counter() - started:.2f}s error={error}")
                return 1
            if action.get("type") not in NOT_RECORDED:
                session["actions"].append(action)
                path.write_text(json.dumps(session, indent=1) + "\n", encoding="utf-8")
        if args.settle:
            wait_settled(udid, timeout=args.settle)
        nodes = wait_nodes(udid, lambda _: True, 6, prompts, first_poll_max_age=HIERARCHY_REUSE if args.settle else 0.0)
        extra = f" result={json.dumps({k: v for k, v in result.items() if k != 'labels'})}" if result else ""
        print_screen(nodes, f"OK steps={len(session['actions'])} engine={time.perf_counter() - started:.2f}s{extra}")
        return 0

    if verb == "save":
        out = Path(args.target or session["flow"]).expanduser().resolve()
        spec = json.loads(Path(session["flow"]).read_text(encoding="utf-8"))
        # In a session the agent's think time lets a scroll come to rest before the next
        # tap; a replay runs back to back, so a scroll_to/swipe gets settle unless it set one.
        spec["actions"] = [
            {**action, "settle": True}
            if action.get("type") in ("scroll_to", "swipe") and "settle" not in action else action
            for action in session["actions"]
        ]
        out.write_text(json.dumps(spec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"SAVED {len(session['actions'])} actions to {out}")
        if args.no_verify:
            return 0
        verify = command([sys.executable, str(Path(__file__).resolve()), str(out)], check=False)
        sys.stdout.write(verify.stdout)
        sys.stderr.write(verify.stderr)
        print(f"VERIFY rc={verify.returncode} engine={time.perf_counter() - started:.2f}s")
        return verify.returncode
    raise FastUIError(f"unknown session command {verb}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a compact JSON-driven iOS Simulator UI flow.")
    parser.add_argument("flow", type=Path, help="Path to a fastUITest JSON file")
    parser.add_argument("--reuse-app", action="store_true", help="Skip source freshness checks")
    parser.add_argument(
        "--build-only", action="store_true",
        help="Build/refresh the cached app and exit without installing or running actions",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="Record the simulator screen; a PASS keeps <artifacts-root>/<task>/final[-<session>].mp4",
    )
    parser.add_argument(
        "--artifacts-root", type=Path, default=os.environ.get("FAST_UI_ROOT") or None,
        help="Where runs, screenshots and videos go (default <project_root>/build/fast-ui; env FAST_UI_ROOT)",
    )
    parser.add_argument(
        "--session", default=os.environ.get("FAST_UI_SESSION", ""),
        help="Group this run under a fan-out session; fast_ui_report.py collates by it",
    )
    parser.add_argument(
        "--keep-state", action="store_true",
        help="Leave the seeded setup.preferences in place after the run (default: remove them)",
    )
    parser.add_argument(
        "--appearance", choices=sorted(UI_SETTINGS["appearance"]),
        help="Run as a matrix variant in this appearance (overrides setup.appearance; task name gets @<value>)",
    )
    parser.add_argument(
        "--content-size", choices=list(UI_SETTINGS["content_size"]),
        help="Run as a Dynamic Type variant (overrides setup.content_size; task name gets @<short size>)",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete result JSON")
    parser.add_argument("--artifacts", type=Path, help="Override the artifact directory")
    return parser.parse_args()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in SESSION_COMMANDS:
        try:
            return session_main(sys.argv[1:])
        except (FastUIError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
            print(f"FAIL error={error}", file=sys.stderr)
            return 1
    args = parse_arguments()
    flow_path = args.flow.expanduser().resolve()
    try:
        spec = json.loads(flow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FastUIError(f"cannot read flow {flow_path}: {error}") from error
    if spec.get("version") != 1:
        raise FastUIError("flow version must be 1")

    base = flow_path.parent
    app = required(spec, "app", "flow")
    setup = spec.get("setup", {})
    variant = ui_variant(setup, args.appearance, args.content_size)
    name = variant_name(required(spec, "name", "flow"), variant)
    udid = required(app, "simulator", "app")
    bundle_id = required(app, "bundle_id", "app")
    # A prebuilt app is driven as installed; a fresh install needs an .app to put back.
    fresh_install = bool(setup.get("fresh_install", not app.get("prebuilt")))
    if fresh_install and app.get("prebuilt") and not app.get("app_path"):
        raise FastUIError("prebuilt app without app_path cannot be fresh-installed")
    project_root = resolved_path(app.get("project_root", "."), base)
    artifacts_root = (
        Path(args.artifacts_root).expanduser().resolve()
        if args.artifacts_root
        else project_root / "build/fast-ui"
    )
    artifacts = (
        args.artifacts.expanduser().resolve()
        if args.artifacts
        else artifacts_root / name
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{udid[:8]}-{os.getpid()}"
    )
    prune_artifacts(artifacts_root, float(os.environ.get("FAST_UI_KEEP_DAYS", "3")))
    artifacts.mkdir(parents=True, exist_ok=True)
    metrics = Metrics()
    results: list[dict[str, Any]] = []
    built = False
    current_step = "prepare"
    recorder = Recorder(udid, artifacts / "run.mp4") if args.record else None
    video: Path | None = None
    labels_per_step: list[dict[str, Any]] = []
    previous_ui = PREVIOUS_UI
    previous_ui.clear()
    _SYSTEM_ALERTS.clear()
    AUTO_TAPPED.clear()
    warnings: list[dict[str, str]] = []
    global LEASE
    lease: SimulatorLease | None = None
    if not args.build_only:
        # One run per simulator: a second run on the same UDID would interleave taps.
        lease = SimulatorLease(udid, flow_path)
        try:
            with metrics.measure("lease"):
                lease.acquire(float(os.environ.get("FAST_UI_LOCK_WAIT", "0")))
        except FastUIError as error:
            print(f"BUSY task={name} error={error}", file=sys.stderr)
            return 3
        LEASE = lease
        lease.touch("bundle_id", bundle_id)
        if setup.get("preferences"):
            lease.touch("preferences", sorted(setup["preferences"]))
    previous_handlers = {
        sig: signal.signal(sig, raise_interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    torn_down = False

    try:
        with metrics.measure("prepare"):
            app_path, built = prepare_app(app, base, udid, args.reuse_app)
        if args.build_only:
            location = file_link(app_path) if app_path else "installed"
            print(f"READY task={name} build={'yes' if built else 'skip'} app={location} total={metrics.total:.1f}s")
            return 0
        if not recorder:
            # A recorder orphaned by a killed run keeps the device busy; Recorder.start
            # does the same when this run records.
            stop_stale_recorder(udid)
        if fresh_install:
            current_step = "fresh_install"
            with metrics.measure(current_step):
                command(["xcrun", "simctl", "uninstall", udid, bundle_id], check=False)
                # The simulator keychain survives an uninstall, so a "fresh" install still
                # sees the last run's tokens. A reset wipes every app's items, so only do it
                # on throwaway simulators (fastwf-/fastui-) unless setup.reset_keychain says so.
                reset_keychain = setup.get("reset_keychain")
                if reset_keychain is None:
                    reset_keychain = simulator_name(udid).startswith(DISPOSABLE_PREFIXES)
                keychain = (
                    subprocess.Popen(["xcrun", "simctl", "keychain", udid, "reset"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if reset_keychain else None
                )
                # `defaults write` lands in the simulator's user domain, which an
                # uninstall leaves behind; without this wipe one flow's state leaks
                # into the next fresh install.
                command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id], check=False)
                # Permission grants (notifications included) outlive an uninstall; without
                # this the second run on the same simulator never sees the permission alert.
                command(["xcrun", "simctl", "privacy", udid, "reset", "all", bundle_id], check=False)
                command(["xcrun", "simctl", "install", udid, str(app_path)])
                if keychain:
                    keychain.wait(timeout=60)
        current_step = "state_and_launch"
        with metrics.measure(current_step):
            apply_ui_settings(udid, variant, previous_ui)
            if previous_ui:
                journal("ui", dict(previous_ui))
            set_preferences(udid, bundle_id, setup.get("preferences", {}))
            if recorder:
                recorder.start()
            launch = ["xcrun", "simctl", "launch", udid, bundle_id]
            if not fresh_install and setup.get("relaunch", True):
                # A kept install may still be running from the last run, on whatever
                # screen it was left; `simctl launch` alone would just foreground it.
                launch.insert(3, "--terminate-running-process")
            launch += [str(value) for value in setup.get("launch_arguments", [])]
            command(launch)

        optional_prompt_labels = setup.get("optional_permission_labels", [])
        defaults = {"settle": True} if setup.get("settle_after_actions") else {}
        for index, action in enumerate(required(spec, "actions", "flow"), start=1):
            current_step = f"{index:02d}_{action.get('type', 'unknown')}"
            _CURRENT_STEP[0] = current_step
            with metrics.measure(current_step):
                result = execute_action(
                    action, udid, artifacts, index, optional_prompt_labels, defaults, bundle_id
                )
            if recorder:
                labels_per_step.append(step_labels(udid, current_step, recorder.elapsed(), result))
            if result:
                results.append({"step": index, **result})

        # A system alert that covered the app while the flow ran (or still does) means
        # the taps and asserts may have hit SpringBoard instead of the app: flag it.
        current_step = "system_alert_check"
        _CURRENT_STEP[0] = "end"
        with metrics.measure(current_step):
            hierarchy(udid)
        warnings = [
            {"system_alert_seen": title, "step": step} for title, step in _SYSTEM_ALERTS.items()
        ]
        if warnings and setup.get("system_alert") == "fail":
            raise FastUIError(f"system alert covered the app: {warnings}")

        if recorder:
            recorder.stop()
            video = publish_video(artifacts.parent, shrink_video(recorder.path), passed=True, session=args.session)
        payload = {
            "status": "pass", "task": name, "simulator": udid, "session": args.session,
            **({"warnings": warnings} if warnings else {}),
            **({"auto_tapped": AUTO_TAPPED} if AUTO_TAPPED else {}),
            **({"expect": spec["expect"]} if spec.get("expect") else {}),
            **({"variant": variant} if variant else {}),
            "built": built, "total_seconds": metrics.total,
            "steps_seconds": metrics.steps, "results": results, "artifacts": str(artifacts),
            **({"step_labels": labels_per_step} if recorder else {}),
            **({"video": str(video)} if video else {}),
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            motion = next((result.get("motion_ratio") for result in reversed(results) if "motion_ratio" in result), None)
            suffix = f" motion={motion:.6f}" if motion is not None else ""
            dumps = [result for result in results if "labels" in result]
            if dumps:
                suffix += f" labels={json.dumps(dumps[-1]['labels'], ensure_ascii=False)}"
            if video:
                suffix += f" video={file_link(video)}"
            if warnings:
                suffix += " WARN system_alert_seen=" + json.dumps(
                    [f"{item['system_alert_seen']} @{item['step']}" for item in warnings], ensure_ascii=False
                )
            print(f"PASS task={name} total={metrics.total:.1f}s build={'yes' if built else 'skip'}{suffix} artifacts={file_link(artifacts)}")
        return 0
    except (Interrupted, KeyboardInterrupt) as error:
        # Stop fast and leave evidence: no AXe dump, no video encode, just the raw file.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        reason = str(error) or "interrupted by SIGINT"
        if recorder:
            try:
                recorder.stop()
            except Exception:
                pass
        payload = {
            "status": "interrupted", "task": name, "simulator": udid, "session": args.session,
            "step": current_step, "error": reason,
            "built": built, "total_seconds": metrics.total, "steps_seconds": metrics.steps,
            "results": results, "artifacts": str(artifacts),
            **({"raw_video": str(recorder.path)} if recorder and recorder.path.exists() else {}),
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"INTERRUPTED task={name} step={current_step} total={metrics.total:.1f}s error={reason} artifacts={file_link(artifacts)}", file=sys.stderr)
        return 130
    except (FastUIError, OSError, subprocess.TimeoutExpired) as error:
        failed_at = recorder.elapsed() if recorder else 0.0
        if recorder:
            try:
                recorder.stop()
                video = publish_video(artifacts.parent, shrink_video(recorder.path), passed=False, session=args.session)
            except Exception:
                video = None
        try:
            screenshot(udid, artifacts / "failure.png")
            failure_nodes = hierarchy(udid)
            write_dump(artifacts / "failure-ui.json", failure_nodes)
            if recorder and recorder.started is not None:
                labels_per_step.append(
                    {"step": current_step, "at": round(failed_at, 2), "labels": visible_labels(failure_nodes)}
                )
        except Exception:
            pass
        payload = {
            "status": "fail", "task": name, "simulator": udid, "session": args.session,
            **({"expect": spec["expect"]} if spec.get("expect") else {}),
            **({"variant": variant} if variant else {}),
            "step": current_step, "error": str(error),
            **({"auto_tapped": AUTO_TAPPED} if AUTO_TAPPED else {}),
            **({"system_alert_seen": [{"alert": t, "step": s} for t, s in _SYSTEM_ALERTS.items()]}
               if _SYSTEM_ALERTS else {}),
            "built": built, "total_seconds": metrics.total, "steps_seconds": metrics.steps,
            "results": results, "artifacts": str(artifacts),
            **({"step_labels": labels_per_step} if recorder else {}),
            **({"video": str(video)} if video else {}),
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            video_suffix = f" video={file_link(video)}" if video else ""
            print(f"FAIL task={name} step={current_step} total={metrics.total:.1f}s error={error}{video_suffix} artifacts={file_link(artifacts)}", file=sys.stderr)
        return 1
    finally:
        # A TERM during teardown would leave half the undo done; SIGKILL still can, and
        # the journal's dirty flag covers that.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if lease is not None:
                teardown(udid, bundle_id, lease.journal, previous_ui, setup.get("preferences", {}),
                         fresh_install, args.keep_state)
                torn_down = True
        finally:
            if lease is not None:
                lease.release(clean=torn_down)
            LEASE = None
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def teardown(
    udid: str, bundle_id: str, touched: dict[str, Any], previous_ui: dict[str, str],
    preferences: dict[str, Any], fresh_install: bool, keep_state: bool,
) -> None:
    """Undo only what this run changed; the independent simctl calls run side by side."""
    calls: list[list[str]] = []
    if touched.get("status_bar"):
        calls.append(["xcrun", "simctl", "status_bar", udid, "clear"])
    if touched.get("location"):
        calls.append(["xcrun", "simctl", "location", udid, "clear"])
    calls += [["xcrun", "simctl", "ui", udid, key, value] for key, value in previous_ui.items()]
    calls += [["xcrun", "simctl", "privacy", udid, "reset", service, app]
              for service, app in touched.get("privacy", [])]
    if not keep_state:
        # Leave no app running on whatever screen the flow ended: the next run (or a
        # person using the simulator) would start from it.
        calls.append(["xcrun", "simctl", "terminate", udid, bundle_id])
    processes = [
        subprocess.Popen(call, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for call in calls
    ]
    for process in processes:
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
    if not keep_state and preferences:
        if fresh_install:
            command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id], check=False)
        else:
            for key in preferences:
                command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id, key], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
