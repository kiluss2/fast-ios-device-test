---
description: Build contact sheets from a recorded fastSimulatorTest session and review every case's video
argument-hint: <session name> [artifacts root]
allowed-tools: Bash, Read, Write, Edit, Glob
---

Review the recordings of fastSimulatorTest session `$ARGUMENTS`.

Read and follow `references/review.md` of the shared `fast-ios-device-test`
skill (`~/.claude/skills/fast-ios-device-test/references/review.md`). The tool is:

```bash
"${CLAUDE_PLUGIN_ROOT:-$HOME/.local}/bin/fastUIReview" --root <artifacts-root> --session <name>
```

- The artifacts root is the `--artifacts-root` the session was run with
  (default `~/Library/Caches/fast-ios-device-test/artifacts/<project>-<hash>/fast-ui`; `fastSimulatorTest clean --list` shows it).
- Run the tool once, then Read EVERY contact sheet it lists. Do not sample.
  Each sheet's header carries the flow's `expect`; judge the frames against it.
- Fill `review-notes-<session>.json` (written by the tool) for each case:
  `note` = what the frames show and at which second, `verdict` = `OK`,
  `product bug` (the app did the wrong thing) or `flow bug` (the flow asserted
  the wrong thing). A PASS whose frames show the wrong screen is a finding, not
  a rerun. Rerun the tool at the end: `pending=0` means every case has a verdict.
- If the argument is a combined video (`<mp4> --manifest <json>`), run
  `fastUIReview --combined <mp4> --manifest <json>` instead, then Read
  `combined-titles.png` and `combined-ends.png` and report the `COMBINED` line.
- Return the per-case verdicts and the path of the review directory; if a report
  file exists for the session, append them under a "Video review" heading.
