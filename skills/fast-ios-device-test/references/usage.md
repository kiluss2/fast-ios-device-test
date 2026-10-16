# Usage reference

## Prerequisites

- The iPhone is trusted and available through Xcode/CoreDevice.
- Developer Mode is enabled.
- The device OS satisfies the app deployment target.
- Development signing and provisioning already work in Xcode.

## Common commands

Run a generated or user-provided Simulator UI flow with automatic app freshness checking:

```bash
scripts/fast_ui.py /absolute/path/to/task-flow.json
```

Fastest repeat when the cached app definitely matches current source:

```bash
scripts/fast_ui.py /absolute/path/to/task-flow.json --reuse-app
```

Build a new flow step by step on the live app instead of rerunning the whole flow to find
labels (`fastSimulatorTest` takes the same verbs):

```bash
scripts/fast_ui.py start /abs/draft-flow.json      # build if needed, fresh install, launch; prints labels
scripts/fast_ui.py do '{"type":"tap_label","label":"Settings"}'   # one action or a JSON array
scripts/fast_ui.py labels                           # what is on screen now (--settle N to wait)
scripts/fast_ui.py save [/abs/flow.json]            # write the recorded actions, then verify with a full run
```

`start` needs only the draft's `app` and `setup`. Each `do` runs through the same action code
as a full run and prints `OK steps=N … labels=[…]` or `FAIL step=N error=…`; failed steps,
`dump`, `screenshot` and `labels` are not recorded. `save` adds `"settle": true` to every
`scroll_to` / `swipe` that has no `settle` of its own (a replay has no think time between
steps) and runs the saved flow once from a fresh install (`--no-verify` skips that).
`--udid` picks a session when several simulators have one. State a session changes
(appearance, location, …) is not undone until the next full run on that simulator.

Refresh the cached app once before fanning flows out to several simulators:

```bash
scripts/fast_ui.py /absolute/path/to/any-flow.json --build-only   # prints READY
```

The app is built once per DerivedData, not per simulator: the fingerprint
excludes the simulator UDID, and a file lock in DerivedData serializes
concurrent builds. Flows running in parallel on different simulators should
all pass `--reuse-app`. Artifacts land in
`build/fast-ui/<task>/<timestamp>-<udid8>-<pid>/`, so parallel runs never
overwrite each other.

One run per simulator: a run holds a lock on its UDID, and a second run (or a session `do`)
on the same simulator fails at once with `simulator busy: … is running <flow> (pid …)`
(`FAST_UI_LOCK_WAIT=<seconds>` waits instead). Every change a run makes to the simulator is
journalled beside that lock and undone in teardown: seeded preferences, the app (terminated),
appearance, content size, location, status bar, permissions granted with `set_permission`.
SIGTERM / SIGHUP / Ctrl-C still run teardown and write `metrics.json` with
`status=interrupted` (exit 130). A run killed with SIGKILL leaves the journal dirty; the next
run on that simulator prints `WARN dirty=1 … repaired …` and undoes it before starting.
The simulator keychain survives an uninstall: a fresh install resets it only on simulators
named `fastui-…` / `fastwf-…`, or when `setup.reset_keychain` is true.

### SwiftLint and other build failures

Test builds skip SwiftLint by default: `xcodebuild` runs with a no-op `swiftlint` shim
(`scripts/shims/`) first on `PATH`, so a run-script phase that calls plain `swiftlint`
finishes at once and its violations cannot fail the build (measured: a no-op build 6–9 s → 2–3 s).
The engine prints `lint=shimmed` or `lint=not-shimmable (<reason>)`: a phase that runs
swiftlint by path (`${PODS_ROOT}/SwiftLint/swiftlint`, `/opt/homebrew/bin/swiftlint`),
through `mint run`, after prepending to `PATH`, or the SwiftLint build-tool plugin is not
bypassed; the build cache (`build_inputs` fingerprint) still skips those builds when nothing
changed. `"skip_lint": false` in `app`, or `FAST_UI_SKIP_LINT=0`, lints as usual.
A failed build names the failing phase and the first `error:` line, and the full log is
written to `<derived_data>/.fastui-build.log`.

