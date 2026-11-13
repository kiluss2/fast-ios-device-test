# Changelog

## 1.17.0 — 2026-11-13

- iPhone: runner builds unused for 14 days are deleted when a runner starts
  (`FAST_DEVICE_RUNNER_KEEP_DAYS`, `0` keeps them). One is built per Xcode × signing profile ×
  runner source (85–140 MB) and none is used again once one of those changes: 15 of them held
  1.4 GB on one Mac. The one starting and any a process still runs from are never deleted; for a
  build made before this release, its last serve/run xctestrun copy tells when it was last used.
- Both engines: the default `do` folder (`~/Library/Caches/fast-ui/do`) drops shots and dumps
  untouched for `FAST_UI_KEEP_DAYS` (3) before each request; a folder given with `--out` is kept.

## 1.16.0 — 2026-11-13

- iPhone: `fastDeviceTest build --project <P> --scheme <S> [--configuration <C>] [--bundle-id <id>]
  [--install --device <d>]` builds the app for any iPhone into the Simulator flows' `derived_data`
  (default `build/fast-ui-dd` beside the project) under their build lock, so the two builds run in
  turn and a project keeps one folder instead of a second one for device builds (one such
  folder held 6.4 GB). `generic/platform=iOS` leaves a runner on the iPhone alone.
- Tests: the evidence test's Simulator run no longer leaves a `SIM.lock` lease in
  `~/Library/Caches/fast-ui/locks`.

## 1.15.0 — 2026-11-13

- Simulator: `tap_label` no longer reads the whole screen again before every tap (about 1 s each
  since 1.13.0). A screen still for 1 s since the last action is tapped at once; otherwise one read
  at the control's point (`axe describe-ui --point`, 0.40 s against 0.89 s for a whole read on an
  iPhone 17 simulator) confirms it stopped moving, and only another element there sends it back to
  the full re-read. A new `do` process never takes the screen for still. Measured per tap on an
  iPhone 17 simulator (iOS 27.0, Settings): right after a wait 2.10–2.31 → 1.68–1.74 s, on a screen
  still for 1.5 s 2.61–2.97 → 2.02–2.11 s.
- iPhone: taps are sent straight to XCTest's event synthesizer (`XCPointerEventPath` +
  `XCSynthesizedEventRecord`, looked up at runtime) instead of `XCUICoordinate.tap()`, which first
  resolves the app over the USB link. Measured from tap to the next screen's label: Clock's Liquid
  Glass tab bar 1.48 → 1.24 s (20/20 taps landed both ways) and Settings' glass back button
  2.05 → 1.82 s on iOS 27.2.1; back 1.48 → 1.24 s and the status bar 1.09 → 0.79 s on iOS 18.5. A
  tap that pushes a screen gains nothing. `setup.touch: "xcuitest"` keeps XCUITest's tap; a sideways
  iPhone or an Xcode without the private API uses it by itself.

## 1.14.1 — 2026-11-12

