#!/usr/bin/env python3

"""Run a fastSimulatorTest-style JSON flow on a physical iPhone through XCUITest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# bin/ holds symlinks in a git checkout but copies in the zip, so look beside scripts/ too.
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts")]
from fast_media import VIDEO_SUFFIXES, prune_artifacts, publish_video, shrink_video  # noqa: E402
import fast_evidence  # noqa: E402
import fast_guard  # noqa: E402
from fast_ui_map import record  # noqa: E402

DO_OUT = Path.home() / "Library/Caches/fast-ui/do"
RUNNER_SOURCE = HERE.parent / "device-runner"
CACHE_ROOT = Path(os.environ.get("FAST_DEVICE_CACHE", Path.home() / "Library/Caches/fast-device-test"))
PROFILE_DIRS = [
    Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
    Path.home() / "Library/MobileDevice/Provisioning Profiles",
]
TEST_TARGET = "FastDeviceUITests"
TEST_ID = f"{TEST_TARGET}/FlowRunner/testFlow"


class FastDeviceError(RuntimeError):
    pass


def run(args: list[str], env: dict[str, str] | None = None, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, env=env, **kwargs)
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        raise FastDeviceError(f"{' '.join(args[:3])} failed: " + " | ".join(tail))
    return result


def file_link(path: Path | str) -> str:
    """Absolute `file://` URL: terminals open it on click, a bare or relative path they do not."""
    return Path(path).expanduser().resolve().as_uri()


# MARK: - Device and Xcode
def physical_devices() -> list[dict[str, Any]]:
    with tempfile.NamedTemporaryFile(suffix=".json") as output:
        run(["xcrun", "devicectl", "list", "devices", "--json-output", output.name])
        devices = json.loads(Path(output.name).read_text())["result"]["devices"]
    return [
        {
            "udid": device["hardwareProperties"].get("udid"),
            "name": device["deviceProperties"].get("name"),
            "os": device["deviceProperties"].get("osVersionNumber") or "0",
            "connected": device["connectionProperties"].get("tunnelState") == "connected",
        }
        for device in devices
        if device["hardwareProperties"].get("reality") == "physical"
        and device["hardwareProperties"].get("platform") == "iOS"
    ]


def serving_devices() -> list[dict[str, Any]]:
    """The iPhones a `serve` runner was started for, as its state file recorded them."""
    devices = []
    for path in CACHE_ROOT.glob("server-*.json"):
        try:
            device = json.loads(path.read_text()).get("device")
        except (OSError, ValueError):
            continue
        if isinstance(device, dict) and device.get("udid"):
            devices.append(device)
    return devices


def resolve_device(requested: str | None) -> dict[str, Any]:
    """The iPhone `--device` (or FAST_DEVICE) names. One whose runner answers is taken from its
    state file: `xcrun devicectl list devices` costs 0.5–1 s on every `do`."""
    requested = requested or os.environ.get("FAST_DEVICE") or None
    if requested:
        # Only an exact UDID or full name: a part of a name may also match an iPhone that is
        # connected but not serving, and two such matches must still be refused.
        cached = [device for device in serving_devices() if requested in (device["udid"], device["name"])]
        if len(cached) == 1 and server_alive(cached[0]["udid"]):
            return {**cached[0], "serving": True}
    return pick_device(requested, physical_devices())


def pick_device(requested: str | None, devices: list[dict[str, Any]]) -> dict[str, Any]:
    if requested:
        for device in devices:
            if requested in (device["udid"], device["name"]):
                return device
        # "Blue" is enough when only one iPhone has it in its name; a prefix shared by two
        # ("iPhone 16") is refused rather than guessed.
        partial = [device for device in devices if requested.lower() in str(device["name"]).lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            names = [device["name"] for device in partial]
            raise FastDeviceError(f"device '{requested}' matches {len(partial)} iPhones: {names}; pass the full name or UDID")
        raise FastDeviceError(f"device '{requested}' not found; known: {[d['name'] for d in devices]}")
    connected = [device for device in devices if device["connected"]]
    if len(connected) == 1:
        return connected[0]
    names = [f"{d['name']} ({d['udid']}, iOS {d['os']})" for d in connected]
    raise FastDeviceError(f"pass --device: {len(connected)} connected iPhones {names}")


def device_details(udid: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".json") as output:
        run(["xcrun", "devicectl", "device", "info", "details", "--device", udid, "--json-output", output.name])
        return json.loads(Path(output.name).read_text()).get("result", {})


def check_device(device: dict[str, Any]) -> int:
    """`fastDeviceTest check`: what this iPhone needs before a run, each gap with its fix."""
    udid, missing, fixes = device["udid"], [], []
    try:
        details = device_details(udid)
    except FastDeviceError as error:
        details = {}
        missing.append("details")
        fixes.append(f"details: {error}")
    connection = details.get("connectionProperties", {})
    properties = details.get("deviceProperties", {})
    print(f"device={device['name']} udid={udid} ios={device.get('os')} transport={connection.get('transportType', '?')}")
    checks = (
        ("paired", connection.get("pairingState") == "paired",
         "unlock the iPhone, tap Trust for this Mac, or pair it in Xcode › Window › Devices and Simulators"),
        ("connected", connection.get("tunnelState") == "connected",
         "plug the iPhone in (or join this Mac's Wi-Fi) and unlock it"),
        ("developer-mode", properties.get("developerModeStatus") == "enabled",
         "Settings › Privacy & Security › Developer Mode on, then restart the iPhone"),
        ("ddi", properties.get("ddiServicesAvailable") is True,
         "open Xcode with the iPhone connected and unlocked once, so it mounts the developer disk image"),
    )
    for name, ok, fix in checks:
        print(f"{name.replace('-', '_')}={'yes' if ok and name != 'developer-mode' else 'enabled' if ok else 'no'}")
        if not ok:
            missing.append(name)
            fixes.append(f"{name}: {fix}")
    developer_dir = signing = None
    try:
        developer_dir = developer_dir_for(str(device.get("os", "")))
        print(f"xcode={developer_dir}")
    except FastDeviceError as error:
        missing.append("xcode")
        fixes.append(f"xcode: {error}")
    try:
        signing = pick_signing(udid)
        print(f"signing={signing['name']} ({signing['bundle_id']})")
    except FastDeviceError as error:
        missing.append("signing")
        fixes.append(f"signing: {error}")
    if developer_dir and signing:
        built = (CACHE_ROOT / runner_fingerprint(developer_dir, signing) / "signed").exists()
        print("runner=built" if built else "runner=not built yet (the first run builds and signs it, 1–2 min)")
    print(f"serving={'yes' if server_alive(udid) else 'no'}")
    print("ui_automation=not readable: if iOS asks, Settings › Developer › Enable UI Automation")
    print("lock=not readable: the iPhone must stay unlocked during a run (a locked one fails in seconds)")
    if missing:
        print(f"CHECK missing: {', '.join(missing)}")
        for fix in fixes:
            print(f"  {fix}")
        return 1
    print("CHECK OK")
    return 0


def major(version: str) -> int:
    return int(version.split(".")[0] or 0)


def developer_dir_for(os_version: str) -> str:
    if os.environ.get("FAST_DEVICE_DEVELOPER_DIR"):
        return os.environ["FAST_DEVICE_DEVELOPER_DIR"]
    candidates: list[tuple[int, str]] = []
    for app in sorted(Path("/Applications").glob("Xcode*.app")):
        developer = str(app / "Contents/Developer")
        env = {**os.environ, "DEVELOPER_DIR": developer}
        result = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-version"], env=env, check=False)
        if result.returncode == 0:
            candidates.append((major(result.stdout.strip()), developer))
    if not candidates:
        raise FastDeviceError("no Xcode with an iphoneos SDK found in /Applications")
    # The oldest Xcode whose SDK still covers the device; a device newer than every SDK
    # cannot run a test bundle at all.
    fitting = sorted(c for c in candidates if c[0] >= major(os_version))
    if not fitting:
        raise FastDeviceError(f"no installed Xcode supports iOS {os_version}; SDKs: {sorted(c[0] for c in candidates)}")
    return fitting[0][1]


# MARK: - Signing
def signing_identities() -> dict[str, str]:
    output = run(["security", "find-identity", "-v", "-p", "codesigning"]).stdout
    # `-v` still lists revoked certificates, flagged with a CSSMERR suffix.
    return {
        match.group(1): match.group(2)
        for line in output.splitlines()
        if "CSSMERR" not in line
        for match in [re.search(r"\)\s+([0-9A-F]{40})\s+\"([^\"]+)\"", line)]
        if match
    }


def decode_profile(path: Path) -> dict[str, Any] | None:
    result = subprocess.run(["security", "cms", "-D", "-i", str(path)], capture_output=True)
    if result.returncode != 0:
        return None
    return plistlib.loads(result.stdout)


def installed_app_ids(udid: str) -> set[str]:
    with tempfile.NamedTemporaryFile(suffix=".json") as output:
        result = run(["xcrun", "devicectl", "device", "info", "apps", "--device", udid, "--json-output", output.name], check=False)
        if result.returncode != 0:
            return set()
        apps = json.loads(Path(output.name).read_text()).get("result", {}).get("apps", [])
    # A runner left by an earlier run may be replaced; a real app must never be.
    return {app["bundleIdentifier"] for app in apps if not str(app.get("name", "")).endswith("-Runner")}


def taken(bundle_id: str, installed: set[str]) -> bool:
    return any(bundle_id == app or bundle_id.startswith(app + ".") for app in installed)


def pick_signing(udid: str) -> dict[str, Any]:
    identities = signing_identities()
    installed = installed_app_ids(udid)
    override = os.environ.get("FAST_DEVICE_PROFILE")
    now = datetime.now(timezone.utc)
    best: dict[str, Any] | None = None
    for directory in PROFILE_DIRS:
        for path in directory.glob("*.mobileprovision"):
            if override and override not in (path.stem, str(path)):
                continue
            profile = decode_profile(path)
            if not profile:
                continue
            entitlements = profile.get("Entitlements", {})
            expiry = profile.get("ExpirationDate")
            if expiry and expiry.replace(tzinfo=timezone.utc) < now:
                continue
            if udid not in profile.get("ProvisionedDevices", []) or not entitlements.get("get-task-allow"):
                continue
            hashes = [hashlib.sha1(cert).hexdigest().upper() for cert in profile.get("DeveloperCertificates", [])]
            identity = next((h for h in hashes if h in identities), None)
            if identity is None:
                continue
            app_id = entitlements.get("application-identifier", "")
            team, _, suffix = app_id.partition(".")
            bundle_id = f"{suffix[:-1]}fastdevice.runner" if suffix.endswith("*") else suffix
            if suffix == "*":
                bundle_id = "fastdevice.runner"
            if taken(bundle_id, installed) and not override:
                continue
            candidate = {
                "profile": path, "uuid": profile.get("UUID"), "name": profile.get("Name"),
                "identity": identity, "identity_name": identities[identity], "team": team,
                "bundle_id": bundle_id, "entitlements": entitlements,
                "wildcard": suffix.endswith("*"), "expiry": expiry,
            }
            # Prefer a wildcard profile, then the fewest capabilities, then the longest-lived.
            candidate["rank"] = (candidate["wildcard"], -len(entitlements), expiry or datetime.min)
            if best is None or candidate["rank"] > best["rank"]:
                best = candidate
    if best is None:
        raise FastDeviceError(
            f"no usable development profile for device {udid}: it must list the device, match a valid keychain "
            "identity, and not reuse the bundle id of an app installed on the iPhone; "
            "sign in to Xcode (Settings > Accounts) and run the app on this iPhone once, or set FAST_DEVICE_PROFILE"
        )
    return best


# MARK: - Runner build
def runner_fingerprint(developer_dir: str, signing: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for path in sorted(RUNNER_SOURCE.rglob("*")):
        if path.is_file() and path.suffix in {".swift", ".yml", ".pbxproj"}:
            digest.update(path.read_bytes())
    digest.update(developer_dir.encode())
    digest.update(str(signing["uuid"]).encode())
    return digest.hexdigest()[:16]


def base_xctestrun(products: Path) -> Path:
    # `run-` and `serve-` files are per-run copies kept beside the build's own xctestrun.
    return next(p for p in sorted(products.glob("*.xctestrun")) if not p.name.startswith(("run-", "serve-")))


def build_runner(developer_dir: str, signing: dict[str, Any], rebuild: bool) -> Path:
    key = runner_fingerprint(developer_dir, signing)
    work = CACHE_ROOT / key
    products = work / "dd/Build/Products"
    marker = work / "signed"
    if marker.exists() and not rebuild:
        return base_xctestrun(products)
    work.mkdir(parents=True, exist_ok=True)
    # The generated project ships with the tool, so xcodegen is only needed when
    # someone edits project.yml and deletes the .xcodeproj to regenerate it.
    generated = RUNNER_SOURCE / "FastDeviceRunner.xcodeproj"
    items = ["project.yml", "UITests"] + (["FastDeviceRunner.xcodeproj"] if generated.is_dir() else [])
    for item in items:
        target = work / item
        if target.is_dir():
            shutil.rmtree(target)
        source = RUNNER_SOURCE / item
        shutil.copytree(source, target) if source.is_dir() else shutil.copy(source, target)
    if not generated.is_dir():
        if not shutil.which("xcodegen"):
            raise FastDeviceError("device-runner/FastDeviceRunner.xcodeproj missing and xcodegen not installed (brew install xcodegen)")
        run(["xcodegen", "generate", "--quiet"], cwd=work)
    env = {**os.environ, "DEVELOPER_DIR": developer_dir}
    build = run(
        [
            "xcodebuild", "build-for-testing", "-project", "FastDeviceRunner.xcodeproj",
            "-scheme", "FastDevice", "-destination", "generic/platform=iOS",
            "-derivedDataPath", str(work / "dd"), "CODE_SIGNING_ALLOWED=NO",
        ],
        env=env, cwd=work, check=False,
    )
    if build.returncode != 0:
        errors = [line for line in build.stdout.splitlines() if "error:" in line][:5]
        raise FastDeviceError("runner build failed: " + " | ".join(errors))
    sign_runner(products, signing)
    marker.write_text(datetime.now().isoformat())
    return base_xctestrun(products)


def sign_runner(products: Path, signing: dict[str, Any]) -> None:
    app = next(products.glob("*-iphoneos/*-Runner.app"))
    identity = signing["identity"]
    run(["/usr/libexec/PlistBuddy", "-c", f"Set CFBundleIdentifier {signing['bundle_id']}", str(app / "Info.plist")])
    shutil.copy(signing["profile"], app / "embedded.mobileprovision")
    entitlements = dict(signing["entitlements"])
    entitlements["application-identifier"] = f"{signing['team']}.{signing['bundle_id']}"
    entitlements_path = products / "runner.entitlements"
    entitlements_path.write_bytes(plistlib.dumps(entitlements))
    nested = [
        path for path in app.rglob("*")
        if path.suffix in {".framework", ".dylib"} and ".dSYM" not in str(path)
    ]
    for path in sorted(nested, key=lambda p: len(p.parts), reverse=True):
        run(["codesign", "-f", "-s", identity, str(path)])
    for bundle in app.glob("PlugIns/*.xctest"):
        run(["codesign", "-f", "-s", identity, str(bundle)])
    run(["codesign", "-f", "-s", identity, "--entitlements", str(entitlements_path), str(app)])
    xctestrun = base_xctestrun(products)
    run(["plutil", "-replace", f"{TEST_TARGET}.TestHostBundleIdentifier", "-string", signing["bundle_id"], str(xctestrun)])


# MARK: - Flow
def preference_arguments(preferences: dict[str, Any]) -> list[str]:
    # The argument domain overrides UserDefaults for this launch only and is never
    # written to disk, so seeded state cannot outlive the run on the device.
    arguments: list[str] = []
    for key, value in preferences.items():
        # Typed plist fragments: a bare `YES` arrives as a String and `object(forKey:) as? Bool` misses it.
        if isinstance(value, bool):
            encoded = "<true/>" if value else "<false/>"
        elif isinstance(value, int):
            encoded = f"<integer>{value}</integer>"
        elif isinstance(value, float):
            encoded = f"<real>{value}</real>"
        elif isinstance(value, str):
            encoded = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, dict) and set(value) == {"$json_data"}:
            data = json.dumps(value["$json_data"], separators=(",", ":"), ensure_ascii=False).encode()
            encoded = f"<{data.hex()}>"
        else:
            raise FastDeviceError(f"unsupported preference type for '{key}'")
        arguments += [f"-{key}", encoded]
    return arguments


CONTENT_SIZES = {
    "extra-small": "XS", "small": "S", "medium": "M", "large": "L", "extra-large": "XL",
    "extra-extra-large": "XXL", "extra-extra-extra-large": "XXXL",
    "accessibility-medium": "AccessibilityM", "accessibility-large": "AccessibilityL",
    "accessibility-extra-large": "AccessibilityXL", "accessibility-extra-extra-large": "AccessibilityXXL",
    "accessibility-extra-extra-extra-large": "AccessibilityXXXL",
}


def prompt_entries(entries: Any) -> list[Any]:
    try:
        return fast_guard.prompt_entries(entries)
    except ValueError as error:
        raise FastDeviceError(str(error)) from error


def parse_prompt_labels(text: str) -> list[Any]:
    try:
        return fast_guard.parse_prompt_labels(text)
    except ValueError as error:
        raise FastDeviceError(str(error)) from error


def report_notes(report: dict[str, Any]) -> str:
    """What the runner did on its own: prompts it answered, and an idle wait it could not skip."""
    notes = ""
    if report.get("wait_for_idle") == "unsupported":
        notes += " wait_for_idle=unsupported"
    if report.get("dismissed"):
        taps = [f"{entry.get('title', '')} → {entry.get('tapped', '')}" for entry in report["dismissed"]]
        notes += f" dismissed={json.dumps(taps, ensure_ascii=False)}"
    results = report.get("results") or ([report["result"]] if report.get("result") else [])
    moving = [str(result.get("screenshot")) for result in results if result.get("stable") is False]
    if moving:
        notes += f" unsettled={','.join(moving)}"
    return notes


def prepare_flow(spec: dict[str, Any], appearance: str | None = None, content_size: str | None = None) -> dict[str, Any]:
    setup = dict(spec.get("setup", {}))
    if "optional_permission_labels" in setup:
        prompt_entries(setup["optional_permission_labels"])
    if appearance:
        setup["appearance"] = appearance
    content_size = content_size or setup.pop("content_size", None)
    arguments = preference_arguments(setup.pop("preferences", {}))
    if content_size:
        if content_size not in CONTENT_SIZES:
            raise FastDeviceError(f"unknown content size '{content_size}'; use one of {list(CONTENT_SIZES)}")
        # A device has no simctl; UIKit reads the category from the argument domain instead.
        arguments += ["-UIPreferredContentSizeCategoryName", f"UICTContentSizeCategory{CONTENT_SIZES[content_size]}"]
    setup["launch_arguments"] = arguments + [str(value) for value in setup.get("launch_arguments", [])]
    return {**spec, "setup": setup}


def enable_recording(xctestrun: Path) -> None:
    # XCTest records the device screen during every UI test, but its default attachment
    # lifetime deletes the recording when the test passes.
    for key, value in {"PreferredScreenCaptureFormat": "screenRecording", "SystemAttachmentLifetime": "keepAlways"}.items():
        run(["plutil", "-replace", f"{TEST_TARGET}.{key}", "-string", value, str(xctestrun)])


def publish_recording(artifacts: Path, passed: bool, session: str = "") -> Path | None:
    """XCTest's screen recording of the run at review quality: a PASS keeps it as
    <task>/final[-<session>].mp4, a FAIL as run.mp4 beside its other artifacts."""
    recordings = sorted(
        (path for path in artifacts.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda path: path.stat().st_size, reverse=True,
    )
    if not recordings:
        return None
    primary, *others = recordings
    for other in others:
        shrink_video(other, other.with_suffix(".mp4"))
    return publish_video(artifacts.parent, shrink_video(primary, artifacts / "run.mp4"), passed, session)


def install_app(udid: str, app_path: str) -> None:
    run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, app_path])


def export_attachments(result_bundle: Path, output: Path, developer_dir: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for developer in (developer_dir, None):
        env = {**os.environ, "DEVELOPER_DIR": developer} if developer else None
        exported = run(
            ["xcrun", "xcresulttool", "export", "attachments", "--path", str(result_bundle), "--output-path", str(output)],
            env=env, check=False,
        )
        if exported.returncode == 0:
            break
    manifest = output / "manifest.json"
    if not manifest.exists():
        return
    for test in json.loads(manifest.read_text()):
        for attachment in test.get("attachments", []):
            source = output / attachment["exportedFileName"]
            name = re.sub(r"_\d+_[0-9A-F-]{36}$", "", Path(attachment["suggestedHumanReadableName"]).stem)
            suffix = Path(attachment["exportedFileName"]).suffix
            if source.exists():
                source.rename(output / f"{Path(name).stem}{suffix}")


# MARK: - Server
SERVER_PORT = int(os.environ.get("FAST_DEVICE_PORT", "8765"))


def server_state_path(udid: str) -> Path:
    return CACHE_ROOT / f"server-{udid}.json"


def tunnel_address(udid: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json") as output:
        run(["xcrun", "devicectl", "list", "devices", "--json-output", output.name])
        devices = json.loads(Path(output.name).read_text())["result"]["devices"]
    for device in devices:
        if device["hardwareProperties"].get("udid") == udid:
            address = device["connectionProperties"].get("tunnelIPAddress")
            if address:
                return address
    raise FastDeviceError("no CoreDevice tunnel address; unlock the iPhone and keep it connected")


def request(udid: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    state_path = server_state_path(udid)
    if not state_path.exists():
        raise FastDeviceError("server not running")
    state = json.loads(state_path.read_text())
    with socket.create_connection((state["address"], state["port"]), timeout=timeout) as connection:
        connection.sendall((json.dumps(payload) + "\n").encode())
        chunks = []
        while True:
            chunk = connection.recv(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
    if not chunks:
        reason = runner_end_reason(Path(state.get("log", "")))
        # This request's runner is gone (it answers every request it reads to the end); its
        # xcodebuild lingers collecting diagnostics and would make the next request find the
        # iPhone busy. Ended by the pattern `stop` uses, never by a pid that may be reused, and
        # the state file goes only while it still names that runner.
        kill_runners(udid)
        try:
            if json.loads(state_path.read_text()).get("pid") == state.get("pid"):
                state_path.unlink()
        except (OSError, ValueError):
            pass
        raise FastDeviceError(
            "the runner ended without a reply" + (f": {reason}" if reason else "")
            + "; the next request starts it again"
        )
    return json.loads(b"".join(chunks))


RUNNER_FAILURE = re.compile(r"(?:error:|Expected failure in)\s*-\[[^\]]*\]\s*:\s*(.+)")


def runner_end_reason(log: Path) -> str:
    """The last XCTest failure in the runner's log: a UI Automation failure (a picker value the
    wheel does not have) ends the whole test, and with it the persistent runner."""
    try:
        lines = log.read_text(errors="replace").splitlines()[-400:]
    except OSError:
        return ""
    for line in reversed(lines):
        found = RUNNER_FAILURE.search(line)
        if found:
            return found.group(1).split("Reason: (")[0].strip()[:300]
    return ""


def server_alive(udid: str) -> bool:
    try:
        return request(udid, {"op": "ping"}, timeout=3).get("pong") is True
    except (OSError, FastDeviceError, json.JSONDecodeError):
        return False


def remember_device(udid: str, device: dict[str, Any]) -> None:
    """Write the iPhone into a serving runner's state file (older ones lack it), so the next
    request resolves it without devicectl."""
    path = server_state_path(udid)
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    known = {key: value for key, value in device.items() if key != "serving"}
    if state.get("device") != known:
        path.write_text(json.dumps({**state, "device": known}))


def start_server(device: dict[str, Any], rebuild: bool) -> None:
    udid = device["udid"]
    # `serving` means resolve_device just had an answer from this runner: no second ping.
    if device.get("serving") or server_alive(udid):
        remember_device(udid, device)
        return
    holders = run(["pgrep", "-f", f"serve-{udid}.xctestrun"], check=False)
    if holders.returncode == 0 and left_behind(udid, holders.stdout):
        end_runner(udid)
    elif holders.returncode == 0:
        raise FastDeviceError(
            f"{device['name']} is busy: another runner holds it (maybe another agent). "
            "Reuse it, or `fastDeviceTest stop` only if it is yours"
        )
    developer_dir = developer_dir_for(device["os"])
    xctestrun = build_runner(developer_dir, pick_signing(udid), rebuild)
    serve_xctestrun = xctestrun.with_name(f"serve-{udid}.xctestrun")
    shutil.copy(xctestrun, serve_xctestrun)
    for key, value in {"FAST_PORT": str(SERVER_PORT), "FAST_IDLE": os.environ.get("FAST_DEVICE_IDLE", "300")}.items():
        run(["plutil", "-replace", f"{TEST_TARGET}.TestingEnvironmentVariables.{key}", "-string", value, str(serve_xctestrun)])
    log = CACHE_ROOT / f"server-{udid}.log"
    process = subprocess.Popen(
        [
            "xcodebuild", "test-without-building", "-xctestrun", str(serve_xctestrun),
            "-destination", f"id={udid}", f"-only-testing:{TEST_TARGET}/FlowRunner/testServe",
            "-test-timeouts-enabled", "NO",
        ],
        env={**os.environ, "DEVELOPER_DIR": developer_dir},
        stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True,
    )
    server_state_path(udid).write_text(json.dumps({
        "pid": process.pid, "address": tunnel_address(udid), "port": SERVER_PORT, "log": str(log),
        "device": {key: value for key, value in device.items() if key != "serving"},
    }))
    deadline = time.perf_counter() + 120
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            tail = " | ".join(log.read_text(errors="replace").strip().splitlines()[-6:])
            server_state_path(udid).unlink(missing_ok=True)
            raise FastDeviceError(f"runner exited before serving: {tail}")
        if server_alive(udid):
            return
        # A locked iPhone makes xcodebuild wait for the passcode for as long as it takes.
        if "because the device is locked" in log.read_text(errors="replace"):
            end_runner(udid)
            raise FastDeviceError(f"{device['name']} is locked: unlock it and run again "
                                  "(a longer Auto-Lock on a test phone helps)")
        time.sleep(1)
    end_runner(udid)
    raise FastDeviceError(f"runner did not answer on port {SERVER_PORT} within 120s; log {log}")


def left_behind(udid: str, pgrep_output: str) -> bool:
    """A runner this Mac's state file names, that no longer answers, and that started longer
    ago than a start can take: what an idle-out leaves while xcodebuild collects diagnostics.
    Anything else (another agent's runner, one still starting) stays "busy"."""
    path = server_state_path(udid)
    try:
        pid = json.loads(path.read_text()).get("pid")
        age = time.time() - path.stat().st_mtime
    except (OSError, ValueError):
        return False
    return str(pid) in pgrep_output.split() and age > 130


def end_runner(udid: str) -> None:
    """End a runner this launcher just started and that never served, so the iPhone is free."""
    kill_runners(udid)
    server_state_path(udid).unlink(missing_ok=True)


def stop_server(udid: str) -> None:
    state_path = server_state_path(udid)
    if not state_path.exists():
        return
    try:
        request(udid, {"op": "stop"}, timeout=5)
    except (OSError, FastDeviceError, json.JSONDecodeError):
        pass
    release_runner(udid)


def lock_device(device: dict[str, Any], rebuild: bool) -> None:
    """Press the lock button as the runner's last request, then free the iPhone as `stop` does.
    Only at the end of the work: nothing can drive a locked iPhone until someone types its passcode."""
    udid = device["udid"]
    start_server(device, rebuild)
    try:
        response = request(udid, {"op": "lock"}, timeout=15)
    finally:
        release_runner(udid)
    if response.get("status") != "pass":
        raise FastDeviceError(response.get("error") or "the runner did not lock the iPhone")


def release_runner(udid: str) -> None:
    server_state_path(udid).unlink(missing_ok=True)
    time.sleep(1)
    kill_runners(udid)


def kill_runners(udid: str) -> None:
    # Matches every serve xcodebuild for this device, including one whose state file was
    # lost or overwritten by a later `serve`, so the iPhone is really free afterwards.
    run(["pkill", "-f", f"serve-{udid}.xctestrun"], check=False)


def save_trail(report: dict[str, Any]) -> None:
    """Merge the runner's trail into the screen map (fastUIMap) and keep it out of the report,
    which an agent reads; a map problem never changes the run's result."""
    trail = report.pop("trail", None)
    if not trail:
        return
    try:
        record(trail, "device")
    except Exception as error:  # noqa: BLE001 - the map is a hint, the run's result stands
        print(f"map=not-saved ({error})", file=sys.stderr)


def save_screenshots(response: dict[str, Any], artifacts: Path) -> list[str]:
    saved = []
    for shot in response.pop("screenshots", []) or []:
        path = artifacts / f"{shot['name']}.png"
        path.write_bytes(base64.b64decode(shot["png"]))
        saved.append(str(path))
    return saved


def save_dumps(results: list[dict[str, Any]], artifacts: Path) -> list[str]:
    # A dump's nodes go to <name>-ui.json, as in the Simulator engine, so the report stays short.
    saved = []
    for result in results:
        nodes = result.pop("nodes", None)
        if nodes is None:
            continue
        path = artifacts / f"{result.get('name') or 'step-%02d' % result.get('step', 0)}-ui.json"
        path.write_text(json.dumps(nodes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result["path"] = str(path)
        saved.append(str(path))
    return saved


# MARK: - Main
def parse_flow_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fastDeviceTest",
        description="Run a JSON UI flow on a physical iPhone via XCUITest. "
        "Subcommands: serve | stop | status | check | do '<action json>'.",
    )
    parser.add_argument("flow", type=Path)
    parser.add_argument("--device", help="UDID or name; required when several iPhones are connected")
    parser.add_argument("--install", help="Install this .app on the device before the run")
    parser.add_argument("--rebuild-runner", action="store_true", help="Rebuild and re-sign the cached runner")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--one-shot", action="store_true",
        help="Do not start the persistent runner: without one running, the flow runs through one "
        "xcodebuild test (a running runner is still used)",
    )
    parser.add_argument("--appearance", choices=["light", "dark"])
    parser.add_argument("--content-size", choices=list(CONTENT_SIZES))
    parser.add_argument("--artifacts-root", type=Path, default=os.environ.get("FAST_DEVICE_ROOT") or None)
    parser.add_argument(
        "--record", action="store_true",
        help="Keep XCTest's screen recording (one-shot runs only); a PASS keeps "
        "<artifacts-root>/<task>/final[-<session>].mp4 at review quality",
    )
    parser.add_argument(
        "--session", default=os.environ.get("FAST_UI_SESSION", ""),
        help="Group this run under a session; fastUIReport and fastUIReview collate by it",
    )
    parser.add_argument(
        "--before", action="store_true",
        help="This run shows the bug (e.g. on the base build): a failure at the outcome step is filed as "
        "the case's before shot (env FAST_EVIDENCE=0 files nothing)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.record and args.serve:
        parser.error("--record needs a one-shot run; drop --serve")
    return args


def variant_name(name: str, appearance: str | None, content_size: str | None) -> str:
    # Same tags as the Simulator engine, so fastUIReport rows line up across both.
    size_tag = {
        size: ("ax-" if size.startswith("accessibility-") else "") + short.replace("Accessibility", "").lower()
        for size, short in CONTENT_SIZES.items()
    }
    return name + (f"@{appearance}" if appearance else "") + (f"@{size_tag[content_size]}" if content_size else "")


def run_flow(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    flow_path = args.flow.expanduser().resolve()
    spec = json.loads(flow_path.read_text(encoding="utf-8"))
    if spec.get("version") != 1:
        raise FastDeviceError("flow version must be 1")
    app = spec.get("app", {})
    setup = spec.get("setup", {})
    appearance = args.appearance or setup.get("appearance")
    content_size = args.content_size or setup.get("content_size")
    name = variant_name(spec.get("name") or flow_path.stem, args.appearance, args.content_size)
    device = resolve_device(args.device or app.get("device"))
    if not device["connected"]:
        raise FastDeviceError(f"{device['name']} is not connected; unlock it and plug it in or join the same Wi-Fi")
    if args.install or app.get("install_path"):
        install_app(device["udid"], str(Path(args.install or app["install_path"]).expanduser()))
    # A flow starts or reuses the persistent runner (~5 s once, then 4–6 s a flow instead of
    # 14–20 s one-shot); the runner idles out after FAST_DEVICE_IDLE (5 min). A recording needs
    # a one-shot run.
    if not (args.record or args.one_shot):
        start_server(device, args.rebuild_runner)
    if args.record and server_alive(device["udid"]):
        raise FastDeviceError(
            f"--record needs a one-shot run, but a persistent runner holds {device['name']}; "
            "stop it (`fastDeviceTest stop`) if it is yours, then rerun"
        )

    project_root = Path(app.get("project_root", flow_path.parent)).expanduser()
    root = (args.artifacts_root or project_root / "build/fast-device").expanduser().resolve()
    artifacts = root / name / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{device['udid'][:8]}"
    prune_artifacts(root, float(os.environ.get("FAST_UI_KEEP_DAYS", "3")))
    artifacts.mkdir(parents=True, exist_ok=True)
    prepared = prepare_flow(spec, appearance, content_size)

    if server_alive(device["udid"]):
        mode = "server"
        budget = 60 + sum(float(a.get("timeout", 6)) + float(a.get("seconds", 0)) for a in spec.get("actions", []))
        report = request(device["udid"], {"op": "flow", "flow": prepared}, timeout=budget)
        report["screenshots"] = save_screenshots(report, artifacts)
    else:
        mode = "one-shot"
        report = run_one_shot(device, prepared, artifacts, args.rebuild_runner, args.record)
    save_trail(report)
    save_dumps(report.get("results", []), artifacts)
    video = publish_recording(artifacts, report["status"] == "pass", args.session)

    report.update({
        "task": name, "device": device["name"], "udid": device["udid"], "ios": device["os"], "mode": mode,
        "session": args.session, "total_seconds": round(time.perf_counter() - started, 1), "artifacts": str(artifacts),
        **({"expect": spec["expect"]} if spec.get("expect") else {}),
        **({"video": str(video)} if video else {}),
    })
    (artifacts / "metrics.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    # A run in a root the caller chose (--artifacts-root, FAST_DEVICE_ROOT) is copied from, never emptied.
    filed, evidence_error = fast_evidence.after_run(
        spec, project_root, name, device["name"], "before" if args.before else None, artifacts, video,
        tidy=args.artifacts_root is None,
    )
    evidence_suffix = ""
    if evidence_error:
        report["evidence_error"] = evidence_error
        evidence_suffix = f" evidence_error={evidence_error}"
    elif filed:
        report = json.loads((artifacts / "metrics.json").read_text())
        video = Path(report["video"]) if report.get("video") else None
        evidence_suffix = f" evidence={file_link(filed['dir'])}"
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif report["status"] == "pass":
        dumps = [r for r in report.get("results", []) if "labels" in r]
        suffix = f" labels={json.dumps(dumps[-1]['labels'], ensure_ascii=False)}" if dumps else ""
        suffix += f" video={file_link(video)}" if video else ""
        suffix += report_notes(report)
        print(f"PASS task={name} device={device['name']} mode={mode} total={report['total_seconds']}s{suffix} artifacts={file_link(artifacts)}{evidence_suffix}")
    else:
        print(
            f"FAIL task={name} device={device['name']} mode={mode} step={report.get('step')} "
            f"error={report.get('error')}{f' video={file_link(video)}' if video else ''}{report_notes(report)} "
            f"artifacts={file_link(artifacts)}{evidence_suffix}",
            file=sys.stderr,
        )
    return 0 if report["status"] == "pass" else 1


def run_one_shot(
    device: dict[str, Any], prepared: dict[str, Any], artifacts: Path, rebuild: bool, record: bool = False
) -> dict[str, Any]:
    developer_dir = developer_dir_for(device["os"])
    signing = pick_signing(device["udid"])
    xctestrun = build_runner(developer_dir, signing, rebuild)
    encoded = base64.b64encode(json.dumps(prepared).encode()).decode()
    # Stays beside the cached products: an xctestrun resolves __TESTROOT__ from its own folder.
    run_xctestrun = xctestrun.with_name(f"run-{os.getpid()}.xctestrun")
    shutil.copy(xctestrun, run_xctestrun)
    run(["plutil", "-replace", f"{TEST_TARGET}.TestingEnvironmentVariables.FAST_FLOW", "-string", encoded, str(run_xctestrun)])
    if record:
        enable_recording(run_xctestrun)
    result_bundle = artifacts / "result.xcresult"
    test = run(
        [
            "xcodebuild", "test-without-building", "-xctestrun", str(run_xctestrun),
            "-destination", f"id={device['udid']}", f"-only-testing:{TEST_ID}",
            "-resultBundlePath", str(result_bundle),
        ],
        env={**os.environ, "DEVELOPER_DIR": developer_dir}, check=False,
    )
    run_xctestrun.unlink(missing_ok=True)
    (artifacts / "xcodebuild.log").write_text(test.stdout + test.stderr)
    export_attachments(result_bundle, artifacts, developer_dir)
    result_file = next(artifacts.glob("zz_result.*"), None)
    if result_file:
        report = json.loads(result_file.read_text())
    else:
        errors = [line.strip() for line in (test.stdout + test.stderr).splitlines() if "error" in line.lower()][:4]
        report = {"status": "fail", "step": "runner", "error": " | ".join(errors) or "runner produced no result"}
    report["signing"] = f"{signing['name']} ({signing['bundle_id']})"
    return report


def run_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"fastDeviceTest {argv[0]}")
    parser.add_argument("--device")
    parser.add_argument("--rebuild-runner", action="store_true")
    if argv[0] == "do":
        parser.add_argument(
            "action",
            help='One action as JSON, e.g. \'{"type":"tap_label","label":"Continue"}\', '
            "or a JSON array of actions run in one request (stops at the first failure)",
        )
        parser.add_argument("--app", help="Bundle id to drive from this (or the first) action on")
        parser.add_argument("--out", type=Path, default=DO_OUT, help=f"Where screenshots go (default {DO_OUT})")
        parser.add_argument(
            "--idle-wait", action="store_true",
            help="Use XCUITest's wait-for-idle around each event; by default it is off and the runner "
            "waits for the layout to stop moving itself",
        )
        parser.add_argument("--no-idle-wait", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument(
            "--prompt-labels",
            help='Prompt buttons this request may tap on its own: "Allow, Don’t Allow", or a JSON array '
            'with rules such as [{"title_contains":"track","tap":"Ask App Not to Track"}]. '
            "Without it the request taps no prompt",
        )
    args = parser.parse_args(argv[1:])
    if argv[0] == "do":
        try:
            parsed_actions = json.loads(args.action)
            parsed_actions = parsed_actions if isinstance(parsed_actions, list) else [parsed_actions]
            fast_guard.check_actions(parsed_actions)
        except ValueError as error:
            raise FastDeviceError(str(error)) from error
        risky = fast_guard.risky_step(parsed_actions)
        if risky:
            raise FastDeviceError(f"step {risky[0]}: '{risky[1]}' looks destructive; send it alone or add \"confirm\": true")
    device = resolve_device(args.device)
    udid = device["udid"]
    if argv[0] == "check":
        return check_device(device)
    if argv[0] == "serve":
        start_server(device, args.rebuild_runner)
        state = json.loads(server_state_path(udid).read_text())
        print(f"SERVING device={device['name']} address=[{state['address']}]:{state['port']} log={state['log']}")
    elif argv[0] == "stop":
        stop_server(udid)
        print(f"STOPPED device={device['name']}")
    elif argv[0] == "lock":
        lock_device(device, args.rebuild_runner)
        print(f"LOCKED device={device['name']}")
    elif argv[0] == "status":
        print(f"{'SERVING' if server_alive(udid) else 'IDLE'} device={device['name']}")
    else:
        start_server(device, args.rebuild_runner)
        parsed = json.loads(args.action)
        actions = parsed if isinstance(parsed, list) else [parsed]
        if args.app and actions and "app" not in actions[0]:
            actions[0]["app"] = args.app
        timeout = 30 + sum(float(a.get("timeout", 6)) + float(a.get("seconds", 0)) for a in actions)
        extra: dict[str, Any] = {} if args.idle_wait else {"wait_for_idle": False}
        if args.prompt_labels:
            extra["optional_permission_labels"] = parse_prompt_labels(args.prompt_labels)
        if isinstance(parsed, list):
            response = request(udid, {"op": "actions", "actions": actions, **extra}, timeout=timeout)
            results = response.get("results", [])
        else:
            response = request(udid, {"op": "action", "action": actions[0], **extra}, timeout=timeout)
            results = [response["result"]] if response.get("result") else []
        save_trail(response)
        args.out.mkdir(parents=True, exist_ok=True)
        shots = save_screenshots(response, args.out)
        dumps = save_dumps(results, args.out)
        status = "PASS" if response.get("status") == "pass" else "FAIL"
        if isinstance(parsed, list):
            failed = response.get("step")
            head = f"actions={len(actions)}" + (
                f" step={failed} type={actions[failed - 1].get('type')}" if status == "FAIL" and failed else ""
            )
        else:
            head = str(actions[0].get("type"))
        error = f" error={response.get('error')}" if response.get("error") else ""
        shot = f" screenshot{'s' if len(shots) > 1 else ''}={','.join(map(file_link, shots))}" if shots else ""
        ui = f" ui={','.join(map(file_link, dumps))}" if dumps else ""
        labels = json.dumps(response.get("labels", []), ensure_ascii=False)
        print(f"{status} {head}{error}{shot}{ui}{report_notes(response)} labels={labels}")
        return 0 if status == "PASS" else 1
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in {"serve", "stop", "lock", "status", "do", "check"}:
        return run_command(argv)
    return run_flow(parse_flow_arguments(argv))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FastDeviceError as error:
        print(f"FAIL error={error}", file=sys.stderr)
        raise SystemExit(1)
