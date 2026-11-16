#!/usr/bin/env python3

"""Build one captioned evidence sheet (a PR attachment) from flow screenshots.

    fastEvidenceSheet <spec.json>

    {
      "title": "PRJ-12 · Paywall gating (Staging, iPhone iOS 26.6)",
      "root": "/abs/MyApp/build/evidence-prj-12/device",
      "out": "/abs/MyApp/build/evidence-prj-12/pr/gating.jpg",
      "cells": [
        {"task": "gating-prod", "shot": "1-paywall", "label": "G1 · Production", "caption": "paywall on reopen"},
        {"task": "gating-off", "shot": "main", "label": "G2 · Off", "caption": "reopen: false →\\nno paywall"},
        {"image": "/abs/before.png", "label": "Before", "caption": "the bug"}
      ]
    }

`root` is the --artifacts-root the flows ran with. A `task` cell takes `<shot>.png` from that
task's latest PASSING run (pin one with `"run": "<run folder name>"`); an `image` cell takes
any file. Relative paths resolve against the spec file. `out` ending in `.jpg` (the default)
writes review quality (JPEG 80, ~250 KB for seven phone shots); `.png` keeps it lossless.
Optional: `columns` (cells per row; left out, the sheet comes out landscape, about 16:10)
and `cell_width` (px, default 360).

Every cell is checked before anything is drawn, so one run reports every missing shot.

    fastEvidenceSheet --folder <project root>

prints the evidence folder flow runs of that checkout are filed into, and creates its
`source/flows/`, where the ticket's flows are kept.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

# bin/ holds symlinks in a git checkout but copies in the zip, so look beside scripts/ too.
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "scripts")]

# Arial Unicode covers Vietnamese and other diacritics that Helvetica draws as boxes.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
BACKGROUND = (246, 247, 249)
BORDER = (200, 200, 200)
INK = (20, 20, 20)
MUTED = (80, 80, 80)
MARGIN = 24
GAP = 24
TITLE_SIZE, LABEL_SIZE, CAPTION_SIZE = 34, 22, 20
LINE_SPACING = 4
JPEG_QUALITY = 80
PIXELS_PER_TOKEN = 750
# Reviewers read the sheet on a landscape desktop screen.
LANDSCAPE_RATIO = 16 / 10


class SheetError(RuntimeError):
    pass


def font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def resolve(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def passing_run(task_dir: Path) -> Path | None:
    """The newest run folder whose metrics.json says pass; run folders sort by their timestamp."""
    runs = sorted((entry for entry in task_dir.iterdir() if entry.is_dir()), reverse=True) if task_dir.is_dir() else []
    for run in runs:
        try:
            if json.loads((run / "metrics.json").read_text(encoding="utf-8")).get("status") == "pass":
                return run
        except (OSError, json.JSONDecodeError):
            continue
    return None


def filed_shot(run: Path, stem: str) -> Path | None:
    """A filed PASS run keeps only metrics.json; its shots live in the evidence folder it names."""
    try:
        filed = json.loads((run / "metrics.json").read_text(encoding="utf-8")).get("evidence_shots") or {}
    except (OSError, json.JSONDecodeError):
        return None
    return Path(filed[stem]) if stem in filed else None


def cell_image(cell: dict[str, Any], index: int, root: Path | None, base: Path) -> Path:
    """Path of one cell's picture, or a SheetError that says what is missing and what exists."""
    if cell.get("image"):
        path = resolve(cell["image"], base)
        if not path.is_file():
            raise SheetError(f"cell {index}: image {path} not found")
        return path
    task, shot = cell.get("task"), cell.get("shot")
    if not task or not shot:
        raise SheetError(f"cell {index}: give either `image` or both `task` and `shot`")
    cell_root = resolve(cell["root"], base) if cell.get("root") else root
    if cell_root is None:
        raise SheetError(f"cell {index}: `task` needs `root` (the flows' --artifacts-root)")
    task_dir = cell_root / task
    if cell.get("run"):
        run = task_dir / cell["run"]
        if not run.is_dir():
            raise SheetError(f"cell {index}: run {run} not found")
    else:
        run = passing_run(task_dir)
        if run is None:
            raise SheetError(f"cell {index}: no passing run of '{task}' under {cell_root}")
    path = run / (shot if Path(shot).suffix else f"{shot}.png")
    if not path.is_file():
        path = filed_shot(run, Path(shot).stem) or path
    if not path.is_file():
        shots = sorted(entry.stem for entry in run.glob("*.png"))
        raise SheetError(f"cell {index}: '{path.name}' not in {run}; shots there: {shots}")
    return path


