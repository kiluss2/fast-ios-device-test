# fastDeviceTest — the same flows on a physical iPhone

Read this before running on a device. The flow format, selectors and timing rules are the
ones in [../SKILL.md](../SKILL.md); only what differs on a device is listed here.

`fastDeviceTest <flow.json> [--device <udid|name>] [--install <App.app>]` builds a
generic XCUITest runner once (cached in `~/Library/Caches/fast-device-test`),
re-signs it with an existing development profile, and runs the flow on the device.

- **Signing needs no Apple ID login.** The launcher picks a non-expired
  development profile that lists the device and matches a non-revoked keychain
  identity, preferring a wildcard profile, then the fewest capabilities. It
  never takes the bundle id of an app installed on the iPhone (or a child id
  such as an extension); override with `FAST_DEVICE_PROFILE=<uuid>`.
- **Xcode is chosen per device:** the oldest installed Xcode whose iphoneos SDK
  covers the device's iOS (`FAST_DEVICE_DEVELOPER_DIR` overrides).
- **The app is not built or fresh-installed.** It drives what is installed;
  `--install` pushes an iphoneos `.app` first.
- **`setup.preferences` become launch arguments** (typed plist fragments, `$json_data`
  as `<hex>`): the argument domain lasts one launch and is never written, so no
  cleanup is needed. Anything the app itself writes while running does persist.
- **Other apps:** add `"app": "<bundle id>"` to an action to drive Settings,
  Shortcuts or SpringBoard from that step on; `activate` / `launch` / `terminate`
  act on the current app. Permission alerts are looked up on SpringBoard.
- Supported actions: `wait`, `wait_absent`, `assert_absent`, `tap_label`,
  `tap_first`, `tap_nearest`, `tap_point`, `swipe` (points, `direction`, or inside an
  element), `scroll_to`, `type_text`, `pause`, `screenshot`, `dump`, `activate`, `launch`
  (optional `launch_arguments`), `terminate`, `open_url`, and the gesture, keyboard,
  control, assertion and device-state actions in the table in
  [usage.md](usage.md#gestures-keyboard-controls-assertions-and-device-state).
  Only the iPhone has multi-touch and real hardware: `pinch`, `rotate_gesture`,
  `two_finger_tap`, `set_orientation`, volume/action/camera buttons, and SwiftUI
  drag-and-drop (`drag` between `.draggable` and `.dropDestination`). Simulator-only:
  `motion`, `push_notification`, `status_bar`, permission `grant`/`revoke` (a device can
  only `reset`, which terminates the app), arrow keys, `set_picker` by `index`.
- `set_slider` and `set_picker` use XCUITest's own adjustments (`adjust(toNormalizedSliderPosition:)`,
  `adjust(toPickerWheelValue:)`); `tap_alert` looks on SpringBoard first, then the app.

Checked on an iPhone 15 Plus with iOS 26.6.1 (AssistiveTouch on):

- **Clipboard:** iOS keeps the pasteboard from a background process, so `set_clipboard` /
  `assert_clipboard` bring the runner to the front for a moment and then reactivate the app
  under test; the status bar briefly shows a "◀ FastDeviceUITests" back link.
- **`open_url`** goes to the system handler (`https://` opens Safari, `App-prefs:` Settings),
  like `simctl openurl`.
- **Notification Center or banners on screen** make XCTest wait ~15 s, up to 4 times, before
  each event, which can outlast the ~48 s request timeout. Close Notification Center with
  `press_button` `home`, not `edge_swipe` `bottom`.
- **A Home Screen context menu** (after `long_press` on an icon) keeps SpringBoard from going
  idle: run those steps with `--no-idle-wait` / `"wait_for_idle": false`.
- **`two_finger_tap` / `pinch` on the whole app** can fail with "Unable to compute coordinates
  for gesture" when a full-screen overlay (AssistiveTouch) covers it; target a labelled element
  or an `id`. The XCTest failure is a normal step FAIL and the runner keeps serving.
- **`pinch` below 1 (zoom out)** did not zoom Maps out on that phone (it pulled the sheet or
  panned); zoom in did. Check it on a phone without AssistiveTouch before relying on it.
- **`set_location`** also moves the automatic time zone, and `clear: true` does not move it
  back: toggle Settings › General › Date & Time › Set Automatically afterwards.
- **`dismiss_keyboard`** cannot close the keyboard of an empty search field (iOS disables its
  Search key); pass `button`.
