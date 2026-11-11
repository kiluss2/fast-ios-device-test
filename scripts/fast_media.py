#!/usr/bin/env python3

"""Media helpers shared by the Simulator and device engines: review-quality video, the
one-final-clip-per-task rule, and the cached AVFoundation helper binaries.

Imports nothing outside the standard library, so both engines load it without Pillow.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HELPER_CACHE = Path.home() / "Library/Caches/fast-ios-device-test"

# Review quality: copy stays readable and transitions stay smooth at a size a PR attachment
# can carry. A 17 s iPhone recording goes from 20 MB (1290x2796, 10 Mbit/s) to about 1.4 MB.
# FAST_VIDEO_LONG_SIDE=0 keeps the original recording.
REVIEW_LONG_SIDE = int(os.environ.get("FAST_VIDEO_LONG_SIDE", "960"))
REVIEW_KBPS = int(os.environ.get("FAST_VIDEO_KBPS", "800"))
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}

SHRINK_SOURCE = '''
import AVFoundation
// shrink <in> <out> <longSide> <kbps>: H.264 MP4, aspect kept, never upscaled, no audio.
let args = CommandLine.arguments
guard args.count == 5, let longSide = Double(args[3]), let kbps = Int(args[4]) else {
    FileHandle.standardError.write("usage: shrink <in> <out> <longSide> <kbps>\\n".data(using: .utf8)!)
    exit(2)
}
let source = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let target = URL(fileURLWithPath: args[2])
try? FileManager.default.removeItem(at: target)
guard let track = source.tracks(withMediaType: .video).first else {
    FileHandle.standardError.write("no video track\\n".data(using: .utf8)!)
    exit(1)
}
let natural = track.naturalSize
let scale = min(1, longSide / max(natural.width, natural.height))
let width = Int((natural.width * scale / 2).rounded()) * 2
let height = Int((natural.height * scale / 2).rounded()) * 2
do {
    let reader = try AVAssetReader(asset: source)
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
    ])
    output.alwaysCopiesSampleData = false
    reader.add(output)
    let writer = try AVAssetWriter(outputURL: target, fileType: .mp4)
    let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
        AVVideoCodecKey: AVVideoCodecType.h264,
        AVVideoWidthKey: width,
        AVVideoHeightKey: height,
        AVVideoScalingModeKey: AVVideoScalingModeResizeAspect,
        AVVideoCompressionPropertiesKey: [
            AVVideoAverageBitRateKey: kbps * 1000,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
        ],
    ])
    input.expectsMediaDataInRealTime = false
    input.transform = track.preferredTransform
    writer.add(input)
    guard reader.startReading(), writer.startWriting() else {
        FileHandle.standardError.write("cannot start: \\(reader.error ?? writer.error as Any)\\n".data(using: .utf8)!)
        exit(1)
    }
    var started = false
    while let sample = output.copyNextSampleBuffer() {
        if !started {
            writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sample))
            started = true
        }
        while !input.isReadyForMoreMediaData { usleep(2000) }
        if !input.append(sample) { break }
    }
    input.markAsFinished()
    let done = DispatchSemaphore(value: 0)
    writer.finishWriting { done.signal() }
    done.wait()
    guard started, reader.status == .completed, writer.status == .completed else {
        FileHandle.standardError.write("failed: \\(reader.error ?? writer.error as Any)\\n".data(using: .utf8)!)
        exit(1)
    }
    print("\\(width)x\\(height)")
} catch {
    FileHandle.standardError.write("failed: \\(error)\\n".data(using: .utf8)!)
    exit(1)
}
'''


def helper_command(source: str, cache_dir: Path = HELPER_CACHE, name: str = "frames") -> list[str]:
    """Command prefix that runs an AVFoundation helper: a binary compiled once per helper
    source (`swiftc`, ~1 s, then 0.03 s per call) or, if that fails, the `swift`
    interpreter (~4 s on first use, 0.3 s after)."""
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary = cache_dir / f"{name}-{digest}"
    script = cache_dir / f"{name}-{digest}.swift"
    if binary.exists():
        return [str(binary)]
    script.write_text(source, encoding="utf-8")
    compiled = subprocess.run(
        ["swiftc", "-O", str(script), "-o", str(binary)], check=False, capture_output=True, text=True, timeout=300
    )
    if compiled.returncode == 0:
        return [str(binary)]
    return ["swift", str(script)]


def shrink_video(source: Path, target: Path | None = None, long_side: int = REVIEW_LONG_SIDE, kbps: int = REVIEW_KBPS) -> Path:
    """Move `source` to `target` (default: in place) re-encoded at review quality.

    The re-encode only replaces the original when it succeeded and came out smaller; any
    failure keeps the original recording under the target name, so evidence is never lost.
    """
    target = target or source
    if not source.exists():
        return source
    keep_original = long_side <= 0
    encoded = target.with_name(f".{target.stem}.review.mp4")
    if not keep_original:
        try:
            completed = subprocess.run(
                [*helper_command(SHRINK_SOURCE, name="shrink"), str(source), str(encoded), str(long_side), str(kbps)],
                check=False, capture_output=True, text=True, timeout=600,
            )
            keep_original = (
                completed.returncode != 0
                or not encoded.exists()
                or encoded.stat().st_size == 0
                or encoded.stat().st_size >= source.stat().st_size
            )
        except (OSError, subprocess.TimeoutExpired):
            keep_original = True
    if keep_original:
        encoded.unlink(missing_ok=True)
        if source != target:
            shutil.move(str(source), target)
        return target
    encoded.replace(target)
    if source != target:
        source.unlink(missing_ok=True)
    return target


def publish_video(task_dir: Path, video: Path, passed: bool, session: str = "") -> Path | None:
    """Keep exactly one final video per task (per session when tagged); failures keep only their own clip."""
    if not video.exists():
        return None
    for old in task_dir.glob("*/run.mp4"):
        if old != video:
            old.unlink(missing_ok=True)
    if not passed:
        return video
    final = task_dir / (f"final-{session}.mp4" if session else "final.mp4")
    shutil.move(str(video), final)
    return final


def prune_entries(folder: Path, keep_days: float) -> int:
    """Delete what sits directly in `folder` untouched for `keep_days`; 0 keeps everything."""
    if keep_days <= 0 or not folder.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for entry in folder.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed
RUN_NAME = re.compile(r"^\d{8}-\d{6}-")  # <timestamp>-<device/pid>: what both engines name a run folder
FINAL_NAME = re.compile(r"^final(-[^/]+)?\.mp4$")  # <task>/final[-<session>].mp4
PROJECT_NAME = re.compile(r"^.+-[0-9a-f]{8}$")  # <project>-<hash8>, see default_artifacts_root
KINDS = ("fast-ui", "fast-device")
MARKER = ".fast-ios-device-test-artifacts"


def _is_run_entry(path: Path) -> bool:
    """A run folder or final video, by name; symlinks and anything else are never touched."""
    if path.is_symlink():
        return False
    if path.is_dir():
        return bool(RUN_NAME.match(path.name))
    return bool(FINAL_NAME.match(path.name))


def _same(a: Path, b: Path) -> bool:
    """Same filesystem object (survives letter case on macOS volumes, unlike comparing strings)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def unsafe_root(root: Path) -> str | None:
    """Why no run folder may be deleted under `root` (a custom or default root), or None."""
    try:
        resolved = Path(root).expanduser().resolve()
        user = Path.home().resolve()
    except OSError:
        return "cannot resolve the folder"
    if _same(resolved, Path(resolved.anchor)) or _same(resolved, user) or any(_same(resolved, p) for p in user.parents):
        return f"{root} is your home folder or one of its parents"
    if _same(resolved, HELPER_CACHE):
        return f"{root} is the helper cache itself"
    return None


