#!/usr/bin/env python3

"""Evidence sheet tests: python3 -m unittest test_fast_evidence_sheet (needs Pillow)"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_evidence_sheet as sheet  # noqa: E402

from PIL import Image  # noqa: E402


def make_run(root: Path, task: str, name: str, status: str, shots: tuple[str, ...] = ("main",)) -> Path:
    run = root / task / name
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(json.dumps({"status": status}))
    for shot in shots:
        Image.new("RGB", (120, 260), (30, 60, 90)).save(run / f"{shot}.png")
    return run


def write_spec(directory: Path, spec: dict) -> Path:
    path = directory / "sheet.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


class RunSelectionTests(unittest.TestCase):
    def test_newest_passing_run_wins_over_a_newer_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_run(root, "flow", "20261013-090000-aaaa", "pass")
            wanted = make_run(root, "flow", "20261013-100000-aaaa", "pass")
            make_run(root, "flow", "20261013-110000-aaaa", "fail")
            (root / "flow" / ".DS_Store").write_bytes(b"finder")
            self.assertEqual(sheet.passing_run(root / "flow"), wanted)

    def test_task_without_a_passing_run_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "fail")
            spec = write_spec(Path(raw), {"root": raw, "cells": [{"task": "flow", "shot": "main"}]})
            with self.assertRaisesRegex(sheet.SheetError, "no passing run of 'flow'"):
                sheet.build(spec)

    def test_every_missing_shot_is_reported_with_the_shots_that_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass", shots=("1-paywall", "2-sale"))
            spec = write_spec(Path(raw), {"root": raw, "cells": [
                {"task": "flow", "shot": "1-paywal"},
                {"task": "flow", "shot": "1-paywall"},
                {"image": "missing.png"},
            ]})
            with self.assertRaises(sheet.SheetError) as caught:
                sheet.build(spec)
        message = str(caught.exception)
        self.assertIn("cell 1: '1-paywal.png'", message)
        self.assertIn("['1-paywall', '2-sale']", message)
        self.assertIn("cell 3: image", message)
        self.assertNotIn("cell 2", message)

    def test_a_pinned_run_is_used_even_when_it_failed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pinned = make_run(Path(raw), "flow", "20261013-090000-aaaa", "fail", shots=("before",))
            make_run(Path(raw), "flow", "20261013-100000-aaaa", "pass")
            path = sheet.cell_image({"task": "flow", "shot": "before", "run": pinned.name}, 1, Path(raw), Path(raw))
            self.assertEqual(path, pinned / "before.png")


class RenderTests(unittest.TestCase):
    def test_jpeg_sheet_has_one_column_per_cell_and_relative_paths_resolve_to_the_spec(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass")
            spec = write_spec(Path(raw), {
                "title": "PRJ-1 · Kiểm tra", "root": ".", "out": "out/sheet.jpg",
                "cells": [{"task": "flow", "shot": "main", "label": f"C{i}", "caption": "đóng paywall"} for i in range(3)],
            })
            result = sheet.build(spec)
            out = Path(raw) / "out" / "sheet.jpg"
            with Image.open(out) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.width, sheet.MARGIN * 2 + 3 * 360 + 2 * sheet.GAP)
            self.assertEqual(Path(result["path"]), out.resolve())
            self.assertEqual(result["cells"], 3)

    def test_columns_wrap_cells_into_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass")
            cells = [{"task": "flow", "shot": "main"} for _ in range(3)]
            one_row = sheet.build(write_spec(Path(raw), {"root": raw, "out": "a.png", "cells": cells}))
            two_rows = sheet.build(write_spec(Path(raw), {"root": raw, "out": "b.png", "columns": 2, "cells": cells}))
        self.assertLess(two_rows["width"], one_row["width"])
        self.assertGreater(two_rows["height"], one_row["height"])

    def test_without_columns_a_sheet_of_phone_shots_is_landscape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass")
            results = {
                count: sheet.build(write_spec(Path(raw), {"root": raw, "out": f"{count}.png", "cells": [
                    {"task": "flow", "shot": "main", "label": f"C{i}", "caption": "đóng paywall"} for i in range(count)]}))
                for count in (7, 15, 30)
            }
        for count, result in results.items():
            self.assertGreaterEqual(result["width"], result["height"], f"{count} cells")
        self.assertEqual(results[7]["columns"], 7)
        self.assertEqual(results[15]["columns"], 8)
        self.assertEqual(results[30]["columns"], 10)

    def test_too_few_cells_for_landscape_stay_on_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass")
            result = sheet.build(write_spec(Path(raw), {"root": raw, "out": "a.png", "cells": [
                {"task": "flow", "shot": "main"} for _ in range(2)]}))
        self.assertEqual(result["columns"], 2)

    def test_columns_in_the_spec_win_over_the_landscape_choice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass")
            result = sheet.build(write_spec(Path(raw), {"root": raw, "out": "a.png", "columns": 4, "cells": [
                {"task": "flow", "shot": "main"} for _ in range(15)]}))
        self.assertEqual(result["columns"], 4)
        self.assertLess(result["width"], result["height"])

    def test_long_caption_wraps_inside_the_cell_and_grows_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            make_run(Path(raw), "flow", "20261013-110000-aaaa", "pass")
            short = sheet.build(write_spec(Path(raw), {"root": raw, "out": "a.png", "cells": [
                {"task": "flow", "shot": "main", "caption": "ok"}]}))
            long = sheet.build(write_spec(Path(raw), {"root": raw, "out": "b.png", "cells": [
                {"task": "flow", "shot": "main", "caption": "chỉ onboarding: false → reopen vẫn bật, banner vẫn tắt " * 3}]}))
        self.assertGreater(long["height"], short["height"])
        self.assertEqual(long["width"], short["width"])

    def test_wrap_keeps_explicit_line_breaks(self) -> None:
        lines = sheet.wrap("reopen: false →\nkhông paywall", sheet.font(20), 360)
        self.assertEqual(lines, ["reopen: false →", "không paywall"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