- `type_text` with a selector taps the field near its trailing edge but left of any small
  button inside it (a search bar's Dictate button opens the Siri sheet). A search field is
  `SearchField`; `element_type: "TextField"` matches it too.
- `fastDeviceTest do` prints PASS/FAIL and labels; per-step results (`count`, `toggled`,
  `value`) are in the flow's `metrics.json` / `--json`.
- Only on-screen nodes match: hidden tabs stay in the SwiftUI tree, and text in a
  list row scrolled out of view reports a made-up frame near the top of the list,
  so a node must also sit inside its own cell. Use `scroll_to` to bring a row in.
- `launch_arguments` stick to the app object: a later `launch` without them reuses
  the previous ones. Pass `"launch_arguments": []` to launch clean.
- Selectors take `id` (accessibilityIdentifier) and `value` as on the simulator; `dump`
  nodes carry `id` and `value` when set. `tap_label` falls back to the one node of any
  type with that label when `element_type` is not given (result `fallback_type`).
- **Screens that never go idle** (a GIF, a ticking clock) make XCUITest wait ~2.5 s after
  every tap. `"setup": {"wait_for_idle": false}` in a flow, or `--no-idle-wait` on
  `fastDeviceTest do`, switches that wait off for the run (private XCTest API, found at
  runtime; the report shows `wait_for_idle`); the flow's own `wait`s keep it correct.
- `--device` accepts a UDID, the full name, or a unique part of the name (`Blue`); a part
  that matches two iPhones is refused.

### Screen recording (`--record`)

The iPhone screen **can** be recorded: XCTest records every UI test on a device by itself
(`PreferredScreenCaptureFormat = screenRecording` is the runner's default), but its default
attachment lifetime (`deleteOnSuccess`) throws the clip away when the test passes. `--record`
sets `SystemAttachmentLifetime = keepAlways` in that run's `.xctestrun`, and the clip comes
back with the other attachments. No Control Center, no QuickTime, no recording dot on screen,
nothing written to the iPhone's Photos.

```bash
fastDeviceTest flow.json --device <d> --record --session pr-12
# PASS … video=file://…/<task>/final-pr-12.mp4 artifacts=file://…
```

- One-shot runs only: the persistent runner is one long test, so its clip would span the
  whole session. `--record` with `--serve`, or while `serve` holds the iPhone, is refused.
  A recorded flow costs one-shot time (~20 s) plus ~2 s to re-encode.
- The clip is re-encoded at review quality (H.264 MP4, long side 960 px, ~800 kbit/s:
  17 s of a 1290×2796 recording goes from 20 MB to ~1.4 MB). A PASS keeps
  `<task>/final[-<session>].mp4` (one per task, older clips deleted), a FAIL keeps `run.mp4`
  in its run folder. The full-resolution original stays in `result.xcresult`.
- Without `--record` nothing is kept, a FAIL included: the runner reports a failed flow
  in its result file, so for XCTest the test passed.
- Video inside the app keeps playing while XCTest records (checked on iOS 26.6 with an
  `AVPlayer` guide video). Recording over the USB cable with QuickTime / CoreMediaIO
  is the method that froze in-app video; do not use it.
- The run has no per-step timestamps, so `fastUIReview` samples device clips on its
  interval grid.

### Persistent runner — use it for anything beyond one run

`fastDeviceTest serve --device <udid|name>` starts the runner once (~5 s) and keeps
it answering on the CoreDevice tunnel (port 8765, idles out after 30 min). While
it is up, a flow takes ~4–6 s instead of ~14 s, and single actions ~1 s:

```bash
fastDeviceTest do '{"type":"tap_label","label":"Lyrics","element_type":"RadioButton"}' --device <d>
fastDeviceTest do '{"type":"screenshot","name":"now"}' --device <d> --out <dir>
fastDeviceTest do '{"type":"launch"}' --app com.apple.Preferences --device <d>
fastDeviceTest status|stop --device <d>
```

Each `do` prints PASS/FAIL plus the visible labels after the action, so an agent
can look, decide, and act step by step.

Once the steps are known, send them as one JSON array: the runner executes them in
one request, stops at the first failure (`FAIL actions=N step=K type=…`) and snapshots
the hierarchy once at the end, instead of a process, a round trip and a snapshot per
step:

```bash
fastDeviceTest do '[{"type":"terminate"},{"type":"launch"},
  {"type":"tap_label","label":"Wi-Fi, Office"},
  {"type":"tap_nearest","target":{"label":"More Info"},"anchor":{"label":"Office","element_type":"StaticText"}},
  {"type":"scroll_to","label":"Configure Proxy"},{"type":"screenshot","name":"wifi"}]' \
  --app com.apple.Preferences --device <d> --out <dir>
```

`dump` writes the on-screen nodes with type and frame to `<out>/<name>-ui.json`
(`ui=` in the PASS line); the frame keys are the selector keys, so a dump feeds
`frame` bounds, `tap_point` and `tap_nearest` directly. In a flow they land in the
run's artifacts folder. `--serve` on a flow run starts the server
if needed. Always `stop` when done: the runner holds the device, and another
agent's xcodebuild run on the same iPhone will collide with it. `serve` refuses a
device that already has a runner ("is busy"); pick another iPhone, never `stop`
a runner you did not start.

- Artifacts: `<project_root>/build/fast-device/<task>/<run>/` with screenshots,
  `failure.png`, `failure-ui.txt`, `metrics.json`, the `.xcresult` and, with `--record`,
  the clip (`run.mp4` on FAIL; `<task>/final[-<session>].mp4` on PASS).
- Device must be unlocked, in Developer Mode, trusting the Mac; enable
  Settings › Developer › Enable UI Automation if iOS asks. One run per device at a time.
