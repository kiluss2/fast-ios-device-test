#!/usr/bin/env python3

"""Evidence filing tests: python3 -m unittest test_fast_evidence (needs Pillow and git)"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_evidence as evidence  # noqa: E402
import fast_evidence_sheet as sheet  # noqa: E402

from PIL import Image  # noqa: E402

FLOW = {"version": 1, "name": "gating-off", "expect": "no paywall on reopen",
        "actions": [{"type": "launch"}, {"type": "wait", "label": "Home"}, {"type": "screenshot", "name": "main.png"}]}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def repo(parent: Path, name: str = "MyApp", branch: str = "feature/PRJ-12-paywall") -> Path:
    root = parent / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "init")
    git(root, "checkout", "-q", "-b", branch)
    return root


def make_run(task_dir: Path, status: str, shots: tuple[str, ...] = ("main.png",), step: str = "",
             clip: bool = True) -> tuple[Path, Path | None]:
    run = task_dir / f"20261029-100000-SIM-{len(list(task_dir.glob('*')))}"
    run.mkdir(parents=True)
    for shot in shots:
        Image.new("RGB", (1179, 2556), (30, 60, 90)).save(run / shot)
    video = None
    if clip:
        video = (task_dir / "final.mp4") if status == "pass" else (run / "run.mp4")
        video.write_bytes(b"mp4")
    metrics = {"status": status, "task": task_dir.name, "artifacts": str(run),
               **({"step": step} if step else {}), **({"video": str(video)} if video else {})}
    (run / "metrics.json").write_text(json.dumps(metrics))
    return run, video


class Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        self.desktop = self.tmp / "Desktop"
        patcher = mock.patch.dict(evidence.os.environ, {"FAST_EVIDENCE_DIR": str(self.desktop)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("FAST_EVIDENCE", "FAST_EVIDENCE_TICKET", "FAST_EVIDENCE_PROJECT", "FAST_UI_KEEP_DAYS"):
            evidence.os.environ.pop(key, None)

    def file(self, root: Path, run: Path, video: Path | None, flow: dict | None = None, role: str = "after",
             device: str = "iPhone 17", tidy: bool = True) -> tuple[dict | None, str | None]:
        return evidence.after_run(flow or FLOW, root, (flow or FLOW)["name"], device, role, run, video, tidy)


class TicketTests(Base):
    def test_ticket_keys_come_from_the_branch_in_order(self) -> None:
        self.assertEqual(evidence.tickets_in("feature/PRJ-12_PRJ-13-paywall_dark_2"), "PRJ-12 PRJ-13")
        self.assertEqual(evidence.tickets_in("bugfix/PRJ-859-ui_sweep_v1_2_0"), "PRJ-859")

    def test_a_lowercase_word_with_a_number_is_not_a_ticket(self) -> None:
        self.assertIsNone(evidence.tickets_in("feature/fix-crash-2"))

    def test_branch_without_a_ticket_names_the_folder(self) -> None:
        root = repo(self.tmp, branch="feature/fix-crash-2")
        self.assertEqual(evidence.ticket_name(root, {}), "feature-fix-crash-2")

    def test_flow_ticket_beats_the_environment_which_beats_the_branch(self) -> None:
        root = repo(self.tmp)
        with mock.patch.dict(evidence.os.environ, {"FAST_EVIDENCE_TICKET": "PRJ-77"}):
            self.assertEqual(evidence.ticket_name(root, {"ticket": "PRJ-99"}), "PRJ-99")
            self.assertEqual(evidence.ticket_name(root, {}), "PRJ-77")
        self.assertEqual(evidence.ticket_name(root, {}), "PRJ-12")

    def test_a_folder_outside_git_has_no_ticket_unless_one_is_given(self) -> None:
        plain = self.tmp / "scratch"
        plain.mkdir()
        self.assertIsNone(evidence.ticket_name(plain, {}))
        self.assertEqual(evidence.ticket_name(plain, {"ticket": "PRJ-12"}), "PRJ-12")


class ProjectTests(Base):
    def test_a_worktree_of_the_base_branch_files_under_the_main_checkout(self) -> None:
        root = repo(self.tmp)
        base = self.tmp / "MyApp-base"
        git(root, "worktree", "add", "-q", str(base), "main")
        self.assertEqual(evidence.project_name(base, {}), "MyApp")
        self.assertEqual(evidence.project_name(root, {}), "MyApp")

    def test_overrides_and_plain_folders(self) -> None:
        plain = self.tmp / "MyApp-copy"
        plain.mkdir()
        self.assertEqual(evidence.project_name(plain, {}), "MyApp-copy")
        self.assertEqual(evidence.project_name(plain, {"project": "MyApp"}), "MyApp")
        with mock.patch.dict(evidence.os.environ, {"FAST_EVIDENCE_PROJECT": "Other"}):
            self.assertEqual(evidence.project_name(plain, {}), "Other")


class WhenFiledTests(unittest.TestCase):
    ACTIONS = [{"type": "launch"}, {"type": "tap_label", "label": "Go"}, {"type": "wait", "label": "Fixed"}]

    def test_a_pass_is_filed_and_an_after_failure_is_not(self) -> None:
        self.assertTrue(evidence.should_file("pass", "after", "", self.ACTIONS))
        self.assertFalse(evidence.should_file("fail", "after", "03_wait", self.ACTIONS))

    def test_a_before_failure_counts_only_at_or_after_the_outcome_step(self) -> None:
        self.assertTrue(evidence.should_file("fail", "before", "03_wait", self.ACTIONS))
        self.assertFalse(evidence.should_file("fail", "before", "02_tap_label", self.ACTIONS))
        self.assertFalse(evidence.should_file("fail", "before", "prepare", self.ACTIONS))
        self.assertFalse(evidence.should_file("fail", "before", "state_and_launch", self.ACTIONS))

    def test_captures_after_the_check_do_not_move_the_outcome_step(self) -> None:
        actions = self.ACTIONS + [{"type": "pause", "seconds": 1}, {"type": "screenshot"}, {"type": "dump"}]
        self.assertTrue(evidence.should_file("fail", "before", "03_wait", actions))
        self.assertFalse(evidence.should_file("fail", "before", "02_tap_label", actions))

    def test_a_marked_outcome_step_moves_the_line(self) -> None:
        actions = [dict(action) for action in self.ACTIONS]
        actions[1]["outcome"] = True
        self.assertTrue(evidence.should_file("fail", "before", "02_tap_label", actions))
        self.assertFalse(evidence.should_file("fail", "before", "01_launch", actions))

    def test_turned_off_by_the_flow_or_the_environment(self) -> None:
        self.assertIsNone(evidence.settings({**FLOW, "evidence": False}))
        with mock.patch.dict(evidence.os.environ, {"FAST_EVIDENCE": "0"}):
            self.assertIsNone(evidence.settings(FLOW))
        self.assertEqual(evidence.settings({**FLOW, "evidence": {"label": "G1"}}), {"label": "G1"})


class FilingTests(Base):
    def test_a_pass_lands_light_in_the_ticket_folder_and_leaves_only_metrics(self) -> None:
        root = repo(self.tmp)
        run, video = make_run(root / "build/fast-ui/gating-off", "pass")
        filed, error = self.file(root, run, video)
        self.assertIsNone(error)
        folder = self.desktop / "MyApp PRJ-12"
        self.assertEqual(filed["dir"], folder)
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["PRJ-12.jpg", "gating-off-iPhone-17.mp4", "source"])
        shot = folder / "source/shots/gating-off-iPhone-17/after-main.jpg"
        with Image.open(shot) as image:
            self.assertEqual((image.width, image.format), (720, "JPEG"))
        self.assertLess(shot.stat().st_size, 150_000)
        self.assertEqual([path.name for path in run.iterdir()], ["metrics.json"])
        self.assertFalse(video.exists())
        metrics = json.loads((run / "metrics.json").read_text())
        self.assertEqual(metrics["video"], str(folder / "gating-off-iPhone-17.mp4"))
        self.assertEqual(metrics["evidence"], str(folder))
        self.assertEqual(metrics["evidence_shots"], {"main": str(shot)})

    def test_the_sheet_spec_renders_again_with_the_sheet_tool(self) -> None:
        root = repo(self.tmp)
        run, video = make_run(root / "build/fast-ui/gating-off", "pass")
        filed, _ = self.file(root, run, video)
        spec = filed["dir"] / "source/sheet.json"
        cells = json.loads(spec.read_text())["cells"]
        self.assertEqual([(cell["label"], cell["caption"]) for cell in cells],
                         [("gating-off · iPhone 17", "no paywall on reopen")])
        result = sheet.build(spec)
        self.assertEqual(Path(result["path"]), filed["dir"] / "PRJ-12.jpg")

    def test_a_rerun_keeps_edited_text_position_and_the_before_cell(self) -> None:
        root = repo(self.tmp)
        task_dir = root / "build/fast-ui/gating-off"
        before = {**FLOW, "evidence": {"before_caption": "paywall shows"}}
        run, video = make_run(task_dir, "fail", shots=("failure.png",), step="02_wait")
        self.file(root, run, video, flow=before, role="before")
        other = {**FLOW, "name": "gating-prod"}
        run, video = make_run(root / "build/fast-ui/gating-prod", "pass")
        self.file(root, run, video, flow=other)
        run, video = make_run(task_dir, "pass")
        filed, _ = self.file(root, run, video)
        spec_path = filed["dir"] / "source/sheet.json"
        spec = json.loads(spec_path.read_text())
        self.assertEqual([cell["key"] for cell in spec["cells"]], [
            "gating-off-iPhone-17/before/failure", "gating-off-iPhone-17/after/main", "gating-prod-iPhone-17/after/main"])
        self.assertEqual(spec["cells"][0]["label"], "gating-off · iPhone 17 · before")
        self.assertEqual(spec["cells"][0]["caption"], "paywall shows")
        spec["cells"][1]["caption"] = "edited by the agent"
        spec["cells"] = [spec["cells"][2], spec["cells"][0], spec["cells"][1]]
        spec_path.write_text(json.dumps(spec))
        run, video = make_run(task_dir, "pass")
        self.file(root, run, video)
        cells = json.loads(spec_path.read_text())["cells"]
        self.assertEqual([cell["key"] for cell in cells], [
            "gating-prod-iPhone-17/after/main", "gating-off-iPhone-17/before/failure", "gating-off-iPhone-17/after/main"])
        self.assertEqual(cells[2]["caption"], "edited by the agent")
        self.assertTrue((filed["dir"] / "gating-off-iPhone-17-before.mp4").exists())

    def test_a_cell_the_agent_removed_is_not_added_back(self) -> None:
        root = repo(self.tmp)
        task_dir = root / "build/fast-ui/gating-off"
        run, video = make_run(task_dir, "pass", shots=("main.png", "detail.png"))
        flow = {**FLOW, "actions": FLOW["actions"] + [{"type": "screenshot", "name": "detail.png"}]}
        filed, _ = self.file(root, run, video, flow=flow)
        spec_path = filed["dir"] / "source/sheet.json"
        spec = json.loads(spec_path.read_text())
        self.assertEqual([cell["label"] for cell in spec["cells"]],
                         ["gating-off · iPhone 17 · main", "gating-off · iPhone 17 · detail"])
        spec["removed"] = [spec["cells"].pop(1)["key"]]
        spec_path.write_text(json.dumps(spec))
        run, video = make_run(task_dir, "pass", shots=("main.png", "detail.png"))
        self.file(root, run, video, flow=flow)
        self.assertEqual(len(json.loads(spec_path.read_text())["cells"]), 1)

    def test_flow_settings_pick_shots_sheet_case_and_text(self) -> None:
        root = repo(self.tmp)
        flow = {**FLOW, "actions": FLOW["actions"] + [{"type": "screenshot"}],
                "evidence": {"shots": ["step-04"], "sheet": "gating", "case": "G2", "label": "G2 · Off", "caption": "c"}}
        run, video = make_run(root / "build/fast-ui/gating-off", "pass", shots=("main.png", "step-04.png"))
        filed, _ = self.file(root, run, video, flow=flow)
        folder = filed["dir"]
        self.assertTrue((folder / "gating.jpg").exists())
        self.assertTrue((folder / "G2.mp4").exists())
        cells = json.loads((folder / "source/sheet-gating.json").read_text())["cells"]
        self.assertEqual([(cell["key"], cell["label"], cell["caption"]) for cell in cells],
                         [("G2/after/step-04", "G2 · Off", "c")])

    def test_iphone_shot_names_without_an_extension_are_found(self) -> None:
        root = repo(self.tmp)
        flow = {**FLOW, "actions": [{"type": "screenshot", "name": "home"}]}
        run, video = make_run(root / "build/fast-device/gating-off", "pass", shots=("home.png",), clip=False)
        filed, error = self.file(root, run, video, flow=flow)
        self.assertIsNone(error)
        self.assertEqual(list(filed["shots"]), ["home"])

    def test_a_custom_root_is_copied_from_not_emptied(self) -> None:
        root = repo(self.tmp)
        run, video = make_run(self.tmp / "qa/gating-off", "pass")
        filed, _ = self.file(root, run, video, tidy=False)
        self.assertTrue((run / "main.png").exists())
        self.assertTrue(video.exists())
        self.assertTrue((filed["dir"] / "PRJ-12.jpg").exists())

    def test_a_failure_that_is_not_a_before_stays_put(self) -> None:
        root = repo(self.tmp)
        run, video = make_run(root / "build/fast-ui/gating-off", "fail", shots=("failure.png",), step="02_wait")
        filed, error = self.file(root, run, video)
        self.assertEqual((filed, error), (None, None))
        self.assertFalse(self.desktop.exists())
        self.assertTrue((run / "failure.png").exists())

    def test_nothing_is_filed_outside_git_or_when_turned_off(self) -> None:
        plain = self.tmp / "scratch"
        run, video = make_run(plain / "build/fast-ui/gating-off", "pass")
        self.assertEqual(self.file(plain, run, video), (None, None))
        root = repo(self.tmp)
        run, video = make_run(root / "build/fast-ui/gating-off", "pass")
        self.assertEqual(self.file(root, run, video, flow={**FLOW, "evidence": False}), (None, None))
        self.assertFalse(self.desktop.exists())
        self.assertTrue((run / "main.png").exists())

    def test_a_filing_error_is_reported_and_the_run_is_kept(self) -> None:
        root = repo(self.tmp)
        run, video = make_run(root / "build/fast-ui/gating-off", "pass")
        with mock.patch.object(evidence, "store_shot", side_effect=OSError("disk full")):
            filed, error = self.file(root, run, video)
        self.assertIsNone(filed)
        self.assertIn("disk full", error)
        self.assertTrue((run / "main.png").exists())


class EngineTests(Base):
    """Both engines file a run through after_run and print where it went."""

    def run_sim(self, root: Path, actions: list[dict], *argv: str) -> tuple[int, str, str, Path]:
        import base64  # noqa: F401
        import contextlib
        import io
        import fast_ui
        import test_fast_ui as fixtures
        flow = root / "flows/gating-off.json"
        flow.parent.mkdir(exist_ok=True)
        flow.write_text(json.dumps({"version": 1, "name": "gating-off", "expect": "Go shown", "actions": actions,
                                    "app": {"bundle_id": "com.example.app", "prebuilt": True, "simulator": "SIM",
                                            "project_root": str(root)}}))

        def shot(udid: str, path: Path) -> None:
            Image.new("RGB", (1179, 2556), (200, 10, 10)).save(path)

        out, err = io.StringIO(), io.StringIO()
        # main() takes a simulator lease: keep it out of ~/Library/Caches/fast-ui/locks.
        with mock.patch.object(fast_ui.sys, "argv", ["fastSimulatorTest", str(flow), *argv]), \
                mock.patch.object(fast_ui, "LOCK_DIR", root / "locks"), \
                mock.patch.object(fast_ui, "prepare_app", return_value=(None, False)), \
                mock.patch.object(fast_ui, "command"), mock.patch.object(fast_ui, "apply_ui_settings"), \
                mock.patch.object(fast_ui, "set_preferences"), \
                mock.patch.object(fast_ui, "restore_ui_settings"), mock.patch.object(fast_ui, "record"), \
                mock.patch.object(fast_ui, "clear_prompts"), mock.patch.object(fast_ui, "screenshot", side_effect=shot), \
                mock.patch.object(fast_ui, "axe", side_effect=fixtures.FakeAxe(fixtures.SimulatorTrailTests.A)), \
                mock.patch.object(fast_ui.time, "sleep"), \
                mock.patch.object(evidence, "simulator_name", return_value="iPhone 17"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fast_ui.main()
        return code, out.getvalue(), err.getvalue(), next((root / "build/fast-ui/gating-off").iterdir())

    def test_a_simulator_pass_is_filed_and_emptied(self) -> None:
        root = repo(self.tmp)
        code, out, _, run = self.run_sim(root, [{"type": "wait", "label": "Go"}, {"type": "screenshot", "name": "main.png"}])
        self.assertEqual(code, 0)
        folder = self.desktop / "MyApp PRJ-12"
        self.assertIn(f"evidence={folder.as_uri()}", out)
        self.assertTrue((folder / "PRJ-12.jpg").exists())
        self.assertEqual([path.name for path in run.iterdir()], ["metrics.json"])

    def test_a_simulator_before_run_files_its_failure_screen(self) -> None:
        root = repo(self.tmp)
        code, _, err, run = self.run_sim(root, [{"type": "wait", "label": "Fixed", "timeout": 0.05}], "--before")
        self.assertEqual(code, 1)
        self.assertIn("evidence=", err)
        cells = json.loads((self.desktop / "MyApp PRJ-12/source/sheet.json").read_text())["cells"]
        self.assertEqual([cell["key"] for cell in cells], ["gating-off-iPhone-17/before/failure"])
        self.assertTrue((run / "failure.png").exists())

    def test_a_simulator_filing_error_keeps_the_pass(self) -> None:
        root = repo(self.tmp)
        with mock.patch.object(evidence, "store_shot", side_effect=OSError("disk full")):
            code, out, _, run = self.run_sim(root, [{"type": "screenshot", "name": "main.png"}])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("PASS task=gating-off"))
        self.assertIn("evidence_error=OSError: disk full", out)
        self.assertTrue((run / "main.png").exists())

    def run_device(self, root: Path, report: dict, *argv: str) -> tuple[int, str]:
        import contextlib
        import io
        import fast_device
        flow = root / "flows/home.json"
        flow.parent.mkdir(exist_ok=True)
        flow.write_text(json.dumps({"version": 1, "name": "home", "app": {"bundle_id": "com.example.app",
                                    "project_root": str(root)}, "actions": [{"type": "screenshot", "name": "home"}]}))
        device = {"udid": "U0000000", "name": "Test iPhone", "os": "26.0", "connected": True}
        out = io.StringIO()
        with mock.patch.object(fast_device, "resolve_device", return_value=device), \
                mock.patch.object(fast_device, "server_alive", return_value=True), \
                mock.patch.object(fast_device, "request", return_value=dict(report)), \
                mock.patch.object(fast_device, "record"), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = fast_device.run_flow(fast_device.parse_flow_arguments([str(flow), *argv]))
        return code, out.getvalue()

    def png(self) -> str:
        import base64
        import io
        buffer = io.BytesIO()
        Image.new("RGB", (1179, 2556), (10, 200, 10)).save(buffer, "PNG")
        return base64.b64encode(buffer.getvalue()).decode()

    def test_an_iphone_pass_is_filed_and_emptied(self) -> None:
        root = repo(self.tmp)
        report = {"status": "pass", "results": [], "labels": [], "screenshots": [{"name": "home", "png": self.png()}]}
        code, out = self.run_device(root, report)
        self.assertEqual(code, 0)
        folder = self.desktop / "MyApp PRJ-12"
        self.assertIn(f"evidence={folder.as_uri()}", out)
        cells = json.loads((folder / "source/sheet.json").read_text())["cells"]
        self.assertEqual([cell["label"] for cell in cells], ["home · Test iPhone"])
        run = next((root / "build/fast-device/home").iterdir())
        self.assertEqual([path.name for path in run.iterdir()], ["metrics.json"])

    def test_iphone_runs_prune_old_run_folders(self) -> None:
        import os
        import time
        root = repo(self.tmp)
        old = root / "build/fast-device/home/20260101-000000-U0000000"
        old.mkdir(parents=True)
        (old / "metrics.json").write_text("{}")
        stale = time.time() - 4 * 86400
        os.utime(old, (stale, stale))
        self.run_device(root, {"status": "pass", "results": [], "labels": []})
        self.assertFalse(old.exists())


class FolderCommandTests(Base):
    def run_folder(self, root: Path) -> tuple[int, str, str]:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sheet.sys, "argv", ["fastEvidenceSheet", "--folder", str(root)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sheet.main()
        return code, out.getvalue(), err.getvalue()

    def test_folder_prints_the_ticket_folder_and_makes_its_flows_dir(self) -> None:
        root = repo(self.tmp)
        code, out, _ = self.run_folder(root)
        folder = self.desktop / "MyApp PRJ-12"
        self.assertEqual(code, 0)
        self.assertEqual(out, f"FOLDER path={folder.as_uri()} flows={(folder / 'source/flows').as_uri()}\n")
        self.assertTrue((folder / "source/flows").is_dir())

    def test_folder_outside_git_says_how_to_name_it(self) -> None:
        plain = self.tmp / "scratch"
        plain.mkdir()
        code, _, err = self.run_folder(plain)
        self.assertEqual(code, 1)
        self.assertIn("FAST_EVIDENCE_TICKET", err)
        self.assertFalse(self.desktop.exists())


class SheetFallbackTests(Base):
    def test_a_task_cell_finds_a_filed_shot_after_the_run_was_emptied(self) -> None:
        root = repo(self.tmp)
        run, video = make_run(root / "build/fast-ui/gating-off", "pass")
        self.file(root, run, video)
        spec = self.tmp / "spec.json"
        spec.write_text(json.dumps({"root": str(root / "build/fast-ui"), "out": str(self.tmp / "s.jpg"),
                                    "cells": [{"task": "gating-off", "shot": "main"}]}))
        self.assertEqual(sheet.build(spec)["cells"], 1)


if __name__ == "__main__":
    unittest.main()
