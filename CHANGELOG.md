# Changelog

## Unreleased

### Faster to prepare, safer to reuse a simulator

Found and measured with a debate-and-experiment pass on the fixture app (load 20–390 on a
shared Mac; ratios from interleaved runs):

- **`start` / `do` / `labels` / `save`:** build a flow on the live app one action at a time,
  then save it and verify it with one fresh-install run. Exploring a screen two steps deep
  went from 49 s and 5 flow rewrites (rerun + `dump`) to 15 s and one write.
- **SwiftLint skipped in test builds** through a no-op `swiftlint` shim on `PATH` (no-op
  build 6–9 s → 2–3 s; a lint error no longer fails the test build). `lint=not-shimmable`
  names phases the shim cannot reach; `"skip_lint": false` / `FAST_UI_SKIP_LINT=0` lints.
- **Build failures say why:** the failing phase and first `error:` line (they were read from
  stderr while `-quiet` prints them on stdout), full log in `<derived_data>/.fastui-build.log`.
- **No state left for the next run:** a per-simulator lock (`simulator busy`), a journal of
  what the run changed, undone in teardown and on SIGTERM / SIGHUP / Ctrl-C (`status=interrupted`),
  and repaired by the next run after a SIGKILL (`WARN dirty=1`). Appearance changed by
  `set_appearance`, status bar overrides, location and granted permissions no longer leak
  (a following flow used to PASS wrongly in dark mode). The app is terminated at the end, and
  relaunched with `--terminate-running-process` when `fresh_install` is false.
  Keychain reset on fresh install for `fastui-…` simulators or `setup.reset_keychain`.
- **System alerts:** `optional_permission_labels` taps only buttons inside a system alert,
  never an in-app button with the same label; `tap_alert` takes `system` and `optional`;
  an alert left on screen is reported (`WARN system_alert_seen`, `setup.system_alert: "fail"`);
  `scroll_to` stops at once when a system alert covers the app.

### Every interaction a real UI test needs

Flows could tap, swipe, type and wait. They can now drive the whole surface of an app, with the
same action names on Simulator and iPhone (table in `references/usage.md`):

- **Gestures:** `long_press`, `double_tap`, `drag` (hold, then move: reorder, Home Screen
  icons, drag-and-drop), `swipe` inside an element (swipe-to-delete), `back`, `edge_swipe`,
  `pull_to_refresh`; on the iPhone `pinch`, `rotate_gesture`, `two_finger_tap`.
- **Keyboard:** `type_text` into a named field with `clear: true`, `clear_text`, `press_key`,
  `dismiss_keyboard`.
- **Controls:** `set_switch` (taps only when needed and waits for the new value), `set_slider`,
  `set_picker`, `tap_alert` (system or app alert).
- **Assertions:** `assert_value` (`equals` / `not_equals` / `contains` / `matches` on a value or
  label, polled), `assert_count`, and the selector key `enabled`.
- **Device state:** `press_button`, `activate`, `set_appearance`, `set_location`,
  `set_permission`, `set_clipboard` / `assert_clipboard`; Simulator `push_notification` and
  `status_bar`; iPhone `set_orientation`, `open_url`.

Measured on Xcode 27.0 / iOS 27 and built into the Simulator engine: a switch flips only for a
~0.3 s touch; the back gesture, row swipe actions and edge swipes need `axe drag`, not `axe
swipe`; keys are dropped unless sent inside an `axe batch` with a sleep on both sides; AXe sends
numeric values as numbers (row 0 of a wheel is `0`, not empty).

On the iPhone (checked on iOS 26.6.1): `open_url` uses the system handler instead of handing the
URL to the current app; the clipboard is read and written with the runner briefly in front
(iOS drops background access); an XCTest gesture failure is now a step FAIL instead of ending
the persistent runner; search fields are `SearchField` and match `TextField`; `dump` lists
unlabelled nodes that carry an identifier.

### Fixed

