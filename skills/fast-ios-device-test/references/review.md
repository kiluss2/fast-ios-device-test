# fastUIReview — reviewing recorded runs

Read this before reviewing a `--record` session, from the Simulator or an iPhone
(`fastDeviceTest --record`). Flow format and timing rules are in
[../SKILL.md](../SKILL.md); the tool's flags are in [usage.md](usage.md).

## A recorded run is not verified until someone watched it

A PASS line proves the selectors resolved; it does not prove the screen looked
right (a card can sit behind Apple's sheet, a wrong screen can carry the same
labels). Community video skills all reduce to the same recipe — sample frames,
drop the ones that did not change, show them to the model with timestamps — and
that is what the tool does, without ffmpeg. After a `--record` session:

1. Give every flow an `expect` line (top-level key, one sentence: what the last
   frames must show, e.g. `"Apple review sheet on top, no app card under it"`).
   The engine copies it into `metrics.json`; the tool prints it on the sheet.
2. `fastUIReview --root <artifacts-root> --session <name>` writes one contact
   sheet per case (`<task>-<sim>.png`): the frame at the end of every flow step
   plus one per second, minus interval frames that look like the previous one
   (`--change-threshold`, default 1 % of pixels; `0` keeps all). Each SHEET line
   prints kept/total frames and a rough token cost; keep a sheet under ~15k tokens
   by raising `--interval` rather than skipping it.
3. It also writes `review-notes-<session>.json` with one entry per case
   (`expect`, `sheet`, `video`, `verdict: null`, `note: ""`). Re-runs keep
   verdicts already written.
4. Read every sheet with the Read tool. Fill the entry: `note` = what the frames
   show at which second, `verdict` = `OK` / `product bug` (the app did the wrong
   thing) / `flow bug` (the flow asserted the wrong thing). A sheet that
   contradicts the PASS line is a finding, never a rerun. `pending=0` in the
   NOTES line is the only "reviewed" signal.
5. Put the notes in the report next to the PASS/FAIL table and link each case's
   `final-<session>.mp4`. Only then call the case tested.

What a frame can and cannot prove: a popup, a sheet, a toast, a pushed screen —
yes. That a trigger fired and was *blocked for the right reason* — no; an empty
screen only shows nothing appeared. Blocked cases need the flow's `assert_absent`
plus a unit test on the gate, and the note should say so.

Combined deliverable: when the clips are concatenated into one file from a
manifest (`[{path, title, subtitle}]`, one title card per clip), verify the file
with `fastUIReview --combined <mp4> --manifest <json> [--title-seconds 2.5]`.
It writes `combined-titles.png` / `combined-ends.png` (middle of each title card,
last second of each clip) and prints `COMBINED ... OK|MISMATCH` comparing the
manifest timeline with the file's duration. Read both sheets: order 01→N and
every end frame against its title's expectation.

Keep the artifacts root somewhere durable (`--artifacts-root`, `FAST_UI_KEEP_DAYS=0`) so the
videos survive the 3-day prune and the size cap and live beside the report.

Two things that spoil recordings: another Xcode run on the same simulator
(`testmanagerd` installs its app over yours and the review sheet loses the
foreground — pick a simulator nobody else uses), and `Not Now`/`Allow` labels in
`optional_permission_labels` (the poller would tap the sheet away).
