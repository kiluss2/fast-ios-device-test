# Changelog

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
