---
name: fast-ios-device-test
description: Use when asked to run, verify, re-test, or record an iOS screen or flow quickly on Simulator (/fastSimulatorTest) or a physical iPhone (/fastDeviceTest), including a matrix of several cases or several devices, or a system app such as Settings. Not for release, archive, or full-suite runs.
---

# Fast iOS Device Test

## Which tool

| Need | Use |
|---|---|
| Prove a behavior, rerun it, run a matrix, record video, report | this skill: `fastSimulatorTest <flow.json>` / `fastDeviceTest <flow.json>` |
| Explore an unknown screen, read the app's console log | a subagent with Xcode 27's `device-interaction` skill (`DeviceInteraction*` MCP tools), on a **different** simulator; then write the flow here |
| Watch the videos of a `--record` session | `fastUIReview` — read [references/review.md](references/review.md) first |
| One captioned sheet of chosen screenshots for a PR or ticket | `fastEvidenceSheet <spec.json>` — "Evidence sheet" in [references/usage.md](references/usage.md) |
| Physical iPhone | read [references/device.md](references/device.md) first |

Never point both tools at one simulator: Xcode reinstalls the app over the fresh install.
The engine is feature-agnostic: screen names, labels and assertions live only in flow
JSON. Never edit the engine for one screen. Every action, `setup` key and CLI flag is in
[references/usage.md](references/usage.md).

## Flow file

Everything the engine needs is in the flow; there is no per-project config.

```json
{
  "version": 1,
  "name": "home-premium",
  "expect": "Home tab on screen, no paywall",
  "app": {
    "project_root": "/abs/MyApp",
    "project": "/abs/MyApp/MyApp.xcworkspace",
    "scheme": "MyApp-DEV",
    "configuration": "Debug-DEV",
    "bundle_id": "com.example.myapp.dev",
    "simulator": "<simulator UDID>",
    "derived_data": "/abs/MyApp/build/fast-ui-dd",
    "app_path": "/abs/MyApp/build/fast-ui-dd/Build/Products/Debug-DEV-iphonesimulator/MyApp.app",
    "build_inputs": ["/abs/MyApp/MyApp", "/abs/MyApp/MyApp.xcodeproj/project.pbxproj"]
  },
  "setup": {
    "preferences": {"isPremium": true},
    "optional_permission_labels": ["Allow", "Don’t Allow"]
  },
  "actions": [
    {"type": "wait", "label": "Home", "timeout": 20},
    {"type": "tap_label", "label": "Lyrics", "element_type": "RadioButton"},
    {"type": "assert_absent", "label": "Upgrade to Premium", "seconds": 3},
    {"type": "screenshot", "name": "lyrics.png"}
  ]
}
```

- **App already on the simulator** (Settings, a build Xcode installed): `"app": {"bundle_id":
  "com.apple.Preferences", "prebuilt": true, "simulator": "<UDID>", "project_root": "<dir>"}`;
  no project, build or fresh install, start with `{"type": "launch"}`.
- Absolute paths only. `simulator` is a UDID from `xcrun simctl list devices available` that
  nobody else runs the app on. `derived_data` is its own folder, never Xcode's DerivedData;
  `app_path` is the `.app` inside it. `build_inputs` (source folders + `project.pbxproj`) drive
  the build cache; without them every run rebuilds.
- Selector keys on `wait`, `wait_absent`, `assert_absent`, `tap_first`, `tap_label`, `scroll_to`
  and every gesture, control and assertion action: `label` (exact), `label_contains`, `id`
  (accessibilityIdentifier), `value` (a switch's "1"/"0", a field's text), `element_type`,
  `enabled`, `label_is_empty`, `frame` bounds (`{"min_y": 400}`).