- Simulator: `setup.location` ("lat,lon"), or `FAST_UI_LOCATION`, grants the app location and sets
  the Simulator's place after the install and before launch, so a fresh install no longer drops a
  grant made before the run (the app's own location prompt or sheet then never shows).
- Docs: `open_url` with an https link reaches the app only through Associated Domains; otherwise
  Safari asks "Open this page in “<App>”?". Host steps: an undo that restores the network waits for
  the server, and a Wi-Fi-off step needs a check that the Mac is really offline (an iPhone on USB
  keeps it online).

## 1.14.0 — 2026-11-12

- Simulator flows: a `host` step runs a command on the Mac (argv, no shell, in the flow's `cwd`)
  and keeps fields of its JSON output as `${name}` for later steps; its `undo` runs when the flow
  ends, last first, before teardown, and is journaled so a killed run's repair runs it. An `undo`
  action runs a command now. Flow keys `vars` and `cwd`; built-ins `${udid}`, `${bundle_id}`,
  `${flow_dir}`.
- `wait` with `"save": {"var": …}` keeps the first match's label; selector key `label_matches`
  (regular expression).
- `fastSimulatorTest do --vars '<json>'`; its line ends with ` vars=` and ` undo_pending=`.
- `fastDeviceTest` refuses `host`, `undo` and `save` with "host steps run on the simulator only", and
  `${…}` and `label_matches`, which its runner does not support.
- `fastSimulatorTest do` repairs a killed run's journal first (its undos included), as a flow run does.

## 1.13.0 — 2026-11-11

- Simulator: on a kept install (`fresh_install: false`) a seeded `setup.preferences` key that
  already held a value gets that value back after the run (and after a killed run's repair)
  instead of being deleted; a key the app did not have is still removed.
- `tap_label` re-reads its control until the frame stops moving (up to 1.5 s) before tapping, so
  a tab on a sheet still sliding in is hit where it lands, not where it was listed.
- `fastSimulatorTest do` and `fastDeviceTest do` write pictures and dumps to `--out`, by default
  `~/Library/Caches/fast-ui/do`, no longer the shell's folder (it put screenshots in the app repo).

## 1.12.0 — 2026-11-03

Ported from DungVC-Dev/fast-ios-device-test 2.1.1 (its 1.11.0 work), adapted to this line and
checked on an iPhone 17 simulator (iOS 27, Xcode 27.0).

### Checks and controls on both engines

- `assert_value` (`equals` / `not_equals` / `contains` / `matches` on a value or `"attribute":
  "label"`, polled), `assert_count` (`count` / `min` / `max`, one per box) and the selector key
  `enabled`; a dump marks a disabled node `"enabled": false`.
- `set_switch` `on`: touches only when the value differs and waits for the new value. On the
  Simulator the touch is held 0.3 s (the iOS 27 switch ignores a shorter one) and sent once more
  when the switch has not moved after 2 s: on a loaded Mac (load 40+) 1 of 8 touches was lost.
- `tap_alert` `button` (`system`, `optional`), `dismiss_keyboard` (`button`, else Return),
  `pull_to_refresh` (the screen or a selector's element).
- Fixed against the source: `assert_value` read a row's container, which shares the control's id
  but has no value (Settings › Display & Text Size: Group, CheckBox and Button all carry
  `BUTTON_SHAPES`); it now reads the first match that has the attribute and names its type.
- Simulator prompts: a bare label in `optional_permission_labels` no longer taps a same-named
  button on the app's own screen (checked: Maps' in-app `Not Now` stayed); prompts inside a
  system alert are tapped as before. One wait taps at most 4 prompts.
- `tap_alert` joins the destructive-word guard of multi-step `do` (`"button": "Delete"`).
- iPhone: an XCTest failure inside a step fails that step instead of ending the runner.
- Checked on an iPhone 15 Pro Max, iOS 27.2: `set_switch` (on, already on, off), `assert_value` by an
  id three nodes share, `assert_count`, `enabled: false`, `dismiss_keyboard` (Return and a named
  `close`, 3/3 each), `pull_to_refresh` (Safari reported "Refreshing content"), `tap_alert` on
  Settings' Location Services confirmation (`Cancel`; `system: true` skipped it as the app's own).
- Fixed on the iPhone while checking: iOS 27 reports a UIAlertController as a sheet, so
  `tap_alert` and the prompt rules (`optional_permission_labels`, `--prompt-labels`) now look for
  alerts and sheets in one query, and a dump names that type `Sheet`, as AXe does. Before, the
  prompt rules could not see an app's own alert on iOS 27 (SpringBoard's permission prompts on iOS
  27 were not available to check). `dismiss_keyboard` waits up to 2 s for the
  keyboard to slide away instead of failing a tap that worked.
- `dismiss_keyboard` on the Simulator could not be shown closing a keyboard: Device Hub with the
  Mac keyboard shows none.

### One run per simulator, and nothing left behind

- A flow run locks its simulator: a second run prints `BUSY` and exits 3, `fastSimulatorTest do`
  fails with `simulator busy` (`FAST_UI_LOCK_WAIT` waits instead).
- The lock file journals what the run changed; at the end, Ctrl-C, SIGTERM and SIGHUP included
  (`INTERRUPTED`, exit 130), the engine restores appearance and text size, terminates the app and
  deletes seeded keys (`--keep-state` keeps app and keys). After a SIGKILL the next run repairs
  from the journal (`WARN dirty=1`). Checked live: dark mode back to light, the seeded key gone.
- `"fresh_install": false` relaunches the app (`--terminate-running-process`); `"relaunch":
  false` keeps the old behaviour.

### SwiftLint skipped in test builds, and build failures that say why

- A no-op `swiftlint` (PATH shim plus an exported shell function) in `fastSimulatorTest` builds
  and `fastDeviceTest`'s build of an app's tests; `lint=shimmed|not-shimmable|on` on stderr;
  `"skip_lint": false` / `FAST_UI_SKIP_LINT=0` lints.
- A failed build prints the failing phase and its first `error:` line instead of the last log
  lines, and keeps the log in `<derived_data>/.fastui-build.log`. Checked with xcodebuild on a
  probe app with a `force_cast` error: lint on → `build failed in SwiftLint (run-script phase
  'SwiftLint') first error: …App.swift:11:22: error: Force Cast Violation…`; default → `READY`.

## 1.11.0 — 2026-11-02

### `capture`: the picture and the tree of the same instant

- New Simulator step `{"type": "capture", "name": "<name>"}` writes `<name>.png` and
  `<name>-ui.json`, the screenshot taken while the tree is read. A `dump` then a `screenshot` are
  0.5–1.5 s apart, enough for a 2 s toast to be in the picture and gone from the tree (checked:
  CarWidgets "Trip saved!" toast, iPhone 17 simulator, iOS 27).

## 1.10.1 — 2026-11-02

### Taps land on Liquid Glass controls

- A tap is now a short hold (`axe touch`, 0.08 s) instead of `axe tap`. On iOS 26+ the tab bar and
  glass buttons ignored the instant press: AXe reported success and the screen did not change
  (checked: iPhone 17 simulator, iOS 27, the Convoy tab and Start Drive of CarWidgets; a 0.1 s hold
  switched the tab every time). The Device Hub settle delay still follows the tap.

## 1.10.0 — 2026-10-31

### Locking the iPhone and shutting simulators down at the end

- `fastDeviceTest lock --device <d>`: the runner's last request presses the lock button, then the
  iPhone is freed as `stop` frees it (`LOCKED device=<name>`). For a test phone kept on Auto-Lock
  "Never": the agent locks it once the work is done. XCUIDevice has no public lock button, so the
  runner calls the private `pressLockButton` (as WebDriverAgent does), looked up at runtime; an
  Xcode without it refuses with the reason (checked: iPhone 15 Pro Max, iOS 27, the lock state
  read `passcodeRequired: true` right after). A lock that fails still frees the runner.
- `press_button` `lock` inside a flow stays refused on an iPhone (the next steps would wait for
  the passcode); its message now points at `fastDeviceTest lock`.
- `fastSimulatorTest shutdown` shuts down every booted simulator, `--simulator <udid>` only that
  one, ending a recorder left running first (`SHUTDOWN simulators=…`).

## 1.9.0 — 2026-10-30

### Screenshots wait until the screen stops moving

- `screenshot` on the Simulator and on an iPhone now shoots until two shots 0.25 s apart differ by
  at most 0.5 % of the screen below the status bar (at most 3 s), and keeps the last one. An
  evidence sheet once shipped a "Trip is very short" popup caught half-faded mid-slide because a
  fixed `pause` was too short. A screen that never settles is named in the PASS line as
  `unsettled=<name>`; `"stable": false` keeps the old single shot. Tunable per action with
  `stable_ratio` and `stable_timeout`.
- A Simulator screenshot now gives up after 30 s. `simctl io … screenshot` on a simulator that died
  mid-run used to hang the flow for good (one sat for 19 minutes); the flow now fails with the reason.

## 1.8.1 — 2026-10-30

### Restarting a stopped runner no longer fails

- `fastDeviceTest do` after `fastDeviceTest stop` could end in `shutil.SameFileError` on
  `serve-<udid>.xctestrun`: the runner picked its xctestrun with a glob that skipped the per-run
  `run-` copies but not the `serve-` copy left beside the build, so whichever came first was copied
  onto itself. The build's own file is now chosen in one place, ignoring both prefixes (checked:
  three `do` / `stop` cycles in a row on an iPhone 15 Pro Max with the `serve-` file present).