def wrap(text: str, typeface: ImageFont.ImageFont, width: int) -> list[str]:
    """Explicit `\\n` breaks first, then word wrap to the cell width."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words, line = paragraph.split(), ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if line and typeface.getlength(candidate) > width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines


def landscape_columns(count: int, layout: Callable[[int], tuple[Any, int, int]]) -> int:
    """Cells per row for a sheet read on a landscape desktop screen: among the row counts that
    leave the sheet wider than tall, the one closest to 16:10. Phone shots are ~2.6 times taller
    than wide, so 7 cells take one row, 15 take two of 8, 30 take three of 10. Too few cells for
    any landscape layout stay on one row, the widest there is."""
    best, best_distance = count, math.inf
    for rows in range(1, count + 1):
        columns = math.ceil(count / rows)
        if math.ceil(count / columns) != rows:
            continue
        _, width, height = layout(columns)
        if width < height:
            break
        distance = abs(math.log(width / height / LANDSCAPE_RATIO))
        if distance < best_distance:
            best, best_distance = columns, distance
    return best


def render(cells: list[dict[str, Any]], images: list[Path], out: Path, title: str = "", columns: int | None = None, cell_width: int = 360) -> dict[str, Any]:
    title_font, label_font, caption_font = font(TITLE_SIZE), font(LABEL_SIZE), font(CAPTION_SIZE)
    thumbs = []
    for path in images:
        with Image.open(path) as image:
            image = image.convert("RGB")
            thumbs.append(image.resize((cell_width, round(image.height * cell_width / image.width)), Image.LANCZOS))
    captions = [wrap(str(cell.get("caption", "")), caption_font, cell_width) if cell.get("caption") else [] for cell in cells]
    labels = [wrap(str(cell.get("label", "")), label_font, cell_width) if cell.get("label") else [] for cell in cells]
    label_line, caption_line = LABEL_SIZE + LINE_SPACING + 4, CAPTION_SIZE + LINE_SPACING + 2
    title_height = TITLE_SIZE + MARGIN if title else 0
    text_heights = [len(labels[i]) * label_line + len(captions[i]) * caption_line for i in range(len(cells))]
    thumb_heights = [thumb.height for thumb in thumbs]

    def layout(count: int) -> tuple[list[tuple[range, int, int]], int, int]:
        rows = []
        for row in range(math.ceil(len(cells) / count)):
            members = range(row * count, min((row + 1) * count, len(cells)))
            text_height = max(text_heights[i] for i in members)
            rows.append((members, text_height + 8 if text_height else 0, max(thumb_heights[i] for i in members)))
        width = MARGIN + count * (cell_width + GAP) - GAP + MARGIN
        height = MARGIN + title_height + sum(text + thumb + GAP for _, text, thumb in rows) - GAP + MARGIN
        return rows, width, height

    columns = max(1, min(columns, len(cells))) if columns else landscape_columns(len(cells), layout)
    row_layout, width, height = layout(columns)
    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    y = MARGIN
    if title:
        draw.text((MARGIN, y), title, font=title_font, fill=INK)
        y += title_height
    for members, text_height, thumb_height in row_layout:
        for column, index in enumerate(members):
            x = MARGIN + column * (cell_width + GAP)
            line_y = y
            for line in labels[index]:
                draw.text((x, line_y), line, font=label_font, fill=INK)
                line_y += label_line
            for line in captions[index]:
                draw.text((x, line_y), line, font=caption_font, fill=MUTED)
                line_y += caption_line
            top = y + text_height
            sheet.paste(thumbs[index], (x, top))
            draw.rectangle((x - 1, top - 1, x + cell_width, top + thumbs[index].height), outline=BORDER)
        y += text_height + thumb_height + GAP

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".png":
        sheet.save(out, optimize=True)
    else:
        sheet.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return {
        "path": str(out), "cells": len(cells), "columns": columns, "width": width, "height": height,
        "kilobytes": round(out.stat().st_size / 1024), "tokens": math.ceil(width * height / PIXELS_PER_TOKEN),
    }


def build(spec_path: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    base = spec_path.parent
    cells = spec.get("cells") or []
    if not cells:
        raise SheetError("spec has no cells")
    root = resolve(spec["root"], base) if spec.get("root") else None
    images, errors = [], []
    for index, cell in enumerate(cells, start=1):
        try:
            images.append(cell_image(cell, index, root, base))
        except SheetError as error:
            errors.append(str(error))
    if errors:
        raise SheetError("; ".join(errors))
    out = resolve(spec.get("out") or f"{spec_path.stem}.jpg", base)
    return render(
        cells, images, out, str(spec.get("title", "")),
        int(spec["columns"]) if spec.get("columns") else None, int(spec.get("cell_width", 360)),
    )


def print_folder(project_root: Path) -> int:
    import fast_evidence

    named = fast_evidence.folder_for(project_root, {})
    if named is None:
        print(f"FAIL error={project_root} is not in git: set FAST_EVIDENCE_TICKET (and FAST_EVIDENCE_PROJECT)",
              file=sys.stderr)
        return 1
    flows = named[0] / "source/flows"
    flows.mkdir(parents=True, exist_ok=True)
    print(f"FOLDER path={named[0].as_uri()} flows={flows.as_uri()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fastEvidenceSheet", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spec", type=Path, nargs="?", help="sheet spec JSON (see above)")
    parser.add_argument(
        "--folder", type=Path, metavar="PROJECT_ROOT",
        help="Print (and create) the evidence folder of this checkout's ticket and its source/flows/, "
        "where the ticket's flows are kept",
    )
    args = parser.parse_args()
    if args.folder:
        return print_folder(args.folder.expanduser().resolve())
    if not args.spec:
        parser.error("give a spec, or --folder <project root>")
    try:
        result = build(args.spec.expanduser().resolve())
    except (SheetError, OSError, json.JSONDecodeError) as error:
        print(f"FAIL error={error}", file=sys.stderr)
        return 1
    print(
        f"SHEET cells={result['cells']} columns={result['columns']} size={result['width']}x{result['height']} "
        f"kb={result['kilobytes']} tokens≈{result['tokens']} path={Path(result['path']).resolve().as_uri()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
