"""Resize a PCB drawing sheet to fit the board outline and center it.

Provides a standalone command that runs :meth:`PCB.page_fit` over a
``.kicad_pcb`` file: the ``(paper ...)`` node is rewritten to a tight
``(paper "User" W H)`` sized to the Edge.Cuts bounding box plus a uniform
margin, and every board item is translated so the board sits centered with
that margin all around.

This is a pure geometric transform -- routing and DRC validity are preserved
because all items shift together -- so no re-routing is needed.  It makes the
interactive viewer (KiCanvas, which fits its camera to the whole sheet) show
the board filling and centered in the frame instead of tiny in an A4 page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_page_fit(
    pcb_path: Path,
    margin: float = 5.0,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
) -> int:
    """Resize a PCB page to fit the board with a uniform margin.

    Args:
        pcb_path: Path to .kicad_pcb file.
        margin: Margin around the board in mm (default 5.0).
        dry_run: Preview the new page size without modifying the file.
        output_path: Alternative output path for the modified PCB.
        output_format: "text" or "json".

    Returns:
        Exit code (0 for success, 1 for errors).
    """
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        _print_error(f"Failed to load PCB: {e}", output_format)
        return 1

    old_w, old_h = pcb.board_size

    if dry_run:
        bbox = pcb._edge_cuts_bbox_sexp()
        if bbox is None:
            _print_error(
                "page_fit() requires an Edge.Cuts board outline; none found.",
                output_format,
            )
            return 1
        min_x, min_y, max_x, max_y = bbox
        new_w = round((max_x - min_x) + 2 * margin, 6)
        new_h = round((max_y - min_y) + 2 * margin, 6)
    else:
        try:
            new_w, new_h = pcb.page_fit(margin=margin)
        except ValueError as e:
            _print_error(str(e), output_format)
            return 1

        save_path = output_path or pcb_path
        try:
            pcb.save(save_path)
        except Exception as e:
            _print_error(f"Failed to save PCB: {e}", output_format)
            return 1

    result = {
        "pcb": str(pcb_path),
        "dry_run": dry_run,
        "margin": margin,
        "board_size": [round(old_w, 6), round(old_h, 6)],
        "new_page": [new_w, new_h],
        "output": str(output_path or pcb_path) if not dry_run else None,
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        label = "PCB Page Fit (dry run)" if dry_run else "PCB Page Fit"
        print(label)
        print(f"  PCB: {pcb_path}")
        print(f"  Board size: {old_w} x {old_h} mm")
        print(f"  Margin: {margin} mm")
        print(f"  New page (User): {new_w} x {new_h} mm")
        if dry_run:
            print("  Would resize page and center board")
        else:
            print(f"  Saved to: {result['output']}")

    return 0


def _print_error(message: str, output_format: str) -> None:
    """Print an error in the appropriate format."""
    if output_format == "json":
        print(json.dumps({"error": message}, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
