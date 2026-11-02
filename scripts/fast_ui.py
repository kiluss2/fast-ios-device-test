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
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

# bin/ holds symlinks in a git checkout but copies in the zip, so look beside scripts/ too.
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "scripts")]
from fast_media import prune_artifacts, publish_video, shrink_video  # noqa: E402
import fast_evidence  # noqa: E402
from fast_guard import check_actions, parse_prompt_labels, risky_step  # noqa: E402
from fast_ui_map import NOISE_LABEL, SCROLL_BAR_LABEL, Trail, record  # noqa: E402


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
    args: list[str], *, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
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
# The running flow's screen-map trail (fast_ui_map.Trail); None when FAST_UI_MAP=0.
TRAIL: Trail | None = None


def invalidate_hierarchy(udid: str) -> None:
    _HIERARCHY_CACHE.pop(udid, None)


def not_booted(error: Exception) -> bool:
    """AXe and simctl on a shut-down simulator; retrying a read will not boot it."""
    text = str(error)
    return "not booted" in text or "current state: Shutdown" in text


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
            if TRAIL is not None:
                TRAIL.read(nodes)
            return nodes
        except (FastUIError, json.JSONDecodeError) as error:
            if not_booted(error):
                raise
            last_error = error
            time.sleep(0.4 * (attempt + 1))
    raise FastUIError(f"describe-ui failed {DESCRIBE_UI_ATTEMPTS} times: {last_error}")


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


SELECTOR_KEYS = ("label", "label_contains", "label_is_empty", "id", "value", "element_type", "frame")


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


def prompt_choice(title: str, buttons: list[str], entries: list[Any]) -> str | None:
    """The button to tap on a prompt with this title, or None. Entries are bare button
    labels or rules {"title_contains", "tap"}. A rule whose title matches owns the prompt:
    when its button is missing nothing is tapped, so a bare "Don’t Allow" never answers a
    prompt a rule was written for. Bare labels answer in the order they are declared.
    Mirrors PromptRules.choice in the device runner."""
    owned = False
    for rule in entries:
        if isinstance(rule, dict) and str(rule.get("title_contains", "")).casefold() in title.casefold():
            owned = True
            if rule.get("tap") in buttons:
                return rule["tap"]
    if owned:
        return None
    return next((label for label in entries if isinstance(label, str) and label in buttons), None)


def prompt_target(nodes: list[dict[str, Any]], entries: list[Any]) -> dict[str, Any] | None:
    """The declared prompt button to tap. System prompts are Sheets titled with the
    prompt's title and holding its buttons; a bare label also matches a Button outside any
    sheet (StoreKit's sign-in alert, Safari's "Open in …?")."""
    if not entries:
        return None
    buttons = [node for node in nodes if node_type(node) == "Button"]
    in_sheets: list[dict[str, Any]] = []
    for sheet in (node for node in nodes if node_type(node) == "Sheet"):
        frame = sheet.get("frame") or {}
        own = [button for button in buttons if within(button, frame)]
        in_sheets += own
        choice = prompt_choice(str(sheet.get("AXLabel") or ""), [str(b.get("AXLabel")) for b in own], entries)
        if choice is not None:
            return next(button for button in own if button.get("AXLabel") == choice)
    labels = [entry for entry in entries if isinstance(entry, str)]
    return next(
        (button for button in buttons
         if button.get("AXLabel") in labels and not any(button is other for other in in_sheets)),
        None,
    )


def within(node: dict[str, Any], frame: dict[str, Any]) -> bool:
    try:
        x, y = frame_center(node)
        return (float(frame["x"]) <= x <= float(frame["x"]) + float(frame["width"])
                and float(frame["y"]) <= y <= float(frame["y"]) + float(frame["height"]))
    except (FastUIError, KeyError, TypeError, ValueError):
        return False


def wait_nodes(
    udid: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    timeout: float,
    optional_prompt_labels: list[Any] | None = None,
    first_poll_max_age: float = 0.0,
) -> list[dict[str, Any]]:
    deadline = time.perf_counter() + timeout
    nodes: list[dict[str, Any]] = []
    max_age = first_poll_max_age
    while time.perf_counter() < deadline:
        nodes = hierarchy(udid, max_age)
        max_age = 0.0
        prompt = prompt_target(nodes, optional_prompt_labels or [])
        if prompt is not None:
            tap_point(udid, *frame_center(prompt), 0.08)
            continue
        if predicate(nodes):
            return nodes
        time.sleep(0.12)
    labels = ", ".join(typed_labels(nodes, limit=25))
    error = FastUIError(f"timed out waiting for UI selector; visible labels: [{labels}]")
    error.nodes = nodes
    raise error