def prune_artifacts(root: Path, keep_days: float) -> int:
    """Delete run folders and final videos untouched for `keep_days` (never one written to within the
    protect window); task folders this emptied go too. Refuses `/`, your home folder and its parents."""
    if keep_days <= 0 or not root.is_dir() or unsafe_root(root):
        return 0
    now = time.time()
    cutoff, busy = now - keep_days * 86400, now - protect_seconds()
    removed = 0
    for task_dir in root.iterdir():
        if task_dir.is_symlink() or not task_dir.is_dir():
            continue
        emptied = False
        for entry in task_dir.iterdir():
            try:
                if not _is_run_entry(entry):
                    continue
                newest = _newest_mtime(entry)
                if newest >= cutoff or newest >= busy:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
                emptied = True
            except OSError:
                continue
        try:
            if emptied and not any(task_dir.iterdir()):
                task_dir.rmdir()
        except OSError:
            pass
    return removed


def mkdir_retry(path: Path, attempts: int = 5) -> None:
    """mkdir -p that survives another run's cleanup removing an empty parent between two steps."""
    for attempt in range(attempts):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except FileNotFoundError:
            if attempt == attempts - 1:
                raise


def ready_do_folder(out: Path, default: Path) -> Path:
    """`out` created for a `do` request. In the default folder, shots and dumps untouched for
    FAST_UI_KEEP_DAYS (3) go first, as run folders do; a folder given with --out is the caller's."""
    if out == default:
        prune_entries(default, keep_days())
    mkdir_retry(out)
    return out


