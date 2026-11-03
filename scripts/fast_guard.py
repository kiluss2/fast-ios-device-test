"""Checks shared by fastSimulatorTest and fastDeviceTest before a `do` request runs."""

from __future__ import annotations

import json
import os
import re
from typing import Any

# Words a step should not reach unless the agent chose that step alone: in a multi-step `do`
# the later steps run on a guess about the screens in between, on a phone the team shares.
# A word list (English and Vietnamese, plus FAST_GUARD_WORDS), not an understanding of the app.
DESTRUCTIVE_WORDS = (
    "delete", "remove", "erase", "reset", "pay", "buy", "purchase", "subscribe", "send", "sign[ -]?out",
    "log[ -]?out", "unpair", "forget", "xóa", "xoá", "gỡ", "đăng xuất", "thanh toán", "mua", "gửi", "đặt lại",
)
GESTURES = {"tap_label", "tap_first", "tap_nearest", "long_press", "double_tap", "two_finger_tap", "tap_alert"}
SELECTOR_TEXT = ("label", "label_contains", "id")


def destructive(text: str) -> bool:
    """`deleteAccountButton`, `btn_delete` and `Sign-Out` read as words before they are matched."""
    extra = [re.escape(word.strip()) for word in os.environ.get("FAST_GUARD_WORDS", "").split(",") if word.strip()]
    words = "|".join([*DESTRUCTIVE_WORDS, *extra])
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[_\-.]", " ", text)
    return re.search(rf"\b({words})\b", spaced, re.IGNORECASE) is not None


def check_actions(actions: list[Any]) -> None:
    """Shapes a launcher can refuse before anything runs."""
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise ValueError(f"step {index}: each action must be a JSON object")
        if "expect" in action and not isinstance(action["expect"], dict):
            raise ValueError(f"step {index}: expect must be a selector object, such as {{\"label\": \"About\"}}")


def risky_step(actions: list[dict[str, Any]]) -> tuple[int, str] | None:
    """(step, label) of the first destructive-looking gesture in a request of several actions
    (of any size with FAST_GUARD_SINGLE=1, for an explorer that sends one step at a time) that
    does not say `"confirm": true`; None when the request may run."""
    if len(actions) < 2 and os.environ.get("FAST_GUARD_SINGLE") != "1":
        return None
    for index, action in enumerate(actions, start=1):
        if action.get("type") not in GESTURES or action.get("confirm") is True:
            continue
        selector = action.get("target") if action.get("type") == "tap_nearest" else action
        if action.get("type") == "tap_alert":
            # An alert's button is the confirmation itself ("Delete" on "Delete this trip?").
            selector = {"label": action.get("button")}
        for key in SELECTOR_TEXT:
            text = str((selector or {}).get(key) or "")
            if destructive(text):
                return index, text
    return None


def prompt_entries(entries: Any) -> list[Any]:
    """Checks optional_permission_labels: bare button labels, or rules that name the button
    for one kind of prompt, {"title_contains": "track", "tap": "Ask App Not to Track"}."""
    if not isinstance(entries, list):
        raise ValueError("optional_permission_labels must be a list")
    for entry in entries:
        if isinstance(entry, dict):
            for key in ("title_contains", "tap"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"prompt rule {json.dumps(entry, ensure_ascii=False)} needs a '{key}' string")
        elif not isinstance(entry, str):
            raise ValueError(f"prompt entry {entry!r} is neither a label nor a rule")
    return entries


def parse_prompt_labels(text: str) -> list[Any]:
    """`--prompt-labels "Allow, Don’t Allow"`, or a JSON array when it carries rules."""
    if text.lstrip().startswith("["):
        return prompt_entries(json.loads(text))
    return [label.strip() for label in text.split(",") if label.strip()]