# The cap of any later wait: a system sheet can ignore the first tap while it is still animating in.
PROMPT_CLEAR_TIMEOUT = 8.0


def clear_prompts(udid: str, optional_prompt_labels: list[str]) -> None:
    """Tap away any declared prompt before an action that reads no tree: a prompt raised
    after the flow's last wait (a notification request during a `pause`) would otherwise
    end up in the screenshot."""
    if not optional_prompt_labels:
        return
    try:
        wait_nodes(udid, lambda _: True, PROMPT_CLEAR_TIMEOUT, optional_prompt_labels, HIERARCHY_REUSE)
    except FastUIError as error:
        labels = str(error).split("visible labels:")[-1].strip()
        raise FastUIError(
            f"a declared prompt is still on screen after {PROMPT_CLEAR_TIMEOUT:g}s; visible labels: {labels}"
        ) from None


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


def screen_box(nodes: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    """x, y, width, height of the screen: the application node's frame. Without one, the largest
    frame; a scroll view's content can be taller than the screen, so never prefer it."""
    root = next((node.get("frame") for node in nodes if node.get("type") == "Application" and node.get("frame")), None)
    screen = root or max(
        ((node.get("frame") or {}) for node in nodes),
        key=lambda frame: float(frame.get("width", 0)) * float(frame.get("height", 0)),
        default={},
    )
    return (float(screen.get("x", 0)), float(screen.get("y", 0)),
            float(screen.get("width", 0)), float(screen.get("height", 0)))


def edge_points(edge: str, at: float, distance: float, box: tuple[float, float, float, float]) -> tuple[list[float], list[float]]:
    """A swipe that starts one point inside `edge`, `at` (0-1) along it, and travels `distance`
    (0-1) of the screen inward: the back swipe, Control Center (top, at 0.9), going home (bottom)."""
    x0, y0, width, height = box
    along_x, along_y = x0 + width * at, y0 + height * at
    points = {
        "left": ([x0 + 1, along_y], [x0 + width * distance, along_y]),
        "right": ([x0 + width - 1, along_y], [x0 + width * (1 - distance), along_y]),
        "top": ([along_x, y0 + 1], [along_x, y0 + height * distance]),
        "bottom": ([along_x, y0 + height - 1], [along_x, y0 + height * (1 - distance)]),
    }
    if edge not in points:
        raise FastUIError(f"edge must be left, right, top or bottom, not {edge!r}")
    start, end = points[edge]
    return [round(value, 2) for value in start], [round(value, 2) for value in end]


def swipe_points(action: dict[str, Any], nodes: list[dict[str, Any]], index: int) -> tuple[list[float], list[float]]:
    """`from`/`to` points, or a `direction` the finger moves in ("up" scrolls content down)."""
    if "direction" not in action:
        return required(action, "from", f"action {index}"), required(action, "to", f"action {index}")
    x0, y0, width, height = screen_box(nodes)
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
    if TRAIL is not None:
        TRAIL.touch()
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
    if TRAIL is not None:
        TRAIL.touch()
    # iOS 26+ Liquid Glass controls (tab bar, glass buttons) ignore an instant down/up: `axe tap`
    # reports success and nothing happens. A short hold lands on every control.
    axe(udid, "touch", "-x", f"{x:.2f}", "-y", f"{y:.2f}", "--down", "--up", "--delay", str(TAP_HOLD))
    if max(post_delay, TAP_SETTLE):
        time.sleep(max(post_delay, TAP_SETTLE))


def long_press(udid: str, x: float, y: float, duration: float, post_delay: float) -> None:
    """Hold one finger for `duration` seconds: a context menu, or a widget's Edit Widget."""
    invalidate_hierarchy(udid)
    if TRAIL is not None:
        TRAIL.touch()
    axe(udid, "touch", "-x", f"{x:.2f}", "-y", f"{y:.2f}", "--down", "--up", "--delay", str(duration))
    time.sleep(max(post_delay, TAP_SETTLE))


def double_tap(udid: str, x: float, y: float, post_delay: float) -> None:
    """Both taps in one AXe session: two `axe tap` processes are too far apart to count as one."""
    invalidate_hierarchy(udid)
    if TRAIL is not None:
        TRAIL.touch()
    step = f"tap -x {x:.2f} -y {y:.2f}"
    axe(udid, "batch", "--step", step, "--step", step)
    time.sleep(max(post_delay, TAP_SETTLE))


def drag(udid: str, start: list[float], end: list[float], hold: float, duration: float, post_delay: float) -> None:
    """Move one finger from `start` to `end`; with `hold`, rest on `start` first, as a drag and
    drop or a reorder needs (AXe's own drag starts moving at once)."""
    invalidate_hierarchy(udid)
    if TRAIL is not None:
        TRAIL.touch()
    (sx, sy), (ex, ey) = start, end
    if hold <= 0:
        axe(udid, "drag", "--start-x", f"{sx:.2f}", "--start-y", f"{sy:.2f}", "--end-x", f"{ex:.2f}",
            "--end-y", f"{ey:.2f}", "--duration", str(duration))
    else:
        axe(udid, "batch", "--step", f"touch -x {sx:.2f} -y {sy:.2f} --down", "--step", f"sleep {hold}",
            "--step", f"swipe --start-x {sx:.2f} --start-y {sy:.2f} --end-x {ex:.2f} --end-y {ey:.2f} --duration {duration}",
            "--step", f"touch -x {ex:.2f} -y {ey:.2f} --up")
    time.sleep(max(post_delay, TAP_SETTLE))


def screenshot(udid: str, path: Path) -> None:
    image_type = path.suffix.removeprefix(".").lower() or "png"
    if image_type == "jpg":
        image_type = "jpeg"
    # simctl io hangs for good when the simulator dies mid-run; fail the flow instead of waiting forever.
    command(["xcrun", "simctl", "io", udid, "screenshot", f"--type={image_type}", str(path)], timeout=30)


# A popup sliding in or a toast fading in changes several percent of the screen between two
# shots 0.25 s apart; a ticking timer or a pulsing dot stays under half a percent.
STABLE_RATIO = 0.005
STABLE_TIMEOUT = 3.0
STABLE_INTERVAL = 0.25
# The status bar clock and signal icons change on their own.
STABLE_REGION = [0, 0.07, 1, 1]


def stable_screenshot(udid: str, path: Path, timeout: float, ratio: float) -> bool | None:
    """Shoot until two shots in a row differ by at most `ratio`, keeping the last one; an image
    taken mid-animation is not evidence. True when settled, False after `timeout`, None without
    Pillow (one plain shot)."""
    screenshot(udid, path)
    probe = path.with_name(f".probe-{path.name}")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            time.sleep(STABLE_INTERVAL)
            screenshot(udid, probe)
            try:
                changed = motion_ratio(path, probe, STABLE_REGION, 12)
            except (FastUIError, OSError):
                return None
            probe.replace(path)
            if changed <= ratio:
                return True
        return False
    finally:
        probe.unlink(missing_ok=True)


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
TAP_HOLD = 0.08


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
    result = command(build, check=False, timeout=float(app.get("build_timeout", 300)))
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        raise FastUIError("incremental build failed: " + " | ".join(tail))
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


def unsettled_note(results: list[dict[str, Any]]) -> str:
    """Names the screenshots still moving when the stable wait ran out; never use them as evidence."""
    moving = [Path(str(result.get("path") or result.get("screenshot", "?"))).name
              for result in results if result.get("stable") is False]
    return f" unsettled={','.join(moving)}" if moving else ""


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
    "tap_label", "tap_first", "tap_nearest", "tap_point", "long_press", "double_tap", "drag", "edge_swipe", "swipe",
    "scroll_to",
}
# Steps the screen map records as a way from one screen to the next.
TRAIL_GESTURES = GESTURE_KINDS | {
    "type_text", "launch", "activate", "terminate", "open_url", "double_tap", "drag", "edge_swipe", "pinch", "rotate",
    "orientation", "press_key", "press_button", "set_slider", "set_picker", "shake", "two_finger_tap", "two_finger_drag",
    "key_combo", "clear_text",
}
# HID keycodes for `press_key` and `key_combo` (AXe on the Simulator).
KEY_CODES = {
    "delete": 42, "return": 40, "tab": 43, "escape": 41, "space": 44,
    "right": 79, "left": 80, "down": 81, "up": 82,
}
MODIFIER_CODES = {"control": 224, "shift": 225, "option": 226, "command": 227}
FIELD_TYPES = ("TextField", "SecureTextField", "SearchField", "TextView")
# What AXe on the Simulator cannot do; the iPhone runner can.
IPHONE_ONLY = {
    "pinch": "it needs two fingers and AXe has one",
    "rotate": "it needs two fingers and AXe has one",
    "orientation": "the Simulator has no command to rotate the device",
    "set_picker": "AXe cannot set a picker wheel",
    "two_finger_tap": "it needs two fingers and AXe has one",
    "two_finger_drag": "it needs two fingers and AXe has one",
}


