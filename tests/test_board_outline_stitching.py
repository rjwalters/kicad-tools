"""Regression tests for Edge.Cuts outline stitching (issue #4948).

``kct placement check`` (the off-board placement-validity gate also invoked
from ``kct route``'s ``--allow-offboard`` pre-check) reported nearly every
footprint on the STRF benchmark board as "courtyard fully outside Edge.Cuts
outline" even though the footprints sit well inside the board's real outline.

Root cause: ``PCB.get_board_outline()`` chains ``gr_line``/``gr_arc`` segments
into a closed polygon by matching endpoints within a fixed tolerance,
starting from whichever segment happens to be first in file order. Two
independent problems combined to produce the bug:

1. The endpoint-matching tolerance (1 micron) was tighter than the sub-DRC
   gaps real-world KiCad exports commonly leave between segments that are
   otherwise visually and electrically continuous (STRF had a ~1.5 micron
   gap between two outline segments). The very first segment then looked
   "isolated" and the chain terminated after two points -- a degenerate
   sliver instead of the closed outline.
2. The Edge.Cuts layer can carry more than one closed contour (the real
   board edge plus mounting holes, fiducials, or other small shapes).
   Naively starting from ``segments[0]`` risks assembling the wrong
   (smaller) closed loop instead of the actual board edge.

These tests build a synthetic rounded-rectangle Edge.Cuts outline out of a
mix of ``gr_line`` and ``gr_arc`` elements, deliberately stored out of
end-to-end path order, with one endpoint gap that mirrors STRF's, alongside
an unrelated smaller closed contour (a "mounting hole" square) on the same
layer -- exercising both failure modes at once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.placement import ConflictType, PlacementAnalyzer
from kicad_tools.schema.pcb import PCB

# Rounded rectangle outline, sheet-absolute coordinates, bounding box
# (50, 50) - (90, 70), corner radius 3mm. Corners are gr_arc elements
# (modern start/mid/end encoding); straight sides are gr_line elements.
# Board-relative bbox is therefore (0, 0) - (40, 20) once the origin (the
# min of all straight-line endpoints, i.e. (50, 50)) is detected.
#
#   TL arc: (50,53) -> mid (50.879,50.879) -> (53,50)
#   top line:   (53,50) -> (87,50)
#   TR arc: (87,50) -> mid (89.121,50.879) -> (90,53)
#   right line: (90,53.0015) -> (90,67)   <- 1.5um gap from TR arc's end,
#                                             mirrors the real STRF gap
#   BR arc: (90,67) -> mid (89.121,69.121) -> (87,70)
#   bottom line:(87,70) -> (53,70)
#   BL arc: (53,70) -> mid (50.879,69.121) -> (50,67)
#   left line:  (50,67) -> (50,53)
#
# Elements below are deliberately NOT stored in path order, and a small
# unrelated closed contour (a "mounting hole" square, bbox (60,58)-(62,60),
# area 4mm^2 vs. the outline's ~800mm^2 bbox) is interleaved among them.
_ROUNDED_RECT_PCB_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (gr_line (start 60 58) (end 62 58) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 90 53.0015) (end 90 67) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_arc (start 53 70) (mid 50.879 69.121) (end 50 67) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 53 50) (end 87 50) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_arc (start 87 50) (mid 89.121 50.879) (end 90 53) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 62 58) (end 62 60) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 67) (end 50 53) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_arc (start 90 67) (mid 89.121 69.121) (end 87 70) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 62 60) (end 60 60) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 87 70) (end 53 70) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_arc (start 50 53) (mid 50.879 50.879) (end 53 50) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 60 60) (end 60 58) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{suffix}")
    (at {fx} {fy})
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""


def _rounded_rect_pcb(footprint_at: tuple[float, float], suffix: str = "10") -> str:
    fx, fy = footprint_at
    return _ROUNDED_RECT_PCB_TEMPLATE.format(fx=fx, fy=fy, suffix=suffix)


@pytest.fixture
def rounded_rect_pcb(tmp_path: Path) -> Path:
    """Rounded-rect board with a footprint at its center (well inside)."""
    pcb_file = tmp_path / "rounded_rect.kicad_pcb"
    pcb_file.write_text(_rounded_rect_pcb((70, 60)))
    return pcb_file


class TestGetBoardOutlineMixedLineArc:
    def test_chains_full_closed_loop_not_a_sliver(self, rounded_rect_pcb: Path):
        """The assembled outline must span the whole rounded rect, not a 2-point sliver."""
        pcb = PCB.load(str(rounded_rect_pcb))
        outline = pcb.get_board_outline()

        # The pre-fix chain terminated after the seed segment (2 points) the
        # moment its neighbor's endpoint fell 1.5um outside the 1um
        # tolerance -- reproducing the STRF sliver bug exactly.
        assert len(outline) > 2, (
            f"outline has only {len(outline)} point(s) -- looks like the "
            "degenerate sliver from issue #4948, not a closed polygon"
        )

        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)

        # Board-relative bbox should be ~(0,0)-(40,20) -- the full rounded
        # rectangle -- not the ~10mm x ~0.1mm sliver the bug produced, and
        # not the 2mm x 2mm mounting hole.
        assert width == pytest.approx(40.0, abs=0.1)
        assert height == pytest.approx(20.0, abs=0.1)

    def test_ignores_smaller_unrelated_contour(self, rounded_rect_pcb: Path):
        """The mounting-hole square must not be selected over the real outline."""
        pcb = PCB.load(str(rounded_rect_pcb))
        outline = pcb.get_board_outline()

        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))

        # Real outline bbox area ~= 40 * 20 = 800; mounting hole ~= 2 * 2 = 4.
        assert area > 100.0

    def test_handles_out_of_order_segments_regardless_of_seed(self, tmp_path: Path):
        """Reordering which element is first in the file must not change the result.

        Rebuilds the fixture with the segment list reversed (a different
        seed for the greedy chain) to confirm the outline doesn't depend on
        file order.
        """
        original = _rounded_rect_pcb((70, 60))
        lines = original.splitlines()
        edge_idx = [i for i, line in enumerate(lines) if "Edge.Cuts" in line]
        edge_lines = [lines[i] for i in edge_idx]
        reordered_edges = list(reversed(edge_lines))
        for pos, i in enumerate(edge_idx):
            lines[i] = reordered_edges[pos]
        reordered_content = "\n".join(lines)

        pcb_file = tmp_path / "reordered.kicad_pcb"
        pcb_file.write_text(reordered_content)

        pcb = PCB.load(str(pcb_file))
        outline = pcb.get_board_outline()
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        assert max(xs) - min(xs) == pytest.approx(40.0, abs=0.1)
        assert max(ys) - min(ys) == pytest.approx(20.0, abs=0.1)


class TestPlacementAnalyzerRoundedOutline:
    def test_footprint_inside_rounded_outline_is_not_off_board(self, rounded_rect_pcb: Path):
        """A footprint well inside the real outline must not be flagged OFF_BOARD.

        Before the fix, the sliver bbox left almost every footprint on a
        rounded-corner board "fully outside" the (bogus) outline.
        """
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(rounded_rect_pcb)

        off_board = [c for c in conflicts if c.type == ConflictType.OFF_BOARD]
        assert not off_board, f"unexpected OFF_BOARD conflicts: {off_board}"