`--record` captures the simulator screen (`simctl io recordVideo`) and re-encodes it at
review quality: H.264 MP4, long side 960 px, ~800 kbit/s, so a 20 s clip is 0.5–2 MB
and plays in any browser or PR attachment. The re-encode only replaces the recording when
it succeeded and came out smaller (`FAST_VIDEO_LONG_SIDE=0` keeps the original). On
PASS the clip becomes `<artifacts-root>/<task>/final.mp4` (`final-<session>.mp4`
when `--session` is set, so two sessions never overwrite each other) and every
older clip of that task is deleted; on FAIL the clip stays as `run.mp4` in that
run's folder. The artifacts root defaults to `<project_root>/build/fast-ui`, which
is scratch space: pass `--artifacts-root <dir>` (or `FAST_UI_ROOT`) to keep runs,
screenshots and videos somewhere durable, e.g. the QA report folder, and set
`FAST_UI_KEEP_DAYS=0` there. Every run first prunes the root: run folders and
final videos untouched for 3 days are deleted (`FAST_UI_KEEP_DAYS` overrides; `0`
disables).
`--session <name>` (or `FAST_UI_SESSION`) tags `metrics.json`;
`scripts/fast_ui_report.py --root <project>/build/fast-ui --session <name>`
(launcher `fastUIReport`) collates every tagged run into a flow × simulator
table; `--since YYYYmmdd-HHMMSS` works without tags; `--json` for raw cells.

The engine boots the simulator (`simctl bootstatus -b`) before building, so a
shut-down device fails in seconds instead of after a full build.

The runner prints one PASS/FAIL line and stores screenshots, failure hierarchy,
and per-step timings under `build/fast-ui/<task>/<timestamp>/`. Add `--json`
only when full machine-readable results are needed.

## Dynamic UI flow format

A flow contains app/build configuration, fresh-install state, and an ordered
list of generic actions. Feature-specific labels belong only in flow files.

Supported actions:

- `wait`: wait for a selector.
- `pause`: hold for `seconds` without touching the UI.
- `wait_absent`: wait until nothing matches the selector (a popup closed, a
  sheet dismissed); fails if it is still there at `timeout`.
- `assert_absent`: poll for `seconds` and fail the moment the selector appears.
  This is how a "must NOT show" case is proven — never infer absence from a tap
  that happens to succeed (a full-screen cover can leave the tab bar tappable).
- `tap_label`: accessibility tap; resolves the selector through the same polling
  loop as `wait`, so a permission alert that appears between actions is dismissed
  instead of swallowing the tap. Pass `optional: true` to skip that resolution and
  tolerate a missing control.
- `tap_point`: tap a declared coordinate.
- `swipe`: gesture from `from` to `to` coordinates, or `direction`
  (`up`/`down`/`left`/`right`, the way the finger moves: `up` scrolls content down).
- `scroll_to`: swipe in `direction` (default `up`) until the selector is on screen,
  at most `max_swipes` (default 6) times; fails with the visible labels otherwise.
- `tap_first`: filter nodes, sort by frame keys, and tap the first match.
- `tap_nearest`: tap a target closest to one unambiguous anchor, e.g. the ⓘ of one
  Wi-Fi row: `{"type": "tap_nearest", "target": {"label": "More Info"},
  "anchor": {"label": "Office", "element_type": "StaticText"}}`. `axis` is `y`
  (same row, default) or `x` (same column).
- `screenshot`: save one named image.
- `motion`: capture frames and assert a minimum changed-pixel ratio in a
  normalized `[left, top, right, bottom]` region.
- `dump`: write the on-screen nodes to `<name>-ui.json` (one flat JSON list: `label`,
  `type`, `id`, `value`, `frame`, tap `center`; a few tens of KB) and print the visible
  labels (screen order, status-bar digits removed) in the PASS line. Use it once to
  discover a selector; remove it from the final flow.
