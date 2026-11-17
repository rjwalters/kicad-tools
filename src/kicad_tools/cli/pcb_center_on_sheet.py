"""Center a PCB on its engineering drawing sheet.

``kct pcb center-on-sheet`` rigidly translates ALL board geometry by a
single grid-snapped ``(dx, dy)`` so the Edge.Cuts bounding box sits centered
in the sheet's usable drawing area: inside the 10 mm frame border and above
the 35 mm title-block band at the bottom (KiCad default worksheet geometry).

``(paper "User" W H)`` sheets are upgraded to the smallest standard
landscape size whose usable area fits the board with >= 15 mm of slack per
side (a to-the-board "User" page is why such boards hug the frame corner in
KiCanvas).

The transform is pure text editing with exact decimal arithmetic: only the
X/Y atoms of coordinate nodes change (plus the ``(paper ...)`` node when the
sheet size changes), so relative geometry -- routing, clearances, 45-degree
copper, DRC results -- is preserved exactly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_center_on_sheet(
    pcb_path: Path,
    output_path: Path | None = None,
    paper: str = "auto",
    margin: float | None = None,
    title_block: float | None = None,
    grid: float | None = None,
    dry_run: bool = False,
    output_format: str = "text",
) -> int:
    """Center *pcb_path* on its drawing sheet.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.
        output_path: Alternative output path (default: modify in place).
        paper: ``"auto"`` (default), ``"keep"``, or an explicit standard
            size name (``A4``..``A0``, landscape).
        margin: Frame border inset per side in mm (default 10).
        title_block: Reserved title-block band height in mm (default 35).
        grid: Grid to snap the translation to in mm (default 0.05).
        dry_run: Report the would-be transform without writing.
        output_format: ``"text"`` or ``"json"``.

    Returns:
        Exit code (0 success, 1 error).
    """
    from kicad_tools.pcb.center_sheet import (
        DEFAULT_FRAME_MARGIN_MM,
        DEFAULT_GRID_MM,
        DEFAULT_TITLE_BLOCK_MM,
        center_on_sheet,
    )

    kwargs = {
        "margin": margin if margin is not None else DEFAULT_FRAME_MARGIN_MM,
        "title_block": title_block if title_block is not None else DEFAULT_TITLE_BLOCK_MM,
        "grid": grid if grid is not None else DEFAULT_GRID_MM,
    }

    try:
        report = center_on_sheet(
            pcb_path,
            output_path=output_path,
            paper=paper,
            dry_run=dry_run,
            **kwargs,
        )
    except Exception as e:
        if output_format == "json":
            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    dest = output_path if output_path is not None else pcb_path
    result = {
        "input": str(pcb_path),
        "output": str(dest) if (report.changed and not dry_run) else None,
        "dry_run": dry_run,
        "changed": report.changed,
        "paper_before": report.paper_before,
        "paper_after": report.paper_after,
        "bbox_before": list(report.bbox_before),
        "bbox_after": list(report.bbox_after),
        "usable_area": list(report.usable_area),
        "dx_mm": report.dx_mm,
        "dy_mm": report.dy_mm,
        "translated_items": report.translated_items,
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        label = " (dry run)" if dry_run else ""
        print(f"PCB Center On Sheet{label}")
        print(f"  Input: {pcb_path}")
        bb = report.bbox_before
        print(
            f"  Board bbox: ({bb[0]:g}, {bb[1]:g}) - ({bb[2]:g}, {bb[3]:g}) "
            f"[{bb[2] - bb[0]:g} x {bb[3] - bb[1]:g} mm]"
        )
        if report.paper_changed:
            print(f"  Paper: {report.paper_before} -> {report.paper_after}")
        else:
            print(f"  Paper: {report.paper_before} (unchanged)")
        ua = report.usable_area
        print(f"  Usable area: ({ua[0]:g}, {ua[1]:g}) - ({ua[2]:g}, {ua[3]:g})")
        if not report.changed:
            print("  Already centered: no changes needed.")
        else:
            verb = "Would translate" if dry_run else "Translated"
            print(f"  {verb} by dx={report.dx_mm:g} mm, dy={report.dy_mm:g} mm")
            ba = report.bbox_after
            print(f"  New bbox: ({ba[0]:g}, {ba[1]:g}) - ({ba[2]:g}, {ba[3]:g})")
            if report.translated_items:
                print(f"  Items translated: {report.translated_items}")
            if not dry_run:
                print()
                print(f"  Saved to: {dest}")

    return 0