# --- Where run artifacts live -------------------------------------------------------------
# Runs are written outside the project (a repo should not collect screenshots, xcresults and
# logs), one folder per project: <home>/<project name>-<hash of its path>/{fast-ui,fast-device}.
# Everything under <home> is disposable and bounded: age pruning and a size cap run on every
# default-root run, and `clean` lists or empties it by hand. Deletion is deliberately narrow:
# only a home this tool marked, only <project>-<hash8>/<kind>/<task>/<run or final video>, never
# a symlink, never a run that was written to recently.
DEFAULT_KEEP_DAYS = 3.0
DEFAULT_MAX_MB = 2048.0
DEFAULT_PROTECT_MINUTES = 30.0


def artifacts_home() -> Path:
    """Parent of every project's default artifacts (FAST_ARTIFACTS_HOME overrides)."""
    return Path(os.environ.get("FAST_ARTIFACTS_HOME") or HELPER_CACHE / "artifacts").expanduser()


def default_artifacts_root(project_root: Path, kind: str) -> Path:
    """Default root for `kind` ("fast-ui" or "fast-device") runs of one project."""
    resolved = Path(project_root).expanduser().resolve()
    key = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in resolved.name) or "project"
    return artifacts_home() / f"{safe}-{key}" / kind


def _env_number(name: str, default: float) -> float:
    """A non-negative number from the environment; anything else falls back to the default."""
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def keep_days() -> float:
    return _env_number("FAST_UI_KEEP_DAYS", DEFAULT_KEEP_DAYS)


def max_bytes() -> int:
    """Size cap of the whole artifacts home in bytes; FAST_ARTIFACTS_MAX_MB=0 disables it."""
    return int(_env_number("FAST_ARTIFACTS_MAX_MB", DEFAULT_MAX_MB) * 1024 * 1024)


def protect_seconds() -> float:
    """A run written to this recently may still be running: never deleted by anything."""
    return _env_number("FAST_ARTIFACTS_PROTECT_MIN", DEFAULT_PROTECT_MINUTES) * 60


def _overlaps(a: Path, b: Path) -> bool:
    """Same folder, or one inside the other (identity-based, so letter case cannot hide it)."""
    try:
        ra, rb = a.resolve(), b.resolve()
    except OSError:
        return False
    return _same(ra, rb) or any(_same(ra, p) for p in rb.parents) or any(_same(rb, p) for p in ra.parents)


def home_problem(home: Path) -> str | None:
    """Why deleting under `home` is refused, or None when it is a home this tool owns."""
    if not home.is_absolute():
        return "the folder must be an absolute path"
    problem = unsafe_root(home)
    if problem:
        return problem
    if (home / ".git").exists():
        return f"{home} is a git checkout"
    if home.is_symlink():
        return f"{home} is a symlink"
    if home.is_dir() and not (home / MARKER).exists():
        if any(child.name != ".DS_Store" for child in home.iterdir()):
            return f"{home} has files but no {MARKER} marker, so it is not a folder of this tool"
    for mine, other in ((artifacts_home(), unit_tests_home()), (unit_tests_home(), artifacts_home())):
        if (home == mine or _same(home, mine)) and _overlaps(mine, other):
            return f"the artifacts folder and the build cache folder overlap ({artifacts_home()} / {unit_tests_home()})"
    return None