- Swipes by `direction` measured the screen as the largest frame in the tree, which in a long
  list is the scroll content (y −1580); they now use the application's frame.

## 1.3.0 — 2026-10-13

### The iPhone screen can be recorded

`fastDeviceTest --record` keeps XCTest's own screen recording of a one-shot run. XCTest records
every UI test on a device by default, but its attachment lifetime `deleteOnSuccess` threw the
clip away whenever the test passed — and the runner's tests always pass, even when the flow
fails, because the flow reports its result in a file. `--record` sets `SystemAttachmentLifetime =
keepAlways` in that run's `.xctestrun`. No Control Center, no QuickTime, no recording dot, nothing
in Photos; video inside the app keeps playing (QuickTime/CoreMediaIO capture froze it).

- A PASS keeps `<task>/final[-<session>].mp4`, a FAIL `run.mp4`, as on the Simulator; the PASS and
  FAIL lines carry `video=file://…`. The full-resolution original stays in `result.xcresult`.
- `--record` is refused with `--serve` or while a persistent runner holds the iPhone: the runner
  is one long test, so its clip would span the whole session.
- `fastDeviceTest --session` (or `FAST_UI_SESSION`, shared with the Simulator) tags device runs;
  `fastUIReport` and `fastUIReview` now read them (keyed by the iPhone's UDID).

### Clips light enough for a PR

Both engines re-encode recordings at review quality: H.264 MP4, long side 960 px, ~800 kbit/s.

| Clip | Before | After |
|---|---|---|
| iPhone, 17 s, 1290×2796 | 20 MB (H.264, 10 Mbit/s) | 1.4 MB, 442×960, ~2 s to encode |
| Simulator, 23 s | full-resolution HEVC | 0.4 MB H.264, plays in every browser |

The re-encode replaces a clip only when it succeeded and came out smaller; any failure keeps
the original. `FAST_VIDEO_LONG_SIDE` / `FAST_VIDEO_KBPS` tune it, `FAST_VIDEO_LONG_SIDE=0` keeps
originals. AVFoundation only, no ffmpeg.

### `fastEvidenceSheet`

A new launcher lays chosen screenshots into one captioned sheet for a PR or ticket: a title,
then a label and a wrapped caption above each shot, from a JSON spec. Task cells take the shot
from the task's newest passing run; JPEG quality 80 by default (seven phone shots ≈ 300 KB);
the font covers Vietnamese diacritics; one FAIL line lists every missing shot with the shots
that exist.

### Smaller fixes

- `fastUIReview` prints its sheets, notes and review folder as `file://` links, like the engines.
- Tests: `test_fast_media.py` (including a real AVFoundation encode) and
  `test_fast_evidence_sheet.py`; 102 tests in all, one of them runs every launcher as the copy a
  zip stores. `install.sh` self-tests the media module too.

## 1.2.0 — 2026-10-13

Compared with the first version (1.1.0 by DungVC, with the Xcode 27 fixes listed below).
Every change came from measurements on Xcode 27.0 and from 63 past sessions that used the
tool; the research and the plan are in `docs/`.

### Fewer reruns: failures explain themselves

The most common failure in those sessions was a wrong selector, and each rerun waited out a
20 s timeout because the FAIL line showed labels without saying why none matched.

- A FAIL line now lists `label (Type)` for every visible label, says when the wanted label
  exists under another element type (`'Add Widget' exists as StaticText, not Button`), and
  otherwise suggests the closest labels.
- `tap_label` without `element_type` taps the one node carrying the label when no Button
  does, and reports `fallback_type`. It never taps the Application or Window root, and on
  a device it treats nested containers with the same label and frame as one target.
- New selectors on simulator and device: `id` (accessibilityIdentifier, the most stable
  key) and `value` (a switch's `1`/`0`, a field's text). `tap_label` accepts every key.
- AXe sometimes fails to open its automation session on the first tap after a boot; the
  engine now retries once instead of failing the flow.

### Faster

| Where | Before | After |
|---|---|---|
| Simulator, each `wait` followed by a tap | two `describe-ui` calls (0.9–2.7 s each) | one: a tree younger than 300 ms is reused |
| iPhone, each tap on a screen that never stops animating | ~2.4 s of XCUITest wait-for-idle | ~0 with `setup.wait_for_idle: false` or `do --no-idle-wait` |
| `fastUIReview` helper | interpreted by `swift` on every run (~4 s cold) | compiled once and cached |
| Install on a new Mac with Xcode | Homebrew had to install AXe and xcodegen | under a second, nothing downloaded |

`"settle": true` on a tap or swipe is new and opt-in: it waits until the hierarchy stops
changing (at most 2.5 s), for screens where a fixed `post_delay` is a guess.

### Does more

- Simulator actions `type_text`, `launch`, `terminate` and `open_url` (deep links).
- `"prebuilt": true` in `app` drives what is already on the simulator, such as Settings or
  a build Xcode installed, without a project, DerivedData or fresh install.
- `--device` accepts a unique part of the iPhone's name; a part shared by two is refused.
- The engine runs without Pillow; only `motion` and `fastUIReview` need it.

### Lighter on tokens and disk

- `dump` and `failure-ui.json` are a flat list of label, type, id, value, frame and tap
  centre. A real failure dump went from 11 MB to 47 KB.
- `SKILL.md` went from 1366 to 887 words and the two main slash commands from 316 and 231
  to 158 and 135 words, so each invocation loads about 1.5k fewer tokens. In a before/after
  test an agent found the prebuilt-app mode in `SKILL.md` and passed on the first run
  (3 tool calls instead of 8).
- `SKILL.md` now says when to use Xcode 27's own `device-interaction` MCP tools (exploring
  an unknown screen, pinch, drag, console log) and when to use this tool (repeatable proof,
  matrix, video, report).