def hid_code(key: Any) -> int:
    """A named key (`return`, `left`), a letter or a digit as its USB HID keycode."""
    name = str(key).lower()
    if name in KEY_CODES:
        return KEY_CODES[name]
    if len(name) == 1 and "a" <= name <= "z":
        return 4 + ord(name) - ord("a")
    if len(name) == 1 and name.isdigit():
        return 39 if name == "0" else 29 + int(name)
    raise FastUIError(f"key must be a letter, a digit or one of {', '.join(KEY_CODES)}, not {key!r}")


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
    if TRAIL is not None:
        TRAIL.start_action()
    result = run_action(action, udid, artifacts, index, optional_prompt_labels, bundle_id)
    if TRAIL is not None and kind in TRAIL_GESTURES and not (result or {}).get("optional_missing"):
        TRAIL.gesture(action)
    expected = action.get("expect")
    if expected is not None and not isinstance(expected, dict):
        raise FastUIError(f"step {index}: expect must be a selector object")
    # An optional tap that found nothing changed no screen, so there is nothing to expect.
    if expected and not (result or {}).get("optional_missing"):
        # A guard on the screen the step leads to: a multi-step request stops at the first
        # surprise instead of acting on a screen the agent did not plan for.
        try:
            wait_selector(udid, expected, float(action.get("expect_timeout", 2)), optional_prompt_labels)
        except FastUIError as error:
            shown = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
            raise FastUIError(f"step {index}: expected {shown} after {kind}; {error}") from error
    settle = action.get("settle", (defaults or {}).get("settle"))
    if kind in GESTURE_KINDS and settle:
        timeout = SETTLE_TIMEOUT if isinstance(settle, bool) else float(settle)
        result = {**(result or {}), "settled": wait_settled(udid, timeout=timeout)}
    return result


