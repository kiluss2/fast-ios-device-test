# fastDeviceTest — the same flows on a physical iPhone

Read this before running on a device. The flow format, selectors and timing rules are the
ones in [../SKILL.md](../SKILL.md); only what differs on a device is listed here.

New iPhone, or a run that fails before any step: `fastDeviceTest check --device <udid|name>`
lists pairing, connection, Developer Mode, the Xcode and signing profile it will use, and the
fix for each gap ([../../../SETUP.md](../../../SETUP.md) has the whole setup).

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
  act on the current app. Permission alerts are looked up on SpringBoard (XCUITest reports
  them as `Alert` on iOS 26, where AXe on the Simulator says `Sheet`); `screenshot` taps a
  declared one away first, as on the Simulator.
- Supported actions: `wait`, `wait_absent`, `assert_absent`, `tap_label`,
  `tap_first`, `tap_nearest`, `tap_point`, `long_press`, `double_tap`, `drag`, `edge_swipe`,
  `swipe` (points or `direction`), `scroll_to`, `type_text`, `press_key`, `key_combo`,
  `clear_text`, `press_button`, `set_slider`, `set_switch`, `assert_value`, `assert_count`, `tap_alert`,
  `dismiss_keyboard`, `pull_to_refresh`, `pause`, `screenshot`, `dump`, `activate`, `launch` (optional
  `launch_arguments`), `terminate`, `open_url`, and the iPhone-only `two_finger_tap`, `two_finger_drag`, `pinch`, `rotate`,
  `orientation`, `set_picker` (see [usage.md](usage.md)). `motion` and `shake` are
  simulator-only; `press_button` `lock` is refused inside a flow (the next steps would wait
  for the passcode): lock at the end with `fastDeviceTest lock`.
- **Editing a Home Screen widget:** go home with `terminate` on the app, then
  `long_press` the widget on `com.apple.springboard` (the widget node carries the app's
  name as `id`), then tap `id`
  `com.apple.springboardhome.application-shortcut-item.configure-widget` (`Edit Widget`).
  The sheet that opens is not in SpringBoard's tree: drive it with
  `"app": "com.apple.WorkflowUI.WidgetConfigurationExtension"`. Close it with a `tap_point`
  above the sheet on SpringBoard. Checked on an iPhone 11, iOS 26.7.1.
- Only on-screen nodes match: hidden tabs stay in the SwiftUI tree, and text in a
  list row scrolled out of view reports a made-up frame near the top of the list,
  so a node must also sit inside its own cell. Use `scroll_to` to bring a row in.
- `launch_arguments` stick to the app object: a later `launch` without them reuses
  the previous ones. Pass `"launch_arguments": []` to launch clean.
- `tap_alert` looks in SpringBoard's alerts (permission prompts) and then the app's own;
  `"system": true` only in SpringBoard's. iOS 27 reports an app's UIAlertController as a `Sheet` (iOS 26:
  `Alert`); both are found, by `tap_alert` and by the prompt rules. A dialog an app draws itself is
  neither (Safari's "cannot open the page": `tap_label` its `OK`). An XCTest failure inside a step (a gesture XCTest cannot
  place) fails that step with `XCTest: …` instead of ending the persistent runner.
- Selectors take `id` (accessibilityIdentifier), `value` and `enabled` as on the simulator; `dump`
  nodes carry `id` and `value` when set, and `"enabled": false` on a disabled node. `tap_label` falls back to the one node of any
  type with that label when `element_type` is not given (result `fallback_type`).
- **Screens that never go idle** (a GIF, a ticking clock) make XCUITest wait ~2.5 s after
  every tap. `fastDeviceTest do` runs with that wait off by default (`--idle-wait` turns it
  on); a flow switches it off with `"setup": {"wait_for_idle": false}` (private XCTest API,
  found at runtime). The runner then settles on its own: a tap by selector waits until two snapshots
  agree on the target's frame (up to 1.5 s, ~0.5 s per tap), `launch` / `activate` wait
  until the app is frontmost and its layout stops moving (up to 2 s; changing text such as a
  clock does not count), and a `do` reads the screen it ends on once the layout stops moving
  (up to 1.5 s). The PASS/FAIL line says `wait_for_idle=unsupported` when this Xcode
  has no such API and the wait stayed on.
- **Prompts are per request.** A `do` taps no permission prompt unless it passes
  `--prompt-labels` (`"Allow, Don’t Allow"`, or a JSON array with rules as in
  [usage.md](usage.md)); labels a flow declared do not carry over to later requests. Every
  prompt the runner answered is listed as `dismissed=["<title> → <button>"]`. When the prompt
  is what you are testing, tap it in its own step (`tap_label` with
  `"app": "com.apple.springboard"`) and leave its labels out.
- `type_text` goes through the iPhone's current keyboard: a Vietnamese Telex keyboard turns
  `wifi` into other letters. Type digits, or switch the keyboard by hand; the runner never
  changes the phone's keyboards.
- **Screen map:** the runner returns what it read and the steps that succeeded with every
  response, and the launcher adds them to `fastUIMap` (see [usage.md](usage.md)). A `do` that
  only taps a point starts from the screen the previous `do` ended on (within 2 minutes). A
  runner already started with `serve` before this feature records nothing until
  `fastDeviceTest stop`.
- `--device` (or `FAST_DEVICE`) accepts a UDID, the full name, or a unique part of the name
  (`Blue`); a part that matches two iPhones is refused. An iPhone named by its UDID or full name
  whose `serve` runner answers is taken from the runner's state file, without `xcrun devicectl
  list devices` (0.15–1 s); a part of a name always asks devicectl.

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
  whole session. `--record` runs one-shot; while `serve` holds the iPhone it is refused.
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

A flow and every `do` start the runner if it is not up (`fastDeviceTest serve --device
<udid|name>` does it alone, ~5 s) and reuse it; it answers on the CoreDevice tunnel (port
8765) and idles out after 5 min (`FAST_DEVICE_IDLE`). While it is up, a flow takes ~4–6 s
instead of ~14–20 s, and single actions ~1 s; `--one-shot` (and `--record`) run a flow
through one xcodebuild test instead:

```bash
fastDeviceTest do '{"type":"tap_label","label":"Lyrics","element_type":"RadioButton"}' --device <d>
fastDeviceTest do '{"type":"screenshot","name":"now"}' --device <d> --out <dir>
fastDeviceTest do '{"type":"launch"}' --app com.apple.Preferences --device <d>
fastDeviceTest status|stop --device <d>
fastDeviceTest lock --device <d>     # end of the work: stop, then lock the screen
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
run's artifacts folder. Always `stop` when done (`lock` at the end of the work, which also
locks the screen): the runner holds the device, and another
agent's xcodebuild run on the same iPhone will collide with it. A device that already has a
runner is refused ("is busy"); pick another iPhone, never `stop` a runner you did not start.
A runner this Mac started that no longer answers (after an idle-out, while xcodebuild
collects diagnostics) is ended and replaced; a locked iPhone is reported in seconds.

- Artifacts: `<project_root>/build/fast-device/<task>/<run>/` with screenshots,
  `failure.png`, `failure-ui.txt`, `metrics.json`, the `.xcresult` and, with `--record`,
  the clip (`run.mp4` on FAIL; `<task>/final[-<session>].mp4` on PASS).
- Device must be unlocked, in Developer Mode, trusting the Mac; enable
  Settings › Developer › Enable UI Automation if iOS asks. One run per device at a time.