- `type_text`: type `text` into the focused field (US keyboard characters).
- `launch` / `terminate`: `simctl launch` / `terminate` the flow's app, or `"app":
  "<bundle id>"`; `launch` takes `launch_arguments`.
- `open_url`: `simctl openurl` with `url` (deep links, universal links).

### Gestures, keyboard, controls, assertions and device state

A gesture target is a selector (the same keys as `wait`) on the action itself, `x`/`y`, or
for `drag` a `from`/`to` that is either a selector mapping or an `[x, y]` point. Where an
engine cannot do an action it fails with a message naming the one that can. Columns: Sim =
`fastSimulatorTest`, iPhone = `fastDeviceTest`.

| Action | Keys | Sim | iPhone |
|---|---|---|---|
| `long_press` | target, `duration` (1.0 s) | ✓ | ✓ |
| `double_tap` | target | ✓ | ✓ |
| `two_finger_tap` | optional selector (default: whole app) | – | ✓ |
| `drag` | `from`, `to`, `hold` (0.6 s still before moving: reorder, drag-and-drop, Home Screen icons), `duration` (0.8 s) | ✓ | ✓ |
| `pinch` | optional selector, `scale` (>1 zooms in), `velocity` | – | ✓ |
| `rotate_gesture` | optional selector, `degrees` (90) | – | ✓ |
| `swipe` on an element | selector + `direction`: the swipe stays inside that frame (swipe-to-delete a row) | ✓ | ✓ |
| `back` | swipe from the left edge | ✓ | ✓ |
| `edge_swipe` | `edge`: `left`/`right`/`top` (Notification Center)/`bottom` (home indicator) | ✓ | ✓ |
| `pull_to_refresh` | optional selector of the list | ✓ | ✓ |
| `type_text` | `text`; optional selector taps the field first; `clear: true` empties it first | ✓ | ✓ |
| `clear_text` | optional field selector (else the focused field) | ✓ | ✓ |
| `press_key` | `key`: `return`/`enter`, `delete`/`backspace`, `tab`, `space`, `escape`; Sim also `up`/`down`/`left`/`right`; `count` | ✓ | ✓ (no arrows) |
| `dismiss_keyboard` | optional `button` (a toolbar "Done"); else Return, repeated while the keyboard stays | ✓ | ✓ |
| `set_switch` | selector, `on`: true/false; taps only when needed, then waits for the new value; result `toggled` | ✓ | ✓ |
| `set_slider` | selector, `percent` 0–100, `tolerance` (2) | ✓ | ✓ |
| `set_picker` | selector, `option` (row text) or `index` (row number) | ✓ | ✓ (`option` only) |
| `tap_alert` | `button`: a button inside a system or app alert, waited for; `system: true` only a system alert; `optional: true` passes (`skipped`) when no alert came | ✓ | ✓ |
| `assert_value` | selector, `attribute` (`value`/`label`), `equals` / `not_equals` / `contains` / `matches` (regex); polls until `timeout` | ✓ | ✓ |
| `assert_count` | selector, `count` / `min` / `max`; a box counts once; result `count` | ✓ | ✓ |
| `press_button` | `button`: `home`; Sim also `lock`, `side`, `siri`, `apple_pay`; iPhone also `volume_up`, `volume_down`, `action`, `camera` | ✓ | ✓ |
| `activate` | brings the app (or `app`) to the front without relaunching | ✓ | ✓ |
| `set_orientation` | `portrait`, `portrait_upside_down`, `landscape_left`, `landscape_right` | – | ✓ |
| `set_appearance` | `light` / `dark`, mid-flow | ✓ | ✓ |
| `set_location` | `latitude`, `longitude`, or `clear: true` | ✓ | ✓ (iOS 16.4+) |
| `set_permission` | `service` (`photos`, `camera`, `location`, `contacts`, `microphone`, …), `action` (required) `grant`/`revoke`/`reset` | ✓ | `reset` only |
| `push_notification` | `title`/`body`, or a whole APNs `payload`; `app` | ✓ | – |
| `status_bar` | `time`, `battery_level`, `battery_state`, `wifi_bars`, `cellular_bars`, `data_network`, `operator_name`, …, or `clear: true` | ✓ | – |
| `set_clipboard` / `assert_clipboard` | `text` / `equals`, `contains`, … | ✓ | ✓ |
| `open_url` | `url` | ✓ | ✓ (iOS 16.4+) |

Selectors also take `enabled: false` to find a disabled control (`assert_count` with
`"enabled": false, "max": 0` proves nothing on screen is disabled); `dump` marks disabled nodes.

What the Simulator needs, measured on Xcode 27.0 with AXe 1.8 and iOS 27:

- A switch flips only for a touch held ~0.3 s; AXe's taps, `--tap-style physical` included,
  leave it alone. `set_switch` does this. AXe reports a switch as `CheckBox` on iOS 27 (it
  accepts `Switch` too); a scroll view's scroll bars are `Slider`s, so `set_slider` skips them.
- The back gesture, row swipe actions and edge swipes ignore `axe swipe` (a fling); the engine
  sends them as `axe drag`, every touch move. Plain `swipe` without a selector is still a fling,
  so scrolling keeps its momentum.
- A key is dropped when AXe sends it right as a session opens or closes, like a tap: keys and
  typed text go out in one `axe batch` with a sleep before and after (a lone `axe type` lost
  the last character). iOS still applies smart punctuation: `'` and `"` arrive curly, and two
  spaces become ". ".
