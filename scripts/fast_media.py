#!/usr/bin/env python3

"""Media helpers shared by the Simulator and device engines: review-quality video, the
one-final-clip-per-task rule, and the cached AVFoundation helper binaries.

Imports nothing outside the standard library, so both engines load it without Pillow.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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


def prune_artifacts(root: Path, keep_days: float) -> int:
    """Delete run folders and final videos untouched for `keep_days`; empty task dirs go too."""
    if keep_days <= 0 or not root.is_dir():
        return 0
    removed = 0
    for task_dir in root.iterdir():
        if not task_dir.is_dir():
            continue
        removed += prune_entries(task_dir, keep_days)
        if not any(task_dir.iterdir()):
            task_dir.rmdir()
    return removed


def ready_do_folder(out: Path, default: Path) -> Path:
    """`out` created for a `do` request. In the default folder, shots and dumps untouched for
    FAST_UI_KEEP_DAYS (3) go first, as run folders do; a folder given with --out is the caller's."""
    if out == default:
        prune_entries(default, float(os.environ.get("FAST_UI_KEEP_DAYS", "3")))
    out.mkdir(parents=True, exist_ok=True)
    return out
