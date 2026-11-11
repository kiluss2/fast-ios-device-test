#!/usr/bin/env python3

"""Video and device-recording tests (no Pillow needed): python3 -m unittest test_fast_media"""

from __future__ import annotations

import json
import os
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
# Engine tests with the default root must never write into the real cache.
_ARTIFACTS_SANDBOX = tempfile.TemporaryDirectory()
os.environ["FAST_ARTIFACTS_HOME"] = _ARTIFACTS_SANDBOX.name
os.environ["FAST_UNIT_TESTS_HOME"] = _ARTIFACTS_SANDBOX.name + "-unit"
import fast_device  # noqa: E402
import fast_media  # noqa: E402
import fast_ui_report  # noqa: E402

# Writes <out> as an H.264 clip of <width>x<height> at a high bitrate, <frames> frames at 24 fps.
MAKE_CLIP = '''
import AVFoundation
let args = CommandLine.arguments
let out = URL(fileURLWithPath: args[1])
let width = Int(args[2])!, height = Int(args[3])!, frames = Int(args[4])!
try? FileManager.default.removeItem(at: out)
let writer = try! AVAssetWriter(outputURL: out, fileType: .mp4)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 20_000_000],
])
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
    kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
])
writer.add(input)
writer.startWriting()
writer.startSession(atSourceTime: .zero)
for index in 0..<frames {
    var buffer: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
    CVPixelBufferLockBaseAddress(buffer!, [])
    let base = CVPixelBufferGetBaseAddress(buffer!)!.assumingMemoryBound(to: UInt8.self)
    let row = CVPixelBufferGetBytesPerRow(buffer!)
    for y in 0..<height { for x in 0..<width {
        let pixel = base + y * row + x * 4
        pixel[0] = UInt8((x * 7 + index * 13) & 255); pixel[1] = UInt8((y * 3 + index * 5) & 255)
        pixel[2] = UInt8(((x ^ y) + index) & 255); pixel[3] = 255
    } }
    CVPixelBufferUnlockBaseAddress(buffer!, [])
    while !input.isReadyForMoreMediaData { usleep(1000) }
    adaptor.append(buffer!, withPresentationTime: CMTime(value: CMTimeValue(index), timescale: 24))
}
input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
print(writer.status == .completed ? "ok" : "failed")
'''

# Prints "<width>x<height> <codec>" of a clip's video track.
CLIP_INFO = '''
import AVFoundation
let track = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1])).tracks(withMediaType: .video)[0]
let codec = CMFormatDescriptionGetMediaSubType(track.formatDescriptions[0] as! CMFormatDescription)
let name = String(bytes: [24, 16, 8, 0].map { UInt8((codec >> $0) & 255) }, encoding: .ascii)!
print("\\(Int(track.naturalSize.width))x\\(Int(track.naturalSize.height)) \\(name)")
'''


def fake_encoder(size: int, returncode: int = 0):
    """A stand-in for the shrink helper that writes `size` bytes to its output argument."""
    def run(args, **kwargs):
        if returncode == 0:
            Path(args[-3]).write_bytes(b"e" * size)
        return mock.Mock(returncode=returncode, stdout="", stderr="")
    return run


class ShrinkVideoTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(fast_media, "helper_command", return_value=["shrink"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_smaller_encode_replaces_the_original_under_the_target_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "Screen Recording.mp4"
            source.write_bytes(b"r" * 1000)
            with mock.patch.object(fast_media.subprocess, "run", side_effect=fake_encoder(100)):
                kept = fast_media.shrink_video(source, Path(raw) / "run.mp4")
            self.assertEqual(kept, Path(raw) / "run.mp4")
            self.assertEqual(kept.read_bytes(), b"e" * 100)
            self.assertFalse(source.exists())
            self.assertEqual(sorted(p.name for p in Path(raw).iterdir()), ["run.mp4"])

    def test_failed_encode_keeps_the_original_recording(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "Screen Recording.mp4"
            source.write_bytes(b"r" * 1000)
            with mock.patch.object(fast_media.subprocess, "run", side_effect=fake_encoder(100, returncode=1)):
                kept = fast_media.shrink_video(source, Path(raw) / "run.mp4")
            self.assertEqual(kept.read_bytes(), b"r" * 1000)
            self.assertEqual(sorted(p.name for p in Path(raw).iterdir()), ["run.mp4"])

    def test_encode_that_is_not_smaller_keeps_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "run.mp4"
            source.write_bytes(b"r" * 100)
            with mock.patch.object(fast_media.subprocess, "run", side_effect=fake_encoder(500)):
                kept = fast_media.shrink_video(source)
            self.assertEqual(kept.read_bytes(), b"r" * 100)
            self.assertEqual(sorted(p.name for p in Path(raw).iterdir()), ["run.mp4"])

    def test_timeout_keeps_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "run.mp4"
            source.write_bytes(b"r" * 100)
            timeout = subprocess.TimeoutExpired(cmd="shrink", timeout=1)
            with mock.patch.object(fast_media.subprocess, "run", side_effect=timeout):
                self.assertEqual(fast_media.shrink_video(source).read_bytes(), b"r" * 100)

    def test_zero_long_side_skips_the_encode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "raw.mov"
            source.write_bytes(b"r")
            with mock.patch.object(fast_media.subprocess, "run") as run:
                kept = fast_media.shrink_video(source, Path(raw) / "run.mp4", long_side=0)
            run.assert_not_called()
            self.assertEqual(kept.read_bytes(), b"r")

    def test_missing_source_is_returned_untouched(self) -> None:
        missing = Path(tempfile.gettempdir()) / "fast-media-missing.mp4"
        self.assertEqual(fast_media.shrink_video(missing), missing)


@unittest.skipUnless(shutil.which("swiftc"), "needs swiftc and AVFoundation")
class ShrinkVideoEncodeTests(unittest.TestCase):
    """Runs the real helper on a real clip, so an SDK change that breaks it shows up here."""

    def test_portrait_recording_comes_out_h264_at_review_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clip = Path(raw) / "Screen Recording.mp4"
            make = fast_media.helper_command(MAKE_CLIP, name="test-clip")
            made = subprocess.run([*make, str(clip), "1290", "2796", "24"], capture_output=True, text=True, timeout=600)
            self.assertEqual(made.stdout.strip(), "ok", made.stderr)
            kept = fast_media.shrink_video(clip, Path(raw) / "run.mp4")
            info = fast_media.helper_command(CLIP_INFO, name="test-info")
            described = subprocess.run([*info, str(kept)], capture_output=True, text=True, timeout=120)
            self.assertEqual(kept, Path(raw) / "run.mp4")
            self.assertFalse(clip.exists())
            self.assertEqual(described.stdout.strip(), "442x960 avc1", described.stderr)


class DeviceRecordingTests(unittest.TestCase):
    def test_enable_recording_keeps_the_recording_of_a_passing_test(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            xctestrun = Path(raw) / "run.xctestrun"
            xctestrun.write_bytes(plistlib.dumps({fast_device.TEST_TARGET: {
                "SystemAttachmentLifetime": "deleteOnSuccess", "PreferredScreenCaptureFormat": "screenshots",
            }}))
            fast_device.enable_recording(xctestrun)
            target = plistlib.loads(xctestrun.read_bytes())[fast_device.TEST_TARGET]
        self.assertEqual(target["SystemAttachmentLifetime"], "keepAlways")
        self.assertEqual(target["PreferredScreenCaptureFormat"], "screenRecording")

    def test_publish_recording_names_the_passing_clip_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(fast_device, "shrink_video", side_effect=lambda s, t=None: Path(shutil.move(str(s), t or s))):
            run = Path(raw) / "task" / "20261013-110324-00008120"
            run.mkdir(parents=True)
            (run / "Screen Recording 2026-10-13 at 11.03.mp4").write_bytes(b"v" * 10)
            (run / "1-paywall.png").write_bytes(b"p")
            (run / "result.xcresult").mkdir()
            video = fast_device.publish_recording(run, passed=True, session="qa")
            self.assertEqual(video, Path(raw) / "task" / "final-qa.mp4")
            self.assertTrue((run / "1-paywall.png").exists())
            self.assertFalse(list(run.glob("*.mp4")))

    def test_publish_recording_keeps_a_failing_clip_as_run_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(fast_device, "shrink_video", side_effect=lambda s, t=None: Path(shutil.move(str(s), t or s))):
            run = Path(raw) / "task" / "20261013-110324-00008120"
            run.mkdir(parents=True)
            (run / "Screen Recording.mp4").write_bytes(b"v" * 10)
            video = fast_device.publish_recording(run, passed=False)
            self.assertEqual(video, run / "run.mp4")

    def test_publish_recording_without_a_recording_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "shot.png").write_bytes(b"p")
            self.assertIsNone(fast_device.publish_recording(Path(raw), passed=True))

    def test_record_and_serve_are_refused_together(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            fast_device.parse_flow_arguments(["flow.json", "--record", "--serve"])

    def test_session_defaults_to_the_shared_environment_variable(self) -> None:
        with mock.patch.dict(os.environ, {"FAST_UI_SESSION": "pr-12"}):
            self.assertEqual(fast_device.parse_flow_arguments(["flow.json"]).session, "pr-12")

    def test_record_is_refused_while_a_persistent_runner_holds_the_iphone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            flow = Path(raw) / "flow.json"
            flow.write_text(json.dumps({"version": 1, "name": "f", "app": {"project_root": raw}, "actions": []}))
            device = {"udid": "00008120-AAAA", "name": "Test iPhone", "os": "26.6", "connected": True}
            with mock.patch.object(fast_device, "resolve_device", return_value=device), \
                    mock.patch.object(fast_device, "server_alive", return_value=True), \
                    mock.patch.object(fast_device, "run_one_shot") as one_shot:
                with self.assertRaisesRegex(fast_device.FastDeviceError, "--record needs a one-shot run"):
                    fast_device.run_flow(fast_device.parse_flow_arguments([str(flow), "--record"]))
            one_shot.assert_not_called()


class LauncherCopyTests(unittest.TestCase):
    """A zip (and some plugin installs) store bin/ launchers as copies, not symlinks."""

    def test_copied_launchers_still_find_the_shared_module(self) -> None:
        here = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            shutil.copytree(here, root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "bin").mkdir()
            for launcher, script in {
                "fastDeviceTest": "fast_device.py", "fastSimulatorTest": "fast_ui.py", "fastUIReview": "fast_ui_review.py", "fastUIMap": "fast_ui_map.py",
            }.items():
                shutil.copy(here / script, root / "bin" / launcher)
                with self.subTest(launcher=launcher):
                    ran = subprocess.run(
                        [sys.executable, str(root / "bin" / launcher), "--help"],
                        capture_output=True, text=True, timeout=60, cwd=raw,
                    )
                    self.assertEqual(ran.returncode, 0, ran.stderr[-300:])


class ReportTargetTests(unittest.TestCase):
    def test_device_runs_are_keyed_by_their_udid(self) -> None:
        self.assertEqual(fast_ui_report.target({"udid": "00008120-00041CD2"}), "00008120")
        self.assertEqual(fast_ui_report.target({"simulator": "ABCDEF12-3456"}), "ABCDEF12")
        self.assertEqual(fast_ui_report.target({}), "?")


PROJECT_A = "MyApp-1a2b3c4d"
PROJECT_B = "Other-5e6f7a8b"
RUN = "20260101-000000-SIM"


class ArtifactsHomeTests(unittest.TestCase):
    """Run artifacts live outside the project, bounded by age and size; deletion stays narrow."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        self.home = self.tmp / "home"
        self.unit_home = self.tmp / "unit"
        patcher = mock.patch.dict(os.environ, {"FAST_ARTIFACTS_HOME": str(self.home),
                                               "FAST_UNIT_TESTS_HOME": str(self.unit_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("FAST_UI_KEEP_DAYS", "FAST_ARTIFACTS_MAX_MB", "FAST_ARTIFACTS_PROTECT_MIN", "FAST_UNIT_CACHE_DAYS"):
            os.environ.pop(key, None)
        self.home.mkdir()
        (self.home / fast_media.MARKER).touch()

    def age(self, path: Path, days: float) -> None:
        stamp = time.time() - days * 86400
        for item in [path, *path.rglob("*")] if path.is_dir() else [path]:
            os.utime(item, (stamp, stamp), follow_symlinks=False)

    def run_dir(self, project: str, kind: str, task: str, name: str, size: int = 10, age_days: float = 1) -> Path:
        run = self.home / project / kind / task / name
        run.mkdir(parents=True)
        (run / "metrics.json").write_bytes(b"x" * size)
        self.age(run, age_days)
        return run

    # --- default root -------------------------------------------------------------------
    def test_default_root_is_outside_the_project_and_unique_per_path(self) -> None:
        app = self.tmp / "work/MyApp"
        other = self.tmp / "other/MyApp"
        app.mkdir(parents=True)
        other.mkdir(parents=True)
        root = fast_media.default_artifacts_root(app, "fast-ui")
        self.assertTrue(root.is_relative_to(self.home))
        self.assertFalse(root.is_relative_to(app))
        self.assertEqual(root.name, "fast-ui")
        self.assertRegex(root.parent.name, r"^MyApp-[0-9a-f]{8}$")
        self.assertNotEqual(root, fast_media.default_artifacts_root(other, "fast-ui"))
        self.assertEqual(root, fast_media.default_artifacts_root(app, "fast-ui"))

    def test_home_defaults_to_the_helper_cache_and_can_be_moved(self) -> None:
        self.assertEqual(fast_media.artifacts_home(), self.home)
        with mock.patch.dict(os.environ, {"FAST_ARTIFACTS_HOME": ""}):
            self.assertEqual(fast_media.artifacts_home(), fast_media.HELPER_CACHE / "artifacts")

    # --- age and size -------------------------------------------------------------------
    def test_housekeeping_ages_out_runs_in_every_project(self) -> None:
        old_a = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=4)
        old_b = self.run_dir(PROJECT_B, "fast-device", "t", RUN, age_days=9)
        fresh = self.run_dir(PROJECT_A, "fast-ui", "t", "20260102-000000-SIM", age_days=1)
        result = fast_media.housekeep_artifacts()
        self.assertFalse(old_a.exists())
        self.assertFalse(old_b.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual((result["aged"], result["capped"]), (2, 0))
        self.assertFalse((self.home / PROJECT_B).exists(), "empty project folders go too")
        self.assertTrue(self.home.exists() and (self.home / fast_media.MARKER).exists())

    def test_age_counts_the_newest_file_inside_a_run_not_the_folder(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=9)
        (run / "late.png").write_bytes(b"y")  # written just now, folder mtime stays old
        os.utime(run, (time.time() - 9 * 86400,) * 2)
        fast_media.housekeep_artifacts()
        self.assertTrue(run.exists())

    def test_final_videos_age_out_too_and_other_files_are_left_alone(self) -> None:
        task = self.home / PROJECT_A / "fast-ui" / "t"
        task.mkdir(parents=True)
        final, notes = task / "final-s1.mp4", task / "notes.txt"
        final.write_bytes(b"v")
        notes.write_bytes(b"n")
        self.age(final, 9)
        self.age(notes, 9)
        fast_media.housekeep_artifacts()
        self.assertFalse(final.exists())
        self.assertTrue(notes.exists())

    def test_zero_keep_days_disables_age_pruning(self) -> None:
        old = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=400)
        os.environ["FAST_UI_KEEP_DAYS"] = "0"
        fast_media.housekeep_artifacts()
        self.assertTrue(old.exists())

    def test_size_cap_removes_the_oldest_runs_first(self) -> None:
        mb = 1024 * 1024
        oldest = self.run_dir(PROJECT_A, "fast-ui", "t", "20260101-000001-SIM", size=mb, age_days=2)
        middle = self.run_dir(PROJECT_A, "fast-ui", "t", "20260101-000002-SIM", size=mb, age_days=1.5)
        newest = self.run_dir(PROJECT_B, "fast-device", "t", "20260101-000003-SIM", size=mb, age_days=1)
        os.environ["FAST_ARTIFACTS_MAX_MB"] = "2"
        result = fast_media.housekeep_artifacts()
        self.assertFalse(oldest.exists())
        self.assertTrue(middle.exists() and newest.exists())
        self.assertEqual(result["capped"], 1)
        self.assertLessEqual(result["bytes"], 2 * mb)

    def test_size_cap_at_zero_is_off_and_under_the_cap_deletes_nothing(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, size=1024 * 1024)
        os.environ["FAST_ARTIFACTS_MAX_MB"] = "0"
        self.assertEqual(fast_media.housekeep_artifacts()["capped"], 0)
        os.environ["FAST_ARTIFACTS_MAX_MB"] = "5"
        self.assertEqual(fast_media.housekeep_artifacts()["capped"], 0)
        self.assertTrue(run.exists())

    def test_a_run_written_to_recently_is_never_deleted_even_over_the_cap(self) -> None:
        mb = 1024 * 1024
        live = self.run_dir(PROJECT_A, "fast-device", "t", "20260101-000001-DEV", size=3 * mb, age_days=0)
        os.utime(live, (time.time() - 20 * 60,) * 2)  # folder looks 20 min old, files were just written
        done = self.run_dir(PROJECT_A, "fast-device", "t", "20260101-000002-DEV", size=mb, age_days=1)
        os.environ["FAST_ARTIFACTS_MAX_MB"] = "1"
        result = fast_media.housekeep_artifacts()
        self.assertTrue(live.exists(), "in-progress run must survive the cap")
        self.assertFalse(done.exists())
        self.assertEqual(result["capped"], 1)

    def test_protection_window_is_configurable(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, size=2 * 1024 * 1024, age_days=0.01)
        os.environ["FAST_ARTIFACTS_MAX_MB"] = "1"
        os.environ["FAST_ARTIFACTS_PROTECT_MIN"] = "1"
        fast_media.housekeep_artifacts()
        self.assertFalse(run.exists())

    # --- guards -------------------------------------------------------------------------
    def test_only_the_exact_layout_is_ever_deleted(self) -> None:
        """Depth-4 entries that are not <project>-<hash8>/<kind>/<task>/<run> are never touched."""
        wrong = [
            self.home / "Documents/Repo/Sources/App",            # arbitrary tree
            self.home / PROJECT_A / "other-kind/t" / RUN,         # unknown kind folder
            self.home / "no-hash/fast-ui/t" / RUN,                # project folder without a hash
            self.home / PROJECT_A / "fast-ui/t/not-a-run",        # name is not a run
        ]
        for path in wrong:
            path.mkdir(parents=True)
            (path / "f").write_bytes(b"x")
            self.age(path, 400)
        fast_media.housekeep_artifacts()
        self.clean("--all")
        self.assertTrue(all(path.exists() for path in wrong))

    def test_symlinks_are_never_followed(self) -> None:
        outside = self.tmp / "outside"
        (outside / RUN).mkdir(parents=True)
        (outside / RUN / "keep").write_bytes(b"x")
        self.age(outside, 400)
        (self.home / PROJECT_A / "fast-ui").mkdir(parents=True)
        os.symlink(outside, self.home / PROJECT_A / "fast-ui" / "task")
        os.symlink(outside, self.home / PROJECT_B)
        task = self.home / PROJECT_A / "fast-device" / "t"
        task.mkdir(parents=True)
        os.symlink(outside / RUN, task / RUN)
        fast_media.housekeep_artifacts()
        self.clean("--all")
        self.assertTrue((outside / RUN / "keep").exists())

    def test_unsafe_homes_are_refused(self) -> None:
        user = Path.home().resolve()
        for bad in ["~", "/", str(user), str(user.parent), "relative/dir", str(fast_media.HELPER_CACHE)]:
            with self.subTest(home=bad), mock.patch.dict(os.environ, {"FAST_ARTIFACTS_HOME": bad}):
                self.assertIsNotNone(fast_media.home_problem(fast_media.artifacts_home()), bad)
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"FAST_ARTIFACTS_HOME": str(repo)}):
            self.assertIn("git", fast_media.home_problem(repo))

    def test_a_folder_with_files_but_no_marker_is_not_ours_and_nothing_is_deleted(self) -> None:
        other = self.tmp / "someones-folder"
        victim = other / PROJECT_A / "fast-ui" / "t" / RUN
        victim.mkdir(parents=True)
        self.age(victim, 400)
        with mock.patch.dict(os.environ, {"FAST_ARTIFACTS_HOME": str(other)}):
            self.assertEqual(fast_media.housekeep_artifacts()["skipped"], 1)
            self.assertEqual(self.clean("--all")[0], 1)
            fast_media.prepare_artifacts_root(other / "x", custom=False)
        self.assertTrue(victim.exists())
        self.assertFalse((other / fast_media.MARKER).exists())

    def test_an_absent_or_empty_home_is_created_and_marked_by_the_first_run(self) -> None:
        fresh = self.tmp / "fresh-home"
        with mock.patch.dict(os.environ, {"FAST_ARTIFACTS_HOME": str(fresh)}):
            fast_media.prepare_artifacts_root(fresh / "p-1a2b3c4d" / "fast-ui", custom=False)
        self.assertTrue((fresh / fast_media.MARKER).exists())

    def test_bad_environment_values_fall_back_to_defaults_and_never_raise(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=9)
        os.environ["FAST_ARTIFACTS_MAX_MB"] = "2G"
        os.environ["FAST_UI_KEEP_DAYS"] = "three"
        os.environ["FAST_ARTIFACTS_PROTECT_MIN"] = "-5"
        result = fast_media.housekeep_artifacts()
        self.assertEqual(result["cap"], int(fast_media.DEFAULT_MAX_MB * 1024 * 1024))
        self.assertEqual(fast_media.keep_days(), fast_media.DEFAULT_KEEP_DAYS)
        self.assertFalse(run.exists(), "defaults apply: 9 days old is older than 3")
        fast_media.prepare_artifacts_root(self.home / "p-1a2b3c4d" / "fast-ui", custom=False)

    def test_housekeeping_without_a_home_is_a_no_op(self) -> None:
        shutil.rmtree(self.home)
        self.assertEqual(fast_media.housekeep_artifacts()["aged"], 0)
        self.assertFalse(self.home.exists())

    def test_warning_only_appears_when_the_cap_deleted_something(self) -> None:
        import contextlib
        import io
        quiet, loud = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(quiet):
            fast_media.warn_if_capped({"capped": 0, "aged": 5, "bytes": 1, "cap": 2 * 1024 * 1024})
        with contextlib.redirect_stderr(loud):
            fast_media.warn_if_capped({"capped": 3, "aged": 0, "bytes": 1024 * 1024, "cap": 2 * 1024 * 1024})
        self.assertEqual(quiet.getvalue(), "")
        self.assertIn("removed 3 oldest runs", loud.getvalue())
        self.assertIn("clean", loud.getvalue())

    # --- custom roots and the default root ------------------------------------------------
    def test_a_custom_root_is_only_age_pruned_and_never_touches_the_home(self) -> None:
        custom = self.tmp / "mine"
        old = custom / "t" / RUN
        old.mkdir(parents=True)
        (old / "metrics.json").write_text("{}")
        stray = custom / "t" / "keep-me"
        stray.mkdir()
        self.age(old, 5)
        self.age(stray, 5)
        in_home = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=9)
        fast_media.prepare_artifacts_root(custom, custom=True)
        self.assertFalse(old.exists())
        self.assertTrue(stray.exists(), "only run-named folders are pruned")
        self.assertTrue(in_home.exists())

    def test_the_default_root_runs_housekeeping_across_projects(self) -> None:
        in_home = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=9)
        fast_media.prepare_artifacts_root(self.home / PROJECT_B / "fast-ui", custom=False)
        self.assertFalse(in_home.exists())

    # --- clean --------------------------------------------------------------------------
    def clean(self, *argv: str) -> tuple[int, str]:
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = fast_media.clean_main(list(argv))
        return code, out.getvalue()

    def test_clean_lists_by_default_and_changes_nothing(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, size=2048)
        code, out = self.clean()
        self.assertEqual(code, 0)
        self.assertIn(PROJECT_A, out)
        self.assertIn("runs=1", out)
        self.assertTrue(run.exists())

    def test_clean_list_wins_over_all(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN)
        self.clean("--list", "--all")
        self.assertTrue(run.exists())

    def test_clean_on_a_missing_home_says_empty(self) -> None:
        shutil.rmtree(self.home)
        self.assertEqual(self.clean("--all"), (0, f"EMPTY {self.home}\n"))

    def test_clean_days_deletes_only_older_runs_and_rejects_negative(self) -> None:
        old = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=10)
        fresh = self.run_dir(PROJECT_A, "fast-ui", "t", "20260102-000000-SIM", age_days=1)
        _, out = self.clean("--days", "3")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertIn("DELETED 1/2 runs", out)
        with self.assertRaises(SystemExit):
            self.clean("--days", "-1")
        self.assertTrue(fresh.exists())

    def test_clean_all_empties_the_home_but_a_dry_run_does_not(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN)
        _, dry = self.clean("--all", "--dry-run")
        self.assertTrue(run.exists())
        self.assertIn("WOULD DELETE 1/1 runs", dry)
        self.assertIn("would free", dry)
        _, out = self.clean("--all")
        self.assertFalse(run.exists())
        self.assertIn("DELETED 1/1 runs", out)
        self.assertFalse((self.home / PROJECT_A).exists())

    def test_clean_keeps_runs_that_may_still_be_running(self) -> None:
        live = self.run_dir(PROJECT_A, "fast-device", "t", RUN, age_days=0)
        _, out = self.clean("--all")
        self.assertTrue(live.exists())
        self.assertIn("1 in use", out)

    def test_clean_project_filter_leaves_other_projects_alone(self) -> None:
        mine = self.run_dir(PROJECT_A, "fast-ui", "t", RUN)
        other = self.run_dir(PROJECT_B, "fast-ui", "t", RUN)
        self.clean("--all", "--project", "MyApp")
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())

    def test_both_engines_dispatch_clean_before_touching_a_device_or_flow(self) -> None:
        import fast_ui
        with mock.patch.object(fast_ui.sys, "argv", ["fastSimulatorTest", "clean", "--list"]), \
                mock.patch.object(fast_ui, "clean_main", return_value=0) as ui_clean:
            self.assertEqual(fast_ui.main(), 0)
        ui_clean.assert_called_once_with(["--list"])
        with mock.patch.object(fast_device.sys, "argv", ["fastDeviceTest", "clean", "--all"]), \
                mock.patch.object(fast_device, "clean_main", return_value=0) as device_clean, \
                mock.patch.object(fast_device, "resolve_device", side_effect=AssertionError("no device needed")):
            self.assertEqual(fast_device.main(), 0)
        device_clean.assert_called_once_with(["--all"])

    def test_the_unit_test_launcher_cache_key_has_no_trailing_dash(self) -> None:
        script = (Path(__file__).resolve().parent / "fast_device_test.sh").read_text()
        self.assertIn("printf '%s' \"$(basename \"$PROJECT_ROOT\")\" | tr -cs", script)


    # --- fastUnitTest build caches ----------------------------------------------------------
    def unit_cache(self, project: str, key: str, age_days: float, size: int = 100) -> Path:
        self.unit_home.mkdir(exist_ok=True)
        (self.unit_home / fast_media.MARKER).touch()
        cache = self.unit_home / project / key
        (cache / "DerivedData").mkdir(parents=True)
        (cache / "DerivedData" / "blob").write_bytes(b"x" * size)
        (cache / "logs").mkdir()
        (cache / "source.fingerprint").write_text("f")
        stamp = time.time() - age_days * 86400
        for item in (cache / "logs", cache / "source.fingerprint", cache):
            os.utime(item, (stamp, stamp))
        return cache

    def test_unused_build_caches_age_out_and_recent_ones_stay(self) -> None:
        old = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=40)
        fresh = self.unit_cache(PROJECT_B, "Scheme-Debug-UDID", age_days=5)
        self.assertEqual(fast_media.housekeep_unit_caches(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse((self.unit_home / PROJECT_A).exists(), "empty project folder goes too")

    def test_a_build_cache_used_by_a_recent_run_is_kept_whatever_its_folder_age(self) -> None:
        cache = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=60)
        (cache / "logs" / "run.log").write_text("x")
        os.utime(cache / "logs", None)  # a run just wrote a log
        self.assertEqual(fast_media.housekeep_unit_caches(), 0)
        self.assertTrue(cache.exists())

    def test_build_cache_age_is_configurable_and_zero_keeps_everything(self) -> None:
        cache = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=10)
        os.environ["FAST_UNIT_CACHE_DAYS"] = "0"
        self.assertEqual(fast_media.housekeep_unit_caches(), 0)
        os.environ["FAST_UNIT_CACHE_DAYS"] = "7"
        self.assertEqual(fast_media.housekeep_unit_caches(), 1)
        self.assertFalse(cache.exists())

    def test_build_cache_housekeeping_follows_the_ownership_rules(self) -> None:
        stray = self.unit_home / "Documents/Repo/x"
        stray.mkdir(parents=True)
        os.utime(stray, (1, 1))
        no_hash = self.unit_home / "no-hash" / "Scheme"
        no_hash.mkdir(parents=True)
        os.utime(no_hash, (1, 1))
        outside = self.tmp / "outside"
        (outside / "DerivedData").mkdir(parents=True)
        os.utime(outside, (1, 1))
        (self.unit_home / fast_media.MARKER).touch()
        os.symlink(outside, self.unit_home / PROJECT_A)
        fast_media.housekeep_unit_caches()
        self.assertTrue(stray.exists() and no_hash.exists() and (outside / "DerivedData").exists())
        other = self.tmp / "unmarked"
        victim = other / PROJECT_A / "Scheme"
        victim.mkdir(parents=True)
        os.utime(victim, (1, 1))
        with mock.patch.dict(os.environ, {"FAST_UNIT_TESTS_HOME": str(other)}):
            self.assertEqual(fast_media.housekeep_unit_caches(), 0)
        self.assertTrue(victim.exists())

    def test_prepare_unit_cache_marks_the_home_and_never_raises(self) -> None:
        old = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=40)
        fast_media.prepare_unit_cache()
        self.assertFalse(old.exists())
        shutil.rmtree(self.unit_home)
        fast_media.prepare_unit_cache()
        self.assertTrue((self.unit_home / fast_media.MARKER).exists())
        with mock.patch.dict(os.environ, {"FAST_UNIT_CACHE_DAYS": "x", "FAST_UNIT_TESTS_HOME": "relative"}):
            fast_media.prepare_unit_cache()

    def test_clean_lists_build_caches_and_leaves_them_alone(self) -> None:
        cache = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=12, size=2048)
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN)
        code, out = self.clean()
        self.assertEqual(code, 0)
        self.assertIn(f"build cache {PROJECT_A}/Scheme-Debug-UDID", out)
        self.assertIn("last used 12d ago", out)
        self.clean("--all")
        self.assertTrue(cache.exists(), "--all is for run artifacts only")
        self.assertFalse(run.exists())

    def test_clean_builds_deletes_only_build_caches(self) -> None:
        old = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=12)
        fresh = self.unit_cache(PROJECT_B, "Scheme-Debug-UDID", age_days=0)
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN)
        _, dry = self.clean("--builds", "--dry-run")
        self.assertTrue(old.exists())
        self.assertIn("WOULD DELETE build cache", dry)
        _, out = self.clean("--builds")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), "used within the protect window")
        self.assertIn("KEPT", out)
        self.assertTrue(run.exists(), "--builds never touches run artifacts")

    def test_clean_builds_with_days_and_project_filters(self) -> None:
        young = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=3)
        old_a = self.unit_cache(PROJECT_A, "Scheme-Release-UDID", age_days=20)
        old_b = self.unit_cache(PROJECT_B, "Scheme-Debug-UDID", age_days=20)
        self.clean("--builds", "--days", "10", "--project", "MyApp")
        self.assertTrue(young.exists())
        self.assertFalse(old_a.exists())
        self.assertTrue(old_b.exists())

    def test_clean_days_alone_leaves_build_caches_alone(self) -> None:
        cache = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=60)
        self.clean("--days", "1")
        self.assertTrue(cache.exists())

    def test_clean_refuses_an_unmarked_build_cache_home(self) -> None:
        other = self.tmp / "unmarked"
        victim = other / PROJECT_A / "Scheme"
        victim.mkdir(parents=True)
        os.utime(victim, (1, 1))
        with mock.patch.dict(os.environ, {"FAST_UNIT_TESTS_HOME": str(other)}):
            self.assertEqual(self.clean("--builds")[0], 1)
        self.assertTrue(victim.exists())

    def test_the_launcher_and_the_shell_runner_use_the_build_cache_home(self) -> None:
        here = Path(__file__).resolve().parent
        launcher = (here / "fastUnitTest").read_text()
        self.assertIn("prepare_unit_cache()", launcher)
        runner = (here / "fast_device_test.sh").read_text()
        self.assertIn("FAST_UNIT_TESTS_HOME", runner)
        self.assertIn(".fast-ios-device-test-artifacts", runner)

    # --- second review: identity checks, overlap, shell marker, in-use caches ---------------------
    def test_identity_rules_refuse_empty_folders_the_marker_rule_would_accept(self) -> None:
        """Each of these is empty, so only the root/home/parent/helper-cache rules can refuse it."""
        user = self.tmp / "fakehome" / "me"
        user.mkdir(parents=True)
        helper = self.tmp / "helper-cache"
        helper.mkdir()
        with mock.patch.object(fast_media.Path, "home", return_value=user), \
                mock.patch.object(fast_media, "HELPER_CACHE", helper):
            self.assertIn("home folder", fast_media.home_problem(user) or "")
            self.assertIn("home folder", fast_media.home_problem(user.parent) or "")
            self.assertIn("helper cache", fast_media.home_problem(helper) or "")
            self.assertIn("home folder", fast_media.unsafe_root(Path("/")) or "")
            self.assertIsNone(fast_media.home_problem(self.tmp / "ok-home"))

    def test_letter_case_cannot_hide_the_home_folder_on_a_case_insensitive_volume(self) -> None:
        user = self.tmp / "Fakehome" / "Me"
        user.mkdir(parents=True)
        twisted = self.tmp / "FAKEHOME" / "ME"
        if not twisted.exists():
            self.skipTest("case-sensitive volume")
        with mock.patch.object(fast_media.Path, "home", return_value=user):
            self.assertIsNotNone(fast_media.home_problem(twisted))
            self.assertIsNotNone(fast_media.home_problem(twisted.parent))

    def test_a_home_with_only_a_ds_store_is_still_ours_to_use(self) -> None:
        folder = self.tmp / "finder-made"
        folder.mkdir()
        (folder / ".DS_Store").write_bytes(b"x")
        self.assertIsNone(fast_media.home_problem(folder))
        (folder / "other").write_bytes(b"x")
        self.assertIsNotNone(fast_media.home_problem(folder))

    def test_equal_or_nested_artifact_and_build_cache_homes_are_refused(self) -> None:
        for unit in (self.home, self.home / "unit-inside"):
            with self.subTest(unit=str(unit)), mock.patch.dict(os.environ, {"FAST_UNIT_TESTS_HOME": str(unit)}):
                live = self.run_dir(PROJECT_A, "fast-ui", "t", "20260101-000001-LIVE", age_days=0)
                os.utime(live, (1, 1))
                os.utime(live / "metrics.json", (time.time(),) * 2)
                self.assertIsNotNone(fast_media.home_problem(fast_media.artifacts_home()))
                self.assertIsNotNone(fast_media.home_problem(fast_media.unit_tests_home()))
                self.assertEqual(fast_media.housekeep_unit_caches(), 0)
                self.assertEqual(self.clean("--builds")[0], 1)
                self.assertTrue(live.exists())
                shutil.rmtree(self.home / PROJECT_A)

    def test_non_finite_numbers_fall_back_to_defaults(self) -> None:
        run = self.run_dir(PROJECT_A, "fast-ui", "t", RUN, age_days=9)
        for name in ("FAST_ARTIFACTS_MAX_MB", "FAST_UI_KEEP_DAYS", "FAST_ARTIFACTS_PROTECT_MIN", "FAST_UNIT_CACHE_DAYS"):
            os.environ[name] = "inf"
        result = fast_media.housekeep_artifacts()
        self.assertEqual(result["cap"], int(fast_media.DEFAULT_MAX_MB * 1024 * 1024))
        self.assertEqual(fast_media.protect_seconds(), fast_media.DEFAULT_PROTECT_MINUTES * 60)
        self.assertEqual(fast_media.unit_cache_days(), fast_media.DEFAULT_UNIT_CACHE_DAYS)
        self.assertFalse(run.exists())

    def shell_marking(self, folder: Path) -> bool:
        """Run the launcher's real cache-home block in bash and say whether it left the marker."""
        script = (Path(__file__).resolve().parent / "fast_device_test.sh").read_text()
        start = script.index('if [[ -z "$CACHE_ROOT" ]]; then')
        block = script[start:script.index("\nfi\n", start) + 4]
        code = f'set -euo pipefail\nCACHE_ROOT=""\nPROJECT_ROOT=/work/MyApp\n{block}\necho "$CACHE_ROOT"\n'
        done = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                              env={**os.environ, "FAST_UNIT_TESTS_HOME": str(folder)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout.strip(), rf"^{folder}/MyApp-[0-9a-f]{{8}}$")
        return (folder / fast_media.MARKER).exists()

    def test_the_shell_marks_only_a_new_or_empty_cache_home(self) -> None:
        mine = self.tmp / "work-folder"
        mine.mkdir()
        (mine / "readme").write_text("yours")
        (mine / "myapp-1a2b3c4d").mkdir()
        self.assertFalse(self.shell_marking(mine), "a folder with your files must stay unmarked")
        self.assertIsNotNone(fast_media.home_problem(mine))
        self.assertTrue(self.shell_marking(self.tmp / "absent"))
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertTrue(self.shell_marking(empty))
        finder = self.tmp / "finder"
        finder.mkdir()
        (finder / ".DS_Store").write_bytes(b"x")
        self.assertTrue(self.shell_marking(finder))
        marked = self.tmp / "marked"
        marked.mkdir()
        (marked / fast_media.MARKER).touch()
        (marked / "old-1a2b3c4d").mkdir()
        self.assertTrue(self.shell_marking(marked))

    def test_a_build_in_progress_is_kept_though_its_folders_look_old(self) -> None:
        cache = self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=45)
        (cache / "logs" / "20260101-000000-build.log").write_text("still compiling")  # tee appends, folder time stays
        old = time.time() - 45 * 86400
        os.utime(cache / "logs", (old, old))
        os.utime(cache, (old, old))
        self.assertGreater(fast_media._last_used(cache), time.time() - 60)
        self.assertEqual(fast_media.housekeep_unit_caches(), 0)
        self.assertIn("KEPT", self.clean("--builds")[1])
        self.assertTrue(cache.exists())

    def test_only_folders_that_look_like_a_build_cache_are_deleted(self) -> None:
        self.unit_home.mkdir(exist_ok=True)
        (self.unit_home / fast_media.MARKER).touch()
        ordinary = self.unit_home / "myapp-1a2b3c4d" / "Sources"
        ordinary.mkdir(parents=True)
        (ordinary / "main.swift").write_text("code")
        os.utime(ordinary, (1, 1))
        self.assertEqual(fast_media.housekeep_unit_caches(), 0)
        self.clean("--builds")
        self.assertTrue((ordinary / "main.swift").exists())

    def test_custom_roots_get_the_same_protections(self) -> None:
        custom = self.tmp / "my-reports"
        (custom / "t" / RUN).mkdir(parents=True)
        (custom / "t" / RUN / "m.json").write_text("{}")
        self.age(custom / "t" / RUN, 9)
        export = custom / "Movies" / "final_cut_export.mp4"
        export.parent.mkdir()
        export.write_bytes(b"x")
        empty = custom / "EmptyDir"
        empty.mkdir()
        live = custom / "t2" / "20260101-000009-LIVE"
        live.mkdir(parents=True)
        (live / "run.mp4").write_bytes(b"x")
        recent = time.time() - 5 * 60  # older than the keep days below, inside the 30 minute protect window
        os.utime(live / "run.mp4", (recent, recent))
        os.utime(live, (1, 1))  # the folder itself looks ancient
        self.age(export, 400)
        self.age(empty, 400)
        self.assertEqual(fast_media.prune_artifacts(custom, 0.001), 1)  # keep days = 86 s
        self.assertTrue(export.exists() and empty.exists() and live.exists())
        self.assertFalse((custom / "t").exists(), "the task folder it emptied goes")
        user = self.tmp / "fakehome" / "me"
        (user / "Pictures" / RUN).mkdir(parents=True)
        self.age(user / "Pictures" / RUN, 400)
        with mock.patch.object(fast_media.Path, "home", return_value=user):
            self.assertEqual(fast_media.prune_artifacts(user, 3), 0)
            self.assertEqual(fast_media.prune_artifacts(user.parent, 3), 0)
        self.assertTrue((user / "Pictures" / RUN).exists())

    def test_final_video_names_are_exact(self) -> None:
        for name, ok in [("final.mp4", True), ("final-s1.mp4", True), ("final_cut_export.mp4", False),
                         ("finals.mp4", False), ("final.mov", False)]:
            self.assertEqual(bool(fast_media.FINAL_NAME.match(name)), ok, name)

    def test_mkdir_retry_survives_a_parent_removed_in_between_and_gives_up_after_five(self) -> None:
        target = self.tmp / "a" / "b"
        real = Path.mkdir
        calls = {"n": 0}

        def flaky(self_path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise FileNotFoundError("parent vanished")
            return real(self_path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", flaky):
            fast_media.mkdir_retry(target)
        self.assertTrue(target.is_dir())
        calls["n"] = -100
        with mock.patch.object(Path, "mkdir", side_effect=FileNotFoundError("gone")) as always:
            with self.assertRaises(FileNotFoundError):
                fast_media.mkdir_retry(self.tmp / "never")
        self.assertEqual(always.call_count, 5)

    def test_clean_builds_reports_the_build_cache_home(self) -> None:
        self.unit_cache(PROJECT_A, "Scheme-Debug-UDID", age_days=12)
        _, out = self.clean("--builds", "--dry-run")
        self.assertIn(f"home={self.unit_home}", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
