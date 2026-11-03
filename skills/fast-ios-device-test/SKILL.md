---
name: fast-ios-device-test
description: Use when asked to run, verify, re-test, or record an iOS screen or flow quickly on Simulator (/fastSimulatorTest) or a physical iPhone (/fastDeviceTest), including a matrix of several cases or several devices, or a system app such as Settings. Not for release, archive, or full-suite runs.
---

# Fast iOS Device Test

## Which tool

| Need | Use |
|---|---|
| Prove a behavior, rerun it, run a matrix, record video, report | this skill: `fastSimulatorTest <flow.json>` / `fastDeviceTest <flow.json>` |
| Explore an unknown screen on a Simulator, read the app's console log | a subagent with Xcode 27's `device-interaction` skill (`DeviceInteraction*` MCP tools), on a **different** simulator; then write the flow here. It refuses a physical iPhone (Xcode 27.0 and 27.2) |
| Explore or drive a physical iPhone: taps, pinch, rotate, `two_finger_drag` (a map's tilt) | this skill: `fastDeviceTest do '<action>'` step by step ([references/device.md](references/device.md)) |
| Watch the videos of a `--record` session | `fastUIReview` — read [references/review.md](references/review.md) first |
| PR evidence (sheet + clips) | nothing to run: every PASS flow run, and every `--before` run that failed at its outcome step, is filed in `~/Desktop/<project> <ticket>/` — "Evidence folder" in [references/usage.md](references/usage.md) |
| Change that sheet (order, captions, a shot) | edit `source/sheet.json` there, then `fastEvidenceSheet <it>` — "Evidence sheet" in [references/usage.md](references/usage.md) |
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
- **Where flows live**: a ticket's flows go in `fastEvidenceSheet --folder <project_root>`'s
  `flows=` folder (`~/Desktop/<project> <ticket>/source/flows/`, created on the spot), so a later
  session finds them and they go when the user deletes the folder after the merge. Never under
  `build/fast-ui` or `build/fast-device` (pruned after `FAST_UI_KEEP_DAYS`). Throwaway flows may
  stay in the session scratchpad.
- Absolute paths only (a flow outside the repo must name `project_root`). `simulator` is a UDID from `xcrun simctl list devices available` that
  nobody else runs the app on. `derived_data` is its own folder, never Xcode's DerivedData;
  `app_path` is the `.app` inside it. `build_inputs` (source folders + `project.pbxproj`) drive
  the build cache; without them every run rebuilds.
- Selector keys on `wait`, `wait_absent`, `assert_absent`, `tap_first`, `tap_label`, `long_press`,
  `double_tap`, `scroll_to` (gestures such as `drag`, `pinch`, `set_slider` are in usage.md):
  `label` (exact), `label_contains`, `id` (accessibilityIdentifier), `value` (a switch's "1"/"0",
  a field's text), `element_type`, `label_is_empty`, `frame` bounds (`{"min_y": 400}`).
- `tap_label` matches a `Button`; when the flow sets no `element_type` and exactly one node of
  any type has the label, it taps that node and reports `fallback_type`.

## One flow

1. Write the smallest version-1 flow for the requested behavior in a temporary directory
   (delete it afterwards, one `rm <file>` per file).
2. App driven before → `fastUIMap <bundle id> --find <label>` first: selectors and the steps that
   reach them, from what earlier runs saw; `--path-to <screen>` prints them as one guarded `do`
   request. A hint that may be stale; the flow still verifies.
   Maps live in `~/Library/Caches/fast-ui-map/` and hold on-screen labels, which can include
   user data: never copy one into a repo, PR or ticket.
3. Unknown label → one `{"type": "dump"}` at that point: the PASS line prints the visible
   labels and `<name>-ui.json` has label, type, id, value and tap `center` per node. Never
   fake a `wait` on an impossible label to get a dump.
4. Run `fastSimulatorTest <flow.json>`. It boots the simulator, reuses the cached app when
   build inputs are unchanged, fresh-installs, seeds `setup.preferences`, prints one PASS/FAIL line.
5. On FAIL the line lists `visible labels: [Add Widget (StaticText), …]` with types, says when
   the label exists under another type, and suggests close labels. Fix one selector, rerun
   with `--reuse-app`. After two reruns stop and report with the artifact link.

## Step by step (exploring, or one quick check)

`fastSimulatorTest do '<action or array>' --simulator <udid> [--app <bundle id>]` and
`fastDeviceTest do … --device <udid|name>` act on what is on screen now (no build, install or
launch) and print one PASS/FAIL line with the visible labels after the actions.
`FAST_SIMULATOR`, `FAST_UI_APP` and `FAST_DEVICE` save the flags.

- The line already lists 40 labels: no `dump`, `screenshot` or `pause` in a routine step.
- Send the steps you can predict as one array with an `expect` selector on each gesture
  (`{"type": "tap_label", "label": "General", "expect": {"label": "About"}}`): the request stops
  at the first screen that is not the expected one. A destructive-looking step (Delete, Pay,
  Send, Sign Out…) in an array is refused unless it says `"confirm": true`.
- Between steps, one line of reasoning; turn a path that worked into a flow for reruns.
- A screen visited before: `fastUIMap <bundle id> --path-to <screen>` gives the whole way as one
  guarded request. A search of about six steps or more: hand it to the `ui-explorer` subagent
  (Haiku, `FAST_GUARD_SINGLE=1`, returns the guarded request) and decide only the hard steps
  yourself — experimental, check its result before relying on it.

### Timing rules

- First `wait` after launch ≤ 20 s; every later `wait`/`tap_*` ≤ 8 s; `post_delay` ≤ 1 s.
  To let a screen settle, `wait` for the next screen's selector; `pause` only for a
  product-defined delay. `"settle": true` on a tap/swipe waits until the tree stops changing
  (max 2.5 s) — use it on screens that keep animating, not by default.
- "Must not show" → `assert_absent` (`seconds` ≤ 8); "closes" → `wait_absent`. A successful
  tap proves nothing about absence.
- Fresh install wipes the defaults domain, and the engine undoes what a run changed (seeded
  preferences, appearance, text size; the app is terminated) even when the run is interrupted
  (`--keep-state` to inspect). One run per simulator: a second one prints `BUSY` (exit 3) and a
  `do` on it fails with `simulator busy`. Seed Codable values as `{"$json_data": …}`.
- Prove state with checks, not with a tap that happened to work: `assert_value` (`equals`,
  `contains`, `matches`), `assert_count` (`count`, `min`, `max`), the selector key `enabled`.
  Switches: `set_switch` `on` (touches only when needed). Alerts: `tap_alert` `button`, with
  `"system": true, "optional": true` for a permission prompt that may not come again.
- Test builds skip SwiftLint (`lint=shimmed`); `"skip_lint": false` in `app` lints. A failed
  build names the phase and its first `error:` line.
- Apple's own sheets are not in the accessibility tree: prove them with `screenshot`, and close
  one that blocks the flow (the App Store review sheet) with `tap_point`.
- `open_url` delivers a link with no prompt. A link tapped in Safari asks "Open in …?": that flow
  declares `Open` and never `Cancel`, or the runner cancels the link.
- Rerunning a user's existing flow: keep its values, list violations in the report.

Every path the tools print (`artifacts=`, `screenshot=`, `ui=`, `video=`, `path=`) is a `file://`
URL: relay it as a markdown link.

## When the work is done

- iPhone: `fastDeviceTest lock --device <d>` instead of `stop`. It frees the runner and locks
  the screen, for a test phone kept on Auto-Lock "Never". Nothing can drive the phone again
  until someone types its passcode, so only at the very end; it refuses an iPhone another
  runner holds ("busy").
- Simulator: `fastSimulatorTest shutdown` shuts down every booted simulator;
  `--simulator <udid>` only that one. Spare one Xcode is running the app on unless the user is
  done with it too.

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
