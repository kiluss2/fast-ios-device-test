#!/usr/bin/env python3

"""Engine unit tests: python3 scripts/test_fast_ui.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_ui  # noqa: E402
import fast_ui_map  # noqa: E402

# Pillow is optional for the engine (only `motion` needs it), so the suite must run without it.
HAVE_PILLOW = importlib.util.find_spec("PIL") is not None

_LOCKS = tempfile.TemporaryDirectory()


def setUpModule() -> None:
    # main() takes a simulator lease; keep the tests' leases out of ~/Library/Caches.
    fast_ui.LOCK_DIR = Path(_LOCKS.name)


def tearDownModule() -> None:
    _LOCKS.cleanup()


def app_spec(tmp: Path, simulator: str) -> dict:
    src = tmp / "Sources"
    src.mkdir(exist_ok=True)
    if not (src / "A.swift").exists():
        (src / "A.swift").write_text("let a = 1\n")
    return {
        "project": str(tmp / "App.xcodeproj"),
        "scheme": "App",
        "configuration": "Debug",
        "simulator": simulator,
        "bundle_id": "com.example.app",
        "app_path": str(tmp / "App.app"),
        "derived_data": str(tmp / "dd"),
        "build_inputs": [str(src)],
    }


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_ignores_simulator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            first = fast_ui.build_fingerprint(app_spec(tmp, "SIM-A"), tmp)
            second = fast_ui.build_fingerprint(app_spec(tmp, "SIM-B"), tmp)
        self.assertEqual(first, second)

    def test_fingerprint_changes_with_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            spec = app_spec(tmp, "SIM-A")
            first = fast_ui.build_fingerprint(spec, tmp)
            (tmp / "Sources" / "A.swift").write_text("let a = 22\n")
            second = fast_ui.build_fingerprint(spec, tmp)
        self.assertNotEqual(first, second)


class HierarchyTests(unittest.TestCase):
    def test_describe_ui_retries_transient_failure(self) -> None:
        good = mock.Mock(stdout='[{"AXLabel": "Widgets", "type": "Button"}]', returncode=0)
        calls = [fast_ui.FastUIError("boom"), good]

        def fake_axe(udid, *args, check=True):
            value = calls.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(fast_ui, "axe", side_effect=fake_axe), \
                mock.patch.object(fast_ui.time, "sleep"):
            nodes = fast_ui.hierarchy("SIM")
        self.assertEqual(nodes[0]["AXLabel"], "Widgets")

    def test_visible_labels_are_screen_ordered_deduped_and_denoised(self) -> None:
        nodes = [
            {"AXLabel": "B", "frame": {"x": 0, "y": 50}},
            {"AXLabel": "A", "frame": {"x": 0, "y": 10}},
            {"AXLabel": "B", "frame": {"x": 0, "y": 90}},
            {"AXLabel": "14:22", "frame": {"x": 0, "y": 0}},
            {"AXLabel": "100%", "frame": {"x": 0, "y": 0}},
            {"AXLabel": "", "frame": {"x": 0, "y": 0}},
            {"AXLabel": "Vertical scroll bar, 2 pages", "frame": {"x": 0, "y": 5}},
            {"AXLabel": "Horizontal scroll bar, 1 page", "frame": {"x": 0, "y": 6}},
        ]
        self.assertEqual(fast_ui.visible_labels(nodes), ["A", "B"])
        many = [{"AXLabel": f"L{i:03d}"} for i in range(100)]
        self.assertEqual(len(fast_ui.visible_labels(many, limit=12)), 12)

    def test_hierarchy_reuses_a_fresh_tree_until_a_gesture(self) -> None:
        good = mock.Mock(stdout='[{"AXLabel": "A", "type": "Button"}]', returncode=0)
        with mock.patch.object(fast_ui, "axe", return_value=good) as axe, mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.hierarchy("SIM")
            fast_ui.hierarchy("SIM", max_age=fast_ui.HIERARCHY_REUSE)
            self.assertEqual(axe.call_count, 1)
            fast_ui.tap_point("SIM", 1, 1, 0)
            fast_ui.hierarchy("SIM", max_age=fast_ui.HIERARCHY_REUSE)
            fast_ui.hierarchy("SIM")
        self.assertEqual(axe.call_count, 4)  # describe, tap, describe, describe

    def test_flatten_skips_frame_dicts_and_recurses_children_only(self) -> None:
        tree = [{"type": "Application", "frame": {"x": 0, "y": 0, "width": 10, "height": 10},
                 "children": [{"type": "Button", "AXLabel": "Go", "frame": {"x": 1, "y": 2, "width": 4, "height": 4}}]}]
        nodes = list(fast_ui.flatten(tree))
        self.assertEqual([n.get("type") for n in nodes], ["Application", "Button"])

    def test_compact_node_drops_children_and_survives_missing_frame(self) -> None:
        node = {"type": "Button", "AXLabel": "Go", "AXUniqueId": "go", "AXValue": "1",
                "frame": {"x": 1, "y": 2, "width": 4, "height": 4}, "children": [{"type": "Image"}]}
        self.assertEqual(fast_ui.compact_node(node),
                         {"label": "Go", "type": "Button", "id": "go", "value": "1",
                          "frame": {"x": 1, "y": 2, "width": 4, "height": 4}, "center": [3.0, 4.0]})
        self.assertEqual(fast_ui.compact_node({"type": "Other"}), {"type": "Other"})

    def test_dump_file_is_flat_and_compact(self) -> None:
        nodes = [{"AXLabel": "Widgets", "type": "Button", "frame": {"x": 0, "y": 0, "width": 1, "height": 1},
                  "children": [{"type": "Image", "frame": {"x": 0, "y": 0, "width": 1, "height": 1}}]}]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(fast_ui, "hierarchy", return_value=nodes), \
                mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action({"type": "dump", "name": "home"}, "SIM", Path(raw), 3, [])
            text = (Path(raw) / "home-ui.json").read_text()
        self.assertNotIn("children", text)
        self.assertIn('"center"', text)


class ActionTests(unittest.TestCase):
    def test_tap_label_taps_matched_frame_without_axe_label_lookup(self) -> None:
        node = {"AXLabel": "Yes", "type": "Button", "frame": {"x": 10, "y": 20, "width": 100, "height": 40}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[node]), \
                mock.patch.object(fast_ui, "tap_point") as tap, \
                mock.patch.object(fast_ui, "axe") as axe:
            fast_ui.execute_action({"type": "tap_label", "label": "Yes"}, "SIM", Path("/tmp"), 1, [])
        tap.assert_called_once_with("SIM", 60.0, 40.0, 0.15)
        axe.assert_not_called()

    def test_tap_label_waits_for_a_sliding_control_to_stop(self) -> None:
        def at(y: float) -> list[dict[str, Any]]:
            return [{"AXLabel": "Nickname", "type": "Button", "frame": {"x": 200, "y": y, "width": 100, "height": 40}}]
        trees = [at(600), at(420), at(300), at(300)]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=lambda *_a, **_k: trees.pop(0) if len(trees) > 1 else trees[0]), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action({"type": "tap_label", "label": "Nickname"}, "SIM", Path("/tmp"), 1, [])
        tap.assert_called_once_with("SIM", 250.0, 320.0, 0.15)

    def test_optional_tap_label_skips_when_missing(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=[]), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            result = fast_ui.execute_action(
                {"type": "tap_label", "label": "Nope", "optional": True}, "SIM", Path("/tmp"), 1, []
            )
        tap.assert_not_called()
        self.assertEqual(result, {"optional_missing": True})

    def test_dump_action_writes_labels_and_hierarchy(self) -> None:
        nodes = [{"AXLabel": "Widgets", "type": "Button", "frame": {"x": 0, "y": 0, "width": 1, "height": 1}}]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(fast_ui, "hierarchy", return_value=nodes), \
                mock.patch.object(fast_ui.time, "sleep"):
            artifacts = Path(raw)
            result = fast_ui.execute_action({"type": "dump", "name": "home"}, "SIM", artifacts, 3, [])
            self.assertEqual(result["labels"], ["Widgets"])
            self.assertTrue((artifacts / "home-ui.json").exists())

    def test_screenshot_closes_a_declared_prompt_before_capturing(self) -> None:
        prompt = {"AXLabel": "Don’t Allow", "type": "Button", "frame": {"x": 40, "y": 400, "width": 140, "height": 48}}
        calls = mock.Mock()
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[[prompt], []]), \
                mock.patch.object(fast_ui, "tap_point", calls.tap), \
                mock.patch.object(fast_ui, "screenshot", calls.screenshot), \
                mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action({"type": "screenshot", "name": "home.png", "stable": False}, "SIM", Path("/tmp"), 1, ["Don’t Allow"])
        self.assertEqual([call[0] for call in calls.mock_calls], ["tap", "screenshot"])
        calls.tap.assert_called_once_with("SIM", 110.0, 424.0, 0.08)

    def test_capture_takes_the_picture_while_it_reads_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifacts = Path(raw)
            node = {"AXLabel": "Trip saved!", "type": "StaticText", "frame": {"x": 1, "y": 2, "width": 3, "height": 4}}
            with mock.patch.object(fast_ui, "hierarchy", return_value=[node]), \
                    mock.patch.object(fast_ui, "screenshot", side_effect=lambda udid, path: path.write_bytes(b"png")), \
                    mock.patch.object(fast_ui, "clear_prompts"):
                result = fast_ui.execute_action({"type": "capture", "name": "toast"}, "SIM", artifacts, 4, [])
            self.assertEqual(result["path"], str(artifacts / "toast.png"))
            self.assertEqual(result["dump"], str(artifacts / "toast-ui.json"))
            self.assertTrue((artifacts / "toast.png").exists() and (artifacts / "toast-ui.json").exists())
            self.assertIn("Trip saved!", result["labels"])

    def test_screenshot_reads_no_tree_when_the_flow_declares_no_prompt(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy") as tree, mock.patch.object(fast_ui, "screenshot") as shot:
            fast_ui.execute_action({"type": "screenshot", "name": "home.png", "stable": False}, "SIM", Path("/tmp"), 1, [])
        tree.assert_not_called()
        shot.assert_called_once()

    @unittest.skipUnless(HAVE_PILLOW, "needs Pillow")
    def test_screenshot_waits_until_two_shots_match(self) -> None:
        result, kept = self.shoot_frames(["#000", "#fff", "#fff"], timeout=3)
        self.assertEqual(result, {"path": kept, "stable": True})
        from PIL import Image
        self.assertEqual(Image.open(kept).getpixel((5, 50)), (255, 255, 255))

    @unittest.skipUnless(HAVE_PILLOW, "needs Pillow")
    def test_screenshot_reports_a_screen_that_never_settles(self) -> None:
        result, _ = self.shoot_frames(itertools.cycle(["#000", "#fff"]), timeout=0.05)
        self.assertIs(result["stable"], False)

    def shoot_frames(self, colours, timeout: float) -> tuple[dict, str]:
        """A screenshot action whose simctl shots come out in `colours`, one per call."""
        from PIL import Image
        frames = iter(colours)

        def shoot(_udid: str, path: Path) -> None:
            Image.new("RGB", (20, 100), next(frames)).save(path)

        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        with mock.patch.object(fast_ui, "screenshot", side_effect=shoot), mock.patch.object(fast_ui.time, "sleep"):
            result = fast_ui.execute_action({"type": "screenshot", "name": "home.png", "stable_timeout": timeout},
                                            "SIM", folder, 1, [])
        return result, str(folder / "home.png")

    def test_label_contains_matches_a_fragment(self) -> None:
        node = {"AXLabel": "Office, No Internet Connection", "type": "Cell", "frame": {}}
        self.assertTrue(fast_ui.matches(node, {"label_contains": "Office"}))
        self.assertFalse(fast_ui.matches(node, {"label_contains": "Office 2G"}))

    def test_matches_id_and_value(self) -> None:
        node = {"AXLabel": "Wi-Fi", "AXUniqueId": "wifiSwitch", "AXValue": "1", "type": "Switch", "frame": {}}
        self.assertTrue(fast_ui.matches(node, {"id": "wifiSwitch"}))
        self.assertTrue(fast_ui.matches(node, {"value": "1", "element_type": "Switch"}))
        self.assertFalse(fast_ui.matches(node, {"id": "other"}))

    def test_tap_label_accepts_id_and_label_contains(self) -> None:
        node = {"AXLabel": "Continue to app", "AXUniqueId": "cta", "type": "Button",
                "frame": {"x": 0, "y": 0, "width": 10, "height": 10}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[node]), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            fast_ui.execute_action({"type": "tap_label", "id": "cta"}, "SIM", Path("/tmp"), 1, [])
            fast_ui.execute_action({"type": "tap_label", "label_contains": "Continue"}, "SIM", Path("/tmp"), 2, [])
        self.assertEqual(tap.call_count, 2)
        with self.assertRaisesRegex(fast_ui.FastUIError, "missing 'label' or 'id'"):
            fast_ui.execute_action({"type": "tap_label"}, "SIM", Path("/tmp"), 3, [])

    def test_wait_timeout_lists_types_and_explains_type_mismatch(self) -> None:
        nodes = [{"AXLabel": "Add Widget", "type": "StaticText", "frame": {"x": 0, "y": 10}}]
        with mock.patch.object(fast_ui, "hierarchy", return_value=nodes), mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.wait_selector("SIM", {"label": "Add Widget", "element_type": "Button"}, 0.01, [])
        self.assertIn("Add Widget (StaticText)", str(caught.exception))
        self.assertIn("exists as StaticText, not Button", str(caught.exception))

    def test_wait_timeout_suggests_close_labels(self) -> None:
        nodes = [{"AXLabel": "Widget Gallery", "type": "StaticText", "frame": {"x": 0, "y": 10}}]
        with mock.patch.object(fast_ui, "hierarchy", return_value=nodes), mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.wait_selector("SIM", {"label": "Widget gallery"}, 0.01, [])
        self.assertIn("closest labels: [Widget Gallery]", str(caught.exception))

    def test_tap_label_falls_back_to_the_only_node_with_that_label(self) -> None:
        node = {"AXLabel": "Add Widget", "type": "StaticText", "frame": {"x": 0, "y": 0, "width": 10, "height": 10}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[node]), mock.patch.object(fast_ui, "tap_point") as tap:
            result = fast_ui.execute_action({"type": "tap_label", "label": "Add Widget"}, "SIM", Path("/tmp"), 1, [])
        tap.assert_called_once_with("SIM", 5.0, 5.0, 0.15)
        self.assertEqual(result, {"fallback_type": "StaticText"})

    def test_tap_label_does_not_fall_back_when_ambiguous_or_type_explicit(self) -> None:
        a = {"AXLabel": "Add", "type": "StaticText", "frame": {"x": 0, "y": 0, "width": 10, "height": 10}}
        b = {"AXLabel": "Add", "type": "Image", "frame": {"x": 0, "y": 20, "width": 10, "height": 10}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[a, b]), mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            with self.assertRaises(fast_ui.FastUIError):
                fast_ui.execute_action({"type": "tap_label", "label": "Add", "timeout": 0.01}, "SIM", Path("/tmp"), 1, [])
            with self.assertRaises(fast_ui.FastUIError):
                fast_ui.execute_action({"type": "tap_label", "label": "Add Widget", "element_type": "Button", "timeout": 0.01},
                                       "SIM", Path("/tmp"), 2, [])
        tap.assert_not_called()

    def test_tap_label_never_falls_back_to_the_application_root(self) -> None:
        root = {"AXLabel": "Demo App", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[root]), mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            with self.assertRaises(fast_ui.FastUIError):
                fast_ui.execute_action({"type": "tap_label", "label": "Demo App", "timeout": 0.01}, "SIM", Path("/tmp"), 1, [])
        tap.assert_not_called()

    def test_long_press_holds_the_resolved_node(self) -> None:
        node = {"AXLabel": "Weather", "type": "Other", "frame": {"x": 0, "y": 0, "width": 10, "height": 10}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[node]), mock.patch.object(fast_ui, "axe") as axe, \
                mock.patch.object(fast_ui.time, "sleep"):
            result = fast_ui.execute_action({"type": "long_press", "label": "Weather"}, "SIM", Path("/tmp"), 1, [])
        axe.assert_called_once_with("SIM", "touch", "-x", "5.00", "-y", "5.00", "--down", "--up", "--delay", "1.0")
        self.assertEqual(result, {"fallback_type": "Other"})

    def test_long_press_at_a_point_takes_a_duration(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy") as tree, mock.patch.object(fast_ui, "axe") as axe, \
                mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action({"type": "long_press", "x": 40, "y": 80, "duration": 2}, "SIM", Path("/tmp"), 1, [])
        tree.assert_not_called()
        axe.assert_called_once_with("SIM", "touch", "-x", "40.00", "-y", "80.00", "--down", "--up", "--delay", "2.0")

    def test_long_press_needs_a_selector_or_a_point(self) -> None:
        with self.assertRaisesRegex(fast_ui.FastUIError, "long_press needs"):
            fast_ui.execute_action({"type": "long_press", "x": 40}, "SIM", Path("/tmp"), 1, [])

    def run_gesture(self, action: dict, nodes: list | None = None) -> mock.Mock:
        with mock.patch.object(fast_ui, "hierarchy", return_value=nodes or []), \
                mock.patch.object(fast_ui, "axe") as axe, mock.patch.object(fast_ui, "command") as cmd, \
                mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action(action, "SIM", Path("/tmp"), 1, [])
        axe.command = cmd
        return axe

    SCREEN = [{"AXLabel": "Demo", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}}]

    def test_double_tap_sends_both_taps_in_one_batch(self) -> None:
        node = {"AXLabel": "Photo", "type": "Image", "frame": {"x": 0, "y": 0, "width": 10, "height": 10}}
        axe = self.run_gesture({"type": "double_tap", "label": "Photo"}, [node])
        axe.assert_called_once_with("SIM", "batch", "--step", "tap -x 5.00 -y 5.00", "--step", "tap -x 5.00 -y 5.00")
        axe = self.run_gesture({"type": "double_tap", "x": 40, "y": 80})
        axe.assert_called_once_with("SIM", "batch", "--step", "tap -x 40.00 -y 80.00", "--step", "tap -x 40.00 -y 80.00")
        with self.assertRaisesRegex(fast_ui.FastUIError, "double_tap needs"):
            self.run_gesture({"type": "double_tap", "x": 40})

    def test_drag_moves_between_points_or_selectors(self) -> None:
        axe = self.run_gesture({"type": "drag", "from": [10, 20], "to": [30, 40], "hold": 0})
        axe.assert_called_once_with("SIM", "drag", "--start-x", "10.00", "--start-y", "20.00", "--end-x", "30.00",
                                    "--end-y", "40.00", "--duration", "0.6")
        row = {"AXLabel": "Milk", "type": "Cell", "frame": {"x": 0, "y": 100, "width": 400, "height": 40}}
        axe = self.run_gesture({"type": "drag", "from": {"label": "Milk"}, "to": [200, 400]}, [row])
        axe.assert_called_once_with("SIM", "batch", "--step", "touch -x 200.00 -y 120.00 --down", "--step", "sleep 0.6",
                                    "--step", "swipe --start-x 200.00 --start-y 120.00 --end-x 200.00 --end-y 400.00 --duration 0.6",
                                    "--step", "touch -x 200.00 -y 400.00 --up")

    def test_screen_is_the_application_frame_not_a_taller_scroll_content(self) -> None:
        content = {"AXLabel": "", "type": "Group", "frame": {"x": 0, "y": -900, "width": 400, "height": 2400}}
        self.assertEqual(fast_ui.screen_box(self.SCREEN + [content]), (0.0, 0.0, 400.0, 800.0))
        self.assertEqual(fast_ui.swipe_points({"direction": "up"}, [content] + self.SCREEN, 1), ([200.0, 560.0], [200.0, 240.0]))

    def test_edge_swipe_starts_on_the_edge(self) -> None:
        self.assertEqual(fast_ui.edge_points("left", 0.5, 0.5, (0, 0, 400, 800)), ([1.0, 400.0], [200.0, 400.0]))
        self.assertEqual(fast_ui.edge_points("top", 0.9, 0.5, (0, 0, 400, 800)), ([360.0, 1.0], [360.0, 400.0]))
        self.assertEqual(fast_ui.edge_points("bottom", 0.5, 0.25, (0, 0, 400, 800)), ([200.0, 799.0], [200.0, 600.0]))
        self.assertEqual(fast_ui.edge_points("right", 0.25, 0.5, (0, 0, 400, 800)), ([399.0, 200.0], [200.0, 200.0]))
        # Measured on iOS 26.5: a plain `axe swipe` or `axe drag` misses the system edge gestures
        # (home swipe on the lock screen: 1 in 3); a finger put down first, then moved, gets them.
        # The default travel is 80 %: on an iPhone a back swipe that stops at half the screen springs back.
        for edge, start, end in (("left", [1.0, 400.0], [320.0, 400.0]), ("bottom", [200.0, 799.0], [200.0, 160.0])):
            with mock.patch.object(fast_ui, "drag") as drag, mock.patch.object(fast_ui, "hierarchy", return_value=self.SCREEN):
                fast_ui.execute_action({"type": "edge_swipe", "edge": edge}, "SIM", Path("/tmp"), 1, [])
            drag.assert_called_once_with("SIM", start, end, 0.05, 0.3, 0.15)
        with self.assertRaisesRegex(fast_ui.FastUIError, "edge must be"):
            fast_ui.edge_points("middle", 0.5, 0.5, (0, 0, 400, 800))

    def test_iphone_only_gestures_say_so_on_the_simulator(self) -> None:
        for action in ({"type": "pinch", "scale": 2}, {"type": "rotate", "radians": 1}, {"type": "two_finger_tap"},
                       {"type": "two_finger_drag", "dy": -0.25},
                       {"type": "orientation", "value": "landscape_left"}, {"type": "set_picker", "value": "10"},
                       {"type": "press_button", "button": "volume_up"}):
            with self.subTest(action=action["type"]), self.assertRaisesRegex(fast_ui.FastUIError, "Simulator"):
                self.run_gesture(action)

    def test_key_combo_holds_the_modifiers(self) -> None:
        self.run_gesture({"type": "key_combo", "key": "a", "modifiers": ["command"]}).assert_called_once_with(
            "SIM", "key-combo", "--modifiers", "227", "--key", "4")
        self.run_gesture({"type": "key_combo", "key": "left", "modifiers": ["option", "shift"]}).assert_called_once_with(
            "SIM", "key-combo", "--modifiers", "226,225", "--key", "80")
        self.run_gesture({"type": "key_combo", "key": "0", "modifiers": ["control"]}).assert_called_once_with(
            "SIM", "key-combo", "--modifiers", "224", "--key", "39")
        with self.assertRaisesRegex(fast_ui.FastUIError, "modifiers must be"):
            self.run_gesture({"type": "key_combo", "key": "a", "modifiers": ["hyper"]})
        with self.assertRaisesRegex(fast_ui.FastUIError, "key must be"):
            self.run_gesture({"type": "key_combo", "key": "ab", "modifiers": ["command"]})

    def test_press_key_has_arrows_and_siri_is_a_button(self) -> None:
        self.run_gesture({"type": "press_key", "key": "up", "count": 2}).assert_called_once_with(
            "SIM", "key-sequence", "--keycodes", "82,82")
        # Siri answers a held press, as on an iPhone; AXe's default tap of the button does nothing.
        self.run_gesture({"type": "press_button", "button": "siri"}).assert_called_once_with(
            "SIM", "button", "siri", "--duration", "1.5")

    def test_clear_text_selects_all_in_the_field_and_reports_what_is_left(self) -> None:
        caption = {"AXLabel": "Name", "type": "StaticText", "frame": {"x": 0, "y": 60, "width": 200, "height": 20}}
        field = {"AXLabel": "Name", "type": "TextField", "AXValue": "hello", "frame": {"x": 0, "y": 100, "width": 200, "height": 40}}
        emptied = {**field, "AXValue": "Your name"}
        trees = [self.SCREEN + [caption, field], self.SCREEN + [caption, emptied]]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=lambda udid, max_age=0.0: trees.pop(0) if len(trees) > 1 else trees[0]), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui, "axe") as axe, \
                mock.patch.object(fast_ui.time, "sleep"):
            result = fast_ui.execute_action({"type": "clear_text", "label": "Name"}, "SIM", Path("/tmp"), 1, [])
        # The field, not the caption above it; near its trailing edge, where the caret should go.
        self.assertEqual(tap.call_args.args[:3], ("SIM", 170.0, 120.0))
        self.assertEqual(axe.call_args_list, [mock.call("SIM", "key-combo", "--modifiers", "227", "--key", "4"),
                                              mock.call("SIM", "key-sequence", "--keycodes", "42")])
        # AXe shows a placeholder as the value, so the engine reports what the field shows now.
        self.assertEqual(result, {"value": "Your name"})

    def test_press_key_count_zero_still_presses_once(self) -> None:
        self.run_gesture({"type": "press_key", "key": "tab", "count": 0}).assert_called_once_with(
            "SIM", "key-sequence", "--keycodes", "43")

    def test_slider_ax_cannot_pick_says_what_to_do(self) -> None:
        ambiguous = fast_ui.FastUIError("command failed (1) axe slider: Error: Multiple (2) accessibility elements matched --id 'S'.")
        with mock.patch.object(fast_ui, "axe", side_effect=ambiguous), mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaisesRegex(fast_ui.FastUIError, "several elements.*drag.*iPhone"):
                fast_ui.execute_action({"type": "set_slider", "id": "S", "value": 1}, "SIM", Path("/tmp"), 1, [])

    def test_keys_buttons_slider_and_shake(self) -> None:
        self.run_gesture({"type": "press_key", "key": "delete", "count": 3}).assert_called_once_with(
            "SIM", "key-sequence", "--keycodes", "42,42,42")
        self.run_gesture({"type": "press_key", "key": "return"}).assert_called_once_with("SIM", "key-sequence", "--keycodes", "40")
        with self.assertRaisesRegex(fast_ui.FastUIError, "key must be one of"):
            self.run_gesture({"type": "press_key", "key": "f13"})
        self.run_gesture({"type": "press_button", "button": "home"}).assert_called_once_with("SIM", "button", "home")
        # AXe's default press locks the screen but is too short to wake it again.
        self.run_gesture({"type": "press_button", "button": "lock"}).assert_called_once_with(
            "SIM", "button", "lock", "--duration", "0.3")
        self.run_gesture({"type": "set_slider", "id": "volume", "value": 0.25}).assert_called_once_with(
            "SIM", "slider", "--id", "volume", "--value", "25")
        self.run_gesture({"type": "set_slider", "label": "Brightness", "value": 1}).assert_called_once_with(
            "SIM", "slider", "--label", "Brightness", "--value", "100")
        axe = self.run_gesture({"type": "shake"})
        axe.command.assert_called_once_with(["xcrun", "simctl", "spawn", "SIM", "notifyutil", "-p", "com.apple.UIKit.SimulatorShake"])

    def test_wait_settled_returns_true_when_two_polls_match(self) -> None:
        a = [{"AXLabel": "A", "type": "Button", "frame": {"x": 0, "y": 0, "width": 1, "height": 1}}]
        b = [{"AXLabel": "A", "type": "Button", "frame": {"x": 0, "y": 5, "width": 1, "height": 1}}]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[a, b, b, b]), mock.patch.object(fast_ui.time, "sleep"):
            self.assertTrue(fast_ui.wait_settled("SIM", timeout=5, stable_for=0))

    def test_wait_settled_gives_up_quietly_on_a_moving_screen(self) -> None:
        frames = itertools.count()
        moving = lambda udid, max_age=0.0: [  # noqa: E731
            {"AXLabel": "A", "type": "Image", "frame": {"x": 0, "y": next(frames), "width": 1, "height": 1}}
        ]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=moving), mock.patch.object(fast_ui.time, "sleep"):
            self.assertFalse(fast_ui.wait_settled("SIM", timeout=0.01))

    def test_settle_flag_on_tap_records_result(self) -> None:
        node = {"AXLabel": "Go", "type": "Button", "frame": {"x": 0, "y": 0, "width": 2, "height": 2}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[node]), mock.patch.object(fast_ui, "tap_point"), \
                mock.patch.object(fast_ui, "wait_settled", return_value=True) as settled:
            result = fast_ui.execute_action({"type": "tap_label", "label": "Go", "settle": True}, "SIM", Path("/tmp"), 1, [])
        settled.assert_called_once()
        self.assertEqual(result, {"settled": True})

    def test_simulator_lifecycle_actions_call_simctl_and_axe(self) -> None:
        with mock.patch.object(fast_ui, "command") as cmd, mock.patch.object(fast_ui, "axe") as axe:
            fast_ui.execute_action({"type": "type_text", "text": "hello"}, "SIM", Path("/tmp"), 1, [], {}, "com.x")
            fast_ui.execute_action({"type": "launch", "launch_arguments": ["-flag"]}, "SIM", Path("/tmp"), 2, [], {}, "com.x")
            fast_ui.execute_action({"type": "terminate", "app": "com.apple.Preferences"}, "SIM", Path("/tmp"), 3, [], {}, "com.x")
            fast_ui.execute_action({"type": "open_url", "url": "myapp://home"}, "SIM", Path("/tmp"), 4, [], {}, "com.x")
        axe.assert_called_once_with("SIM", "type", "hello")
        self.assertEqual(cmd.call_args_list[0].args[0], ["xcrun", "simctl", "launch", "SIM", "com.x", "-flag"])
        self.assertEqual(cmd.call_args_list[1].args[0], ["xcrun", "simctl", "terminate", "SIM", "com.apple.Preferences"])
        self.assertEqual(cmd.call_args_list[2].args[0], ["xcrun", "simctl", "openurl", "SIM", "myapp://home"])
        with self.assertRaisesRegex(fast_ui.FastUIError, "needs 'app'"):
            fast_ui.execute_action({"type": "launch"}, "SIM", Path("/tmp"), 5, [], {}, "")

    def test_swipe_direction_uses_the_screen_frame(self) -> None:
        nodes = [
            {"AXLabel": "App", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}},
            {"AXLabel": "Row", "type": "Button", "frame": {"x": 0, "y": 100, "width": 400, "height": 40}},
        ]
        self.assertEqual(fast_ui.swipe_points({"direction": "up"}, nodes, 1), ([200.0, 560.0], [200.0, 240.0]))
        self.assertEqual(fast_ui.swipe_points({"direction": "left"}, nodes, 1), ([320.0, 400.0], [80.0, 400.0]))
        with self.assertRaises(fast_ui.FastUIError):
            fast_ui.swipe_points({"direction": "sideways"}, nodes, 1)

    def test_scroll_to_swipes_until_the_selector_shows(self) -> None:
        screen = {"AXLabel": "", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}}
        target = {"AXLabel": "Configure Proxy", "type": "Cell", "frame": {"x": 0, "y": 600, "width": 400, "height": 44}}
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[[screen], [screen], [screen, target]]), \
                mock.patch.object(fast_ui, "swipe") as swipe:
            fast_ui.execute_action({"type": "scroll_to", "label": "Configure Proxy"}, "SIM", Path("/tmp"), 1, [])
        self.assertEqual(swipe.call_count, 2)

    def test_scroll_to_fails_after_max_swipes(self) -> None:
        screen = {"AXLabel": "Wi-Fi", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[screen]), \
                mock.patch.object(fast_ui, "swipe") as swipe:
            with self.assertRaisesRegex(fast_ui.FastUIError, "not found after 2 swipes"):
                fast_ui.execute_action(
                    {"type": "scroll_to", "label": "Nope", "max_swipes": 2}, "SIM", Path("/tmp"), 1, []
                )
        self.assertEqual(swipe.call_count, 2)

    def test_file_link_is_an_absolute_clickable_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "evidence run"
            folder.mkdir()
            link = fast_ui.file_link(folder)
        self.assertTrue(link.startswith("file:///"))
        self.assertTrue(link.endswith("/evidence%20run"))

    def test_wait_timeout_error_lists_visible_labels(self) -> None:
        nodes = [{"AXLabel": "Widgets"}, {"AXLabel": "Smart"}]
        with mock.patch.object(fast_ui, "hierarchy", return_value=nodes), \
                mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.wait_selector("SIM", {"label": "Missing"}, 0.01, [])
        self.assertIn("Smart", str(caught.exception))
        self.assertIn("Widgets", str(caught.exception))


def prompt_sheet(title: str, *buttons: str) -> list[dict]:
    """A system prompt as describe-ui reports it on iOS 26: a Sheet titled with the prompt's
    title, the title again as StaticText, and the buttons stacked inside the sheet."""
    nodes = [
        {"AXLabel": title, "type": "Sheet", "frame": {"x": 27.5, "y": 131.5, "width": 320, "height": 404}},
        {"AXLabel": title, "type": "StaticText", "frame": {"x": 57.5, "y": 245.5, "width": 260, "height": 64.5}},
    ]
    for offset, label in enumerate(buttons):
        nodes.append({"AXLabel": label, "type": "Button",
                      "frame": {"x": 43.5, "y": 360 + 56 * offset, "width": 288, "height": 48}})
    return nodes


def sim_tree(app: str, *nodes: dict) -> str:
    root = {"AXLabel": app, "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800},
            "children": list(nodes)}
    return json.dumps([root])


def button(label: str, y: float = 300) -> dict:
    return {"AXLabel": label, "type": "Button", "frame": {"x": 20, "y": y, "width": 200, "height": 44}}


class FakeAxe:
    """describe-ui serves the given trees in order (the last one repeats); gestures succeed."""

    def __init__(self, *trees: str) -> None:
        self.trees = list(trees)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, udid: str, *args: str, check: bool = True) -> mock.Mock:
        self.calls.append(args)
        if args[0] == "describe-ui":
            tree = self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]
            return mock.Mock(stdout=tree, returncode=0, stderr="")
        return mock.Mock(stdout="", returncode=0, stderr="")


class SimulatorTrailTests(unittest.TestCase):
    A = sim_tree("Demo", button("Go"), button("Help", 400))
    B = sim_tree("Demo", button("Done"), button("Back", 400))

    def setUp(self) -> None:
        fast_ui.invalidate_hierarchy("SIM")
        fast_ui.TRAIL = fast_ui_map.Trail("com.example.app")
        self.addCleanup(setattr, fast_ui, "TRAIL", None)
        sleep = mock.patch.object(fast_ui.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_actions(self, fake: FakeAxe, *actions: dict, prompts: list | None = None) -> list[dict]:
        with mock.patch.object(fast_ui, "axe", side_effect=fake):
            for index, action in enumerate(actions, start=1):
                fast_ui.execute_action(action, "SIM", Path("/tmp"), index, prompts or [])
        return fast_ui.TRAIL.finish()

    @staticmethod
    def kinds(trail: list[dict]) -> list[str]:
        return [entry["gesture"]["type"] if "gesture" in entry else next(iter(entry)) for entry in trail]

    def test_trail_keeps_the_read_before_each_gesture(self) -> None:
        loading = sim_tree("Demo", button("Help", 400))
        trail = self.run_actions(FakeAxe(loading, loading, self.A), {"type": "wait", "label": "Go"},
                                 {"type": "tap_label", "label": "Go"})
        self.assertEqual(self.kinds(trail), ["screen", "tap_label"])
        self.assertEqual([node["label"] for node in trail[0]["screen"]["nodes"]], ["Go", "Help"])

    def test_trail_skips_prompt_taps_and_scroll_inner_swipes(self) -> None:
        prompt = sim_tree("Demo", *prompt_sheet(NOTIFICATIONS, "Allow", "Don’t Allow"))
        bottom = sim_tree("Demo", button("Done"), button("Bottom", 700))
        fake = FakeAxe(prompt, self.A, self.A, self.B, self.B, bottom)
        trail = self.run_actions(fake, {"type": "tap_label", "label": "Go"}, {"type": "scroll_to", "label": "Bottom"},
                                 prompts=["Allow"])
        self.assertEqual([entry["gesture"]["type"] for entry in trail if "gesture" in entry], ["tap_label", "scroll_to"])
        self.assertEqual(sum(1 for call in fake.calls if call[0] == "swipe"), 2)

    def test_trail_marks_gesture_without_read(self) -> None:
        trail = self.run_actions(FakeAxe(self.A), {"type": "tap_label", "label": "Go"}, {"type": "tap_point", "x": 5, "y": 5})
        self.assertEqual(self.kinds(trail), ["screen", "tap_label", "tap_point"])
        self.assertNotIn("from", trail[1])
        self.assertIsNone(trail[2]["from"])

    def test_tap_nearest_starts_from_the_screen_it_tapped(self) -> None:
        action = {"type": "tap_nearest", "target": {"label": "Help"}, "anchor": {"label": "Go"}, "success": {"label": "Done"}}
        trail = self.run_actions(FakeAxe(self.A, self.B), action)
        self.assertEqual(self.kinds(trail), ["screen", "tap_nearest", "screen"])
        self.assertEqual(trail[0]["screen"]["nodes"][0]["label"], "Go")
        self.assertEqual(trail[2]["screen"]["nodes"][0]["label"], "Done")

    def test_timed_out_selector_records_no_gesture(self) -> None:
        with self.assertRaises(fast_ui.FastUIError):
            self.run_actions(FakeAxe(self.A), {"type": "tap_label", "label": "Missing", "timeout": 0.01})
        self.assertEqual(self.kinds(fast_ui.TRAIL.finish()), ["screen"])

    def test_optional_tap_label_without_target_records_no_gesture(self) -> None:
        with mock.patch.object(fast_ui, "axe", side_effect=FakeAxe(self.A)):
            result = fast_ui.execute_action({"type": "tap_label", "label": "Missing", "optional": True}, "SIM", Path("/tmp"), 1, [])
        self.assertEqual(result, {"optional_missing": True})
        self.assertEqual(self.kinds(fast_ui.TRAIL.finish()), ["screen"])

    def test_reads_of_another_app_break_the_trail(self) -> None:
        fast_ui.TRAIL.gesture({"type": "launch"})
        safari = sim_tree("Safari", button("Address"))
        trail = self.run_actions(FakeAxe(self.A, self.A, safari), {"type": "tap_label", "label": "Go"}, {"type": "wait", "label": "Address"})
        self.assertEqual(self.kinds(trail), ["launch", "screen", "tap_label", "break"])
        self.assertEqual(trail[1]["screen"]["app"], "com.example.app")

    def test_expect_is_skipped_after_an_optional_tap_that_found_nothing(self) -> None:
        self.run_actions(FakeAxe(self.A), {"type": "tap_label", "label": "Missing", "optional": True,
                                          "expect": {"label": "Nope"}, "expect_timeout": 0.01})

    def test_expect_guards_the_screen_an_action_leads_to(self) -> None:
        self.run_actions(FakeAxe(self.A, self.B), {"type": "tap_label", "label": "Go", "expect": {"label": "Done"}})
        fast_ui.invalidate_hierarchy("SIM")
        with self.assertRaisesRegex(fast_ui.FastUIError, r"step 1: expected \{\"label\":\"Nope\"\} after tap_label.*visible labels"):
            self.run_actions(FakeAxe(self.A, self.B), {"type": "tap_label", "label": "Go", "expect": {"label": "Nope"},
                                                       "expect_timeout": 0.01})

    def sim_do(self, *argv: str, trees: tuple = (), env: dict | None = None) -> tuple[int, str, mock.Mock]:
        out = io.StringIO()
        err = io.StringIO()
        fake = FakeAxe(*(trees or (self.A, self.B)))
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(fast_ui.os.environ, env or {}), \
                mock.patch.object(fast_ui, "ensure_booted") as booted, mock.patch.object(fast_ui, "ensure_device_hub") as hub, \
                mock.patch.object(fast_ui, "command") as cmd, \
                mock.patch.object(fast_ui, "axe", side_effect=fake) as axe, mock.patch.object(fast_ui, "record") as record, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fast_ui.run_do([*argv, "--out", raw])
        axe.err = err.getvalue()
        axe.hub = hub
        axe.record = record
        axe.booted = booted
        axe.command = cmd
        return code, out.getvalue(), axe

    def test_sim_do_acts_on_the_current_screen(self) -> None:
        code, out, axe = self.sim_do('{"type":"tap_label","label":"Go"}', "--simulator", "SIM")
        self.assertEqual((code, out), (0, 'PASS tap_label labels=["Demo", "Done", "Back"]\n'))
        # A booted simulator skips `simctl bootstatus` (0.3 s a step); Device Hub is still checked.
        axe.booted.assert_not_called()
        axe.hub.assert_called_once_with("SIM")
        axe.command.assert_not_called()  # no build, install, launch or preferences
        axe.record.assert_not_called()  # no --app: no screen map

    def test_sim_do_stops_at_the_failing_step(self) -> None:
        code, out, _ = self.sim_do('[{"type":"tap_label","label":"Go"},{"type":"tap_label","label":"Missing","timeout":0.01}]',
                                   "--simulator", "SIM")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("FAIL actions=2 step=2 type=tap_label error="), out)
        self.assertTrue(out.rstrip().endswith('labels=["Demo", "Done", "Back"]'), out)

    def test_sim_do_takes_the_simulator_and_app_from_the_environment(self) -> None:
        code, out, axe = self.sim_do('{"type":"tap_label","label":"Go"}',
                                     env={"FAST_SIMULATOR": "SIM-ENV", "FAST_UI_APP": "com.example.app"})
        self.assertEqual(code, 0)
        axe.hub.assert_called_once_with("SIM-ENV")
        trail, engine = axe.record.call_args.args
        self.assertEqual((engine, trail[0]["screen"]["app"]), ("simulator", "com.example.app"))
        with mock.patch.dict(fast_ui.os.environ, {}, clear=False):
            fast_ui.os.environ.pop("FAST_SIMULATOR", None)
            code, out, axe = self.sim_do('{"type":"wait","label":"Go"}')
        self.assertEqual((code, out), (1, ""))
        self.assertIn("needs --simulator", axe.err)

    def test_sim_do_refuses_a_destructive_step_in_a_batch(self) -> None:
        code, out, axe = self.sim_do('[{"type":"tap_label","label":"Edit"},{"type":"tap_label","label":"Delete"}]',
                                     "--simulator", "SIM")
        self.assertEqual(code, 1)
        self.assertIn("step 2: 'Delete' looks destructive", axe.err)
        axe.assert_not_called()

    def test_sim_do_reports_bad_input_as_a_fail_line(self) -> None:
        for argument, message in (("{not json", "Expecting property name"), ('["a"]', "each action must be a JSON object"),
                                  ('{"type":"tap_label","label":"Go","expect":"Done"}', "expect must be a selector object")):
            with self.subTest(argument=argument):
                code, out, axe = self.sim_do(argument, "--simulator", "SIM")
                self.assertEqual((code, out), (1, ""))
                self.assertIn(message, axe.err)
                axe.assert_not_called()

    def test_sim_do_keeps_pass_when_only_the_final_read_fails(self) -> None:
        good = FakeAxe(self.A)

        def flaky(udid, *args, check=True):
            if args[0] == "describe-ui" and flaky.reads >= 1:
                raise fast_ui.FastUIError("describe-ui failed")
            flaky.reads += args[0] == "describe-ui"
            return good(udid, *args, check=check)
        flaky.reads = 0
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(fast_ui, "ensure_booted"), \
                mock.patch.object(fast_ui, "axe", side_effect=flaky), mock.patch.object(fast_ui.time, "sleep"), \
                contextlib.redirect_stdout(out):
            code = fast_ui.run_do(['{"type":"tap_label","label":"Go"}', "--simulator", "SIM", "--out", raw])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("PASS tap_label read=failed (describe-ui failed"), out.getvalue())

    def test_sim_do_reuses_the_read_its_last_step_just_made(self) -> None:
        code, _, axe = self.sim_do('{"type":"wait","label":"Go"}', "--simulator", "SIM", trees=(self.A,))
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for call in axe.call_args_list if call.args[1] == "describe-ui"), 1)

    def test_sim_do_boots_a_shut_down_simulator_and_runs_from_the_start(self) -> None:
        good = FakeAxe(self.A, self.B)

        def shut_down_once(udid, *args, check=True):
            if not shut_down_once.booted:
                raise fast_ui.FastUIError("Error: Cannot run accessibility commands against SIM as it is not booted")
            return good(udid, *args, check=check)
        shut_down_once.booted = False
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(fast_ui, "ensure_device_hub"), \
                mock.patch.object(fast_ui, "ensure_booted", side_effect=lambda udid: setattr(shut_down_once, "booted", True)) as booted, \
                mock.patch.object(fast_ui, "axe", side_effect=shut_down_once), mock.patch.object(fast_ui.time, "sleep") as sleep, \
                contextlib.redirect_stdout(out):
            code = fast_ui.run_do(['{"type":"tap_label","label":"Go"}', "--simulator", "SIM", "--out", raw])
        self.assertEqual((code, out.getvalue()), (0, 'PASS tap_label labels=["Demo", "Done", "Back"]\n'))
        booted.assert_called_once_with("SIM")
        self.assertNotIn(mock.call(0.4), sleep.call_args_list)  # "not booted" is not retried like a flaky read

    def test_sim_do_boots_when_a_later_step_finds_the_simulator_shut_down(self) -> None:
        # `terminate` on a shut-down simulator passes (it ignores errors); `launch` is the first to fail.
        state = {"booted": False}

        def simctl(arguments, check=True, **kwargs):
            if arguments[2] == "launch" and not state["booted"]:
                raise fast_ui.FastUIError("command failed (149) xcrun simctl launch: Unable to lookup in current state: Shutdown")
            return mock.Mock(returncode=0, stdout="", stderr="")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(fast_ui, "ensure_device_hub"), \
                mock.patch.object(fast_ui, "ensure_booted", side_effect=lambda udid: state.update(booted=True)) as booted, \
                mock.patch.object(fast_ui, "command", side_effect=simctl), \
                mock.patch.object(fast_ui, "axe", side_effect=FakeAxe(self.A)), mock.patch.object(fast_ui.time, "sleep"), \
                contextlib.redirect_stdout(out):
            code = fast_ui.run_do(['[{"type":"terminate"},{"type":"launch"},{"type":"wait","label":"Go"}]',
                                   "--simulator", "SIM", "--app", "com.example.app", "--out", raw])
        self.assertEqual(code, 0, out.getvalue())
        booted.assert_called_once_with("SIM")

    def test_sim_do_lists_screenshots(self) -> None:
        code, out, _ = self.sim_do('{"type":"screenshot","name":"now.png"}', "--simulator", "SIM")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^PASS screenshot screenshot=file://.*/now\.png labels=")

    def test_map_error_keeps_the_run_and_warns(self) -> None:
        fast_ui.TRAIL.read(json.loads(self.A))
        stderr = io.StringIO()
        with mock.patch.object(fast_ui, "record", side_effect=OSError("disk full")) as record, \
                contextlib.redirect_stderr(stderr):
            fast_ui.finish_trail(build_only=False, current_step="03_tap_label")
        record.assert_called_once()
        self.assertEqual(stderr.getvalue(), "map=not-saved (disk full)\n")

    def test_build_only_prepare_and_fast_ui_map_0_record_nothing(self) -> None:
        with mock.patch.object(fast_ui, "record") as record:
            fast_ui.finish_trail(build_only=True, current_step="state_and_launch")
            fast_ui.finish_trail(build_only=False, current_step="prepare")
        record.assert_not_called()
        with mock.patch.dict(fast_ui.os.environ, {"FAST_UI_MAP": "0"}):
            self.assertIsNone(fast_ui.new_trail("com.example.app"))
        self.assertIsInstance(fast_ui.new_trail("com.example.app"), fast_ui_map.Trail)

    def test_a_bad_number_in_an_action_is_a_fail_line_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "bad.json"
            flow.write_text(json.dumps({"version": 1, "name": "bad", "actions": [{"type": "edge_swipe", "edge": "left", "at": "middle"}],
                                        "app": {"bundle_id": "com.example.app", "prebuilt": True, "simulator": "SIM",
                                                "project_root": raw}}))
            err = io.StringIO()
            with mock.patch.object(fast_ui.sys, "argv", ["fastSimulatorTest", str(flow)]), \
                    mock.patch.object(fast_ui, "prepare_app", return_value=(None, False)), \
                    mock.patch.object(fast_ui, "command"), mock.patch.object(fast_ui, "apply_ui_settings"), \
                    mock.patch.object(fast_ui, "set_preferences"), \
                    mock.patch.object(fast_ui, "restore_ui_settings"), mock.patch.object(fast_ui, "record"), \
                    mock.patch.object(fast_ui, "axe", side_effect=FakeAxe(self.A)), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = fast_ui.main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL task=bad step=01_edge_swipe", err.getvalue())

    def test_main_records_the_map_even_when_restoring_ui_settings_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "home.json"
            flow.write_text(json.dumps({"version": 1, "name": "home", "actions": [{"type": "wait", "label": "Go"}],
                                        "app": {"bundle_id": "com.example.app", "prebuilt": True, "simulator": "SIM",
                                                "project_root": raw}}))
            with mock.patch.object(fast_ui.sys, "argv", ["fastSimulatorTest", str(flow)]), \
                    mock.patch.object(fast_ui, "prepare_app", return_value=(None, False)), \
                    mock.patch.object(fast_ui, "command"), mock.patch.object(fast_ui, "apply_ui_settings"), \
                    mock.patch.object(fast_ui, "set_preferences"), \
                    mock.patch.object(fast_ui, "restore_ui_settings", side_effect=RuntimeError("simctl gone")), \
                    mock.patch.object(fast_ui, "axe", side_effect=FakeAxe(self.A)), \
                    mock.patch.object(fast_ui, "record") as record, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "simctl gone"):
                    fast_ui.main()
        trail, engine = record.call_args.args
        self.assertEqual(engine, "simulator")
        self.assertEqual([next(iter(entry)) for entry in trail], ["gesture", "screen"])

    def test_trail_costs_under_50_ms_on_a_large_screen(self) -> None:
        big = json.loads(sim_tree("Demo", *(button(f"Row {index}", 10 + index) for index in range(600))))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        started = time.perf_counter()
        with mock.patch.dict(fast_ui.os.environ, {"FAST_UI_MAP_DIR": directory.name}):
            for _ in range(3):
                fast_ui.TRAIL.read(list(fast_ui.flatten(big)))
                fast_ui.TRAIL.gesture({"type": "tap_point", "x": 1, "y": 1})
            fast_ui.finish_trail(build_only=False, current_step="03_tap_point")
        self.assertLess(time.perf_counter() - started, 0.05)


TRACKING = "Allow “App” to track your activity across other companies’ apps and websites?"
NOTIFICATIONS = "“App” Would Like to Send You Notifications"
LOCATION = "Allow “App” to use your location?"
PROMPT_RULES = [
    {"title_contains": "notifications", "tap": "Allow"},
    {"title_contains": "track", "tap": "Ask App Not to Track"},
]


class PromptRuleTests(unittest.TestCase):
    def test_a_button_name_shared_by_two_prompts_follows_each_prompts_rule(self) -> None:
        tracking = fast_ui.prompt_target(prompt_sheet(TRACKING, "Ask App Not to Track", "Allow"), PROMPT_RULES)
        notifications = fast_ui.prompt_target(prompt_sheet(NOTIFICATIONS, "Don’t Allow", "Allow"), PROMPT_RULES)
        self.assertEqual(tracking["AXLabel"], "Ask App Not to Track")
        self.assertEqual(notifications["AXLabel"], "Allow")

    def test_a_bare_label_never_answers_a_prompt_a_rule_was_written_for(self) -> None:
        nodes = prompt_sheet(LOCATION, "Allow Once", "Allow While Using App", "Don’t Allow")
        entries = [{"title_contains": "Location", "tap": "Always Allow"}, "Don’t Allow"]
        self.assertIsNone(fast_ui.prompt_target(nodes, entries))

    def test_bare_labels_answer_in_the_order_they_are_declared(self) -> None:
        nodes = prompt_sheet(NOTIFICATIONS, "Don’t Allow", "Allow")
        self.assertEqual(fast_ui.prompt_target(nodes, ["Allow", "Don’t Allow"])["AXLabel"], "Allow")

    def test_wait_taps_the_rules_button_and_carries_on(self) -> None:
        sheet = prompt_sheet(TRACKING, "Ask App Not to Track", "Allow")
        home = [{"AXLabel": "Home", "type": "Button", "frame": {"x": 0, "y": 0, "width": 50, "height": 50}}]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[sheet, home]), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action({"type": "wait", "label": "Home"}, "SIM", Path("/tmp"), 1, PROMPT_RULES)
        tap.assert_called_once_with("SIM", 187.5, 384.0, 0.08)


class AbsenceTests(unittest.TestCase):
    def test_wait_absent_returns_once_selector_is_gone(self) -> None:
        polls = [[{"AXLabel": "Popup"}], [{"AXLabel": "Popup"}], [{"AXLabel": "Home"}]]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=polls), \
                mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.execute_action({"type": "wait_absent", "label": "Popup", "timeout": 5}, "SIM", Path("."), 1, [])

    def test_wait_absent_fails_when_selector_stays(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=[{"AXLabel": "Popup"}]), \
                mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.wait_absent("SIM", {"label": "Popup"}, 0.01, [])
        self.assertIn("still on screen", str(caught.exception))

    def test_assert_absent_fails_the_moment_selector_appears(self) -> None:
        polls = [[{"AXLabel": "Home"}], [{"AXLabel": "Home"}, {"AXLabel": "Popup"}]]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=polls), \
                mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.assert_absent("SIM", {"label": "Popup"}, 5, [])
        self.assertIn("appeared while asserting absence", str(caught.exception))
        self.assertIn("Popup", str(caught.exception))

    def test_assert_absent_passes_when_selector_never_shows(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=[{"AXLabel": "Home"}]), \
                mock.patch.object(fast_ui.time, "sleep"):
            fast_ui.assert_absent("SIM", {"label": "Popup"}, 0.01, [])


class MotionTests(unittest.TestCase):
    def test_engine_imports_without_pillow(self) -> None:
        import subprocess
        code = "import sys; sys.modules['PIL'] = None; import fast_ui; print('ok')"
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=str(Path(fast_ui.__file__).parent), capture_output=True, text=True
        )
        self.assertEqual(out.stdout.strip(), "ok", out.stderr[-300:])

    @unittest.skipUnless(HAVE_PILLOW, "Pillow not installed")
    def test_motion_ratio_counts_changed_pixels(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            a = Image.new("RGB", (100, 100), (0, 0, 0))
            b = a.copy()
            for x in range(50):
                for y in range(100):
                    b.putpixel((x, y), (255, 255, 255))
            a.save(tmp / "a.png")
            b.save(tmp / "b.png")
            ratio = fast_ui.motion_ratio(tmp / "a.png", tmp / "b.png", [0, 0, 1, 1], 12)
        self.assertAlmostEqual(ratio, 0.5)


class LifecycleTests(unittest.TestCase):
    def test_build_if_needed_boots_simulator_before_building(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            spec = app_spec(tmp, "SIM-A")
            order: list[str] = []
            with mock.patch.object(fast_ui, "ensure_booted", side_effect=lambda udid: order.append("boot")), \
                    mock.patch.object(fast_ui, "command", side_effect=lambda *a, **k: order.append("xcodebuild") or mock.Mock(returncode=1, stdout="", stderr="x")):
                with self.assertRaises(fast_ui.FastUIError):
                    fast_ui.prepare_app(spec, tmp, "SIM-A", reuse_app=False)
        self.assertEqual(order[:2], ["boot", "xcodebuild"])

    def prepare_with_cached_app(self, tmp: Path, spec: dict) -> tuple[bool, list[str]]:
        (tmp / "App.app").mkdir()
        fast_ui.write_build_fingerprint(tmp / "dd" / ".fastui-build-state.json", fast_ui.build_fingerprint(spec, tmp))
        calls: list[str] = []
        with mock.patch.object(fast_ui, "ensure_booted"), mock.patch.object(fast_ui, "verify_app"), \
                mock.patch.object(fast_ui, "command", side_effect=lambda args, **_: calls.append(args[0]) or mock.Mock(returncode=0, stdout="", stderr="")):
            _, built = fast_ui.prepare_app(spec, tmp, "SIM-A", reuse_app=False)
        return built, calls

    def test_unchanged_inputs_reuse_the_cached_app(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            built, calls = self.prepare_with_cached_app(tmp, app_spec(tmp, "SIM-A"))
        self.assertFalse(built)
        self.assertNotIn("xcodebuild", calls)

    def test_missing_build_inputs_never_reuse_the_cached_app(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            spec = app_spec(tmp, "SIM-A")
            del spec["build_inputs"]
            built, calls = self.prepare_with_cached_app(tmp, spec)
        self.assertTrue(built)
        self.assertIn("xcodebuild", calls)

    def device_hub_calls(self, tmp: Path, running: bool) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake(args: list[str], **_: object) -> mock.Mock:
            calls.append(args)
            return mock.Mock(returncode=0 if running or args[0] != "pgrep" else 1, stdout="", stderr="")

        self.addCleanup(setattr, fast_ui, "TAP_SETTLE", 0.0)
        with mock.patch.dict(fast_ui.os.environ, {"DEVELOPER_DIR": str(tmp / "Contents" / "Developer")}), \
                mock.patch.object(fast_ui, "command", side_effect=fake):
            fast_ui.ensure_device_hub("SIM-A")
        return calls

    def test_device_hub_is_opened_when_xcode_ships_it_and_it_is_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "Contents" / "Applications" / "DeviceHub.app").mkdir(parents=True)
            calls = self.device_hub_calls(tmp, running=False)
        self.assertIn("open", [args[0] for args in calls])

    def test_device_hub_is_left_alone_when_running_or_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            self.assertEqual(self.device_hub_calls(tmp, running=False), [])
            self.assertEqual(fast_ui.TAP_SETTLE, 0.0)
            (tmp / "Contents" / "Applications" / "DeviceHub.app").mkdir(parents=True)
            calls = self.device_hub_calls(tmp, running=True)
        self.assertNotIn("open", [args[0] for args in calls])

    def test_taps_linger_under_device_hub(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "Contents" / "Applications" / "DeviceHub.app").mkdir(parents=True)
            self.device_hub_calls(tmp, running=True)
        with mock.patch.object(fast_ui, "axe") as axe, mock.patch.object(fast_ui.time, "sleep") as sleep:
            fast_ui.tap_point("SIM-A", 10, 20, 0.08)
        sleep.assert_called_once_with(fast_ui.DEVICE_HUB_TAP_SETTLE)
        # A held touch, not an instant tap: Liquid Glass controls ignore a zero-length press.
        self.assertEqual(axe.call_args.args[1:], ("touch", "-x", "10.00", "-y", "20.00", "--down", "--up",
                                                  "--delay", str(fast_ui.TAP_HOLD)))

    def test_prebuilt_app_skips_build_and_derived_data(self) -> None:
        with mock.patch.object(fast_ui, "ensure_booted"), mock.patch.object(fast_ui, "build_if_needed") as build:
            app_path, built = fast_ui.prepare_app(
                {"bundle_id": "com.apple.Preferences", "prebuilt": True}, Path("/tmp"), "SIM", False
            )
        build.assert_not_called()
        self.assertIsNone(app_path)
        self.assertFalse(built)

    def test_build_only_flag_parses(self) -> None:
        with mock.patch.object(sys, "argv", ["fast_ui.py", "flow.json", "--build-only"]):
            args = fast_ui.parse_arguments()
        self.assertTrue(args.build_only)


class RecordingTests(unittest.TestCase):
    def test_recorder_starts_simctl_and_finalizes_on_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(fast_ui.subprocess, "Popen") as popen, \
                mock.patch.object(fast_ui, "stop_stale_recorder"), \
                mock.patch.object(fast_ui.time, "sleep"):
            video = Path(raw) / "run.mp4"
            popen.return_value.wait.return_value = 0
            recorder = fast_ui.Recorder("SIM", video)
            recorder.start()
            video.write_bytes(b"x" * 200_000)
            recorder.stop()
        args = popen.call_args.args[0]
        self.assertEqual(args[:5], ["xcrun", "simctl", "io", "SIM", "recordVideo"])
        popen.return_value.send_signal.assert_called_once()

    def test_publish_video_keeps_only_final_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "task"
            old_run = task_dir / "20260101-000000-x-1"
            old_run.mkdir(parents=True)
            (old_run / "run.mp4").write_bytes(b"old")
            (task_dir / "final.mp4").write_bytes(b"older-final")
            new_run = task_dir / "20260102-000000-x-2"
            new_run.mkdir()
            (new_run / "run.mp4").write_bytes(b"new")
            final = fast_ui.publish_video(task_dir, new_run / "run.mp4", passed=True)
            self.assertEqual(final, task_dir / "final.mp4")
            self.assertEqual(final.read_bytes(), b"new")
            self.assertFalse((old_run / "run.mp4").exists())
            self.assertFalse((new_run / "run.mp4").exists())

    def test_publish_video_names_final_after_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw)
            run = task / "run-1"; run.mkdir()
            (run / "run.mp4").write_bytes(b"x")
            final = fast_ui.publish_video(task, run / "run.mp4", passed=True, session="qa-1")
            self.assertEqual(final, task / "final-qa-1.mp4")
            self.assertTrue(final.exists())

    def test_publish_video_keeps_failure_video_and_previous_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "task"
            run = task_dir / "20260102-000000-x-2"
            run.mkdir(parents=True)
            (task_dir / "final.mp4").write_bytes(b"final")
            (run / "run.mp4").write_bytes(b"fail")
            result = fast_ui.publish_video(task_dir, run / "run.mp4", passed=False)
            self.assertEqual(result, run / "run.mp4")
            self.assertEqual((task_dir / "final.mp4").read_bytes(), b"final")

    def test_record_flag_parses(self) -> None:
        with mock.patch.object(sys, "argv", ["fast_ui.py", "flow.json", "--record"]):
            self.assertTrue(fast_ui.parse_arguments().record)


class PruneTests(unittest.TestCase):
    def test_prune_removes_runs_and_finals_older_than_keep_days(self) -> None:
        import os, time as _time
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old_run = root / "task-a" / "20260101-000000-SIM-1"
            new_run = root / "task-a" / "20260908-000000-SIM-2"
            old_final = root / "task-b" / "final.mp4"
            for d in (old_run, new_run, old_final.parent):
                d.mkdir(parents=True, exist_ok=True)
            (old_run / "metrics.json").write_text("{}")
            (new_run / "metrics.json").write_text("{}")
            old_final.write_bytes(b"v")
            stale = _time.time() - 4 * 86400
            os.utime(old_run, (stale, stale))
            os.utime(old_final, (stale, stale))
            removed = fast_ui.prune_artifacts(root, keep_days=3)
            self.assertFalse(old_run.exists())
            self.assertFalse(old_final.exists())
            self.assertFalse(old_final.parent.exists())
            self.assertTrue(new_run.exists())
            self.assertEqual(removed, 2)


class ReportTests(unittest.TestCase):
    def test_report_uses_latest_attempt_per_cell_and_counts_attempts(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fast_ui_report
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            def write(task, stamp, sim, status, extra=None):
                d = root / task / f"{stamp}-{sim}-1"
                d.mkdir(parents=True)
                (d / "metrics.json").write_text(json.dumps({
                    "task": task, "simulator": sim, "session": "s1",
                    "status": status, "total_seconds": 10.0, "step": "02_wait", **(extra or {}),
                }))
            write("a", "20260101-000001", "SIM1XXXX", "fail")
            write("a", "20260101-000002", "SIM1XXXX", "pass", {"video": "/v/final.mp4"})
            write("a", "20260101-000001", "SIM2XXXX", "fail", {"error": "boom"})
            runs = fast_ui_report.load_runs(root, "s1", "")
            cells = fast_ui_report.latest_per_cell(runs)
            text = fast_ui_report.render(cells, {("a", "SIM1XXXX"): 2, ("a", "SIM2XXXX"): 1})
        self.assertIn("PASS 10s ×2", text)
        self.assertIn("FAIL 10s @02_wait", text)
        self.assertIn("boom", text)
        self.assertIn("/v/final.mp4", text)
        self.assertIn("1/2 PASS", text)


class UIVariantTests(unittest.TestCase):
    def test_cli_overrides_setup_and_names_variant(self) -> None:
        setup = {"appearance": "light", "content_size": "large"}
        variant = fast_ui.ui_variant(setup, "dark", "extra-extra-large")
        self.assertEqual(variant, {"appearance": "dark", "content_size": "extra-extra-large"})
        self.assertEqual(fast_ui.variant_name("home", variant), "home@dark@xxl")
        self.assertEqual(fast_ui.variant_name("home", {}), "home")
        self.assertEqual(
            fast_ui.variant_name("home", fast_ui.ui_variant({"content_size": "accessibility-extra-large"}, None, None)),
            "home@ax-xl",
        )

    def test_unknown_setting_fails_closed(self) -> None:
        with self.assertRaises(fast_ui.FastUIError):
            fast_ui.ui_variant({"content_size": "huge"}, None, None)

    def test_apply_records_previous_and_restore_puts_it_back(self) -> None:
        current = {"appearance": "light", "content_size": "extra-extra-large"}
        calls: list[list[str]] = []

        def fake_command(args, check=True, timeout=None):
            calls.append(args)
            key = args[4]
            if len(args) == 5:
                return mock.Mock(stdout=current[key] + "\n", returncode=0)
            current[key] = args[5]
            return mock.Mock(stdout="", returncode=0)

        previous: dict[str, str] = {}
        with mock.patch.object(fast_ui, "command", side_effect=fake_command):
            fast_ui.apply_ui_settings("SIM", {"appearance": "dark", "content_size": "extra-extra-large"}, previous)
            self.assertEqual(current, {"appearance": "dark", "content_size": "extra-extra-large"})
            self.assertEqual(previous, {"appearance": "light"})
            fast_ui.restore_ui_settings("SIM", previous)
        self.assertEqual(current["appearance"], "light")
        self.assertNotIn(["xcrun", "simctl", "ui", "SIM", "content_size", "extra-extra-large"], calls)

    def test_apply_keeps_previous_when_second_setting_fails(self) -> None:
        def fake_command(args, check=True, timeout=None):
            if len(args) == 5:
                return mock.Mock(stdout="light\n" if args[4] == "appearance" else "large\n", returncode=0)
            if args[4] == "content_size":
                raise fast_ui.FastUIError("boom")
            return mock.Mock(stdout="", returncode=0)

        previous: dict[str, str] = {}
        with mock.patch.object(fast_ui, "command", side_effect=fake_command):
            with self.assertRaises(fast_ui.FastUIError):
                fast_ui.apply_ui_settings("SIM", {"appearance": "dark", "content_size": "small"}, previous)
        self.assertEqual(previous, {"appearance": "light", "content_size": "large"})

    def test_variant_flags_parse(self) -> None:
        with mock.patch.object(sys, "argv", ["fast_ui.py", "flow.json", "--appearance", "dark", "--content-size", "accessibility-large"]):
            args = fast_ui.parse_arguments()
        self.assertEqual((args.appearance, args.content_size), ("dark", "accessibility-large"))


class StepLabelTests(unittest.TestCase):
    def test_step_labels_poll_hierarchy_once(self) -> None:
        nodes = [{"AXLabel": "Title", "frame": {"x": 0, "y": 10}}, {"AXLabel": "12:00", "frame": {"x": 0, "y": 0}}]
        with mock.patch.object(fast_ui, "hierarchy", return_value=nodes) as poll:
            entry = fast_ui.step_labels("SIM", "01_tap_label", 3.456, None)
        poll.assert_called_once()
        self.assertEqual(entry, {"step": "01_tap_label", "at": 3.46, "labels": ["Title"]})

    def test_step_labels_reuse_dump_and_survive_axe_failure(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy") as poll:
            entry = fast_ui.step_labels("SIM", "02_dump", 1.0, {"labels": ["A"]})
        poll.assert_not_called()
        self.assertEqual(entry["labels"], ["A"])
        with mock.patch.object(fast_ui, "hierarchy", side_effect=fast_ui.FastUIError("x")):
            self.assertEqual(fast_ui.step_labels("SIM", "03_wait", 1.0, None)["labels"], [])


class AxeErrorTests(unittest.TestCase):
    def test_automation_session_timeout_is_retried_once(self) -> None:
        timed_out = mock.Mock(returncode=1, stdout="", stderr="Error: Timed out creating the simulator remote automation session")
        good = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(fast_ui, "command", side_effect=[timed_out, good]) as run, \
                mock.patch.object(fast_ui.time, "sleep"):
            self.assertIs(fast_ui.axe("SIM", "tap", "-x", "1", "-y", "1"), good)
        self.assertEqual(run.call_count, 2)
        with mock.patch.object(fast_ui, "command", side_effect=[timed_out, timed_out]), \
                mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaisesRegex(fast_ui.FastUIError, "automation session"):
                fast_ui.axe("SIM", "tap", "-x", "1", "-y", "1")

    def test_simulatorkit_failure_suggests_developer_dir(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="dyld: Library not loaded: /Library/Developer/PrivateFrameworks/SimulatorKit.framework")
        with mock.patch.object(fast_ui, "command", return_value=failed):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.axe("SIM", "describe-ui")
        self.assertIn("AXe 1.8.0", str(caught.exception))

    def test_other_failure_has_no_hint(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="element not found")
        with mock.patch.object(fast_ui, "command", return_value=failed):
            with self.assertRaises(fast_ui.FastUIError) as caught:
                fast_ui.axe("SIM", "tap")
        self.assertNotIn("DEVELOPER_DIR", str(caught.exception))
        self.assertIn("element not found", str(caught.exception))
        with mock.patch.object(fast_ui, "command", return_value=failed):
            self.assertIs(fast_ui.axe("SIM", "tap", check=False), failed)


class ReportVariantTests(unittest.TestCase):
    def test_variants_are_separate_rows_under_their_base_flow(self) -> None:
        import fast_ui_report
        base = {"simulator": "SIM1XXXX", "status": "pass", "total_seconds": 5.0, "_stamp": "1", "_dir": "d"}
        runs = [{**base, "task": task} for task in ("home@dark@xxl", "home-b", "home", "home@dark")]
        cells = fast_ui_report.latest_per_cell(runs)
        text = fast_ui_report.render(cells, {key: 1 for key in cells})
        rows = [line.split(" | ")[0].lstrip("| ") for line in text.splitlines()[2:6]]
        self.assertEqual(rows, ["home", "home@dark", "home@dark@xxl", "home-b"])
        self.assertIn("4/4 PASS", text)


@unittest.skipUnless(shutil.which("swiftc"), "needs swiftc (Xcode)")
class RunnerLogicTests(unittest.TestCase):
    def test_device_runner_prompt_rules_and_settling(self) -> None:
        here = Path(__file__).resolve().parent
        runner = here.parent / "device-runner" / "UITests"
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / "checks"
            build = subprocess.run(
                ["swiftc", "-parse-as-library", str(runner / "PromptRules.swift"), str(runner / "Settle.swift"), str(runner / "Trail.swift"), str(runner / "Gestures.swift"), str(runner / "Requests.swift"),
                 str(here / "runner_logic_checks.swift"), "-o", str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            checks = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(checks.returncode, 0, checks.stdout + checks.stderr)


class ShutdownTests(unittest.TestCase):
    def run_shutdown(self, *argv: str) -> tuple[list[list[str]], str]:
        listed = {"devices": {"iOS-27": [
            {"udid": "A", "name": "iPhone 17", "state": "Booted"},
            {"udid": "B", "name": "iPhone 17e", "state": "Booted"},
        ]}}
        calls: list[list[str]] = []

        def fake(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            stdout = json.dumps(listed) if args[:3] == ["xcrun", "simctl", "list"] else ""
            return subprocess.CompletedProcess(args, 0, stdout, "")

        out = io.StringIO()
        with mock.patch.object(fast_ui, "command", side_effect=fake), contextlib.redirect_stdout(out):
            self.assertEqual(fast_ui.run_shutdown(list(argv)), 0)
        return [call for call in calls if call[:3] == ["xcrun", "simctl", "shutdown"]], out.getvalue()

    def test_shuts_down_every_booted_simulator(self) -> None:
        shutdowns, out = self.run_shutdown()
        self.assertEqual(shutdowns, [["xcrun", "simctl", "shutdown", "A"], ["xcrun", "simctl", "shutdown", "B"]])
        self.assertIn("SHUTDOWN simulators=iPhone 17 (A), iPhone 17e (B)", out)

    def test_simulator_flag_spares_the_others(self) -> None:
        shutdowns, out = self.run_shutdown("--simulator", "B")
        self.assertEqual(shutdowns, [["xcrun", "simctl", "shutdown", "B"]])
        _, none = self.run_shutdown("--simulator", "Z")
        self.assertIn("SHUTDOWN none booted", none)


def pbxproj_with(script: str, name: str = "SwiftLint") -> str:
    escaped = script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return (
        "\t\tAA /* Lint */ = {\n\t\t\tisa = PBXShellScriptBuildPhase;\n"
        f"\t\t\tname = {name};\n\t\t\tshellScript = \"{escaped}\";\n\t\t}};\n"
    )


class LintTests(unittest.TestCase):
    def report(self, script: str) -> tuple[str, list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "App.xcodeproj"
            project.mkdir()
            (project / "project.pbxproj").write_text(pbxproj_with(script))
            return fast_ui.lint_report(project)

    def test_plain_swiftlint_is_shimmed(self) -> None:
        self.assertEqual(self.report("if which swiftlint >/dev/null; then\n  swiftlint\nfi\n"), ("shimmed", ["SwiftLint"]))

    def test_path_prepend_is_shimmed_in_sh_and_reported_in_zsh(self) -> None:
        script = 'if [[ "$(uname -m)" == arm64 ]]; then\n  export PATH="/opt/homebrew/bin:$PATH"\nfi\nswiftlint\n'
        self.assertEqual(self.report(script), ("shimmed", ["SwiftLint"]))
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "App.xcodeproj"
            project.mkdir()
            (project / "project.pbxproj").write_text(
                pbxproj_with(script).replace("isa = PBXShellScriptBuildPhase;", "isa = PBXShellScriptBuildPhase;\n\t\t\tshellPath = /bin/zsh;"))
            state, detail = fast_ui.lint_report(project)
        self.assertEqual(state, "not-shimmable")
        self.assertIn("prepends to PATH", detail[0])

    def test_the_function_beats_a_path_prepend(self) -> None:
        """What a sh run-script phase in SwiftLint's README form runs under a skipped build."""
        with tempfile.TemporaryDirectory() as raw:
            real = Path(raw) / "swiftlint"
            real.write_text("#!/bin/sh\necho REAL; exit 2\n")
            real.chmod(0o755)
            env = fast_ui.lint_skip_env({"PATH": "/usr/bin:/bin"})
            result = subprocess.run(
                ["/bin/sh", "-c", f'export PATH="{raw}:$PATH"\nif which swiftlint >/dev/null; then swiftlint; fi'],
                capture_output=True, text=True, env=env,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("swiftlint skipped", result.stdout)

    def test_absolute_path_is_reported(self) -> None:
        state, detail = self.report('"${PODS_ROOT}/SwiftLint/swiftlint"\n')
        self.assertEqual(state, "not-shimmable")
        self.assertIn("by path", detail[0])

    def test_build_tool_plugin_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "App.xcodeproj"
            project.mkdir()
            (project / "project.pbxproj").write_text("productName = SwiftLintBuildToolPlugin;\n")
            state, detail = fast_ui.lint_report(project)
        self.assertEqual(state, "not-shimmable")
        self.assertIn("build-tool plugin", detail[0])

    def test_workspace_reads_its_projects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "App.xcodeproj").mkdir()
            (root / "App.xcodeproj/project.pbxproj").write_text(pbxproj_with("swiftlint\n"))
            (root / "App.xcworkspace").mkdir()
            (root / "App.xcworkspace/contents.xcworkspacedata").write_text(
                '<Workspace><FileRef location = "group:App.xcodeproj"></FileRef></Workspace>')
            self.assertEqual(fast_ui.lint_report(root / "App.xcworkspace"), ("shimmed", ["SwiftLint"]))

    def test_no_lint_phase(self) -> None:
        self.assertEqual(self.report("echo hi\n"), ("none", []))

    def test_a_commented_out_swiftlint_is_no_phase(self) -> None:
        self.assertEqual(self.report("# swiftlint\necho hi\n"), ("none", []))

    def test_build_failure_names_lint_and_first_error(self) -> None:
        output = "A.swift:3:1: error: Line Length Violation: too long (line_length)\n** BUILD FAILED **\n"
        message = fast_ui.build_failure(output, "on", ["SwiftLint"])
        self.assertIn("SwiftLint", message)
        self.assertIn("first error: A.swift:3:1: error: Line Length Violation", message)
        self.assertIn('remove "skip_lint": false', message)

    def test_build_failure_names_the_failing_phase_and_counts_errors(self) -> None:
        output = (
            "/src/A.swift:1:1: error: cannot find 'x' in scope\n"
            "/src/B.swift:2:1: error: missing return\n"
            "** BUILD FAILED **\n\nThe following build commands failed:\n"
            "\tSwiftCompile normal arm64 /src/A.swift (in target 'App' from project 'App')\n"
            "\tPhaseScriptExecution Generate\\ Assets /dd/Script.sh (in target 'App' from project 'App')\n"
            "(2 failures)\n"
        )
        message = fast_ui.build_failure(output, "none", [])
        self.assertIn("in SwiftCompile normal arm64, run-script phase 'Generate Assets'", message)
        self.assertIn("first error: /src/A.swift:1:1: error: cannot find 'x' in scope (+1 more)", message)
        self.assertNotIn("hint:", message)

    def test_build_failure_without_error_lines_keeps_the_tail(self) -> None:
        message = fast_ui.build_failure("line 1\nline 2\n** BUILD FAILED **\n", "none", [])
        self.assertIn("| line 1 | line 2 | ** BUILD FAILED **", message)

    def test_skip_lint_default_and_overrides(self) -> None:
        with mock.patch.dict(fast_ui.os.environ, {}, clear=True):
            self.assertTrue(fast_ui.skip_lint_enabled({}))
            self.assertFalse(fast_ui.skip_lint_enabled({"skip_lint": False}))
        with mock.patch.dict(fast_ui.os.environ, {"FAST_UI_SKIP_LINT": "0"}):
            self.assertFalse(fast_ui.skip_lint_enabled({"skip_lint": True}))
        with mock.patch.dict(fast_ui.os.environ, {"FAST_UI_SKIP_LINT": "1"}):
            self.assertTrue(fast_ui.skip_lint_enabled({"skip_lint": False}))

    def test_shim_is_executable_noop(self) -> None:
        shim = fast_ui.SHIM_DIR / "swiftlint"
        result = subprocess.run([str(shim), "lint", "--strict"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("swiftlint skipped", result.stdout)

    def build(self, app_extra: dict, environ: dict[str, str], returncode: int = 0,
              output: str = "") -> tuple[mock.MagicMock, str]:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            app = {**app_spec(tmp, "SIM"), **app_extra}
            (tmp / "App.xcodeproj").mkdir()
            (tmp / "App.xcodeproj/project.pbxproj").write_text(pbxproj_with("swiftlint\n"))
            result = subprocess.CompletedProcess([], returncode, output, "")
            err = io.StringIO()
            with mock.patch.object(fast_ui, "command", return_value=result) as command, \
                    mock.patch.object(fast_ui, "verify_app"), mock.patch.dict(fast_ui.os.environ, environ), \
                    contextlib.redirect_stderr(err):
                try:
                    fast_ui.build_if_needed(app, tmp, "SIM", tmp / "App.app", tmp / "dd")
                    log = ""
                except fast_ui.FastUIError as error:
                    log = (tmp / "dd/.fastui-build.log").read_text()
                    err.write(f"\nERROR {error}")
            return command, err.getvalue() + log

    def test_build_runs_with_the_shim_and_reports_lint_state(self) -> None:
        command, err = self.build({}, {"FAST_UI_SKIP_LINT": "1"})
        env = command.call_args.kwargs["env"]
        self.assertTrue(env["PATH"].startswith(str(fast_ui.SHIM_DIR)))
        self.assertIn("swiftlint", env["BASH_FUNC_swiftlint%%"])
        self.assertIn("lint=shimmed (SwiftLint)", err)

    def test_build_with_lint_on_keeps_the_path(self) -> None:
        command, err = self.build({"skip_lint": False}, {"FAST_UI_SKIP_LINT": "0", "PATH": "/usr/bin"})
        env = command.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("BASH_FUNC_swiftlint%%", env)
        self.assertIn("lint=on", err)

    def test_failed_build_says_why_and_keeps_the_log(self) -> None:
        output = "/src/A.swift:1:1: error: cannot find 'x' in scope\n** BUILD FAILED **\n"
        _, err = self.build({}, {"FAST_UI_SKIP_LINT": "1"}, returncode=65, output=output)
        self.assertIn("ERROR build failed first error: /src/A.swift:1:1: error: cannot find 'x' in scope", err)
        self.assertIn(".fastui-build.log", err)
        self.assertIn("cannot find 'x' in scope", err.split("ERROR")[1])


def frame(x: float, y: float, w: float, h: float) -> dict:
    return {"x": x, "y": y, "width": w, "height": h}


# Shapes measured with AXe on iOS 27: a notification prompt replaces the tree with
# SpringBoard's (root label " "); an in-app alert keeps the app's root.
SYSTEM_PROMPT = [
    {"type": "Application", "AXLabel": " ", "frame": frame(0, 0, 402, 874)},
    {"type": "Sheet", "AXLabel": "“App” Would Like to Send You Notifications", "frame": frame(41, 344, 320, 214)},
    {"type": "Button", "AXLabel": "Don’t Allow", "frame": frame(57, 494, 140, 48)},
    {"type": "Button", "AXLabel": "Allow", "frame": frame(205, 494, 140, 48)},
]
IN_APP_ALERT = [
    {"type": "Application", "AXLabel": "App", "frame": frame(0, 0, 402, 874)},
    {"type": "Sheet", "AXLabel": "Confirm", "frame": frame(41, 388, 320, 126)},
    {"type": "Button", "AXLabel": "OK", "frame": frame(205, 450, 140, 48)},
]
IN_APP_ALLOW_BUTTON = [
    {"type": "Application", "AXLabel": "App", "frame": frame(0, 0, 402, 874)},
    {"type": "Button", "AXLabel": "Allow", "AXUniqueId": "inapp_allow", "frame": frame(16, 208, 370, 52)},
]


class SystemAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        fast_ui.AUTO_TAPPED.clear()

    def run_action(self, action: dict, prompts: list | None = None) -> dict | None:
        return fast_ui.execute_action(action, "SIM", Path("/tmp"), 1, prompts or [])

    def test_system_alert_told_apart_from_in_app(self) -> None:
        self.assertEqual(fast_ui.system_alert(SYSTEM_PROMPT)["type"], "Sheet")
        self.assertIsNone(fast_ui.system_alert(IN_APP_ALERT))
        self.assertIsNone(fast_ui.system_alert(IN_APP_ALLOW_BUTTON))

    def test_tap_alert_ignores_plain_in_app_button(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=IN_APP_ALLOW_BUTTON), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(fast_ui.time, "perf_counter", side_effect=itertools.count(0, 1)):
            result = self.run_action({"type": "tap_alert", "button": "Allow", "timeout": 3, "optional": True})
            with self.assertRaisesRegex(fast_ui.FastUIError, "no alert with a 'Allow' button"):
                self.run_action({"type": "tap_alert", "button": "Allow", "timeout": 3})
        tap.assert_not_called()
        self.assertEqual(result, {"skipped": True})

    def test_tap_alert_taps_system_and_in_app_alerts(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=SYSTEM_PROMPT), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            result = self.run_action({"type": "tap_alert", "button": "Allow"})
        tap.assert_called_once_with("SIM", 275.0, 518.0, 0.15)
        self.assertEqual(result, {"alert": "“App” Would Like to Send You Notifications", "system": True})
        with mock.patch.object(fast_ui, "hierarchy", return_value=IN_APP_ALERT), mock.patch.object(fast_ui, "tap_point"):
            self.assertFalse(self.run_action({"type": "tap_alert", "button": "OK"})["system"])

    def test_tap_alert_system_true_skips_an_in_app_alert(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=IN_APP_ALERT), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(fast_ui.time, "perf_counter", side_effect=itertools.count(0, 1)):
            result = self.run_action({"type": "tap_alert", "button": "OK", "system": True, "timeout": 3, "optional": True})
        tap.assert_not_called()
        self.assertEqual(result, {"skipped": True})

    def test_tap_alert_returns_at_once_when_auto_tap_pressed_it(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[SYSTEM_PROMPT, IN_APP_ALLOW_BUTTON]), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            result = self.run_action({"type": "tap_alert", "button": "Allow", "timeout": 3, "optional": True}, ["Allow"])
        tap.assert_called_once()  # the auto-tap; the in-app Allow is never pressed
        self.assertTrue(result["auto_tapped"])

    def test_auto_tap_only_inside_system_alert(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=IN_APP_ALLOW_BUTTON), \
                mock.patch.object(fast_ui, "tap_point") as tap:
            fast_ui.wait_nodes("SIM", lambda nodes: True, 1, ["Allow"])
        tap.assert_not_called()

    def test_a_button_that_stays_is_tapped_at_most_four_times(self) -> None:
        with mock.patch.object(fast_ui, "hierarchy", return_value=SYSTEM_PROMPT), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(fast_ui.time, "perf_counter", side_effect=itertools.count(0, 0.5)):
            with self.assertRaises(fast_ui.FastUIError):
                fast_ui.wait_nodes("SIM", lambda nodes: False, 3, ["Allow"])
        self.assertEqual(tap.call_count, fast_ui.PROMPT_TAPS)

    def test_tap_alert_is_guarded_in_a_multi_step_request(self) -> None:
        import fast_guard
        self.assertEqual(fast_guard.risky_step([{"type": "tap_label", "label": "Edit"},
                                                {"type": "tap_alert", "button": "Delete"}]), (2, "Delete"))
        self.assertIsNone(fast_guard.risky_step([{"type": "tap_label", "label": "Edit"},
                                                 {"type": "tap_alert", "button": "Delete", "confirm": True}]))


class ControlTests(unittest.TestCase):
    """set_switch, assert_value, assert_count, dismiss_keyboard, pull_to_refresh, `enabled`."""

    def run_action(self, action: dict) -> dict | None:
        return fast_ui.execute_action(action, "SIM", Path("/tmp"), 1, [], {}, "com.x")

    @staticmethod
    def node(**fields) -> dict:
        box = fields.pop("frame", {"x": 0, "y": 100, "width": 100, "height": 40})
        return {"type": "Button", "frame": box, **fields}

    def test_set_switch_taps_only_when_the_value_differs(self) -> None:
        off = self.node(AXLabel="Wi-Fi", type="CheckBox", AXValue=0, frame={"x": 0, "y": 100, "width": 300, "height": 40})
        on = {**off, "AXValue": 1}
        with mock.patch.object(fast_ui, "hierarchy", return_value=[on]), mock.patch.object(fast_ui, "axe") as axe:
            self.assertEqual(self.run_action({"type": "set_switch", "label": "Wi-Fi", "on": True}), {"toggled": False})
        axe.assert_not_called()
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[[off], [on]]), \
                mock.patch.object(fast_ui, "axe") as axe, mock.patch.object(fast_ui.time, "sleep"):
            self.assertEqual(self.run_action({"type": "set_switch", "label": "Wi-Fi", "on": True}), {"toggled": True})
        # A wide (SwiftUI row) switch is touched near its trailing edge, held long enough to flip.
        axe.assert_called_once_with("SIM", "touch", "-x", "264.00", "-y", "120.00", "--down", "--up", "--delay", "0.3")

    def test_set_switch_touches_again_once_when_the_first_touch_was_dropped(self) -> None:
        off = self.node(AXLabel="Wi-Fi", type="CheckBox", AXValue="0", frame={"x": 0, "y": 100, "width": 300, "height": 40})
        on = {**off, "AXValue": "1"}
        clock = itertools.count(0, 0.5)
        # Dropped under load (measured: 1 of 8 touches on a loaded Mac): the value stays 0 for 2 s.
        trees = [[off]] + [[off]] * 5 + [[on]]
        with mock.patch.object(fast_ui, "hierarchy", side_effect=trees), \
                mock.patch.object(fast_ui, "axe") as axe, mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(fast_ui.time, "perf_counter", side_effect=lambda: next(clock)):
            self.assertEqual(self.run_action({"type": "set_switch", "label": "Wi-Fi", "on": True}),
                             {"toggled": True, "touches": 2})
        self.assertEqual(axe.call_count, 2)

    def test_set_switch_that_does_not_flip_fails(self) -> None:
        off = self.node(AXLabel="Wi-Fi", type="Switch", AXValue="0")
        with mock.patch.object(fast_ui, "hierarchy", return_value=[off]), mock.patch.object(fast_ui, "axe"), \
                mock.patch.object(fast_ui.time, "sleep"):
            with self.assertRaisesRegex(fast_ui.FastUIError, "switch did not turn on"):
                self.run_action({"type": "set_switch", "label": "Wi-Fi", "on": True, "timeout": 0.2})
        with self.assertRaisesRegex(fast_ui.FastUIError, "needs 'on'|missing 'on'"):
            self.run_action({"type": "set_switch", "label": "Wi-Fi"})

    def test_set_switch_ignores_a_label_that_is_not_a_switch(self) -> None:
        caption = self.node(AXLabel="Wi-Fi", type="StaticText")
        switch = self.node(AXLabel="Wi-Fi", type="Switch", AXValue="1", frame={"x": 300, "y": 100, "width": 51, "height": 31})
        with mock.patch.object(fast_ui, "hierarchy", return_value=[caption, switch]), \
                mock.patch.object(fast_ui, "axe") as axe:
            self.assertEqual(self.run_action({"type": "set_switch", "label": "Wi-Fi", "on": True}), {"toggled": False})
        axe.assert_not_called()

    def test_assert_value_reads_numeric_zero_and_explains_a_miss(self) -> None:
        field = self.node(AXUniqueId="count", AXValue=0)
        with mock.patch.object(fast_ui, "hierarchy", return_value=[field]), mock.patch.object(fast_ui.time, "sleep"):
            self.run_action({"type": "assert_value", "id": "count", "equals": "0"})
            with self.assertRaisesRegex(fast_ui.FastUIError, "value of the Button is '0', expected '7'"):
                self.run_action({"type": "assert_value", "id": "count", "equals": "7", "timeout": 0.2})
            with self.assertRaisesRegex(fast_ui.FastUIError, "needs equals"):
                self.run_action({"type": "assert_value", "id": "count"})
            self.run_action({"type": "assert_value", "id": "count", "not_equals": "1", "matches": "^[0-9]$"})
            with self.assertRaisesRegex(fast_ui.FastUIError, "no node matched the selector"):
                self.run_action({"type": "assert_value", "id": "total", "equals": "0", "timeout": 0.2})
            with self.assertRaisesRegex(fast_ui.FastUIError, "attribute must be value or label"):
                self.run_action({"type": "assert_value", "id": "count", "attribute": "frame", "equals": "0"})

    def test_assert_value_reads_the_node_that_has_the_value(self) -> None:
        """Measured in Settings on iOS 27: the row's Group, the CheckBox and a Button share the id."""
        row = self.node(AXUniqueId="BUTTON_SHAPES", type="Group", AXValue=None,
                        frame={"x": 20, "y": 240, "width": 360, "height": 52})
        switch = self.node(AXUniqueId="BUTTON_SHAPES", type="CheckBox", AXValue="1",
                           frame={"x": 36, "y": 252, "width": 330, "height": 28})
        with mock.patch.object(fast_ui, "hierarchy", return_value=[row, switch]), mock.patch.object(fast_ui.time, "sleep"):
            self.run_action({"type": "assert_value", "id": "BUTTON_SHAPES", "equals": "1"})
            with self.assertRaisesRegex(fast_ui.FastUIError, r"value of the CheckBox is '1', expected '0'"):
                self.run_action({"type": "assert_value", "id": "BUTTON_SHAPES", "equals": "0", "timeout": 0.2})

    def test_assert_value_checks_the_top_left_match(self) -> None:
        lower = self.node(AXLabel="$5", type="StaticText", frame={"x": 0, "y": 500, "width": 50, "height": 20})
        upper = self.node(AXLabel="$9", type="StaticText", frame={"x": 0, "y": 80, "width": 50, "height": 20})
        with mock.patch.object(fast_ui, "hierarchy", return_value=[lower, upper]), mock.patch.object(fast_ui.time, "sleep"):
            self.run_action({"type": "assert_value", "label_contains": "$", "attribute": "label", "equals": "$9"})
            with self.assertRaisesRegex(fast_ui.FastUIError, r"label of the StaticText is '\$9', expected it to contain '\$5'"):
                self.run_action({"type": "assert_value", "label_contains": "$", "attribute": "label",
                                 "contains": "$5", "timeout": 0.2})

    def test_assert_count_counts_each_box_once(self) -> None:
        row = self.node(AXLabel="Row 1", type="StaticText")
        nested = {**row}
        other = self.node(AXLabel="Row 2", type="StaticText", frame={"x": 0, "y": 200, "width": 100, "height": 40})
        with mock.patch.object(fast_ui, "hierarchy", return_value=[row, nested, other]), \
                mock.patch.object(fast_ui.time, "sleep"):
            self.assertEqual(self.run_action({"type": "assert_count", "label_contains": "Row", "count": 2}), {"count": 2})
            self.assertEqual(self.run_action({"type": "assert_count", "label_contains": "Row", "max": 2}), {"count": 2})
            with self.assertRaisesRegex(fast_ui.FastUIError, "2 nodes matched, expected at least 3"):
                self.run_action({"type": "assert_count", "label_contains": "Row", "min": 3, "timeout": 0.2})
            with self.assertRaisesRegex(fast_ui.FastUIError, "needs count, min or max"):
                self.run_action({"type": "assert_count", "label_contains": "Row"})
            self.assertEqual(self.run_action({"type": "assert_count", "label": "Gone", "count": 0}), {"count": 0})

    def test_enabled_selector_key(self) -> None:
        self.assertTrue(fast_ui.matches({"AXLabel": "Pay", "enabled": False}, {"label": "Pay", "enabled": False}))
        self.assertFalse(fast_ui.matches({"AXLabel": "Pay", "enabled": True}, {"label": "Pay", "enabled": False}))
        self.assertTrue(fast_ui.matches({"AXLabel": "Pay"}, {"label": "Pay", "enabled": True}))
        self.assertEqual(fast_ui.compact_node({"AXLabel": "Pay", "enabled": False})["enabled"], False)
        self.assertNotIn("enabled", fast_ui.compact_node({"AXLabel": "Pay", "enabled": True}))

    def test_dismiss_keyboard_sends_return_until_the_keys_are_gone(self) -> None:
        key = self.node(AXUniqueId="Return")
        with mock.patch.object(fast_ui, "hierarchy", side_effect=[[key], []]), \
                mock.patch.object(fast_ui, "axe") as axe, mock.patch.object(fast_ui.time, "sleep"):
            self.run_action({"type": "dismiss_keyboard"})
        self.assertEqual(axe.call_count, 2)
        self.assertIn(f"key {fast_ui.KEY_CODES['return']}", axe.call_args.args)

    def test_dismiss_keyboard_taps_the_named_button(self) -> None:
        done = self.node(AXLabel="Done", frame={"x": 300, "y": 400, "width": 60, "height": 44})
        with mock.patch.object(fast_ui, "hierarchy", return_value=[done]), \
                mock.patch.object(fast_ui, "tap_point") as tap, mock.patch.object(fast_ui, "axe") as axe:
            self.run_action({"type": "dismiss_keyboard", "button": "Done"})
        tap.assert_called_once_with("SIM", 330.0, 422.0, 0.15)
        axe.assert_not_called()

    def test_pull_to_refresh_drags_down_the_screen_or_the_named_list(self) -> None:
        app = {"AXLabel": "App", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}}
        listing = self.node(AXUniqueId="feed", type="Other", frame={"x": 0, "y": 200, "width": 400, "height": 400})
        with mock.patch.object(fast_ui, "hierarchy", return_value=[app, listing]), \
                mock.patch.object(fast_ui, "drag") as drag:
            self.run_action({"type": "pull_to_refresh"})
            self.assertEqual(drag.call_args.args[1:4], ([200.0, 200.0], [200.0, 600.0], 0))
            self.run_action({"type": "pull_to_refresh", "id": "feed"})
            self.assertEqual(drag.call_args.args[1:3], ([200.0, 300.0], [200.0, 500.0]))

    def test_new_kinds_settle_and_join_the_map(self) -> None:
        for kind in ("set_switch", "pull_to_refresh", "tap_alert"):
            self.assertIn(kind, fast_ui.GESTURE_KINDS)
        self.assertIn("dismiss_keyboard", fast_ui.TRAIL_GESTURES)


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        locks = tempfile.TemporaryDirectory()
        self.addCleanup(locks.cleanup)
        patcher = mock.patch.object(fast_ui, "LOCK_DIR", Path(locks.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_run_on_same_simulator_is_busy(self) -> None:
        first = fast_ui.SimulatorLease("SIM", Path("a.json"))
        first.acquire(0)
        with self.assertRaisesRegex(fast_ui.FastUIError, r"simulator busy: SIM is running a\.json \(pid \d+"):
            fast_ui.SimulatorLease("SIM", Path("b.json")).acquire(0)
        with self.assertRaisesRegex(fast_ui.FastUIError, "simulator busy"):
            fast_ui.ensure_not_busy("SIM")
        other = fast_ui.SimulatorLease("OTHER", Path("c.json"))
        other.acquire(0)
        other.release(clean=True)
        first.release(clean=True)
        fast_ui.ensure_not_busy("SIM")
        second = fast_ui.SimulatorLease("SIM", Path("b.json"))
        second.acquire(0)
        second.release(clean=True)

    def test_a_clean_release_needs_no_repair(self) -> None:
        first = fast_ui.SimulatorLease("SIM", Path("a.json"))
        first.acquire(0)
        first.touch("bundle_id", "com.example.app")
        first.release(clean=True)
        with mock.patch.object(fast_ui, "command") as command, mock.patch.object(fast_ui, "stop_stale_recorder"):
            following = fast_ui.SimulatorLease("SIM", Path("b.json"))
            following.acquire(0)
            following.release(clean=True)
        command.assert_not_called()
        self.assertEqual(following.repaired, [])

    def test_killed_run_is_repaired_by_the_next(self) -> None:
        err = io.StringIO()
        with mock.patch.object(fast_ui, "command") as command, \
                mock.patch.object(fast_ui, "restore_ui_settings") as restore, \
                mock.patch.object(fast_ui, "stop_stale_recorder"), contextlib.redirect_stderr(err):
            killed = fast_ui.SimulatorLease("SIM", Path("a.json"))
            killed.acquire(0)
            killed.touch("ui", {"appearance": "light"})
            killed.touch("bundle_id", "com.example.app")
            killed.touch("preferences", ["isPremium"])
            killed.release(clean=False)
            following = fast_ui.SimulatorLease("SIM", Path("b.json"))
            following.acquire(0)
            following.release(clean=True)
        issued = [call.args[0] for call in command.call_args_list]
        restore.assert_called_once_with("SIM", {"appearance": "light"})
        self.assertIn(["xcrun", "simctl", "terminate", "SIM", "com.example.app"], issued)
        self.assertIn(["xcrun", "simctl", "spawn", "SIM", "defaults", "delete", "com.example.app", "isPremium"], issued)
        self.assertEqual(following.repaired, ["ui", "bundle_id", "preferences"])
        self.assertIn("WARN dirty=1", err.getvalue())

    def test_teardown_undoes_only_what_the_run_changed(self) -> None:
        with mock.patch.object(fast_ui, "command") as command, mock.patch.object(fast_ui, "restore_ui_settings") as restore:
            fast_ui.teardown("SIM", "com.example.app", {"appearance": "light"}, {"isPremium": True}, False, False)
        restore.assert_called_once_with("SIM", {"appearance": "light"})
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ["xcrun", "simctl", "terminate", "SIM", "com.example.app"],
            ["xcrun", "simctl", "spawn", "SIM", "defaults", "delete", "com.example.app", "isPremium"],
        ])
        with mock.patch.object(fast_ui, "command") as command, mock.patch.object(fast_ui, "restore_ui_settings"):
            fast_ui.teardown("SIM", "com.example.app", {}, {"isPremium": True}, True, False)
        self.assertEqual(command.call_args_list[-1].args[0],
                         ["xcrun", "simctl", "spawn", "SIM", "defaults", "delete", "com.example.app"])
        with mock.patch.object(fast_ui, "command") as command, mock.patch.object(fast_ui, "restore_ui_settings") as restore:
            fast_ui.teardown("SIM", "com.example.app", {"appearance": "light"}, {"isPremium": True}, False, True)
        command.assert_not_called()
        restore.assert_called_once()

    def test_teardown_puts_back_keys_a_kept_install_already_had(self) -> None:
        with mock.patch.object(fast_ui, "command") as command, mock.patch.object(fast_ui, "restore_ui_settings"):
            fast_ui.teardown("SIM", "com.example.app", {}, {"isPremium": True, "theme": "dark"}, False, False,
                             {"isPremium": "<false/>"})
        self.assertEqual([call.args[0] for call in command.call_args_list][1:], [
            ["xcrun", "simctl", "spawn", "SIM", "defaults", "write", "com.example.app", "isPremium", "<false/>"],
            ["xcrun", "simctl", "spawn", "SIM", "defaults", "delete", "com.example.app", "theme"],
        ])

    def test_saved_preferences_reads_only_the_seeded_keys_that_exist(self) -> None:
        domain = plistlib.dumps({"isPremium": False, "profile": {"name": "A", "ids": [1, 2]}, "other": 1}).decode()
        with mock.patch.object(fast_ui, "command", return_value=subprocess.CompletedProcess([], 0, domain, "")):
            saved = fast_ui.saved_preferences("SIM", "com.example.app", ["isPremium", "profile", "missing"])
        self.assertEqual(saved["isPremium"], "<false/>")
        self.assertEqual(plistlib.loads(plistlib.dumps("x").replace(b"<string>x</string>", saved["profile"].encode())),
                         {"name": "A", "ids": [1, 2]})
        self.assertNotIn("missing", saved)
        with mock.patch.object(fast_ui, "command", return_value=subprocess.CompletedProcess([], 1, "", "no domain")):
            self.assertEqual(fast_ui.saved_preferences("SIM", "com.example.app", ["isPremium"]), {})

    def test_repair_puts_back_saved_values(self) -> None:
        with mock.patch.object(fast_ui, "command") as command, mock.patch.object(fast_ui, "restore_ui_settings"), \
                mock.patch.object(fast_ui, "stop_stale_recorder"), contextlib.redirect_stderr(io.StringIO()):
            killed = fast_ui.SimulatorLease("SIM", Path("a.json"))
            killed.acquire(0)
            killed.touch("bundle_id", "com.example.app")
            killed.touch("preferences", ["isPremium"])
            killed.touch("preferences_saved", {"isPremium": "<false/>"})
            killed.release(clean=False)
            following = fast_ui.SimulatorLease("SIM", Path("b.json"))
            following.acquire(0)
            following.release(clean=True)
        self.assertIn(["xcrun", "simctl", "spawn", "SIM", "defaults", "write", "com.example.app", "isPremium", "<false/>"],
                      [call.args[0] for call in command.call_args_list])


