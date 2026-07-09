"""Tests for centering a PCB on its drawing sheet (kct pcb center-on-sheet).

Root cause covered: generated boards either sat at a fixed (100, 100) origin
on A4 (large boards overlap the title block) or on a tight (paper "User")
page (board hugs the frame corner).  The centering transform is a rigid,
grid-snapped translation of ALL geometry done with pure text editing and
exact decimal arithmetic, so routing / 45-degree copper / DRC are preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.pcb.center_sheet import (
    DEFAULT_FRAME_MARGIN_MM,
    DEFAULT_TITLE_BLOCK_MM,
    CenterReport,
    center_on_sheet,
    center_pcb_text,
    centered_origin,
    edge_cuts_bbox,
    select_paper,
    translate_pcb_text,
    usable_area,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_pcb(paper: str = '(paper "A4")', extra: str = "") -> str:
    """A minimal .kicad_pcb text with a 30x20 board at (100, 100)."""
    return f"""(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t{paper}
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(gr_rect
\t\t(start 100 100)
\t\t(end 130 120)
\t\t(layer "Edge.Cuts")
\t\t(width 0.1)
\t)
\t(footprint "Lib:FP"
\t\t(layer "F.Cu")
\t\t(at 105 110 90)
\t\t(pad "1" smd rect
\t\t\t(at -0.9125 0 90)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t)
\t)
\t(segment
\t\t(start 105 110)
\t\t(end 110.05 115.05)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 1)
\t)
\t(via
\t\t(at 110.05 115.05)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 1)
\t)
\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(layer "F.Cu")
\t\t(polygon
\t\t\t(pts
\t\t\t\t(xy 100 100)
\t\t\t\t(xy 130 100)
\t\t\t\t(xy 130 120)
\t\t\t\t(xy 100 120)
\t\t\t)
\t\t)
\t\t(filled_polygon
\t\t\t(layer "F.Cu")
\t\t\t(pts
\t\t\t\t(xy 100.25 100.25)
\t\t\t\t(xy 129.75 100.25)
\t\t\t\t(xy 129.75 119.75)
\t\t\t)
\t\t)
\t)
{extra})
"""


A4_W, A4_H = 297.0, 210.0
USABLE_CX = (DEFAULT_FRAME_MARGIN_MM + (A4_W - DEFAULT_FRAME_MARGIN_MM)) / 2  # 148.5
USABLE_CY = (
    DEFAULT_FRAME_MARGIN_MM + (A4_H - DEFAULT_FRAME_MARGIN_MM - DEFAULT_TITLE_BLOCK_MM)
) / 2  # 87.5


# --------------------------------------------------------------------------
# Sheet geometry helpers
# --------------------------------------------------------------------------


class TestSheetGeometry:
    def test_usable_area_a4(self):
        assert usable_area(297, 210) == (10.0, 10.0, 287.0, 165.0)

    def test_select_paper_small_board(self):
        assert select_paper(30, 25) == "A4"

    def test_select_paper_respects_title_block(self):
        # 130 mm tall board: A4 usable height is 155, slack (155-130)/2 < 15
        assert select_paper(100, 130) == "A3"

    def test_select_paper_huge_board_returns_none(self):
        assert select_paper(2000, 2000) is None

    def test_centered_origin_a4(self):
        # 30x20 board on A4: usable center (148.5, 87.5)
        assert centered_origin(30, 20) == (133.5, 77.5)

    def test_centered_origin_snaps_to_grid(self):
        ox, oy = centered_origin(33.333, 20.777)
        assert round(ox / 0.05, 6) == round(ox / 0.05)
        assert round(oy / 0.05, 6) == round(oy / 0.05)

    def test_centered_origin_unknown_paper(self):
        with pytest.raises(ValueError, match="Unknown paper"):
            centered_origin(30, 20, "B5")


# --------------------------------------------------------------------------
# Bounding box discovery
# --------------------------------------------------------------------------


class TestEdgeCutsBbox:
    def test_rect_outline(self):
        assert edge_cuts_bbox(make_pcb()) == (100.0, 100.0, 130.0, 120.0)

    def test_line_outline(self):
        text = """(kicad_pcb
\t(paper "A4")
\t(gr_line (start 10 20) (end 50 20) (layer "Edge.Cuts"))
\t(gr_line (start 50 20) (end 50 60) (layer "Edge.Cuts"))
\t(gr_line (start 10 21) (end 50 21) (layer "F.SilkS"))
)
"""
        assert edge_cuts_bbox(text) == (10.0, 20.0, 50.0, 60.0)

    def test_no_outline(self):
        text = '(kicad_pcb\n\t(paper "A4")\n)\n'
        assert edge_cuts_bbox(text) is None


# --------------------------------------------------------------------------
# Rigid translation exactness
# --------------------------------------------------------------------------


class TestTranslationExactness:
    def test_centers_bbox_in_usable_area(self):
        new_text, report = center_pcb_text(make_pcb())
        bbox = edge_cuts_bbox(new_text)
        assert bbox is not None
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        assert cx == pytest.approx(USABLE_CX, abs=0.025)
        assert cy == pytest.approx(USABLE_CY, abs=0.025)

    def test_delta_is_grid_snapped(self):
        _, report = center_pcb_text(make_pcb())
        assert round(report.dx_mm / 0.05, 6) == round(report.dx_mm / 0.05)
        assert round(report.dy_mm / 0.05, 6) == round(report.dy_mm / 0.05)

    def test_coordinates_are_exact_decimal_strings(self):
        # 30x20 board at (100,100) on A4: dx=33.5, dy=-22.5 exactly.
        new_text, report = center_pcb_text(make_pcb())
        assert (report.dx_mm, report.dy_mm) == (33.5, -22.5)
        # segment endpoints shifted exactly, no float noise
        assert "(start 138.5 87.5)" in new_text  # 105+33.5, 110-22.5
        assert "(end 143.55 92.55)" in new_text  # 110.05+33.5, 115.05-22.5

    def test_45_degree_segment_stays_45(self):
        new_text, _ = center_pcb_text(make_pcb())
        # the fixture segment runs (105,110)->(110.05,115.05): exact 45 deg
        assert "(start 138.5 87.5)" in new_text
        assert "(end 143.55 92.55)" in new_text
        # dx == dy still holds exactly in decimal (no float round-trip)
        from decimal import Decimal

        run = Decimal("143.55") - Decimal("138.5")
        rise = Decimal("92.55") - Decimal("87.5")
        assert run == rise == Decimal("5.05")

    def test_footprint_moves_but_pad_relative_coords_do_not(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert "(at 138.5 87.5 90)" in new_text  # footprint at, angle kept
        assert "(at -0.9125 0 90)" in new_text  # pad-relative at untouched

    def test_rotation_angle_preserved(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert new_text.count(" 90)") >= 2  # both (at ... 90) survive

    def test_zone_polygon_and_filled_polygon_translate(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert "(xy 133.5 77.5)" in new_text  # polygon vertex 100,100
        assert "(xy 133.75 77.75)" in new_text  # filled_polygon 100.25,100.25
        assert "(xy 163.25 97.25)" in new_text  # filled_polygon 129.75,119.75

    def test_only_coordinate_atoms_change(self):
        original = make_pcb()
        new_text, _ = center_pcb_text(original)
        # strip all digits: the non-numeric skeleton must be identical
        import re

        skel = re.compile(r"-?\d+(?:\.\d+)?")
        assert skel.sub("#", original) == skel.sub("#", new_text)

    def test_via_translates(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert "(at 143.55 92.55)" in new_text

    def test_gr_text_translates_but_string_content_untouched(self):
        extra = '\t(gr_text "at 100 100"\n\t\t(at 102 118)\n\t\t(layer "F.SilkS")\n\t)\n'
        new_text, _ = center_pcb_text(make_pcb(extra=extra))
        assert '"at 100 100"' in new_text  # quoted string untouched
        assert "(at 135.5 95.5)" in new_text  # gr_text position moved


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_is_noop(self):
        first, r1 = center_pcb_text(make_pcb())
        second, r2 = center_pcb_text(first)
        assert r1.changed
        assert not r2.changed
        assert second == first
        assert (r2.dx_mm, r2.dy_mm) == (0.0, 0.0)

    def test_file_roundtrip_idempotent(self, tmp_path: Path):
        p = tmp_path / "board.kicad_pcb"
        p.write_text(make_pcb())
        r1 = center_on_sheet(p)
        assert r1.changed
        blob = p.read_bytes()
        r2 = center_on_sheet(p)
        assert not r2.changed
        assert p.read_bytes() == blob


# --------------------------------------------------------------------------
# Paper handling
# --------------------------------------------------------------------------


class TestPaperHandling:
    def test_user_paper_switches_to_a4(self):
        text = make_pcb(paper='(paper "User" 40 30)')
        new_text, report = center_pcb_text(text)
        assert report.paper_before == "User"
        assert report.paper_after == "A4"
        assert '(paper "A4")' in new_text
        assert "User" not in new_text.split("\n")[3]

    def test_user_paper_large_board_picks_a3(self):
        # 130 mm tall board on a User sheet -> needs A3 for 15 mm slack
        text = """(kicad_pcb
\t(paper "User" 110 140)
\t(gr_rect (start 5 5) (end 105 135) (layer "Edge.Cuts"))
)
"""
        new_text, report = center_pcb_text(text)
        assert report.paper_after == "A3"
        assert '(paper "A3")' in new_text

    def test_standard_paper_kept_when_board_fits(self):
        _, report = center_pcb_text(make_pcb())
        assert report.paper_before == "A4"
        assert report.paper_after == "A4"
        assert not report.paper_changed

    def test_keep_refuses_oversized_board(self):
        text = """(kicad_pcb
\t(paper "User" 40 30)
\t(gr_rect (start 5 5) (end 35 25) (layer "Edge.Cuts"))
)
"""
        with pytest.raises(ValueError, match="does not fit"):
            center_pcb_text(text, paper="keep")

    def test_explicit_paper_forced(self):
        new_text, report = center_pcb_text(make_pcb(), paper="A3")
        assert report.paper_after == "A3"
        assert '(paper "A3")' in new_text

    def test_unknown_paper_rejected(self):
        with pytest.raises(ValueError, match="Unknown paper"):
            center_pcb_text(make_pcb(), paper="B5")

    def test_missing_outline_raises(self):
        with pytest.raises(ValueError, match="Edge.Cuts"):
            center_pcb_text('(kicad_pcb\n\t(paper "A4")\n)\n')


# --------------------------------------------------------------------------
# Title-block avoidance
# --------------------------------------------------------------------------


class TestTitleBlockAvoidance:
    def test_board_lands_above_title_block(self):
        # tall board: 100x130 on A3 (usable y: 10..252-35=252? no: 297h)
        text = """(kicad_pcb
\t(paper "A4")
\t(gr_rect (start 100 100) (end 200 195) (layer "Edge.Cuts"))
)
"""
        new_text, report = center_pcb_text(text)
        # A4 usable bottom edge: 210 - 10 - 35 = 165
        assert report.bbox_after[3] <= 165.0
        assert report.bbox_after[1] >= 10.0

    def test_report_usable_area_matches_constants(self):
        _, report = center_pcb_text(make_pcb())
        assert report.usable_area == (10.0, 10.0, 287.0, 165.0)

    def test_custom_title_block_height(self):
        _, report = center_pcb_text(make_pcb(), title_block=0.0)
        # without a title block the usable center is the frame center
        cy = (report.bbox_after[1] + report.bbox_after[3]) / 2
        assert cy == pytest.approx(105.0, abs=0.025)


# --------------------------------------------------------------------------
# Report / dataclass behavior
# --------------------------------------------------------------------------


class TestReport:
    def test_changed_flags(self):
        _, report = center_pcb_text(make_pcb())
        assert isinstance(report, CenterReport)
        assert report.changed
        assert not report.paper_changed

    def test_dry_run_does_not_write(self, tmp_path: Path):
        p = tmp_path / "board.kicad_pcb"
        p.write_text(make_pcb())
        blob = p.read_bytes()
        report = center_on_sheet(p, dry_run=True)
        assert report.changed
        assert p.read_bytes() == blob

    def test_output_path_leaves_input_untouched(self, tmp_path: Path):
        src = tmp_path / "in.kicad_pcb"
        dst = tmp_path / "out.kicad_pcb"
        src.write_text(make_pcb())
        blob = src.read_bytes()
        center_on_sheet(src, output_path=dst)
        assert src.read_bytes() == blob
        assert dst.exists()
        assert edge_cuts_bbox(dst.read_text()) == (133.5, 77.5, 163.5, 97.5)


# --------------------------------------------------------------------------
# translate_pcb_text (explicit-delta rigid translation; board-07 routing
# frame sandwich, PR #4015)
# --------------------------------------------------------------------------


class TestTranslatePcbText:
    def test_translates_all_geometry_by_explicit_delta(self):
        moved = translate_pcb_text(make_pcb(), 6.5, -60.0)
        assert edge_cuts_bbox(moved) == (106.5, 40.0, 136.5, 60.0)
        assert "(at 111.5 50 90)" in moved  # footprint (at ...) moved
        assert "(at -0.9125 0 90)" in moved  # pad-relative coords untouched
        assert "(start 111.5 50)" in moved  # segment
        assert "(xy 106.75 40.25)" in moved  # filled_polygon vertex

    def test_zero_delta_is_identity(self):
        text = make_pcb()
        assert translate_pcb_text(text, 0.0, 0.0) == text

    def test_round_trip_is_byte_exact(self):
        # The board-07 recipe relies on this: centering then un-centering
        # (and vice versa) must restore every coordinate exactly.
        text = make_pcb()
        there = translate_pcb_text(text, -6.5, -60.0)
        back = translate_pcb_text(there, 6.5, 60.0)
        assert back == text

    def test_delta_snaps_to_grid(self):
        # 6.51 snaps to 6.5 on the default 0.05 mm grid.
        assert translate_pcb_text(make_pcb(), 6.51, 0.0) == translate_pcb_text(make_pcb(), 6.5, 0.0)

    def test_paper_node_is_never_touched(self):
        moved = translate_pcb_text(make_pcb(), 10.0, 10.0)
        assert '(paper "A4")' in moved

    def test_45_degree_copper_stays_exact(self):
        moved = translate_pcb_text(make_pcb(), 3.35, -7.15)
        # fixture segment is exactly 45 deg: (105,110)->(110.05,115.05)
        assert "(start 108.35 102.85)" in moved
        assert "(end 113.4 107.9)" in moved