## 1.8.0 — 2026-10-30

### Two fingers dragging together

- New iPhone-only action `two_finger_drag`: two fingers side by side move up or down together
  (`dy`, `spread`, `duration`, `at`, on a selector or `element_type`, else the whole app). On Apple
  Maps, `{"type": "two_finger_drag", "dy": -0.25, "element_type": "Map"}` tilts the map into 3D
  (checked on an iPhone 15 Pro Max, iOS 27.2). XCUITest's public API has no such gesture, so the
  runner builds the touch events from the private `XCPointerEventPath` / `XCSynthesizedEventRecord`
  classes, looked up at runtime like the quiescence switch: an Xcode without them refuses the
  action with the reason and nothing else changes.
- Dragging up tilts the map, down flattens it again (both checked). Fingers that land on Maps'
  bottom sheet drag the sheet instead, so start above it: `"at": 0.3`.
- The Simulator refuses it like `pinch`; the screen map records it with `dy` and `spread`.

## 1.7.0 — 2026-10-29

### Evidence lands on the Desktop by itself

- Every PASS flow run on either engine is filed in `~/Desktop/<project> <ticket>/`
  (`FAST_EVIDENCE_DIR`): a captioned sheet `<ticket>.jpg`, the run's clip as `<case>.mp4`, and
  `source/` with each shot at 720 px (JPEG 80) and the `fastEvidenceSheet` spec, so a sheet can
  be reordered, recaptioned or given another shot and rendered again without loss. The ticket
  comes from the git branch (`evidence.ticket` / `FAST_EVIDENCE_TICKET` override it), the
  project from the main checkout, so a `git worktree` of the base branch files beside it.
