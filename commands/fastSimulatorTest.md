---
description: Run a UI test on Simulator via a JSON flow (one screen, a system app, or a matrix of cases × simulators)
argument-hint: <screen, flow, or list of cases; optionally which simulators>
allowed-tools: Bash, Read, Grep, Glob, Write, Edit, Agent
---

Test only this requested UI behavior: `$ARGUMENTS`.

Read and follow the `fast-ios-device-test` skill
(`~/.claude/skills/fast-ios-device-test/SKILL.md`, or `skills/fast-ios-device-test/SKILL.md`
in this plugin): flow format, selectors, timing rules, the two-rerun cap and when to fan out.
The engine is:

```bash
"${CLAUDE_PLUGIN_ROOT:-$HOME/.local}/bin/fastSimulatorTest" <flow.json> [--reuse-app|--build-only|--record|--session <name>]
"${CLAUDE_PLUGIN_ROOT:-$HOME/.local}/bin/fastSimulatorTest" start <draft.json> | do '<action json>' | labels | save <flow.json>
```

A new screen: `start` + `do` step by step + `save`, not repeated full runs with `dump`.

Decide the shape first: one flow → write and run it; up to about 5 flows on one simulator →
`--build-only` once, then run them here with `--reuse-app`; more flows or 2+ simulators →
the skill's fan-out, collated with `fastUIReport`. "quay"/"record" → `--record`.

Return the PASS/FAIL lines (a flow × simulator table when fanned out) and one concise cause
per FAIL, with artifact links. Remove temporary flows; never edit the engine for one screen.
