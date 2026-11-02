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
disables). `fastDeviceTest` prunes its root the same way. A PASS run filed as evidence
(below) in the default root keeps only its `metrics.json`.
`--session <name>` (or `FAST_UI_SESSION`) tags `metrics.json`;
`scripts/fast_ui_report.py --root <project>/build/fast-ui --session <name>`
(launcher `fastUIReport`) collates every tagged run into a flow × simulator
table; `--since YYYYmmdd-HHMMSS` works without tags; `--json` for raw cells.

The engine boots the simulator (`simctl bootstatus -b`) before building, so a
shut-down device fails in seconds instead of after a full build.

At the end of the work, give the Mac its memory back:

```bash
scripts/fast_ui.py shutdown                      # every booted simulator
scripts/fast_ui.py shutdown --simulator <udid>   # only this one
```

It ends a recorder left running first and prints `SHUTDOWN simulators=<name (udid)>, …`
(`SHUTDOWN none booted` when there was nothing to do).

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
- `long_press`: hold for `duration` seconds (default 1) on a selector, resolved as `tap_label`
  resolves it, or on `x`/`y`: a context menu, or a Home Screen widget's `Edit Widget`.
- `double_tap`: two taps on a selector or `x`/`y` (zoom a photo, like a post).
- `drag`: one finger from `from` to `to`, each `[x, y]` or a selector (the first match, top-left
  first). It rests `hold` seconds on `from` first (default 0.6; `0` drags at once), which a drag
  and drop or a list reorder needs; `duration` is the move (on the iPhone, XCUITest's own speed
  when it is left out). `edge_swipe` takes `duration` the same way.
- `edge_swipe`: from one point inside `edge` (`left`, `right`, `top`, `bottom`), `at` (0–1, default
  0.5) along it, `distance` (0–1, default 0.8; a back swipe that stops at half the screen springs back) of
  the screen inward: back swipe (`left`), Control
  Center (`top`, `at` 0.9), Notification Center (`top`, `at` 0.3), home (`bottom`).
- `press_key`: `key` `delete`, `return`, `tab`, `escape`, `space`, `up`, `down`, `left` or `right`,
  `count` times.
- `key_combo`: `key` (one letter or digit, or a `press_key` name) held with `modifiers`, a list of
  `command`, `shift`, `option`, `control`: `{"type": "key_combo", "key": "a", "modifiers":
  ["command"]}` selects all in a field; iPad and hardware-keyboard shortcuts.
- `clear_text`: taps the text field a selector names (a caption with the same label is skipped;
  else the first text field on screen) near its trailing edge, selects all and deletes. The
  iPhone fails if text is left; the Simulator, where AXe shows a placeholder as the value,
  returns what the field shows afterwards as `value`.
- On the iPhone, `type_text`, `press_key`, `key_combo` and `clear_text` need a focused field and
  say so otherwise (XCUITest would end the runner).
- Simulator edge swipes, measured on iOS 26.5: back (`left`), Control Center (`top`, `at` 0.9) and
  the lock screen's swipe up (`bottom`) work; Notification Center never opens; go home with
  `press_button` `home`, since a `bottom` swipe with the keyboard up stays in the app and on the
  Home Screen it can start icon editing.
- `press_button`: `home` (it does not close an open Control Center; `edge_swipe` `bottom` does;
  on the iPhone the next steps act on SpringBoard until an action names another `app`);
  `siri` (on the iPhone with the request as `text`, which the screen map never keeps);
  `lock` on the Simulator only (a locked iPhone needs its passcode, so it locks only at the end,
  with `fastDeviceTest lock`; a second `lock` wakes the
  Simulator on its lock screen and `edge_swipe` `bottom` unlocks it); `volume_up` /
  `volume_down` on the iPhone only.
- `set_slider`: the slider matched by `id` or `label` (required on both engines) to `value` from
  0 to 1. On the iPhone it
  lands within a few percent (0.7 read back 73 %). On the Simulator AXe refuses a slider whose
  selector matches two nested elements, which SwiftUI often makes; move its thumb with `drag`.