- `--before` (or `"evidence": {"role": "before"}`) marks a run that shows the bug, e.g. the
  same flow on the base build. It is filed when it fails at or after its outcome step (an
  action with `"outcome": true`, else the last action that is not a screenshot, dump, motion or
  pause), so a flow that broke on the way is not shown as the bug. Before cells sit next to the
  case's after cells.
- A re-run of a case replaces its images and clip, keeps the cell's edited text and place, and
  does not bring back a cell listed in the spec's `"removed"`.
- A filed PASS run in the default root keeps only `metrics.json` (its `video` points at the
  filed clip); `fastUIReport`, `fastUIReview` and `task` cells keep working. Custom roots are
  never emptied. `fastDeviceTest` now prunes run folders after `FAST_UI_KEEP_DAYS` (3) days, as
  `fastSimulatorTest` did.
- `fastEvidenceSheet --folder <project root>` prints the ticket's evidence folder and creates
  its `source/flows/`: a ticket's flows live there, found by any later session and deleted with
  the folder after the merge, never under the pruned `build/` roots.
- Lines end with `evidence=file://…`, or `evidence_error=…` without changing PASS/FAIL.
  `FAST_EVIDENCE=0` or `"evidence": false` files nothing; a `project_root` outside git with no
  ticket given files nothing.

## 1.6.1 — 2026-10-27

### Setup an AI can follow

- `SETUP.md`: install, check, restart, a Simulator and an iPhone smoke test, every step with its
  expected output and a table of messages with their fixes.
- `fastDeviceTest check --device <name>` (and `install.sh --check --device <name>`): pairing,
  connection, Developer Mode, developer disk image, the Xcode and signing profile a run will use,
  whether the runner is built and serving, each gap with its fix. Lock state and the
  "Enable UI Automation" switch are not readable and are said so.
- GUIDE.md and README brought up to 1.6: `do`, `--path-to`, gestures, the iPhone checklist.

## 1.6.0 — 2026-10-27