### Install and packaging

- AXe 1.8.0 ships in `vendor/` (MIT, licence included) and the device runner's `.xcodeproj`
  is committed: no Homebrew and no xcodegen.
- `install.sh --check` prints Xcode, AXe, Python, Pillow and signing-profile status and what
  is missing, and still reports cleanly on a Mac with no Xcode selected.
- `make-zip.sh` builds `fast-ios-device-test-<version>.zip`.
- Unit tests: 53 → 77; the suite runs with or without Pillow.

### Known limitations, planned for 1.2.1

- The docs tell prebuilt flows to start with `launch`, but the engine already launches the
  app, so that step costs an extra ~0.5 s.
- Editing `device-runner/project.yml` without regenerating the committed `.xcodeproj`
  builds the old project silently.
- `fastDeviceTest do --no-idle-wait` cannot report when the private XCTest selector is
  missing (flows do report it).
- `install.sh` does not warn when an older `axe` earlier on PATH shadows the bundled one.
- The "add element_type" hint appears even when the selector already has one.

## 1.1.0-xcode27 — 2026-10-02

Local fixes on top of 1.1.0, which was written against Xcode 26.

- The engine opens Device Hub on Xcode 27; AXe's automation session times out without it.
- Taps linger 0.4 s under Device Hub (measured: a post-delay of 0 lost 6 of 6 taps, 0.15 s
  lost 2 of 6, 0.25 s lost none).
- A flow without `build_inputs` no longer reuses a stale app forever.
- `SKILL.md` gained a complete flow example and was split into `references/device.md` and
  `references/review.md`; fan-out uses `sonnet` agents, and only for 2+ simulators or more
  than 5 flows.

## 1.1.0 — DungVC

The original tool: `/fastSimulatorTest`, `/fastDeviceTest` and `/fastUIReview`, JSON flows,
fresh install with seeded UserDefaults, dark and Dynamic Type variants, recording with
contact-sheet review, per-simulator fan-out and the `fastUIReport` table.