- The status bar may be missing from the AX tree: prove a `status_bar` override with a
  `screenshot` (simctl shows `9:41` as `09:41`).
- AX reports an empty text field's value as its placeholder. A SwiftUI `Slider` reports a
  fraction (`0.5`), a UIKit one a percentage (`50%`); a SwiftUI wheel `Picker` is a `Slider`
  whose value is the row index, so pick by `index` there, by `option` on a UIKit picker.
- A system alert that appears while the flow is typing swallows the tap that focused the field.
  When the app asks for a permission at launch, answer it first with
  `{"type": "tap_alert", "button": "Allow", "system": true, "optional": true}`: iOS drops a
  permission request made in the first 40–60 s after a boot under load, so the prompt may
  never come and a required `tap_alert` would fail for nothing.
- SwiftUI `.draggable` / `.dropDestination` never lifts on the Simulator (a 1.5 s hold
  included); drag-and-drop between views needs the iPhone. Home Screen icons do move with
  `drag`.
- `push_notification` fails until the app has notification permission; the error says so.
- `simctl pbcopy` exits 0 but leaves an iOS 27 simulator's pasteboard empty, so
  `set_clipboard` fails there with that message; use an Xcode 26 simulator or the iPhone.
- `double_tap` sends both taps in one AXe session, which is a double tap only while AXe
  delivers a touch in well under 0.3 s. On a slow Simulator (measured 0.73 s per touch event
  right after a CoreSimulator restart) the two taps arrive as two single taps; prove double
  taps on the iPhone, where XCUITest's `doubleTap()` has no such gap.

Any tap or swipe may carry `"settle": true` (or a number of seconds, default 2.5): after the
gesture the engine polls until two consecutive hierarchies match, then continues. It costs the
whole timeout on a screen that never stops animating, so it is opt-in per action;
`setup.settle_after_actions: true` turns it on for a whole flow.

Every `wait` timeout failure prints `visible labels: [Add Widget (StaticText), …]` for the
screen the flow stopped on, with each label's element type; when the wanted label exists under
another type it says so (`'X' exists as StaticText, not Button: add element_type or use
tap_first`), and otherwise lists the closest labels, so a wrong selector is fixed from the
FAIL line alone. `tap_label` without `element_type` taps the one node of any type carrying
the label when no Button does, and reports `fallback_type` in the results.

Visual capture uses Simulator's native framebuffer rather than AXe. Passing
flows capture nothing unless they contain `screenshot` or `motion`; passing
`motion` frames are deleted by default. Set `keep_frames: true` only when the
images themselves are needed. A failing run always keeps one screenshot and UI
hierarchy for diagnosis.

`post_delay` is capped at AXe's 10-second limit and the remainder is held by the
runner, so a longer settle time is expressed directly rather than by chaining
gestures. Prefer `pause` over a one-pixel `swipe` when a flow only needs to wait:
a swipe can scroll the screen it was meant to leave alone.

Selectors accept `label`, `label_contains`, `label_is_empty`, `id` (the
accessibilityIdentifier, `AXUniqueId`), `value` (`AXValue`: "1"/"0" for a switch, the text of
a field), `element_type`, and bounds nested under `frame`, such as `{"frame": {"min_y": 400,
"max_width": 200}}` (`min_`/`max_` of `x`, `y`, `width`, `height`). `id` is the most stable
key when the app sets identifiers; `tap_label` takes every key too. Missing or ambiguous
required selectors fail closed. Optional system prompts are declared under
`setup.optional_permission_labels`. The runner dismisses them from the same UI
hierarchy polling loop used by selectors, so prompts that appear late do not
need fixed sleeps or a separate retry loop. Only a button inside a system alert is
auto-tapped; an in-app button with the same label is the app's own UI and is left alone.
A system alert the flow saw but never dismissed is reported on the PASS line as
`WARN system_alert_seen=…` (`setup.system_alert: "fail"` turns that into a FAIL), and
`scroll_to` fails at once when a system alert covers the app instead of swiping it.