def ensure_home(home: Path) -> str | None:
    """Create and mark an absent or empty home; return why it cannot be used for deletion."""
    problem = home_problem(home)
    if problem:
        return problem
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / MARKER).touch(exist_ok=True)
    except OSError as error:
        return f"cannot create {home}: {error}"
    return None


def path_size(path: Path) -> int:
    """Bytes under `path` (a file counts as itself); symlinks count as themselves, unreadable as 0."""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        total = 0
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                total += path_size(Path(entry.path))
            else:
                try:
                    total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _newest_mtime(path: Path) -> float:
    """Newest modification time of `path` or anything inside it: a folder's own mtime does not move
    while a recording or an .xcresult bundle inside it is still being written."""
    try:
        newest = path.lstat().st_mtime
        if path.is_symlink() or not path.is_dir():
            return newest
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                newest = max(newest, _newest_mtime(Path(entry.path)))
            else:
                try:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
        return newest
    except OSError:
        return 0.0


def _remove(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        return False


def _real_dirs(parent: Path, pattern: str | None = None) -> list[Path]:
    """Sorted child directories of `parent` that are not symlinks (optionally matching `pattern`)."""
    try:
        return sorted(
            c for c in parent.iterdir()
            if c.is_dir() and not c.is_symlink() and (pattern is None or re.match(pattern, c.name))
        )
    except OSError:
        return []


def _run_entries(home: Path) -> list[tuple[float, int, Path]]:
    """(newest mtime inside, bytes, path) of every run folder and final video that sits exactly at
    <home>/<project>-<hash8>/<fast-ui|fast-device>/<task>/<run>, in a home that passed `home_problem`."""
    found: list[tuple[float, int, Path]] = []
    for project in _real_dirs(home, PROJECT_NAME.pattern):
        for kind in KINDS:
            root = project / kind
            if root.is_symlink():
                continue
            for task_dir in _real_dirs(root):
                try:
                    children = list(task_dir.iterdir())
                except OSError:
                    continue
                for entry in children:
                    if _is_run_entry(entry):
                        found.append((_newest_mtime(entry), path_size(entry), entry))
    return found


def _drop_empty_dirs(home: Path) -> None:
    """Remove task, kind and project folders left empty (never the home itself)."""
    for project in _real_dirs(home, PROJECT_NAME.pattern):
        for kind in KINDS:
            root = project / kind
            if root.is_symlink() or not root.is_dir():
                continue
            for task_dir in _real_dirs(root):
                try:
                    if not any(task_dir.iterdir()):
                        task_dir.rmdir()
                except OSError:
                    pass
            try:
                if not any(root.iterdir()):
                    root.rmdir()
            except OSError:
                pass
        try:
            if project.is_dir() and not any(project.iterdir()):
                project.rmdir()
        except OSError:
            pass


def housekeep_artifacts(now: float | None = None) -> dict[str, int]:
    """Bound the artifacts home: drop runs older than the keep days in every project, then the
    oldest runs until the home fits the size cap. Runs written to within the protect window are
    never deleted. Does nothing on a home this tool does not own. Never raises."""
    result = {"aged": 0, "capped": 0, "bytes": 0, "cap": 0, "skipped": 0}
    try:
        result["cap"] = max_bytes()
        home = artifacts_home()
        if not home.is_dir():
            return result
        if home_problem(home):
            result["skipped"] = 1
            return result
        now = now if now is not None else time.time()
        days = keep_days()
        protected_after = now - protect_seconds()
        entries = _run_entries(home)
        if days > 0:
            cutoff = now - days * 86400
            for item in list(entries):
                if item[0] < cutoff and item[0] < protected_after and _remove(item[2]):
                    entries.remove(item)
                    result["aged"] += 1
        total = sum(size for _m, size, _p in entries)
        if result["cap"] > 0:
            for item in sorted(entries, key=lambda e: e[0]):
                if total <= result["cap"]:
                    break
                if item[0] >= protected_after:
                    continue  # possibly still running: leave it, even if the cap stays exceeded
                if _remove(item[2]):
                    total -= item[1]
                    result["capped"] += 1
        result["bytes"] = total
        _drop_empty_dirs(home)
    except Exception:  # a chore before a test run must never fail the run
        pass
    return result


def warn_if_capped(result: dict[str, int]) -> None:
    """Silent normally; one stderr line only when the size cap had to delete runs."""
    if result.get("capped"):
        print(
            f"NOTE artifacts over {result['cap'] // (1024 * 1024)} MB: removed {result['capped']} oldest runs "
            f"(now {result['bytes'] // (1024 * 1024)} MB in {artifacts_home()}); "
            "list or empty with: fastDeviceTest clean --list | --all",
            file=sys.stderr,
        )
    elif result.get("skipped"):
        print(
            f"NOTE artifacts in {artifacts_home()} are not cleaned: {home_problem(artifacts_home())}",
            file=sys.stderr,
        )


def prepare_artifacts_root(root: Path, custom: bool) -> None:
    """Housekeeping before a run. A root the caller chose is only age-pruned (the caller owns it);
    the default home is marked as ours, then aged across every project and size-capped."""
    if custom:
        prune_artifacts(root, keep_days())
        return
    problem = ensure_home(artifacts_home())
    if problem:
        print(f"NOTE artifacts in {artifacts_home()} are not cleaned: {problem}", file=sys.stderr)
        return
    warn_if_capped(housekeep_artifacts())


DEFAULT_UNIT_CACHE_DAYS = 30.0


def unit_tests_home() -> Path:
    """Parent of every project's fastUnitTest build cache: <home>/<project>-<hash8>/<scheme-config-device>/.
    The launcher creates it (FAST_UNIT_TESTS_HOME overrides) and marks it like the artifacts home."""
    return Path(os.environ.get("FAST_UNIT_TESTS_HOME") or HELPER_CACHE / "unit-tests").expanduser()


def unit_cache_days() -> float:
    """Build caches not used for this many days are deleted; FAST_UNIT_CACHE_DAYS=0 keeps them."""
    return _env_number("FAST_UNIT_CACHE_DAYS", DEFAULT_UNIT_CACHE_DAYS)


def _last_used(cache: Path) -> float:
    """When a build cache was last used, from cheap spots only (a DerivedData walk would be slow): the
    cache folder, its fingerprint, its logs folder and the log files in it. A long build keeps appending
    to its log file while the folders' own times stand still."""
    stamps = [0.0]
    for item in (cache, cache / "source.fingerprint", cache / "logs"):
        try:
            stamps.append(item.lstat().st_mtime)
        except OSError:
            pass
    try:
        for entry in os.scandir(cache / "logs"):
            try:
                stamps.append(entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return max(stamps)


def _is_build_cache(cache: Path) -> bool:
    """Looks like what fast_device_test.sh writes: DerivedData, a fingerprint or logs inside."""
    return any((cache / child).exists() for child in ("DerivedData", "source.fingerprint", "logs"))


def _unit_entries(home: Path, with_size: bool = True) -> list[tuple[float, int, Path]]:
    """(last used, bytes, path) of every <home>/<project>-<hash8>/<cache> folder that looks like a
    build cache. Sizes are skipped (0) when only ages matter."""
    found: list[tuple[float, int, Path]] = []
    for project in _real_dirs(home, PROJECT_NAME.pattern):
        for cache in _real_dirs(project):
            if _is_build_cache(cache):
                found.append((_last_used(cache), path_size(cache) if with_size else 0, cache))
    return found


def _drop_empty_projects(home: Path) -> None:
    for project in _real_dirs(home, PROJECT_NAME.pattern):
        try:
            if not any(project.iterdir()):
                project.rmdir()
        except OSError:
            pass


def housekeep_unit_caches(now: float | None = None) -> int:
    """Delete fastUnitTest build caches unused for FAST_UNIT_CACHE_DAYS (30), never one used within the
    protect window. Same ownership rules as the artifacts home. Never raises; returns how many went."""
    removed = 0
    try:
        home = unit_tests_home()
        days = unit_cache_days()
        if days <= 0 or not home.is_dir() or home_problem(home):
            return 0
        now = now if now is not None else time.time()
        cutoff, busy = now - days * 86400, now - protect_seconds()
        for used, _size, path in _unit_entries(home, with_size=False):
            if used < cutoff and used < busy and _remove(path):
                removed += 1
        _drop_empty_projects(home)
    except Exception:  # a chore before a test run must never fail the run
        pass
    return removed


def prepare_unit_cache() -> None:
    """Called by the fastUnitTest launcher before it builds: mark the cache home as ours, age out old caches."""
    try:
        home = unit_tests_home()
        if not ensure_home(home):
            housekeep_unit_caches()
    except Exception:
        pass


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def clean_main(argv: list[str]) -> int:
    """`clean [--list] [--all] [--builds] [--days N] [--project TEXT] [--dry-run]`: run artifacts with
    --all (or --days alone), fastUnitTest build caches with --builds; listing shows both."""
    import argparse

    parser = argparse.ArgumentParser(prog="clean", description="List or delete saved run artifacts and build caches.")
    parser.add_argument("--list", action="store_true", help="show size per project (default; wins over deleting)")
    parser.add_argument("--all", action="store_true", help="delete every saved run that is not in use")
    parser.add_argument("--builds", action="store_true",
                        help="delete fastUnitTest build caches not in use (the next test run rebuilds)")
    parser.add_argument("--days", type=float, help="delete only what was untouched for more than N days (N >= 0)")
    parser.add_argument("--project", help="only projects whose folder name contains this text")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 0:
        parser.error("--days must be 0 or more")
    now = time.time()
    protected_after = now - protect_seconds()
    cutoff = now - args.days * 86400 if args.days is not None else None
    delete_runs = not args.list and (args.all or (args.days is not None and not args.builds))
    delete_builds = not args.list and args.builds
    deleting = delete_runs or delete_builds
    grand = freed = 0
    verb = "WOULD DELETE" if args.dry_run else "DELETED"

    def wanted(project: Path) -> bool:
        return not args.project or args.project in project.name

    def refuse(home: Path) -> str | None:
        problem = home_problem(home) if home.is_dir() else None
        if problem:
            print(f"REFUSED {problem}", file=sys.stderr)
        return problem

    artifacts = artifacts_home()
    refused = False
    if artifacts.is_dir():
        if refuse(artifacts):
            refused = True
        else:
            by_project: dict[Path, list[tuple[float, int, Path]]] = {}
            for item in _run_entries(artifacts):
                by_project.setdefault(item[2].parents[2], []).append(item)
            for project in filter(wanted, _real_dirs(artifacts, PROJECT_NAME.pattern)):
                runs = by_project.get(project, [])
                size = path_size(project)
                grand += size
                if not delete_runs:
                    print(f"{_megabytes(size):>10}  {project.name}  runs={len(runs)}  {project}")
                    continue
                doomed = [e for e in runs if e[0] < protected_after and (cutoff is None or e[0] < cutoff)]
                busy = sum(1 for e in runs if e[0] >= protected_after)
                for _mtime, run_size, path in doomed:
                    if args.dry_run or _remove(path):
                        freed += run_size
                note = f"  ({busy} in use, kept)" if busy else ""
                print(f"{verb} {len(doomed)}/{len(runs)} runs  {project.name}{note}")
            if delete_runs and not args.dry_run:
                _drop_empty_dirs(artifacts)
    elif not unit_tests_home().is_dir():
        print(f"EMPTY {artifacts}")
        return 0

    builds = unit_tests_home()
    if builds.is_dir():
        if refuse(builds):
            refused = True
        else:
            for used, size, path in _unit_entries(builds):
                if not wanted(path.parent):
                    continue
                grand += size
                age = f"last used {(now - used) / 86400:.0f}d ago"
                if not delete_builds:
                    print(f"{_megabytes(size):>10}  build cache {path.parent.name}/{path.name}  {age}  {path}")
                    continue
                if used >= protected_after or (cutoff is not None and used >= cutoff):
                    print(f"KEPT {path.parent.name}/{path.name}  {age}")
                    continue
                if args.dry_run or _remove(path):
                    freed += size
                print(f"{verb} build cache {path.parent.name}/{path.name}  {age}")
            if delete_builds and not args.dry_run:
                _drop_empty_projects(builds)

    label = ("would free" if args.dry_run else "freed") if deleting else "total"
    shown = builds if delete_builds and not delete_runs else artifacts
    print(f"{label} {_megabytes(freed if deleting else grand)}  home={shown}")
    return 1 if refused else 0