- iPhone only (the Simulator says why): `two_finger_tap` on a selector or `element_type`
  (`{"type": "two_finger_tap", "element_type": "Map"}` zooms a map out; `taps`, default 1),
  `two_finger_drag` (two fingers side by side: `{"type": "two_finger_drag", "dy": -0.25,
  "element_type": "Map"}` tilts an Apple Maps map; `dy` is the travel as a fraction of the element's
  height, negative = up, `spread` the gap between the fingers in points (60), `duration` in
  seconds (0.6), `at` (0-1, default 0.5) how far down the element the travel is centred; the whole
  app when no selector is given. A sheet under the fingers drags instead of the map, so on a
  map with a sheet open at its middle height use `"at": 0.3`; built on private XCTest classes, so an
  Xcode that drops them refuses the action with the reason),
  `pinch` with `scale` (above 1 zooms in) and `rotate` with `radians` (two fingers; on the
  element a selector or `element_type` names — `Map`, `ScrollView`, `WebView`, `Image`, `Cell`,
  `Table`, …; an unknown name is refused — else the whole app; XCUITest starts a zoom-out with
  the fingers near the element's edges, so on a full-screen map under a sheet it drags the sheet
  instead), `orientation` with
  `value` `portrait`, `landscape_left`, `landscape_right` or `portrait_upside_down` (a flow turns
  the iPhone back to portrait when it ends; a `do` leaves it; apps that only run upright, such as
  Settings, do not turn), `set_picker` with the wheel's own text as `value` (`07`, not `7`) and
  its `index` (default 0). A value the wheel does not have ends XCTest's whole test: the FAIL
  line quotes XCTest's list of the wheel's values, and the next request starts a new runner.
