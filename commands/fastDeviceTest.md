---
description: Run a UI flow on a physical iPhone (tap/type/swipe/screenshot via XCUITest), including Settings/Shortcuts/SpringBoard
argument-hint: <screen or flow; optionally which iPhone>
allowed-tools: Bash, Read, Grep, Glob, Write, Edit
---

Test only this requested behavior on a physical iPhone: `$ARGUMENTS`.

Read `references/device.md` of the `fast-ios-device-test` skill first
(`~/.claude/skills/fast-ios-device-test/references/device.md`); flow format, selectors and
timing rules are in that skill's `SKILL.md`. The engine is:

```bash
"${CLAUDE_PLUGIN_ROOT:-$HOME/.local}/bin/fastDeviceTest" <flow.json> [--device <udid|name>] [--install <App.app>]
```

Same flow JSON as the simulator; the app must already be installed (build and install it with
`fastDeviceTest build --project <P> --scheme <S> --configuration <C> --bundle-id <id> --install
--device <d>`, which uses the project's Simulator `build/fast-ui-dd`, never a folder of its own). Several iPhones →
use the one the user named (a unique part of the name is enough). More than one run, or
step-by-step exploration → `fastDeviceTest serve`, then flows or `fastDeviceTest do '<json>'`,
and `fastDeviceTest stop` when done; at the end of the work `fastDeviceTest lock` instead (stops
the runner and locks the screen). Video wanted → add `--record` to a one-shot run (not
through `serve`); the PASS line carries `video=`.

Return the PASS/FAIL line and one cause per FAIL, with artifact links. Remove temporary flows.