- `fastUIMap <bundle id> --path-to <text>` prints the recorded way from an entry screen to a
  screen as one `do` request, each step with an `expect` that only the next screen shows (a
  title the screen before also lists, such as Settings' "General" row, is skipped). A screen
  visited before is reached in one request and one model turn.
- `fastSimulatorTest do` skips `simctl bootstatus` (0.3 s a step): a shut-down simulator fails
  the first step before it acts, and only then is it booted and the request run again.
- `fastDeviceTest do` skips its closing read when the last step already read the screen (a
  `wait`, a `dump`, an `expect`): with the idle wait off that read settled for up to 1.5 s.
- `expect` from `--path-to` names something only the next screen shows, never chrome (Back,
  chevron, Close, Done…) and text with digits last (a version differs on the next iPhone).
- iPhone flows start or reuse the persistent runner (14–20 s one-shot → 4–6 s); `--one-shot`
  and `--record` keep the old way. The runner idles out after 5 min instead of 30. A runner this
  Mac started that stopped answering is replaced instead of reported "busy", and a locked
  iPhone fails in seconds ("is locked: unlock it") instead of after 120 s, leaving no runner.
- `agents/ui-explorer.md` (experimental): a Haiku subagent for long step-by-step searches that
  returns one guarded request; `FAST_GUARD_SINGLE=1` applies the destructive-step refusal to
  one-action requests too. `install.sh` links it into `~/.claude/agents`.

## 1.5.0 — 2026-10-26

### Faster step-by-step runs

- `fastSimulatorTest do '<action or array>'` acts on what the Simulator shows now, like
  `fastDeviceTest do`: no build, install or launch, one PASS/FAIL line with the labels after the
  actions. Exploring the Simulator used to re-run a whole flow per step.
- `expect` on any action waits for the screen the step leads to, so one request can carry the
  predictable steps of a path and stop at the first surprise.
- A destructive-looking step (Delete, Pay, Send, Sign Out, Xóa, Đăng xuất…, ids such as
  `deleteAccountButton` included) inside a multi-action `do` is refused unless it says
  `"confirm": true`; `FAST_GUARD_WORDS` adds words. A request that is not valid JSON or has a
  non-object `expect` is refused before it runs.
- `fastDeviceTest do` resolves a serving iPhone from the runner's state file instead of
  `xcrun devicectl list devices` on every call, and runs with XCTest's idle wait off by default
  (`--idle-wait` turns it on). `FAST_DEVICE`, `FAST_SIMULATOR` and `FAST_UI_APP` are defaults.
- SKILL.md: step-by-step rules (no routine `dump`/`screenshot`/`pause`, guarded arrays).

## 1.4.0 — 2026-10-26

### More gestures

- `double_tap`, `drag` (with a `hold` on the start point for drag and drop and reorders),
  `edge_swipe` (back swipe, Control Center, Notification Center, home), `press_key` (`delete`,
  `return`, `tab`, `escape`, `space`), `press_button` (`home`; `lock` on the Simulator,
  `volume_up`/`volume_down` on the iPhone) and `set_slider`, on the Simulator and the iPhone.
- iPhone only, where AXe has no way: `pinch`, `rotate` (two fingers), `orientation` (a flow turns
  the phone back to portrait at its end) and `set_picker`; the Simulator names the reason.
- `open_url` on the iPhone; `shake` on the Simulator (no XCUITest API on a device).
- `key_combo` (a key held with `command`, `shift`, `option`, `control`), `clear_text` (deletes what
  a field holds), arrow keys for `press_key`, `press_button` `siri` (with the request as `text` on
  the iPhone), and the iPhone-only `two_finger_tap`, which zooms a map out where a pinch cannot.
- `press_button` `lock` is refused on an iPhone: a shared test phone would wait for its passcode.
- All of them join the screen map as steps.
- On the iPhone, `type_text`, `press_key`, `key_combo` and `clear_text` refuse to type with no
  focused field, and an unknown `element_type` is refused: both would have been UI Automation
  failures that end the runner. `clear_text` taps near the field's trailing edge and selects all
  before deleting, skips a caption with the field's label, and fails on the iPhone if text is
  left. A runner that dies mid-request is ended by the same pattern `stop` uses.
- Checked on an iOS 26.5 Simulator and an iPhone 11 on iOS 26.7.1, which showed what the first
  version got wrong:
  - Simulator edge swipes put the finger down before moving: a plain `axe swipe` or `axe drag`
    missed the back swipe and the home swipe (1 unlock in 3).
  - `edge_swipe` travels 80 % of the screen by default; an iPhone back swipe that stops at half
    the screen springs back.
  - `press_button` `home` on the iPhone brings SpringBoard forward; `XCUIDevice.press(.home)` did
    nothing on iOS 26.7.1.
  - The Simulator's `lock` is a 0.3 s press: AXe's default press locks but never wakes.
  - A runner that ends mid-request (a `set_picker` value the wheel does not have ends XCTest's
    test) is cleaned up and the FAIL line quotes XCTest's reason, instead of "server closed the
    connection"; the next request no longer finds the iPhone busy.
- `scroll_to` and `swipe` `direction` take the screen from the `Application` node: a scroll
  view's content taller than the screen sent swipes off screen (y = -277).

### Screen map

- Both engines now keep what their runs already read: the screens of each app, the nodes a
  flow can select on them (label, id, element type per engine) and the steps that led from one
  screen to the next, in `~/Library/Caches/fast-ui-map/<bundle id>.json`. No extra tree read,
  tap or crawl.
- `fastUIMap <bundle id>` lists the screens with the steps from launch; `--find <text>` prints a
  selector, its types and the steps to reach it; `--screen`, `clear <bundle id>|--all`. Agents
  look there before exploring with `dump`.
- Only steps that succeeded become paths. Screens unseen for 60 days drop out, at most 400 per
  app. `FAST_UI_MAP=0` turns it off; a map that cannot be saved prints `map=not-saved` and never
  changes PASS/FAIL. Maps hold on-screen labels and stay on the Mac.
- An optional `tap_label` that finds nothing reports `optional_missing` in its step result.

### Long press

- `long_press` holds for `duration` seconds (default 1) on a selector or on `x`/`y`, on the
  Simulator (`axe touch --down --up --delay`) and on the iPhone (XCUITest `press(forDuration:)`).
  It opens a context menu, such as a Home Screen widget's `Edit Widget`; `device.md` has the
  whole path into the widget's configuration sheet, checked on an iPhone 11 on iOS 26.7.1.

### Prompt rules

- `optional_permission_labels` takes rules beside bare labels:
  `{"title_contains": "track", "tap": "Ask App Not to Track"}` answers one kind of prompt,
  matched by its title, on the Simulator and on the iPhone. Bare labels answered every prompt
  with the first declared label it carried, so notifications and tracking (both `Allow`) could
  not get different answers, and a location prompt fell to `Don’t Allow` when its own button was
  not declared. A rule whose title matches owns the prompt: a missing button taps nothing.
- Device: each request starts with no prompt labels. A `do` used to keep the labels of the last
  flow the runner served and tapped those prompts unasked; it now taps only what
  `--prompt-labels` declares. PASS/FAIL lines list what was answered:
  `dismissed=["<title> → <button>"]`.

### Idle wait off, safely

- Device, with `wait_for_idle: false` / `--no-idle-wait`: a tap by selector (`tap_label`,
  `tap_first`, `tap_nearest`) waits until two snapshots agree on the target's frame (up to
  1.5 s), so it no longer lands on a button still sliding in; `launch` and `activate` wait until
  the app is frontmost and its layout stops moving (up to 2 s), so the next `screenshot`,
  `tap_point` or `swipe` no longer acts on the launch screen.