System sheets that block a Simulator flow and their dismiss labels: notification
permission (`Allow` / `Don’t Allow`), App Tracking Transparency (`Allow Tracking`
/ `Ask App Not to Track`), StoreKit sandbox sign-in "You must enter both your
Apple Account and password." (`Cancel`). Declare all of them in
`setup.optional_permission_labels` for any flow that reaches a paywall or
StoreKit call; the sheet persists across fresh installs.

`tap_nearest` may declare a `success` selector and one or more `dismiss`
selectors. If a declared modal appears instead of success, the runner dismisses
it, resolves the original anchor/target again, retries once, and requires the
success selector. This keeps paywalls and other gates in flow data rather than
feature-specific Python.

Scalar preferences support booleans, numbers, and strings. For a Codable value
stored as JSON `Data` in UserDefaults, use `{"$json_data": {...}}`; the runner
encodes that object as UTF-8 data before launch. Keep model-specific fixtures in
the flow file, never in the engine.

Matrix variants: `setup.appearance` (`light`/`dark`) and `setup.content_size`
(a `simctl ui content_size` name), or `--appearance` / `--content-size` on the
command line, which win over the flow. The runner applies them with `simctl ui`
before launch, restores the simulator's previous values after the run (pass or
fail), and names the task `<flow>@dark@xxl` so each variant is its own report
row. An app that forces its own theme from stored settings needs that setting
seeded in `preferences` as well.

With `--record`, every step also stores the visible labels in `metrics.json`
(`step_labels`, with the second it ended in the video); `fastUIReview` prints
them per SHEET and copies them into the review notes.

Prebuilt app: `"app": {"bundle_id": "…", "prebuilt": true, "simulator": "<UDID>",
"project_root": "<dir>"}` drives whatever is installed on the simulator (a system app such as
`com.apple.Preferences`, a build Xcode installed) without `project`, `derived_data` or
`app_path`. `fresh_install` then defaults to false; set it true only together with an
`app_path` to reinstall. Start such a flow with `{"type": "launch"}`.

If an `axe` call fails with a SimulatorKit/PrivateFrameworks error, AXe is older
than 1.8.0 and the selected Xcode is 27: `install.sh` unpacks the bundled 1.8.0 from
`vendor/`. On Xcode 27 AXe also needs Device Hub running; the engine opens it when it is not.
Pillow is only needed by `motion`, `fastUIReview` and `fastEvidenceSheet`.

For a new task, generate the smallest temporary flow containing only its
state/actions/assertions, then remove it after the run. Save reusable flows only
when the user requests persistence. The engine itself should never be changed
for a new screen.

## Physical iPhone (`fastDeviceTest`)

The same flow JSON runs on a device. Unused keys (`simulator`, `derived_data`,
`app_path`, `fresh_install`) are ignored; `app.device` or `--device` picks the
iPhone (required when several are connected).

```bash
fastDeviceTest flow.json --device "My iPhone"          # one-shot, ~14 s
fastDeviceTest serve --device "My iPhone"              # keep a runner alive, ~5 s
fastDeviceTest flow.json --device "My iPhone"          # via the runner, ~5 s
fastDeviceTest do '{"type":"tap_label","label":"Continue"}' --device <d>   # ~1 s
fastDeviceTest do '{"type":"screenshot","name":"now"}' --device <d> --out <dir>
fastDeviceTest stop --device <d>
```

- The runner (`device-runner/`, its `.xcodeproj` committed; regenerate with `xcodegen
  generate` after editing `project.yml`) is cached per Xcode + signing profile + runner
  source in `~/Library/Caches/fast-device-test`; `--rebuild-runner` forces a rebuild.