- Beyond taps and swipes: `long_press`, `double_tap`, `drag`, `back`, `edge_swipe`,
  `pull_to_refresh`, `type_text` into a named field (`clear: true`), `clear_text`, `press_key`,
  `dismiss_keyboard`, `set_switch`, `set_slider`, `set_picker`, `tap_alert`, `assert_value`,
  `assert_count`, `press_button`, `set_location`, `set_permission`, `push_notification`,
  `status_bar`, clipboard; on the iPhone also `pinch`, `rotate_gesture`, `two_finger_tap`,
  `set_orientation`. Keys and which engine runs what: the table in
  [references/usage.md](references/usage.md#gestures-keyboard-controls-assertions-and-device-state).
  Prove a control changed with `assert_value`, not by the tap succeeding.
- `tap_label` matches a `Button`; when the flow sets no `element_type` and exactly one node of
  any type has the label, it taps that node and reports `fallback_type`.

## One flow

1. Write the smallest version-1 flow for the requested behavior in a temporary directory
   (delete it afterwards, one `rm <file>` per file).
2. Unknown screen → build the flow on the live app instead of rerunning it:
   `fastSimulatorTest start <draft.json>` (only `app` + `setup`), then one
   `fastSimulatorTest do '<action json>'` per step (each prints the labels after it, ~1–2 s),
   then `fastSimulatorTest save <flow.json>`, which writes the steps and verifies them with
   one full run. For a single unknown label in an existing flow, one `{"type": "dump"}` at
   that point is enough. Never fake a `wait` on an impossible label to get a dump.
3. Run `fastSimulatorTest <flow.json>`. It boots the simulator, reuses the cached app when
   build inputs are unchanged, fresh-installs, seeds `setup.preferences`, prints one PASS/FAIL line.
4. On FAIL the line lists `visible labels: [Add Widget (StaticText), …]` with types, says when
   the label exists under another type, and suggests close labels. Fix one selector, rerun
   with `--reuse-app`. After two reruns stop and report with the artifact link.

### Timing rules

- First `wait` after launch ≤ 20 s; every later `wait`/`tap_*` ≤ 8 s; `post_delay` ≤ 1 s.
  To let a screen settle, `wait` for the next screen's selector; `pause` only for a
  product-defined delay. `"settle": true` on a tap/swipe waits until the tree stops changing
  (max 2.5 s) — use it on screens that keep animating, not by default.
- "Must not show" → `assert_absent` (`seconds` ≤ 8); "closes" → `wait_absent`. A successful
  tap proves nothing about absence.
- Fresh install wipes the defaults domain, and the engine undoes what a run changed (seeded
  preferences, appearance, location, status bar, granted permissions; the app is terminated)
  even when the run is interrupted (`--keep-state` to inspect). One run per simulator: a second
  one fails with `simulator busy`. Seed Codable values as `{"$json_data": …}`.
- A launch permission prompt may never appear right after a boot: answer it with
  `tap_alert` `"system": true, "optional": true`.
- Test builds skip SwiftLint (`lint=shimmed`); `"skip_lint": false` in `app` lints.
- Apple's own sheets are not in the accessibility tree: prove them with `screenshot`.
- Rerunning a user's existing flow: keep its values, list violations in the report.

Every path the tools print (`artifacts=`, `screenshot=`, `ui=`, `video=`, `path=`) is a `file://`
URL: relay it as a markdown link.

## Evidence, not just selectors

A flow is evidence only when its last actions assert the product outcome. A FAIL whose labels
show the expected screen under another label is a selector bug (fix the flow); labels showing
the wrong state (popup still there, gate not applied) are a product bug: fix the code, rebuild
(`--build-only`, or drop `--reuse-app`), rerun the same flow. Record (`--record`) when the user
asks for video or a flow proves a fix: a PASS keeps one `final[-<session>].mp4` per task, a
FAIL keeps `run.mp4` beside `failure.png`. Both engines record — the iPhone through XCTest's own
screen recording, one-shot runs only ([references/device.md](references/device.md)) — and both
re-encode the clip at review quality (H.264, 960 px, ~1–2 MB for 20 s).

## Several flows or simulators

Build once: `fastSimulatorTest <any-flow.json> --build-only` → `READY`; then every run passes
`--reuse-app`. One simulator and up to about 5 flows: run them here, one after another.
2+ simulators or more flows: fan out, one `sonnet` agent per booted simulator, all in one
message, each with its flow paths, UDID, the command
`fastSimulatorTest <flow> --reuse-app --session <name> [--record]`, the two-rerun cap and
"return only the PASS/FAIL lines"; never two agents on one simulator. Collate with
`fastUIReport --root <artifacts-root> --session <name>` and paste its table. Variants:
`--appearance light|dark`, `--content-size <simctl name>` are their own rows.
