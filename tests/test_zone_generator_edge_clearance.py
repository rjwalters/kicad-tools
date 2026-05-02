"""Tests for ZoneGenerator edge clearance inset behaviour.

Verifies that ``ZoneGenerator`` correctly insets the auto-derived board
outline when ``edge_clearance`` is set, and leaves explicit boundaries
untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Edge clearance inset requires shapely (optional dependency)
shapely = pytest.importorskip("shapely", reason="shapely required for edge clearance tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PCB_TEMPLATE = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "TestLib:TestPkg" (layer "F.Cu") (at 25 25)
    (pad "1" smd roundrect (at 0 0) (size 1.0 1.3)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "GND"))
    (pad "2" smd roundrect (at 2 0) (size 1.0 1.3)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "SIG"))
  )
  (gr_line (start 0 0) (end 100 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 100 0) (end 100 100) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 100 100) (end 0 100) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 0 100) (end 0 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)
"""


def _write_pcb(tmp_path: Path) -> Path:
    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text(_PCB_TEMPLATE)
    return pcb_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestZoneGeneratorEdgeClearance:
    """Tests for edge_clearance inset in ZoneGenerator."""

    def test_inset_applied_to_auto_boundary(self, tmp_path: Path):
        """board_outline is inset when edge_clearance is set."""
        from kicad_tools.zones.generator import ZoneGenerator

        pcb_path = _write_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(pcb_path, edge_clearance=0.5)

        outline = gen.board_outline

        # All coordinates must be at least 0.5mm from 0 and 100
        for x, y in outline:
            assert x >= 0.5 - 0.01, f"X={x} too close to left edge"
            assert x <= 99.5 + 0.01, f"X={x} too close to right edge"
            assert y >= 0.5 - 0.01, f"Y={y} too close to top edge"
            assert y <= 99.5 + 0.01, f"Y={y} too close to bottom edge"

    def test_no_inset_without_edge_clearance(self, tmp_path: Path):
        """board_outline matches board edge when no edge_clearance is set."""
        from kicad_tools.zones.generator import ZoneGenerator

        pcb_path = _write_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(pcb_path)

        outline = gen.board_outline
        xs = [x for x, _ in outline]
        ys = [y for _, y in outline]

        # Should reach the board edge (0 and 100)
        assert min(xs) <= 0.01
        assert max(xs) >= 99.99
        assert min(ys) <= 0.01
        assert max(ys) >= 99.99

    def test_no_inset_with_zero_clearance(self, tmp_path: Path):
        """edge_clearance=0 behaves like no clearance (no inset)."""
        from kicad_tools.zones.generator import ZoneGenerator

        pcb_path = _write_pcb(tmp_path)
        gen_zero = ZoneGenerator.from_pcb(pcb_path, edge_clearance=0)
        gen_none = ZoneGenerator.from_pcb(pcb_path, edge_clearance=None)

        assert gen_zero.board_outline == gen_none.board_outline

    def test_explicit_boundary_not_modified(self, tmp_path: Path):
        """Zones with explicit boundary are not affected by edge_clearance."""
        from kicad_tools.zones.generator import ZoneGenerator

        pcb_path = _write_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(pcb_path, edge_clearance=5.0)

        custom_boundary = [(0, 0), (100, 0), (100, 100), (0, 100)]
        zone = gen.add_zone(
            net="GND", layer="B.Cu", boundary=custom_boundary
        )

        # The explicit boundary should be used as-is, not inset
        assert zone.boundary == custom_boundary

    def test_add_zone_uses_inset_outline(self, tmp_path: Path):
        """add_zone() without boundary uses the inset board outline."""
        from kicad_tools.zones.generator import ZoneGenerator

        pcb_path = _write_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(pcb_path, edge_clearance=1.0)

        zone = gen.add_zone(net="GND", layer="B.Cu")

        # Zone boundary should be the inset outline
        for x, y in zone.boundary:
            assert x >= 1.0 - 0.01, f"X={x} too close to left edge"
            assert x <= 99.0 + 0.01, f"X={x} too close to right edge"
            assert y >= 1.0 - 0.01, f"Y={y} too close to top edge"
            assert y <= 99.0 + 0.01, f"Y={y} too close to bottom edge"


class TestInsetPolygon:
    """Unit tests for the static _inset_polygon helper."""

    def test_square_inset(self):
        """Insetting a square produces a smaller square."""
        from kicad_tools.zones.generator import ZoneGenerator

        coords = [(0, 0), (10, 0), (10, 10), (0, 10)]
        result = ZoneGenerator._inset_polygon(coords, 1.0)

        xs = [x for x, _ in result]
        ys = [y for _, y in result]

        assert min(xs) >= 1.0 - 0.01
        assert max(xs) <= 9.0 + 0.01
        assert min(ys) >= 1.0 - 0.01
        assert max(ys) <= 9.0 + 0.01

    def test_collapsed_polygon_returns_original(self):
        """If inset collapses the polygon, original coords are returned."""
        from kicad_tools.zones.generator import ZoneGenerator

        # Very thin rectangle that will collapse with 5mm inset
        coords = [(0, 0), (1, 0), (1, 100), (0, 100)]
        result = ZoneGenerator._inset_polygon(coords, 5.0)

        # Should return original since inset collapses the polygon
        assert result == coords