- `--device` takes a UDID, the full name, or a unique part of the name.
- `setup.wait_for_idle: false` (flows) or `--no-idle-wait` (`do`) skips XCUITest's
  wait-for-idle around events on screens that never go idle; the report shows `wait_for_idle`.
- Signing: an existing development profile that lists the device and matches a
  non-revoked keychain identity; never the bundle id of an installed app.
  `FAST_DEVICE_PROFILE=<uuid>` pins one.
- `setup.preferences` → launch arguments (one launch, never persisted);
  `setup.content_size` → `-UIPreferredContentSizeCategoryName`; `setup.appearance`
  → `XCUIDevice.appearance`, restored after the flow.
- Artifacts: `<project_root>/build/fast-device/<task>/<run>/` (`FAST_DEVICE_ROOT`).
- `--record` (one-shot runs) keeps XCTest's own screen recording of the iPhone and
  re-encodes it at the same review quality as the Simulator: a PASS keeps
  `<artifacts-root>/<task>/final[-<session>].mp4`, a FAIL `run.mp4` in its run folder.
  Without `--record` no clip is kept, a failing flow included: the runner reports a FAIL
  in its result file, so for XCTest the test passed and its recording is deleted.
  The full-resolution original stays inside `result.xcresult`. Details in
  [device.md](device.md).