def text_field(node: dict[str, Any], selector: dict[str, Any]) -> bool:
    """A field `clear_text` can empty: a text field the selector names (a caption with the same
    label is not one), or any node of the selector's own `element_type`."""
    if selector and not matches(node, selector):
        return False
    return "element_type" in selector or node_type(node) in FIELD_TYPES


def press_at(kind: str, action: dict[str, Any], udid: str, x: float, y: float, post_delay: float) -> None:
    if kind == "long_press":
        long_press(udid, x, y, float(action.get("duration", 1)), post_delay)
    elif kind == "double_tap":
        double_tap(udid, x, y, post_delay)
    else:
        tap_point(udid, x, y, post_delay)


def point_of(spec: Any, udid: str, timeout: float, optional_prompt_labels: list[Any]) -> list[float]:
    """An `[x, y]` point, or the centre of the first node (top-left first) a selector matches."""
    if isinstance(spec, list) and len(spec) == 2:
        return [float(spec[0]), float(spec[1])]
    if not isinstance(spec, dict):
        raise FastUIError(f"a drag end must be [x, y] or a selector, not {spec!r}")
    nodes = wait_selector(udid, spec, timeout, optional_prompt_labels, first_poll_max_age=HIERARCHY_REUSE)
    found = sort_nodes([node for node in nodes if matches(node, spec)], ["y", "x"])
    return list(frame_center(found[0]))


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
    elif kind in IPHONE_ONLY:
        raise FastUIError(f"{kind} works on an iPhone only, not on the Simulator: {IPHONE_ONLY[kind]}")
    elif kind in ("long_press", "double_tap") and "x" in action and "y" in action:
        press_at(kind, action, udid, float(action["x"]), float(action["y"]), post_delay)
    elif kind in ("tap_label", "long_press", "double_tap"):
        if not any(key in action for key in ("label", "label_contains", "id")):
            if kind != "tap_label":
                raise FastUIError(f"{kind} needs 'label', 'id' or x and y in action {index}")
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
            press_at(kind, action, udid, *frame_center(target), post_delay)
            if node_type(target) != selector["element_type"]:
                return {"fallback_type": node_type(target)}
        else:
            return {"optional_missing": True}
    elif kind == "wait_absent":
        wait_absent(udid, action, timeout, optional_prompt_labels)
    elif kind == "assert_absent":
        assert_absent(udid, action, float(action.get("seconds", 3)), optional_prompt_labels)
    elif kind == "dump":
        nodes = wait_nodes(udid, lambda _: True, timeout, optional_prompt_labels)
        name = action.get("name", f"step-{index:02d}")
        path = artifacts / f"{name}-ui.json"
        write_dump(path, nodes)
        return {"path": str(path), "labels": visible_labels(nodes)}
    elif kind == "pause":
        time.sleep(float(action.get("seconds", 1)))
    elif kind == "tap_point":
        tap_point(udid, float(action["x"]), float(action["y"]), post_delay)
    elif kind == "swipe":
        nodes = hierarchy(udid) if "direction" in action else []
        swipe(udid, *swipe_points(action, nodes, index), float(action.get("duration", 0.3)), post_delay)
    elif kind == "scroll_to":
        limit = int(action.get("max_swipes", 6))
        for attempt in range(limit + 1):
            nodes = hierarchy(udid)
            if any(matches(node, action) for node in nodes):
                break
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
    elif kind == "capture":
        # The tree and the picture of the same instant: a toast or an animation is in both or in
        # neither, which a dump and a screenshot taken one after the other cannot promise.
        clear_prompts(udid, optional_prompt_labels)
        name = action.get("name", f"step-{index:02d}")
        picture = artifacts / f"{name}.png"
        shot = threading.Thread(target=screenshot, args=(udid, picture))
        shot.start()
        nodes = hierarchy(udid)
        shot.join()
        tree = artifacts / f"{name}-ui.json"
        write_dump(tree, nodes)
        return {"path": str(picture), "dump": str(tree), "labels": visible_labels(nodes)}
    elif kind == "screenshot":
        clear_prompts(udid, optional_prompt_labels)
        path = artifacts / action.get("name", f"step-{index:02d}.png")
        if action.get("stable", True) is False:
            screenshot(udid, path)
            return {"path": str(path)}
        stable = stable_screenshot(
            udid, path, float(action.get("stable_timeout", STABLE_TIMEOUT)),
            float(action.get("stable_ratio", STABLE_RATIO)),
        )
        return {"path": str(path), "stable": stable}
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
        axe(udid, "type", required(action, "text", f"action {index}"))
        invalidate_hierarchy(udid)
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
    elif kind == "drag":
        start = point_of(required(action, "from", f"action {index}"), udid, timeout, optional_prompt_labels)
        end = point_of(required(action, "to", f"action {index}"), udid, timeout, optional_prompt_labels)
        drag(udid, start, end, float(action.get("hold", 0.6)), float(action.get("duration", 0.6)), post_delay)
    elif kind == "edge_swipe":
        box = screen_box(hierarchy(udid, HIERARCHY_REUSE))
        start, end = edge_points(str(action.get("edge")), float(action.get("at", 0.5)), float(action.get("distance", 0.8)), box)
        # Measured on iOS 26.5: a plain `axe swipe` or `axe drag` misses the system edge gestures
        # (home swipe on the lock screen: 1 in 3); a finger put down first, then moved, gets them.
        drag(udid, start, end, 0.05, float(action.get("duration", 0.3)), post_delay)
    elif kind == "press_key":
        key = action.get("key")
        if key not in KEY_CODES:
            raise FastUIError(f"action {index}: key must be one of {', '.join(KEY_CODES)}, not {key!r}")
        axe(udid, "key-sequence", "--keycodes", ",".join([str(hid_code(key))] * max(int(action.get("count", 1)), 1)))
        invalidate_hierarchy(udid)
    elif kind == "press_button":
        button = action.get("button")
        if button in ("volume_up", "volume_down"):
            raise FastUIError(f"action {index}: AXe has no volume buttons on the Simulator; use an iPhone")
        if button not in ("home", "lock", "siri"):
            raise FastUIError(f"action {index}: button must be home, lock, siri, volume_up or volume_down, not {button!r}")
        # AXe's default press locks the screen but is too short to wake it again, and Siri
        # answers only a held press.
        hold = {"lock": "0.3", "siri": "1.5"}.get(button)
        axe(udid, "button", button, *(["--duration", hold] if hold else []))
        invalidate_hierarchy(udid)
    elif kind == "set_slider":
        target = [("--id", action["id"])] if "id" in action else [("--label", action["label"])] if "label" in action else []
        if not target:
            raise FastUIError(f"action {index}: set_slider needs 'id' or 'label'")
        if TRAIL is not None:
            TRAIL.touch()
        try:
            axe(udid, "slider", *target[0], "--value", str(round(float(required(action, "value", f"action {index}")) * 100)))
        except FastUIError as error:
            if "Multiple" not in str(error):
                raise
            # SwiftUI often nests the slider in a container with the same id and frame.
            raise FastUIError(
                f"action {index}: AXe found several elements for this slider and cannot set one; "
                "move its thumb with `drag`, or run set_slider on an iPhone"
            ) from error
        invalidate_hierarchy(udid)
    elif kind == "key_combo":
        modifiers = action.get("modifiers") or []
        if not modifiers or any(name not in MODIFIER_CODES for name in modifiers):
            raise FastUIError(f"action {index}: modifiers must be a list of {', '.join(MODIFIER_CODES)}")
        axe(udid, "key-combo", "--modifiers", ",".join(str(MODIFIER_CODES[name]) for name in modifiers),
            "--key", str(hid_code(required(action, "key", f"action {index}"))))
        invalidate_hierarchy(udid)
    elif kind == "clear_text":
        selector = {key: action[key] for key in SELECTOR_KEYS if key in action}
        nodes = wait_nodes(
            udid, lambda value: any(text_field(node, selector) for node in value), timeout, optional_prompt_labels,
            HIERARCHY_REUSE,
        )
        field = sort_nodes([node for node in nodes if text_field(node, selector)], ["y", "x"])[0]
        # Near the trailing edge, then select all: the caret position and the text's length
        # stop mattering, and an empty field's placeholder is never "deleted".
        box = field.get("frame") or {}
        tap_point(udid, float(box["x"]) + float(box["width"]) * 0.85, frame_center(field)[1], post_delay)
        axe(udid, "key-combo", "--modifiers", str(MODIFIER_CODES["command"]), "--key", str(hid_code("a")))
        axe(udid, "key-sequence", "--keycodes", str(KEY_CODES["delete"]))
        invalidate_hierarchy(udid)
        # AXe reports an empty field's placeholder as its value, so say what the field shows now.
        after = [node for node in hierarchy(udid) if text_field(node, selector)]
        nearest = min(after, key=lambda node: abs(frame_center(node)[1] - frame_center(field)[1]), default={})
        return {"value": str(nearest.get("AXValue") or "")}
    elif kind == "shake":
        # UIKit on the Simulator listens for this notification; Hardware › Shake posts it too.
        command(["xcrun", "simctl", "spawn", udid, "notifyutil", "-p", "com.apple.UIKit.SimulatorShake"])
        invalidate_hierarchy(udid)
    else:
        raise FastUIError(f"unsupported action type '{kind}'")
    return None


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
    parser.add_argument(
        "--before", action="store_true",
        help="This run shows the bug (e.g. on the base build): a failure at the outcome step is filed as "
        "the case's before shot (env FAST_EVIDENCE=0 files nothing)",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete result JSON")
    parser.add_argument("--artifacts", type=Path, help="Override the artifact directory")
    return parser.parse_args()


def new_trail(bundle_id: str) -> Trail | None:
    return None if os.environ.get("FAST_UI_MAP") == "0" else Trail(bundle_id)


def finish_trail(build_only: bool, current_step: str) -> None:
    """Merge the run's trail into the screen map; a map problem never changes the run's result."""
    if TRAIL is None or build_only or current_step == "prepare":
        return
    try:
        record(TRAIL.finish(), "simulator")
    except Exception as error:  # noqa: BLE001 - the map is a hint, the run's result stands
        print(f"map=not-saved ({error})", file=sys.stderr)


def run_do(argv: list[str]) -> int:
    """`fastSimulatorTest do`: actions on what the Simulator shows now, one PASS/FAIL line with
    the labels after them, like `fastDeviceTest do`. No build, install, launch or preferences."""
    global TRAIL
    parser = argparse.ArgumentParser(
        prog="fastSimulatorTest do",
        description="Run one action, or a JSON array of actions (stops at the first failure), on what the "
        "Simulator shows now.",
    )
    parser.add_argument("action", help='JSON action or array, e.g. \'{"type":"tap_label","label":"Continue"}\'')
    parser.add_argument("--simulator", default=os.environ.get("FAST_SIMULATOR"), help="UDID (env FAST_SIMULATOR)")
    parser.add_argument("--app", default=os.environ.get("FAST_UI_APP", ""),
                        help="Bundle id for launch/terminate and the screen map (env FAST_UI_APP)")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Where screenshots and dumps go")
    parser.add_argument("--prompt-labels", help='Prompt buttons this request may tap on its own, as for fastDeviceTest do')
    args = parser.parse_args(argv)
    # Input refused before anything runs goes to stderr, as `fastDeviceTest do` prints it.
    if not args.simulator:
        print("FAIL error=fastSimulatorTest do needs --simulator <udid> or FAST_SIMULATOR", file=sys.stderr)
        return 1
    try:
        parsed = json.loads(args.action)
        actions = parsed if isinstance(parsed, list) else [parsed]
        check_actions(actions)
        prompts = parse_prompt_labels(args.prompt_labels) if args.prompt_labels else []
    except ValueError as error:
        print(f"FAIL error={error}", file=sys.stderr)
        return 1
    risky = risky_step(actions)
    if risky:
        print(f"FAIL error=step {risky[0]}: '{risky[1]}' looks destructive; send it alone or add \"confirm\": true",
              file=sys.stderr)
        return 1
    udid = args.simulator
    args.out.mkdir(parents=True, exist_ok=True)
    TRAIL = new_trail(args.app) if args.app else None
    results: list[dict[str, Any]] = []
    failed: tuple[int, dict[str, Any], Exception] | None = None
    read_error: Exception | None = None

    def run_steps() -> tuple[tuple[int, dict[str, Any], Exception] | None, list[dict[str, Any]]]:
        done: list[dict[str, Any]] = []
        for index, action in enumerate(actions, start=1):
            try:
                result = execute_action(action, udid, args.out, index, prompts, {}, args.app)
            except (FastUIError, OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as error:
                return (index, action, error), done
            if result:
                done.append({"step": index, **result})
        return None, done

    try:
        # No `simctl bootstatus` (0.3 s a step): on a shut-down simulator no step can act (a
        # `terminate` passes as a no-op), so the first one that says so boots it and the request
        # runs again from the start.
        ensure_device_hub(udid)
        failed, results = run_steps()
        if failed and not_booted(failed[2]):
            ensure_booted(udid)
            failed, results = run_steps()
        # A `wait` or `expect` that just read the tree already shows where the steps ended. The
        # steps ran even when this read fails, so that alone is not a FAIL (resending would tap twice).
        try:
            labels = visible_labels(hierarchy(udid, HIERARCHY_REUSE))
        except (FastUIError, OSError, subprocess.TimeoutExpired) as error:
            labels, read_error = [], error
    except (FastUIError, OSError, subprocess.TimeoutExpired) as error:
        failed, labels = failed or (0, {}, error), []
    finally:
        finish_trail(build_only=False, current_step="do")
        TRAIL = None
    if len(actions) > 1:
        head = f"actions={len(actions)}" + (f" step={failed[0]} type={failed[1].get('type')}" if failed and failed[0] else "")
    else:
        head = str(actions[0].get("type")) if actions else "none"
    shots = [result["path"] for result in results if str(result.get("path", "")).endswith((".png", ".jpg", ".jpeg"))]
    dumps = [result["path"] for result in results if str(result.get("path", "")).endswith("-ui.json")]
    line = "FAIL" if failed else "PASS"
    line += f" {head}" + (f" error={failed[2]}" if failed else "")
    if read_error and not failed:
        line += f" read=failed ({read_error})"
    if shots:
        line += f" screenshot{'s' if len(shots) > 1 else ''}={','.join(map(file_link, shots))}"
    line += unsettled_note(results)
    if dumps:
        line += f" ui={','.join(map(file_link, dumps))}"
    print(f"{line} labels={json.dumps(labels, ensure_ascii=False)}")
    return 1 if failed else 0


def run_shutdown(argv: list[str]) -> int:
    """`fastSimulatorTest shutdown`: at the end of the work, shut the booted simulators down so the
    Mac gets its memory back; `--simulator` limits it to one (spare one Xcode is running on)."""
    parser = argparse.ArgumentParser(
        prog="fastSimulatorTest shutdown",
        description="Shut down every booted simulator, or only the one given.",
    )
    parser.add_argument("--simulator", help="UDID of the one simulator to shut down")
    args = parser.parse_args(argv)
    listed = json.loads(command(["xcrun", "simctl", "list", "devices", "booted", "--json"]).stdout)
    booted = [device for devices in listed.get("devices", {}).values() for device in devices
              if device.get("state") == "Booted"]
    if args.simulator:
        booted = [device for device in booted if device.get("udid") == args.simulator]
    for device in booted:
        stop_stale_recorder(device["udid"])
        command(["xcrun", "simctl", "shutdown", device["udid"]], timeout=60)
    names = ", ".join(f"{device.get('name')} ({device['udid']})" for device in booted)
    print(f"SHUTDOWN simulators={names}" if booted else "SHUTDOWN none booted")
    return 0


def main() -> int:
    global TRAIL
    if sys.argv[1:2] == ["do"]:
        return run_do(sys.argv[2:])
    if sys.argv[1:2] == ["shutdown"]:
        return run_shutdown(sys.argv[2:])
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
    TRAIL = new_trail(bundle_id)
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

    def file_evidence(payload: dict[str, Any], video: Path | None) -> tuple[dict[str, Any], Path | None, str]:
        """File the run as PR evidence after its metrics.json is written; returns the payload and
        clip as they are now (a filed PASS run's clip lives in the evidence folder) and the line
        suffix. A run in a root the caller chose is copied from, never emptied."""
        filed, error = fast_evidence.after_run(
            spec, project_root, name, lambda: fast_evidence.simulator_name(udid),
            "before" if args.before else None, artifacts, video,
            tidy=not (args.artifacts_root or args.artifacts),
        )
        if error:
            return {**payload, "evidence_error": error}, video, f" evidence_error={error}"
        if not filed:
            return payload, video, ""
        payload = json.loads((artifacts / "metrics.json").read_text(encoding="utf-8"))
        video = Path(payload["video"]) if payload.get("video") else None
        return payload, video, f" evidence={file_link(filed['dir'])}"

    metrics = Metrics()
    results: list[dict[str, Any]] = []
    built = False
    current_step = "prepare"
    recorder = Recorder(udid, artifacts / "run.mp4") if args.record else None
    video: Path | None = None
    labels_per_step: list[dict[str, Any]] = []
    previous_ui: dict[str, str] = {}

    try:
        with metrics.measure("prepare"):
            app_path, built = prepare_app(app, base, udid, args.reuse_app)
        if args.build_only:
            location = file_link(app_path) if app_path else "installed"
            print(f"READY task={name} build={'yes' if built else 'skip'} app={location} total={metrics.total:.1f}s")
            return 0
        if fresh_install:
            current_step = "fresh_install"
            with metrics.measure(current_step):
                command(["xcrun", "simctl", "uninstall", udid, bundle_id], check=False)
                # `defaults write` lands in the simulator's user domain, which an
                # uninstall leaves behind; without this wipe one flow's state leaks
                # into the next fresh install.
                command(["xcrun", "simctl", "spawn", udid, "defaults", "delete", bundle_id], check=False)
                command(["xcrun", "simctl", "install", udid, str(app_path)])
        current_step = "state_and_launch"
        with metrics.measure(current_step):
            apply_ui_settings(udid, variant, previous_ui)
            set_preferences(udid, bundle_id, setup.get("preferences", {}))
            if recorder:
                recorder.start()
            launch = ["xcrun", "simctl", "launch", udid, bundle_id]
            launch += [str(value) for value in setup.get("launch_arguments", [])]
            if TRAIL is not None:
                TRAIL.gesture({"type": "launch"})
            command(launch)

        optional_prompt_labels = setup.get("optional_permission_labels", [])
        defaults = {"settle": True} if setup.get("settle_after_actions") else {}
        for index, action in enumerate(required(spec, "actions", "flow"), start=1):
            current_step = f"{index:02d}_{action.get('type', 'unknown')}"
            with metrics.measure(current_step):
                result = execute_action(
                    action, udid, artifacts, index, optional_prompt_labels, defaults, bundle_id
                )
            if recorder:
                labels_per_step.append(step_labels(udid, current_step, recorder.elapsed(), result))
            if result:
                results.append({"step": index, **result})

        if recorder:
            recorder.stop()
            video = publish_video(artifacts.parent, shrink_video(recorder.path), passed=True, session=args.session)
        payload = {
            "status": "pass", "task": name, "simulator": udid, "session": args.session,
            **({"expect": spec["expect"]} if spec.get("expect") else {}),
            **({"variant": variant} if variant else {}),
            "built": built, "total_seconds": metrics.total,
            "steps_seconds": metrics.steps, "results": results, "artifacts": str(artifacts),
            **({"step_labels": labels_per_step} if recorder else {}),
            **({"video": str(video)} if video else {}),
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        payload, video, evidence_suffix = file_evidence(payload, video)
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
            suffix += unsettled_note(results)
            print(f"PASS task={name} total={metrics.total:.1f}s build={'yes' if built else 'skip'}{suffix} artifacts={file_link(artifacts)}{evidence_suffix}")
        return 0
    except (FastUIError, OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as error:
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
            "built": built, "total_seconds": metrics.total, "steps_seconds": metrics.steps,
            "results": results, "artifacts": str(artifacts),
            **({"step_labels": labels_per_step} if recorder else {}),
            **({"video": str(video)} if video else {}),
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        payload, video, evidence_suffix = file_evidence(payload, video)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            video_suffix = f" video={file_link(video)}" if video else ""
            print(f"FAIL task={name} step={current_step} total={metrics.total:.1f}s error={error}{video_suffix} artifacts={file_link(artifacts)}{evidence_suffix}", file=sys.stderr)
        return 1
    finally:
        finish_trail(args.build_only, current_step)
        restore_ui_settings(udid, previous_ui)
        if not args.build_only and not args.keep_state:
            reset_preferences(udid, bundle_id, setup.get("preferences", {}), fresh_install)


if __name__ == "__main__":
    raise SystemExit(main())
