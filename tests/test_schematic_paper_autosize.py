"""Tests for schematic paper auto-sizing (issue #3530).

The softstart schematic declared A4 (297x210 mm) while its content
extended past x=560 mm — every faithful render (kicad-cli SVG/PDF
export, printing) silently clipped half the schematic.  ``Schematic``
now escalates the declared paper along the A4->A0 ladder at write time
so content always fits its sheet.
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.schematic.models.paper import (
    PAPER_SIZES_MM,
    paper_dimensions,
    select_paper_for_extent,
)
from kicad_tools.schematic.models.schematic import Schematic

# ---------------------------------------------------------------------------
# paper module unit tests
# ---------------------------------------------------------------------------


class TestPaperDimensions:
    def test_landscape_sizes(self):
        assert paper_dimensions("A4") == (297.0, 210.0)
        assert paper_dimensions("A2") == (594.0, 420.0)
        assert paper_dimensions("A0") == (1189.0, 841.0)

    def test_portrait_swaps_dimensions(self):
        assert paper_dimensions("A4 portrait") == (210.0, 297.0)

    def test_unknown_paper_returns_none(self):
        assert paper_dimensions("User") is None
        assert paper_dimensions("USLetter") is None
        assert paper_dimensions("") is None


class TestSelectPaperForExtent:
    def test_small_content_stays_a4(self):
        assert select_paper_for_extent(200, 150) == "A4"

    def test_a4_overflow_escalates_to_a3(self):
        # 297 wide does not fit A4 with the 10 mm margin
        assert select_paper_for_extent(297, 150) == "A3"

    def test_softstart_class_extent_needs_a2(self):
        # The issue's original measurement: symbols to x=560, y=279
        assert select_paper_for_extent(560, 279) == "A2"

    def test_label_stub_extent_needs_a0(self):
        # The full softstart extent including labels/wire stubs reaches
        # x=849.6 which exceeds even A1 (841 mm wide)
        assert select_paper_for_extent(849.63, 336.55) == "A0"

    def test_never_smaller_than_minimum(self):
        # Content would fit A4, but the declared sheet is A2 -> keep A2
        assert select_paper_for_extent(100, 100, minimum="A2") == "A2"

    def test_nothing_fits_returns_none(self):
        assert select_paper_for_extent(2000, 100) is None

    def test_unknown_minimum_scans_full_ladder(self):
        assert select_paper_for_extent(100, 100, minimum="A5") == "A4"

    def test_margin_respected(self):
        w, h = PAPER_SIZES_MM["A4"]
        assert select_paper_for_extent(w - 5, 100, margin=10.0) == "A3"
        assert select_paper_for_extent(w - 15, 100, margin=10.0) == "A4"


# ---------------------------------------------------------------------------
# Schematic integration tests
# ---------------------------------------------------------------------------


def _schematic_with_wire_to(x: float, y: float) -> Schematic:
    sch = Schematic("Paper autosize test")
    sch.add_wire((25.4, 25.4), (x, y))
    return sch


class TestSchematicAutoSize:
    def test_content_bounds_covers_wires_and_labels(self):
        sch = _schematic_with_wire_to(150, 100)
        sch.add_wire((150, 100), (200, 120))
        sch.add_label("FAR_LABEL", 200, 120)
        bounds = sch.content_bounds()
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        # Coordinates snap to the 2.54 mm grid, so compare approximately.
        assert min_x == pytest.approx(25.4, abs=2.6)
        assert min_y == pytest.approx(25.4, abs=2.6)
        assert max_x == pytest.approx(200, abs=2.6)
        assert max_y == pytest.approx(120, abs=2.6)

    def test_empty_schematic_has_no_bounds_and_keeps_paper(self):
        sch = Schematic("Empty")
        assert sch.content_bounds() is None
        assert sch.auto_size_paper() == "A4"

    def test_fitting_content_keeps_a4(self, tmp_path):
        sch = _schematic_with_wire_to(200, 150)
        out = tmp_path / "fits.kicad_sch"
        sch.write(out)
        assert '(paper "A4")' in out.read_text()

    def test_overflow_escalates_on_write(self, tmp_path):
        sch = _schematic_with_wire_to(560, 279)
        out = tmp_path / "overflow.kicad_sch"
        sch.write(out)
        assert sch.paper == "A2"
        assert '(paper "A2")' in out.read_text()

    def test_extreme_overflow_lands_on_a0(self, tmp_path):
        sch = _schematic_with_wire_to(849.63, 336.55)
        out = tmp_path / "wide.kicad_sch"
        sch.write(out)
        assert '(paper "A0")' in out.read_text()

    def test_auto_size_disabled_keeps_declared_paper(self, tmp_path, caplog):
        sch = _schematic_with_wire_to(560, 279)
        out = tmp_path / "verbatim.kicad_sch"
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            sch.write(out, auto_size_paper=False)
        assert '(paper "A4")' in out.read_text()
        assert any("overflows" in rec.message for rec in caplog.records)

    def test_escalation_logs_warning(self, caplog):
        sch = _schematic_with_wire_to(400, 200)
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            assert sch.auto_size_paper() == "A3"
        assert any("auto-sizing paper" in rec.message for rec in caplog.records)

    def test_declared_larger_sheet_never_shrinks(self):
        sch = Schematic("Big sheet", paper="A2")
        sch.add_wire((25.4, 25.4), (100, 100))
        assert sch.auto_size_paper() == "A2"

    def test_custom_paper_string_left_alone(self):
        sch = Schematic("Custom", paper="USLetter")
        sch.add_wire((25.4, 25.4), (800, 500))
        assert sch.auto_size_paper() == "USLetter"

    def test_negative_coordinates_warn(self, caplog):
        sch = Schematic("Negative")
        sch.add_wire((-20, 30), (100, 100))
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            sch.auto_size_paper()
        assert any("negative coordinates" in rec.message for rec in caplog.records)

    def test_beyond_a0_clamps_with_warning(self, caplog):
        sch = _schematic_with_wire_to(2000, 300)
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            assert sch.auto_size_paper() == "A0"
        assert any("exceeds even A0" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "extent,expected",
    [
        ((290, 230), "A3"),  # board 04 stm32-devboard
        ((373, 300), "A2"),  # board 05 bldc-motor-controller (y > A3's 297)
        ((190, 231), "A3"),  # boards 06/07 diffpair/matchgroup
    ],
)
def test_fleet_overflow_extents_get_fitting_sheets(extent, expected):
    """Fleet census (issue #3530): boards 04-07 also overflow A4."""
    assert select_paper_for_extent(*extent) == expected