- `--session <name>` (or `FAST_UI_SESSION`, shared with the Simulator engine) tags
  `metrics.json`, so `fastUIReport` and `fastUIReview` collate device runs too (the
  column is the iPhone's UDID prefix).

## Reviewing recordings

`scripts/fast_ui_review.py --root <artifacts-root> --session <name> [--interval 1.0]
[--change-threshold 0.01] [--columns 6] [--out <dir>]` (launcher `fastUIReview`)
reads every `metrics.json` of the session that has a video (Simulator or iPhone runs) and writes
`<out>/<task>-<sim>.png`: a header with the case name, status and the flow's
`expect` text, then one frame at the end of each flow step (labelled with the
step name) plus one every `--interval` seconds. Interval frames whose pixels
changed less than `--change-threshold` (share, 0–1) against the previous kept
frame are dropped; step frames are always kept. Each `SHEET` line reports
`frames=kept/total` and `tokens≈` (pixels / 750, Claude's image cost).

It also writes `<out>/review-notes-<session>.json`:

```json
{"a01-native": {"simulator": "452BF01E", "status": "pass", "expect": "...",
                "sheet": "...png", "video": "...mp4", "verdict": null, "note": ""}}
```

The reviewer fills `verdict` (`OK` / `product bug` / `flow bug`) and `note`;
running the tool again keeps them. The `NOTES ... pending=N` line counts cases
without a verdict.

`--combined <video.mp4> --manifest <items.json> [--title-seconds 2.5] [--tolerance 0.5]`
verifies a concatenated deliverable instead: the manifest is the list of
`{path, title, subtitle}` the file was built from (one title card per clip). The
tool reads every clip's duration, samples the middle of each title card and the
last second of each clip into `combined-titles.png` / `combined-ends.png`, writes
`combined-timeline.json`, and prints `COMBINED items=N expected=…s actual=…s
delta=…s OK|MISMATCH` (exit 2 on mismatch).

Frames come from AVFoundation through a Swift helper, so no ffmpeg is needed. The
sheets are meant to be read one by one; the PASS line only proves the selectors
resolved. Device runs carry no per-step times, so their sheets use the interval grid
only.

## Evidence sheet for a PR (`fastEvidenceSheet`)

`scripts/fast_evidence_sheet.py <spec.json>` (launcher `fastEvidenceSheet`) lays chosen
screenshots side by side with a title, a label and a caption per cell, for a PR or a
ticket. It is not a review tool: `fastUIReview` samples a recording, this one shows the
outcome of each case.

```json
{
  "title": "PRJ-12 · Paywall gating (Staging, iPhone iOS 26.6)",
  "root": "/abs/MyApp/build/evidence-prj-12/device",
  "out": "/abs/MyApp/build/evidence-prj-12/pr/gating.jpg",
  "columns": 7,
  "cells": [
    {"task": "gating-prod", "shot": "1-paywall", "label": "G1 · Production", "caption": "paywall on reopen"},
    {"task": "gating-off", "shot": "main", "label": "G2 · Off", "caption": "reopen: false →\nno paywall"},
    {"image": "/abs/before.png", "label": "Before", "caption": "the bug"}
  ]
}
```

- `root` is the `--artifacts-root` the flows ran with. A `task` cell takes `<shot>.png`
  from that task's newest **passing** run (`metrics.json` status `pass`; Finder files
  and failing runs are skipped); `"run": "<run folder>"` pins one, e.g. a "before" shot
  from a failing run. An `image` cell takes any file. Relative paths resolve against the
  spec file.
- Captions wrap to the cell (`\n` forces a break); the font covers Vietnamese and other
  diacritics. Cells are 360 px wide (`cell_width`), `columns` per row (default 7).
- `out` ending in `.jpg` (the default) writes JPEG quality 80: seven phone shots are
  ~300 KB and still readable at 2× zoom. `.png` keeps it lossless (~3× larger).
- Every cell is checked first; one FAIL line lists every missing shot together with the
  shots that run does have. Success prints `SHEET cells=… size=WxH kb=… tokens≈… path=file://…`.
  Read the sheet back before handing it over.

## Matrix: appearance and Dynamic Type

Both engines take `setup.appearance` / `setup.content_size` or the CLI overrides
`--appearance light|dark` and `--content-size <simctl name>`, and suffix the task
(`flow@dark@xxl`) so `fastUIReport` shows each variant as its own row. The
Simulator engine sets `simctl ui` and restores the previous values afterwards;
the device runner sets `XCUIDevice.appearance` (restored after the flow) and
passes the size as `-UIPreferredContentSizeCategoryName`, which lasts one launch.
An app that pins its own theme (e.g. an `appThemeMode` setting) or uses fixed point
sizes ignores these; seed the theme via preferences, and treat text that does
not grow at `accessibility-*` sizes as a product finding.

## Environment variables

- `FAST_UI_ROOT`: artifacts root for `fastSimulatorTest` (runs, screenshots, videos).
- `FAST_UI_SESSION`: default `--session` for both engines.
- `FAST_VIDEO_LONG_SIDE` / `FAST_VIDEO_KBPS`: review-quality re-encode of recordings
  (960 / 800); `FAST_VIDEO_LONG_SIDE=0` keeps the original clip.
- `FAST_UI_KEEP_DAYS`: prune age for that root; `0` disables pruning.
- `FAST_UI_SKIP_LINT`: `0` lets SwiftLint run in test builds (default: skipped through the shim).
- `FAST_UI_LOCK_WAIT`: seconds a run waits for a busy simulator (0: fail at once).
- `FAST_UI_LOCK_DIR`: where the per-simulator locks and journals live
  (`~/Library/Caches/fast-ui/locks`).
- `FAST_DEVICE_ROOT`: artifacts root for `fastDeviceTest`.
- `FAST_DEVICE_PROFILE`: provisioning profile UUID or path for the device runner.
- `FAST_DEVICE_DEVELOPER_DIR`: Xcode to build/run the device runner with.
- `FAST_DEVICE_PORT` / `FAST_DEVICE_IDLE`: runner port (8765) and idle timeout in seconds (1800).
- `FAST_DEVICE_CACHE`: runner cache root.

## Cache model

The engine also stores its app-build fingerprint inside the configured
DerivedData directory. It never compares source timestamps with the `.app`
directory timestamp, because incremental Xcode builds can preserve an older
output timestamp and otherwise trigger the same build forever. If inputs change
during a build, no new fingerprint is committed and the next invocation safely
rebuilds once more.

## Troubleshooting

- Several iPhones connected → pass `--device` (name or UDID); the launcher never guesses.
- `is busy: another runner holds it` → another agent is serving that iPhone; use a
  different one. Never `stop` a runner you did not start.
- `no usable development profile` → sign in to Xcode once (Settings › Accounts)
  and run any app on that iPhone, or set `FAST_DEVICE_PROFILE`.
- `Unable to Install … identity no longer valid` → a revoked certificate; rerun
  with `--rebuild-runner` after removing it from the keychain.
- The device must be unlocked, in Developer Mode, trusting this Mac, with
  Settings › Developer › Enable UI Automation on if iOS asks.
- A tap reports PASS but nothing changed → check `labels` in the output; only
  on-screen elements match, so the target may need a `swipe` first.