- Simulator only: `shake` (the Simulator's Shake). XCUITest has no shake on an iPhone.
- `swipe`: gesture from `from` to `to` coordinates, or `direction`
  (`up`/`down`/`left`/`right`, the way the finger moves: `up` scrolls content down).
- `scroll_to`: swipe in `direction` (default `up`) until the selector is on screen,
  at most `max_swipes` (default 6) times; fails with the visible labels otherwise.
- `tap_first`: filter nodes, sort by frame keys, and tap the first match.
- `tap_nearest`: tap a target closest to one unambiguous anchor, e.g. the ⓘ of one
  Wi-Fi row: `{"type": "tap_nearest", "target": {"label": "More Info"},
  "anchor": {"label": "Office", "element_type": "StaticText"}}`. `axis` is `y`
  (same row, default) or `x` (same column).
- `screenshot`: save one named image. It first taps away any prompt declared in
  `setup.optional_permission_labels` that is on screen (one tree read, skipped when the flow
  declares none), so a prompt raised during a `pause` stays out of the image. Then it shoots
  until two shots 0.25 s apart differ by at most `stable_ratio` (default 0.005) of the screen
  below the status bar, for at most `stable_timeout` s (default 3), and keeps the last one: a
  popup still sliding or a toast still fading is never the image. A screen that never settled
  is named in the PASS line as `unsettled=<name>`: that image is not evidence. A ticking timer
  stays under the ratio; `"stable": false` takes one plain shot (a video or animation on purpose).
- `motion`: capture frames and assert a minimum changed-pixel ratio in a
  normalized `[left, top, right, bottom]` region.
- `dump`: write the on-screen nodes to `<name>-ui.json` (one flat JSON list: `label`,
  `type`, `id`, `value`, `frame`, tap `center`; a few tens of KB) and print the visible
  labels (screen order, status-bar digits removed) in the PASS line. Use it once to
  discover a selector; remove it from the final flow.
- `capture` (Simulator): `<name>.png` and `<name>-ui.json` of the same instant, the screenshot
  taken while the tree is read. End a comparison flow with it instead of `dump` + `screenshot`:
  a toast or an animation is then in both or in neither.
- `type_text`: type `text` into the focused field (US keyboard characters).
- `launch` / `terminate`: `simctl launch` / `terminate` the flow's app, or `"app":
  "<bundle id>"`; `launch` takes `launch_arguments`.
- `open_url`: `simctl openurl` with `url` (deep links, universal links). It hands the link straight
  to the app, with no prompt. A link tapped in Safari raises "Open in “<App>”?" (`Cancel` / `Open`)
  instead; Safari's page content is not in the accessibility tree, so tap the link with `tap_point`.

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
hierarchy polling loop used by selectors (`wait`, the taps, `dump`, absence checks) and right
before a `screenshot`, so prompts that appear late do not need fixed sleeps or a separate
retry loop. `pause`, `tap_point` and `swipe` read no tree: a prompt raised during them stays
up until the next of those actions. Declare the prompt's label rather than tapping it by
coordinate; its position moves while it animates in.

Bare labels answer in the order they are declared, so two prompts that share a button name
get the same answer: notifications and tracking both have `Allow`. A rule names the button
for one kind of prompt by a fragment of its title (case-insensitive), and is checked before
the bare labels:

```json
"optional_permission_labels": [
  {"title_contains": "Notifications", "tap": "Allow"},
  {"title_contains": "track", "tap": "Ask App Not to Track"},
  {"title_contains": "location", "tap": "Allow While Using App"},
  "Cancel"
]
```

A rule whose title matches owns that prompt: when its button is missing (another iOS
version, another language) nothing is tapped and the step times out showing the prompt's
labels, instead of a bare `Don’t Allow` answering it. Whichever button is tapped sets that
permission for the rest of the run (and what analytics records), so declare the answer the
test needs. A prompt that is itself under test gets its own step, not an entry here.

System sheets that block a Simulator flow and their dismiss labels: notification
permission (`Allow` / `Don’t Allow`), App Tracking Transparency (`Ask App Not to Track` /
`Allow` on iOS 26, `Allow Tracking` before), location (`Allow While Using App` / `Don’t
Allow`), StoreKit sandbox sign-in "You must enter both your
Apple Account and password." (`Cancel`). Declare all of them in
`setup.optional_permission_labels` for any flow that reaches a paywall or
StoreKit call; the sheet persists across fresh installs. A flow that taps a link in Safari is the
exception: `Cancel` also dismisses its "Open in “<App>”?" prompt and the link is lost, so declare
`Open` there instead.

The App Store review sheet ("Enjoying <App>? … Not Now", from `requestReview`) is not in the
accessibility tree, so no label can dismiss it, and it swallows the next tap while it is up. Find
`Not Now` on a `screenshot` and close it with `tap_point` before the next tap.

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
fastDeviceTest lock --device <d>                       # end of the work: stop + lock the screen
```

- The runner (`device-runner/`, its `.xcodeproj` committed; regenerate with `xcodegen
  generate` after editing `project.yml`) is cached per Xcode + signing profile + runner
  source in `~/Library/Caches/fast-device-test`; `--rebuild-runner` forces a rebuild.
- `--device` takes a UDID, the full name, or a unique part of the name.
- `do` skips XCUITest's wait-for-idle by default (`--idle-wait` keeps it); a flow skips it with
  `setup.wait_for_idle: false`. The runner then waits for the layout itself; the report shows
  `wait_for_idle`.
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

## Evidence folder (filed after every flow run)

A flow run on either engine is filed as PR evidence when it passed, or when it is a
**before** run (`--before`, or `"evidence": {"role": "before"}`) that failed at or after its
outcome step. Nothing else is filed: `do`, `--build-only`, other failures.

```
~/Desktop/<project> <ticket>/          FAST_EVIDENCE_DIR overrides ~/Desktop
  <ticket>.jpg                         captioned sheet: attach it
  <case>.mp4, <case>-before.mp4        the run's clip (--record), already 960 px
  source/sheet.json                    fastEvidenceSheet spec: edit, then render again
  source/shots/<case>/<role>-<shot>.jpg   720 px, JPEG 80, ~50–150 KB
```

- **Ticket**: the flow's `evidence.ticket`, else `FAST_EVIDENCE_TICKET`, else the upper-case
  keys in the git branch of `project_root` (`feature/PRJ-12_PRJ-13-x` → `PRJ-12 PRJ-13`), else
  the branch name. A `project_root` outside git with no ticket given files nothing.
- **Project**: `evidence.project`, else `FAST_EVIDENCE_PROJECT`, else the main checkout's folder
  name, so a `git worktree` of the base branch files into the ticket's folder. A plain copy
  (`cp`, `git archive`) of the base branch needs `FAST_EVIDENCE_TICKET` and
  `FAST_EVIDENCE_PROJECT`.
- **Outcome step**: the action with `"outcome": true`, else the last action that is not a
  `screenshot`, `dump`, `motion` or `pause`. A before run is usually the after flow on the base
  build: it fails at the `wait` for the fixed state, and that screen is the bug. A failure
  earlier (launch, a tap on the way) is a broken flow and is not filed. A before FAIL files the
  flow's screenshots it reached plus `failure.png`.
- **Case**: `evidence.case`, else `<task>-<device>`, so a matrix files one case per variant and
  device. A re-run of a case replaces its images and clip and keeps the cell's text and place in
  the sheet; a cell deleted from `cells` and listed in `"removed"` is not added back.
- **Text**: `evidence.label` (default `<task> · <device>`, `· before` added for before cells),
  `evidence.caption` (default: the flow's `expect`), `evidence.before_caption`.
  `evidence.shots` picks and orders the shots; `evidence.sheet` names another sheet
  (`<sheet>.jpg`, spec `source/sheet-<sheet>.json`).

```json
"evidence": {"label": "G2 · Off", "caption": "reopen: false → no paywall",
             "before_caption": "paywall on every reopen", "shots": ["main"]}
```

- The PASS/FAIL line ends with `evidence=file://…` when the run was filed, or
  `evidence_error=…` when filing failed; the run's own result never changes.
- A filed PASS run in the default root (`build/fast-ui`, `build/fast-device`) keeps only
  `metrics.json`, its `video` pointing at the filed clip, so `fastUIReport`, `fastUIReview` and
  `fastEvidenceSheet` `task` cells still work. A root set with `--artifacts-root`,
  `FAST_UI_ROOT` or `FAST_DEVICE_ROOT` is copied from, never emptied.
- `FAST_EVIDENCE=0` or `"evidence": false` in the flow files nothing (throwaway runs).
- **Flows**: keep a ticket's flows in `source/flows/` of its folder.
  `fastEvidenceSheet --folder <project_root>` prints `FOLDER path=… flows=…` and creates that
  folder; outside git it fails and says to set `FAST_EVIDENCE_TICKET`. A flow there is outside
  the repo, so it names `app.project_root` (and `derived_data`, `app_path`) as absolute paths.
- The folder is the user's to attach and delete after the merge; the tool never uploads.

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
  spec file. A filed PASS run keeps only `metrics.json`; its `task` cells take the filed shot.
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

## Step-by-step requests (`do`)

`fastSimulatorTest do '<action or array>' [--simulator <udid>] [--app <bundle id>] [--out <dir>]
[--prompt-labels <labels>]` runs actions on what the Simulator shows now (no build, install,
launch or preferences) and prints `PASS <type>|actions=N [screenshot=…] [ui=…] labels=[…]` or
`FAIL … step=K type=… error=… labels=[…]`; `fastDeviceTest do` prints the same on the iPhone.

- An array stops at its first failure. `expect` (a selector object) and `expect_timeout`
  (default 2 s) on any action wait for the screen the step leads to, so a wrong guess stops
  there; an `optional` tap that found nothing skips its `expect`.
- In an array, a gesture whose label, `label_contains` or `id` reads like Delete, Remove, Erase,
  Reset, Pay, Buy, Purchase, Subscribe, Send, Sign Out, Log Out, Unpair, Forget, Xóa, Gỡ, Đăng
  xuất, Thanh toán, Mua, Gửi or Đặt lại (`deleteAccountButton` and `btn_delete` count) refuses the
  whole request before anything runs, unless it carries `"confirm": true`. It is a word list,
  not an understanding of the app: add words with `FAST_GUARD_WORDS="archive, block"`;
  `tap_point`, `drag` and `swipe` are never checked; one-action requests and flows are not checked.
- A request that is not JSON, not objects, or has a non-object `expect` is refused the same way,
  on stderr. If every step ran but the closing tree read failed, the line says
  `PASS … read=failed (…)`: do not send the steps again.
- `FAST_SIMULATOR` / `FAST_UI_APP` are the defaults for `--simulator` / `--app`, `FAST_DEVICE` for
  `--device` on every `fastDeviceTest` command. `--app` also names the bundle id the screen map
  records the steps under.

## Screen map (`fastUIMap`)

Every `fastSimulatorTest` flow and every `fastDeviceTest` flow or `do` adds what it read anyway
to a map of the app: which screens it saw, the selectable nodes on each (label, id, element
type per engine), and which step led from one screen to the next. Nothing extra is read or
tapped. The map lives in `~/Library/Caches/fast-ui-map/<bundle id>.json`, one per app, shared
by Simulator and iPhone runs.

```bash
fastUIMap                                    # every app with a map, size and file
fastUIMap com.apple.Preferences              # one line per screen and the steps from launch
fastUIMap com.apple.Preferences --find about # selector, types and steps for a label or id
fastUIMap com.apple.Preferences --screen s4  # every node of one screen
fastUIMap com.apple.Preferences --path-to about  # a guarded request to that screen, for `do`
fastUIMap clear com.apple.Preferences        # or: fastUIMap clear --all
```

- `--path-to <text>` prints `PATH … steps=N [select=…]` and one JSON array: the recorded steps
  from an entry screen (launch the app first) to the screen titled with the text (else the one
  showing it), each with an `expect` that only the next screen shows. Paste it into `do`: one
  request, one stop at the first surprise.
- A screen is known by its navigation title and its controls; scrolling stays on one screen.
- Only steps that succeeded become paths; a selector that timed out never does. A step with no
  tree read before it (a `tap_point` right after a `swipe`) starts from an unknown screen.
- A flow's last step has no destination unless an action after it read the tree; a `do` always
  ends with a read.
- Screens not seen for 60 days are dropped, at most 400 per app. A map is a hint from earlier
  runs: the flow still verifies, and deleting the folder is always safe.
- Labels can include user data (names, emails). Long text is not kept, but never copy a map
  into a repo, PR or ticket.
- `FAST_UI_MAP=0` turns recording off; a map that cannot be saved prints
  `map=not-saved (<reason>)` and never changes PASS/FAIL.

## Environment variables

- `FAST_UI_ROOT`: artifacts root for `fastSimulatorTest` (runs, screenshots, videos).
- `FAST_UI_SESSION`: default `--session` for both engines.
- `FAST_VIDEO_LONG_SIDE` / `FAST_VIDEO_KBPS`: review-quality re-encode of recordings
  (960 / 800); `FAST_VIDEO_LONG_SIDE=0` keeps the original clip.
- `FAST_UI_KEEP_DAYS`: prune age for that root; `0` disables pruning.
- `FAST_DEVICE_ROOT`: artifacts root for `fastDeviceTest`.
- `FAST_EVIDENCE`: `0` files no evidence. `FAST_EVIDENCE_DIR`: where evidence folders go
  (`~/Desktop`). `FAST_EVIDENCE_TICKET` / `FAST_EVIDENCE_PROJECT`: name the folder when the
  branch or checkout cannot (a copy of the base branch).
- `FAST_DEVICE_PROFILE`: provisioning profile UUID or path for the device runner.
- `FAST_DEVICE_DEVELOPER_DIR`: Xcode to build/run the device runner with.
- `FAST_DEVICE_PORT` / `FAST_DEVICE_IDLE`: runner port (8765) and idle timeout in seconds (300).
- `FAST_GUARD_WORDS` / `FAST_GUARD_SINGLE=1`: more destructive words; check one-action `do`
  requests too (the `ui-explorer` subagent sets it).
- `FAST_DEVICE_CACHE`: runner cache root.
- `FAST_DEVICE` / `FAST_SIMULATOR` / `FAST_UI_APP`: defaults for `--device`, `--simulator` and
  `--app` (step-by-step `do`).
- `FAST_UI_MAP=0`: no screen map recording; `FAST_UI_MAP_DIR`: map folder
  (`~/Library/Caches/fast-ui-map`).

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