class RunLifecycleTests(unittest.TestCase):
    """main() with a lease: one run per simulator, and what a run changed is undone."""

    TREE = json.dumps([{"type": "Application", "AXLabel": "Demo", "frame": {"x": 0, "y": 0, "width": 402, "height": 874},
                        "children": [{"type": "Button", "AXLabel": "Go", "frame": {"x": 10, "y": 100, "width": 80, "height": 40}}]}])

    def setUp(self) -> None:
        locks = tempfile.TemporaryDirectory()
        self.addCleanup(locks.cleanup)
        patcher = mock.patch.object(fast_ui, "LOCK_DIR", Path(locks.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, actions: list, setup: dict | None = None, argv: tuple[str, ...] = (),
                 axe: object = None) -> tuple[int, list[list[str]], str, str]:
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "home.json"
            flow.write_text(json.dumps({"version": 1, "name": "home", "actions": actions, "setup": setup or {},
                                        "app": {"bundle_id": "com.example.app", "prebuilt": True, "simulator": "SIM",
                                                "project_root": raw}}))
            calls: list[list[str]] = []
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(fast_ui.sys, "argv", ["fastSimulatorTest", str(flow), *argv]), \
                    mock.patch.object(fast_ui, "prepare_app", return_value=(None, False)), \
                    mock.patch.object(fast_ui, "command", side_effect=lambda args, **_: calls.append(args) or
                                      subprocess.CompletedProcess(args, 0, getattr(self, "export", "") if "export" in args else "", "")), \
                    mock.patch.object(fast_ui, "record"), mock.patch.object(fast_ui, "screenshot"), \
                    mock.patch.object(fast_ui, "axe", side_effect=axe or FakeAxe(self.TREE)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = fast_ui.main()
        return code, calls, out.getvalue(), err.getvalue()

    def test_busy_simulator_is_refused_before_anything_runs(self) -> None:
        holder = fast_ui.SimulatorLease("SIM", Path("other.json"))
        holder.acquire(0)
        self.addCleanup(holder.release, True)
        code, calls, _, err = self.run_main([{"type": "wait", "label": "Go"}])
        self.assertEqual(code, 3)
        self.assertIn("BUSY task=home error=simulator busy: SIM is running other.json", err)
        self.assertEqual(calls, [])

    def test_pass_terminates_the_app_and_deletes_seeded_keys(self) -> None:
        code, calls, out, _ = self.run_main([{"type": "wait", "label": "Go"}],
                                            {"fresh_install": False, "preferences": {"isPremium": True}})
        self.assertEqual(code, 0, out)
        self.assertIn(["xcrun", "simctl", "launch", "--terminate-running-process", "SIM", "com.example.app"], calls)
        self.assertEqual(calls[-2:], [
            ["xcrun", "simctl", "terminate", "SIM", "com.example.app"],
            ["xcrun", "simctl", "spawn", "SIM", "defaults", "delete", "com.example.app", "isPremium"],
        ])
        # The lease was released clean: the next run takes it and repairs nothing.
        following = fast_ui.SimulatorLease("SIM", Path("next.json"))
        following.acquire(0)
        self.assertEqual(following.repaired, [])
        following.release(clean=True)

    def test_pass_restores_a_value_the_kept_install_had(self) -> None:
        self.export = plistlib.dumps({"isPremium": False}).decode()
        code, calls, out, _ = self.run_main([{"type": "wait", "label": "Go"}],
                                            {"fresh_install": False, "preferences": {"isPremium": True}})
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[-1], ["xcrun", "simctl", "spawn", "SIM", "defaults", "write", "com.example.app", "isPremium", "<false/>"])

    def test_relaunch_false_keeps_a_running_app(self) -> None:
        _, calls, _, _ = self.run_main([{"type": "wait", "label": "Go"}], {"fresh_install": False, "relaunch": False})
        self.assertIn(["xcrun", "simctl", "launch", "SIM", "com.example.app"], calls)

    def test_keep_state_leaves_the_app_running(self) -> None:
        _, calls, _, _ = self.run_main([{"type": "wait", "label": "Go"}], {"fresh_install": False}, ("--keep-state",))
        self.assertNotIn(["xcrun", "simctl", "terminate", "SIM", "com.example.app"], calls)

    def test_sigterm_mid_flow_still_tears_down(self) -> None:
        tree = FakeAxe(self.TREE)

        def axe(*args: str, **kwargs: object) -> object:
            if "touch" in args:
                fast_ui.raise_interrupted(fast_ui.signal.SIGTERM, None)
            return tree(*args, **kwargs)

        self.addCleanup(fast_ui.signal.signal, fast_ui.signal.SIGINT, fast_ui.signal.default_int_handler)
        code, calls, _, err = self.run_main([{"type": "tap_label", "label": "Go"}, {"type": "wait", "label": "Never"}],
                                            {"fresh_install": False, "preferences": {"isPremium": True}}, axe=axe)
        self.assertEqual(code, 130)
        self.assertIn("INTERRUPTED task=home step=01_tap_label", err)
        self.assertIn("error=interrupted by SIGTERM", err)
        self.assertIn(["xcrun", "simctl", "terminate", "SIM", "com.example.app"], calls)
        self.assertIn(["xcrun", "simctl", "spawn", "SIM", "defaults", "delete", "com.example.app", "isPremium"], calls)
        fast_ui.ensure_not_busy("SIM")

    def test_do_refuses_a_simulator_a_flow_holds(self) -> None:
        holder = fast_ui.SimulatorLease("SIM", Path("other.json"))
        holder.acquire(0)
        self.addCleanup(holder.release, True)
        err = io.StringIO()
        with mock.patch.object(fast_ui, "axe") as axe, contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = fast_ui.run_do(['{"type":"tap_label","label":"Go"}', "--simulator", "SIM"])
        self.assertEqual(code, 1)
        self.assertIn("simulator busy", err.getvalue())
        axe.assert_not_called()


class DeviceLauncherTests(unittest.TestCase):
    def test_save_dumps_writes_nodes_and_keeps_the_report_short(self) -> None:
        import fast_device

        nodes = [{"label": "More Info", "type": "Button", "frame": {"x": 350, "y": 120, "width": 44, "height": 44}}]
        results = [{"step": 2, "name": "wifi", "labels": ["More Info"], "nodes": nodes}, {"step": 3}]
        with tempfile.TemporaryDirectory() as raw:
            saved = fast_device.save_dumps(results, Path(raw))
            self.assertEqual(saved, [str(Path(raw) / "wifi-ui.json")])
            self.assertEqual(json.loads((Path(raw) / "wifi-ui.json").read_text()), nodes)
        self.assertNotIn("nodes", results[0])
        self.assertEqual(results[0]["path"], saved[0])

    def test_resolve_device_matches_partial_name_only_when_unique(self) -> None:
        import fast_device
        devices = [{"udid": "A", "name": "iPhone - Blue - 27", "os": "27.0", "connected": True},
                   {"udid": "B", "name": "iPhone 16 - 26.2", "os": "26.2", "connected": True},
                   {"udid": "C", "name": "iPhone 16 blue 18.6.2", "os": "18.6", "connected": False}]
        with mock.patch.object(fast_device, "physical_devices", return_value=devices):
            self.assertEqual(fast_device.resolve_device("blue - 27")["udid"], "A")
            with self.assertRaisesRegex(fast_device.FastDeviceError, "matches 2 iPhones"):
                fast_device.resolve_device("iPhone 16")

    def test_prepare_flow_keeps_wait_for_idle(self) -> None:
        import fast_device
        prepared = fast_device.prepare_flow({"version": 1, "setup": {"wait_for_idle": False, "preferences": {"a": True}}})
        self.assertFalse(prepared["setup"]["wait_for_idle"])

    def test_do_passes_no_idle_wait_into_the_request(self) -> None:
        import fast_device
        reply = {"status": "pass", "results": [], "labels": []}
        with mock.patch.object(fast_device, "resolve_device", return_value={"udid": "U", "name": "X"}), \
                mock.patch.object(fast_device, "start_server"), \
                mock.patch.object(fast_device, "request", return_value=dict(reply)) as request, \
                tempfile.TemporaryDirectory() as raw:
            fast_device.run_command(["do", '[{"type":"wait","label":"A"}]', "--no-idle-wait", "--out", raw])
            fast_device.run_command(["do", '{"type":"wait","label":"A"}', "--out", raw])
        self.assertIs(request.call_args_list[0].args[1]["wait_for_idle"], False)
        # Off by default too: the runner settles on its own; `--idle-wait` turns XCTest's wait back on.
        self.assertIs(request.call_args_list[1].args[1]["wait_for_idle"], False)
        with mock.patch.object(fast_device, "resolve_device", return_value={"udid": "U", "name": "X"}), \
                mock.patch.object(fast_device, "start_server"), \
                mock.patch.object(fast_device, "request", return_value=dict(reply)) as request, \
                tempfile.TemporaryDirectory() as raw:
            fast_device.run_command(["do", '{"type":"wait","label":"A"}', "--idle-wait", "--out", raw])
        self.assertNotIn("wait_for_idle", request.call_args.args[1])

    def test_lock_is_the_runners_last_request(self) -> None:
        import fast_device
        out = io.StringIO()
        with mock.patch.object(fast_device, "resolve_device", return_value={"udid": "U", "name": "X"}), \
                mock.patch.object(fast_device, "start_server") as start, \
                mock.patch.object(fast_device, "request", return_value={"status": "pass"}) as request, \
                mock.patch.object(fast_device, "release_runner") as release, contextlib.redirect_stdout(out):
            self.assertEqual(fast_device.run_command(["lock", "--device", "X"]), 0)
        start.assert_called_once()
        self.assertEqual(request.call_args.args[1], {"op": "lock"})
        release.assert_called_once_with("U")
        self.assertIn("LOCKED device=X", out.getvalue())

    def test_lock_that_fails_still_frees_the_iphone(self) -> None:
        import fast_device
        with mock.patch.object(fast_device, "resolve_device", return_value={"udid": "U", "name": "X"}), \
                mock.patch.object(fast_device, "start_server"), \
                mock.patch.object(fast_device, "request", return_value={"status": "fail", "error": "no pressLockButton"}), \
                mock.patch.object(fast_device, "release_runner") as release:
            with self.assertRaisesRegex(fast_device.FastDeviceError, "no pressLockButton"):
                fast_device.run_command(["lock"])
        release.assert_called_once_with("U")

    def run_do(self, *argv: str, reply: dict | None = None) -> tuple[mock.Mock, str]:
        import contextlib
        import io
        import fast_device
        reply = reply or {"status": "pass", "results": [], "labels": []}
        out = io.StringIO()
        with mock.patch.object(fast_device, "resolve_device", return_value={"udid": "U", "name": "X"}), \
                mock.patch.object(fast_device, "start_server"), \
                mock.patch.object(fast_device, "request", return_value=dict(reply)) as request, \
                tempfile.TemporaryDirectory() as raw, contextlib.redirect_stdout(out):
            fast_device.run_command(["do", *argv, "--out", raw])
        return request, out.getvalue()

    def test_do_sends_only_the_prompt_labels_it_declares(self) -> None:
        request, _ = self.run_do('{"type":"wait","label":"A"}', "--prompt-labels", "Allow, Don’t Allow")
        self.assertEqual(request.call_args.args[1]["optional_permission_labels"], ["Allow", "Don’t Allow"])
        request, _ = self.run_do('{"type":"wait","label":"A"}', "--prompt-labels",
                                 '[{"title_contains":"track","tap":"Ask App Not to Track"},"Cancel"]')
        self.assertEqual(request.call_args.args[1]["optional_permission_labels"],
                         [{"title_contains": "track", "tap": "Ask App Not to Track"}, "Cancel"])
        request, _ = self.run_do('{"type":"wait","label":"A"}')
        self.assertNotIn("optional_permission_labels", request.call_args.args[1])

    def test_do_refuses_a_prompt_rule_without_a_button(self) -> None:
        import fast_device
        with self.assertRaisesRegex(fast_device.FastDeviceError, "tap"):
            self.run_do('{"type":"wait","label":"A"}', "--prompt-labels", '[{"title_contains":"track"}]')

    def test_do_line_names_each_dismissed_prompt_and_an_idle_wait_it_could_not_skip(self) -> None:
        reply = {"status": "pass", "results": [], "labels": [], "wait_for_idle": "unsupported",
                 "dismissed": [{"title": "Allow “App” to use your location?", "tapped": "Allow While Using App"}]}
        _, out = self.run_do('{"type":"wait","label":"A"}', "--no-idle-wait", reply=reply)
        self.assertIn("wait_for_idle=unsupported", out)
        self.assertIn('dismissed=["Allow “App” to use your location? → Allow While Using App"]', out)
        _, quiet = self.run_do('{"type":"wait","label":"A"}')
        self.assertNotIn("dismissed=", quiet)
        self.assertNotIn("wait_for_idle=", quiet)

    def test_flow_refuses_a_prompt_rule_without_a_title(self) -> None:
        import fast_device
        with self.assertRaisesRegex(fast_device.FastDeviceError, "title_contains"):
            fast_device.prepare_flow({"version": 1, "setup": {"optional_permission_labels": [{"tap": "Allow"}]}})

    def test_flow_line_names_each_dismissed_prompt(self) -> None:
        import contextlib
        import io
        import fast_device
        report = {"status": "pass", "results": [], "labels": [],
                  "dismissed": [{"title": "“App” Would Like to Send You Notifications", "tapped": "Allow"}]}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "notify.json"
            flow.write_text(json.dumps({"version": 1, "app": {"bundle_id": "com.example.app"}, "actions": []}))
            device = {"udid": "U0000000", "name": "X", "os": "26.0", "connected": True}
            with mock.patch.object(fast_device, "resolve_device", return_value=device), \
                    mock.patch.object(fast_device, "server_alive", return_value=True), \
                    mock.patch.object(fast_device, "request", return_value=dict(report)), \
                    contextlib.redirect_stdout(out):
                fast_device.run_flow(fast_device.parse_flow_arguments([str(flow), "--artifacts-root", raw]))
        self.assertIn('dismissed=["“App” Would Like to Send You Notifications → Allow"]', out.getvalue())

    TRAIL = [{"screen": {"app": "com.example.app", "title": "Home", "nodes": [{"label": "Go", "type": "Button", "id": ""}]}},
             {"gesture": {"type": "tap_label", "label": "Go"}}]

    def test_do_records_the_response_trail(self) -> None:
        import fast_device
        reply = {"status": "pass", "results": [], "labels": ["Go"], "trail": self.TRAIL}
        with mock.patch.object(fast_device, "record") as record:
            _, out = self.run_do('{"type":"tap_label","label":"Go"}', reply=reply)
        record.assert_called_once_with(self.TRAIL, "device")
        self.assertTrue(out.startswith("PASS tap_label"))

    def test_do_without_trail_records_nothing(self) -> None:
        import fast_device
        err = io.StringIO()
        with mock.patch.object(fast_device, "record") as record, contextlib.redirect_stderr(err):
            _, out = self.run_do('{"type":"wait","label":"A"}')
        record.assert_not_called()
        self.assertEqual(out, "PASS wait labels=[]\n")
        self.assertEqual(err.getvalue(), "")

    def test_record_error_keeps_the_pass_line(self) -> None:
        import fast_device
        err = io.StringIO()
        reply = {"status": "pass", "results": [], "labels": [], "trail": self.TRAIL}
        with mock.patch.object(fast_device, "record", side_effect=OSError("disk full")), contextlib.redirect_stderr(err):
            _, out = self.run_do('{"type":"tap_label","label":"Go"}', reply=reply)
        self.assertTrue(out.startswith("PASS tap_label"))
        self.assertEqual(err.getvalue(), "map=not-saved (disk full)\n")

    def test_flow_records_the_report_trail_and_keeps_it_out_of_the_report(self) -> None:
        import fast_device
        report = {"status": "pass", "results": [], "labels": [], "trail": self.TRAIL}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "home.json"
            flow.write_text(json.dumps({"version": 1, "app": {"bundle_id": "com.example.app"}, "actions": []}))
            device = {"udid": "U0000000", "name": "X", "os": "26.0", "connected": True}
            with mock.patch.object(fast_device, "resolve_device", return_value=device), \
                    mock.patch.object(fast_device, "server_alive", return_value=True), \
                    mock.patch.object(fast_device, "request", return_value=dict(report)), \
                    mock.patch.object(fast_device, "record") as record, contextlib.redirect_stdout(out):
                fast_device.run_flow(fast_device.parse_flow_arguments([str(flow), "--artifacts-root", raw, "--json"]))
            metrics = json.loads(next(Path(raw).rglob("metrics.json")).read_text())
        record.assert_called_once_with(self.TRAIL, "device")
        self.assertNotIn("trail", metrics)
        self.assertNotIn("trail", json.loads(out.getvalue()))

    RUNNER_LOG = """    t =    49.25s Set value of PickerWheel (Element at index 0) to 7
XCTExpectFailure: matcher accepted UI Automation Failure: Requested adjust to value '7' which is not one of the possible values 01, 02, 03 for the picker wheel "16 o’clock" PickerWheel
FlowRunner.swift:403: Expected failure in -[FastDeviceUITests.FlowRunner testServe]: Requested adjust to value '7' which is not one of the possible values 01, 02, 03 for the picker wheel "16 o’clock" PickerWheelReason: (set_picker value not on the wheel)
\t Executed 1 test, with 0 failures (0 unexpected) in 2.362 (2.364) seconds
"""

    def test_a_runner_that_ends_mid_request_says_why(self) -> None:
        import fast_device
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "server.log"
            log.write_text(self.RUNNER_LOG)
            state = Path(raw) / "state.json"
            state.write_text(json.dumps({"pid": 4242, "address": "::1", "port": 1, "log": str(log)}))
            connection = mock.MagicMock()
            connection.__enter__.return_value.recv.return_value = b""
            with mock.patch.object(fast_device, "server_state_path", return_value=state), \
                    mock.patch.object(fast_device.socket, "create_connection", return_value=connection), \
                    mock.patch.object(fast_device, "kill_runners") as kill:
                with self.assertRaises(fast_device.FastDeviceError) as caught:
                    fast_device.request("U", {"op": "action"}, 5)
            # The xcodebuild that served this request lingers to collect diagnostics; it is ended,
            # by the same pattern `stop` uses (never a stored pid that may have been reused), so the
            # next request can start a runner instead of finding the iPhone busy.
            kill.assert_called_once_with("U")
            self.assertFalse(state.exists())
        message = str(caught.exception)
        self.assertIn("the runner ended", message)
        self.assertIn("Requested adjust to value '7' which is not one of the possible values 01, 02, 03", message)
        self.assertNotIn("PickerWheelReason", message)
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "server.log"
            log.write_text("FlowRunner.swift:398: error: -[FastDeviceUITests.FlowRunner testServe] : Requested adjust to value '7'\n")
            self.assertEqual(fast_device.runner_end_reason(log), "Requested adjust to value '7'")
            self.assertEqual(fast_device.runner_end_reason(Path(raw) / "missing.log"), "")

    def test_a_serving_iphone_resolves_without_devicectl(self) -> None:
        import fast_device
        tester = {"udid": "00008030-AAAA", "name": "tester’s iPhone", "os": "26.7.1", "connected": True}
        other = {"udid": "00008101-BBBB", "name": "iPhone 12 purple", "os": "26.6", "connected": True}
        with tempfile.TemporaryDirectory() as raw:
            for device in (tester, other):
                (Path(raw) / f"server-{device['udid']}.json").write_text(json.dumps({"pid": 1, "device": device}))
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=True), \
                    mock.patch.object(fast_device, "physical_devices", side_effect=AssertionError("devicectl")):
                for requested in ("00008030-AAAA", "tester’s iPhone"):
                    self.assertEqual(fast_device.resolve_device(requested)["udid"], tester["udid"])
                with mock.patch.dict(fast_device.os.environ, {"FAST_DEVICE": "iPhone 12 purple"}):
                    self.assertEqual(fast_device.resolve_device(None)["udid"], other["udid"])
            # A part of a name may also match an iPhone that is not serving: only devicectl knows.
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=True), \
                    mock.patch.object(fast_device, "physical_devices", return_value=[tester, other]) as listed:
                self.assertEqual(fast_device.resolve_device("tester"), tester)
            listed.assert_called_once()
            # A runner that does not answer, or a name two cached iPhones share, asks devicectl.
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=False), \
                    mock.patch.object(fast_device, "physical_devices", return_value=[tester]) as listed:
                self.assertEqual(fast_device.resolve_device("tester"), tester)
            listed.assert_called_once()
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=True), \
                    mock.patch.object(fast_device, "physical_devices", return_value=[tester, other]) as listed:
                with self.assertRaises(fast_device.FastDeviceError):
                    fast_device.resolve_device("iPhone")
            listed.assert_called_once()

    def test_a_serving_iphone_is_not_pinged_twice_and_old_state_learns_its_device(self) -> None:
        import fast_device
        tester = {"udid": "00008030-AAAA", "name": "tester’s iPhone", "os": "26.7.1", "connected": True}
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "server-00008030-AAAA.json"
            state.write_text(json.dumps({"pid": 1, "address": "::1", "port": 1}))
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=True) as alive:
                fast_device.start_server(dict(tester), rebuild=False)
                self.assertEqual(json.loads(state.read_text())["device"], tester)
                alive.reset_mock()
                fast_device.start_server({**tester, "serving": True}, rebuild=False)
                alive.assert_not_called()
                self.assertNotIn("serving", json.loads(state.read_text())["device"])

    def test_do_reports_bad_json_and_shapes_as_fail_lines(self) -> None:
        import fast_device
        for argument, message in (("{not json", "Expecting property name"), ('["a"]', "each action must be a JSON object"),
                                  ('{"type":"tap_label","label":"Go","expect":"Done"}', "expect must be a selector object")):
            with self.subTest(argument=argument), self.assertRaisesRegex(fast_device.FastDeviceError, message):
                self.run_do(argument)

    def test_a_locked_iphone_fails_fast_and_leaves_no_runner(self) -> None:
        import fast_device
        device = {"udid": "U", "name": "tester’s iPhone", "os": "26.7.1", "connected": True}
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "server-U.log"

            def start(*args, **kwargs):
                log.write_text('Error Domain=com.apple.dt.deviceprep Code=-3 "Unlock tester’s iPhone to Continue" '
                               "NSLocalizedRecoverySuggestion=Xcode cannot launch FastDeviceUITests on tester’s iPhone "
                               "because the device is locked.\n")
                process = mock.Mock(pid=4242)
                process.poll.return_value = None
                return process
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=False), \
                    mock.patch.object(fast_device, "run", return_value=mock.Mock(returncode=1)), \
                    mock.patch.object(fast_device, "developer_dir_for", return_value="/Xcode"), \
                    mock.patch.object(fast_device, "pick_signing", return_value={}), \
                    mock.patch.object(fast_device, "build_runner", return_value=Path(raw) / "r.xctestrun"), \
                    mock.patch.object(fast_device.shutil, "copy"), \
                    mock.patch.object(fast_device, "tunnel_address", return_value="::1"), \
                    mock.patch.object(fast_device.subprocess, "Popen", side_effect=start) as popen, \
                    mock.patch.object(fast_device, "kill_runners") as kill, \
                    mock.patch.object(fast_device.time, "sleep"):
                started = time.perf_counter()
                with self.assertRaisesRegex(fast_device.FastDeviceError, "tester’s iPhone is locked: unlock it"):
                    fast_device.start_server(device, rebuild=False)
                self.assertLess(time.perf_counter() - started, 5)
            kill.assert_called_once_with("U")
            self.assertFalse((Path(raw) / "server-U.json").exists())

    def run_device_flow(self, *flags: str) -> tuple[mock.Mock, mock.Mock]:
        import fast_device
        report = {"status": "pass", "results": [], "labels": []}
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "f.json"
            flow.write_text(json.dumps({"version": 1, "app": {"bundle_id": "com.example.app"}, "actions": []}))
            device = {"udid": "U0000000", "name": "X", "os": "26.0", "connected": True}
            with mock.patch.object(fast_device, "resolve_device", return_value=device), \
                    mock.patch.object(fast_device, "start_server") as start, \
                    mock.patch.object(fast_device, "server_alive", side_effect=lambda udid: start.called), \
                    mock.patch.object(fast_device, "request", return_value=dict(report)), \
                    mock.patch.object(fast_device, "run_one_shot", return_value=dict(report)) as one_shot, \
                    mock.patch.object(fast_device, "publish_recording", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()):
                fast_device.run_flow(fast_device.parse_flow_arguments([str(flow), "--artifacts-root", raw, *flags]))
        return start, one_shot

    def test_a_flow_starts_or_reuses_the_runner_unless_recording_or_one_shot(self) -> None:
        start, one_shot = self.run_device_flow()
        start.assert_called_once()
        one_shot.assert_not_called()
        for flag in ("--record", "--one-shot"):
            with self.subTest(flag=flag):
                start, one_shot = self.run_device_flow(flag)
                start.assert_not_called()
                one_shot.assert_called_once()

    def test_a_dead_runner_of_ours_is_cleared_but_a_live_or_foreign_one_is_busy(self) -> None:
        import fast_device
        device = {"udid": "U", "name": "tester’s iPhone", "os": "26.7.1", "connected": True}

        class Building(Exception):
            pass
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "server-U.json"
            state.write_text(json.dumps({"pid": 4242}))
            old = time.time() - 600
            fast_device.os.utime(state, (old, old))
            pgrep = mock.Mock(returncode=0, stdout="4242\n")
            with mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                    mock.patch.object(fast_device, "server_alive", return_value=False), \
                    mock.patch.object(fast_device, "run", return_value=pgrep), \
                    mock.patch.object(fast_device, "kill_runners") as kill, \
                    mock.patch.object(fast_device, "developer_dir_for", side_effect=Building):
                # Ours (the state file's pid), older than a start, and silent: a runner left behind.
                with self.assertRaises(Building):
                    fast_device.start_server(device, rebuild=False)
                kill.assert_called_once_with("U")
                # A process this state file does not name belongs to someone else.
                state.write_text(json.dumps({"pid": 1}))
                fast_device.os.utime(state, (old, old))
                with self.assertRaisesRegex(fast_device.FastDeviceError, "busy"):
                    fast_device.start_server(device, rebuild=False)
                # Ours but just written: maybe still starting for another agent; never killed.
                state.write_text(json.dumps({"pid": 4242}))
                kill.reset_mock()
                with self.assertRaisesRegex(fast_device.FastDeviceError, "busy"):
                    fast_device.start_server(device, rebuild=False)
                kill.assert_not_called()

    def test_the_runner_idles_out_after_five_minutes_by_default(self) -> None:
        import fast_device

        class Started(Exception):
            pass
        calls = []
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                mock.patch.object(fast_device, "server_alive", return_value=False), \
                mock.patch.object(fast_device, "run", side_effect=lambda args, **kw: calls.append(args) or mock.Mock(returncode=1)), \
                mock.patch.object(fast_device, "developer_dir_for", return_value="/X"), \
                mock.patch.object(fast_device, "pick_signing", return_value={}), \
                mock.patch.object(fast_device, "build_runner", return_value=Path(raw) / "r.xctestrun"), \
                mock.patch.object(fast_device.shutil, "copy"), \
                mock.patch.object(fast_device.subprocess, "Popen", side_effect=Started), \
                mock.patch.dict(fast_device.os.environ, {}, clear=False):
            fast_device.os.environ.pop("FAST_DEVICE_IDLE", None)
            with self.assertRaises(Started):
                fast_device.start_server({"udid": "U", "name": "X", "os": "26.0"}, rebuild=False)
        idle = [args for args in calls if any("FAST_IDLE" in str(part) for part in args)]
        self.assertIn("300", idle[0])

    GOOD_DETAILS = {"connectionProperties": {"pairingState": "paired", "tunnelState": "connected", "transportType": "wired"},
                    "deviceProperties": {"developerModeStatus": "enabled", "ddiServicesAvailable": True}}

    def run_check(self, details: dict, signing: object) -> tuple[int, str]:
        import fast_device
        device = {"udid": "U", "name": "tester’s iPhone", "os": "26.7.1", "connected": True}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(fast_device, "CACHE_ROOT", Path(raw)), \
                mock.patch.object(fast_device, "resolve_device", return_value=device), \
                mock.patch.object(fast_device, "device_details", return_value=details), \
                mock.patch.object(fast_device, "developer_dir_for", return_value="/Applications/Xcode.app/Contents/Developer"), \
                mock.patch.object(fast_device, "pick_signing", side_effect=[signing] if isinstance(signing, Exception) else None,
                                  return_value=signing), \
                mock.patch.object(fast_device, "runner_fingerprint", return_value="abc"), \
                mock.patch.object(fast_device, "server_alive", return_value=False), \
                contextlib.redirect_stdout(out):
            code = fast_device.run_command(["check", "--device", "tester"])
        return code, out.getvalue()

    def test_check_reports_a_ready_iphone(self) -> None:
        code, out = self.run_check(self.GOOD_DETAILS, {"name": "Dev Wildcard", "bundle_id": "*", "uuid": "u"})
        self.assertEqual(code, 0, out)
        for line in ("device=tester’s iPhone udid=U ios=26.7.1 transport=wired", "paired=yes", "connected=yes",
                     "developer_mode=enabled", "signing=Dev Wildcard (*)",
                     "runner=not built yet (the first run builds and signs it, 1–2 min)", "serving=no"):
            self.assertIn(line, out)
        self.assertTrue(out.rstrip().endswith("CHECK OK"), out)

    def test_check_lists_what_is_missing_with_the_fix(self) -> None:
        import fast_device
        details = {"connectionProperties": {"pairingState": "paired", "tunnelState": "disconnected", "transportType": "network"},
                   "deviceProperties": {"developerModeStatus": "disabled", "ddiServicesAvailable": False}}
        code, out = self.run_check(details, fast_device.FastDeviceError("no usable development profile for device U"))
        self.assertEqual(code, 1)
        self.assertIn("CHECK missing: connected, developer-mode, ddi, signing", out)
        self.assertIn("Developer Mode", out)
        self.assertIn("no usable development profile", out)

    def test_do_refuses_a_destructive_step_in_a_batch(self) -> None:
        import fast_device
        with self.assertRaisesRegex(fast_device.FastDeviceError, "step 2: 'Sign Out' looks destructive"):
            request, _ = self.run_do('[{"type":"tap_label","label":"Account"},{"type":"tap_label","label":"Sign Out"}]')

    def test_build_runner_uses_committed_xcodeproj_without_xcodegen(self) -> None:
        import fast_device
        calls: list[str] = []

        def fake_run(args, **kwargs):
            calls.append(args[0])
            if args[0] == "xcodebuild":
                products = Path(kwargs["cwd"]) / "dd/Build/Products"
                products.mkdir(parents=True)
                (products / "FastDevice_iphoneos.xctestrun").write_text("")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as raw:
            src = Path(raw) / "src"
            (src / "UITests").mkdir(parents=True)
            (src / "project.yml").write_text("name: x\n")
            (src / "FastDeviceRunner.xcodeproj").mkdir()
            (src / "FastDeviceRunner.xcodeproj" / "project.pbxproj").write_text("v1")
            cache = Path(raw) / "cache"
            with mock.patch.object(fast_device, "RUNNER_SOURCE", src), mock.patch.object(fast_device, "CACHE_ROOT", cache), \
                    mock.patch.object(fast_device.shutil, "which", return_value=None), \
                    mock.patch.object(fast_device, "run", side_effect=fake_run), mock.patch.object(fast_device, "sign_runner"):
                before = fast_device.runner_fingerprint("/Xcode", {"uuid": "u"})
                xctestrun = fast_device.build_runner("/Xcode", {"uuid": "u"}, False)
                (src / "FastDeviceRunner.xcodeproj" / "project.pbxproj").write_text("v2")
                after = fast_device.runner_fingerprint("/Xcode", {"uuid": "u"})
            work = cache / xctestrun.relative_to(cache).parts[0]
            self.assertTrue((work / "FastDeviceRunner.xcodeproj" / "project.pbxproj").is_file())
        self.assertNotIn("xcodegen", calls)
        self.assertEqual(calls, ["xcodebuild"])
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=1)