- `do --no-idle-wait` reads the screen it ends on only once the layout stops moving (up to
  1.5 s): its labels used to show two screens mid-push (checked on an iPhone 11, iOS 26.7.1).
- `do --no-idle-wait` reports `wait_for_idle=unsupported` when the private API is missing, as
  flows already did; it used to keep waiting without a word.
- The runner's pure logic (prompt rules, settling) is checked on the Mac by
  `RunnerLogicTests`, which compiles it with `swiftc`. The next device run rebuilds the runner by
  itself (its cache key covers the Swift sources); a runner already started with `serve` keeps
  the old code until `fastDeviceTest stop`.

### Fixes

- `screenshot` taps away a declared `optional_permission_labels` prompt before capturing, on the
  Simulator and on the iPhone (the device runner needs `--rebuild-runner` once). It read no tree,
  so a notification or location request raised after the flow's last `wait` (during a `pause`)
  ended up in the evidence image. Checked on iOS 26.5 Simulators and an iPhone 11 on iOS 26.7.1.

### Docs

- Only actions that read the tree (and now `screenshot`) dismiss prompts; `pause`, `tap_point` and
  `swipe` do not. iOS 26 labels for the tracking (`Ask App Not to Track`) and location prompts.

- The App Store review sheet is not in the accessibility tree; close it with `tap_point`.
- `open_url` delivers a link with no prompt; a link tapped in Safari raises "Open in …?", where the
  StoreKit `Cancel` label would drop it, so that flow declares `Open`.

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
