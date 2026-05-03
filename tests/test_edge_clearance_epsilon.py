"""Tests for floating-point epsilon tolerance in edge clearance DRC.

Verifies that the EdgeClearanceRule comparisons use a tolerance so that
distances at *exactly* the minimum clearance (or within floating-point
rounding) do not produce false-positive violations.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_tools.validate.rules.edge import EdgeClearanceRule, _CLEARANCE_EPSILON_MM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _outline_segments() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A simple 50x30 rectangular board outline centred at (25, 15).

    Segments returned as (start, end) tuples forming the rectangle.
    """
    return [
        ((0.0, 0.0), (50.0, 0.0)),   # bottom edge
        ((50.0, 0.0), (50.0, 30.0)),  # right edge
        ((50.0, 30.0), (0.0, 30.0)),  # top edge
        ((0.0, 30.0), (0.0, 0.0)),    # left edge
    ]


def _design_rules(
    copper_edge: float = 0.3,
    hole_edge: float = 0.5,
) -> SimpleNamespace:
    """Minimal design-rules object with the two edge clearance attributes."""
    return SimpleNamespace(
        min_copper_to_edge_mm=copper_edge,
        min_hole_to_edge_mm=hole_edge,
    )


def _make_pcb(
    *,
    segments=None,
    vias=None,
    footprints=None,
    zones=None,
) -> MagicMock:
    """Create a lightweight mock PCB with the attributes EdgeClearanceRule reads."""
    pcb = MagicMock()
    pcb.segments = segments or []
    pcb.vias = vias or []
    pcb.footprints = footprints or []
    pcb.zones = zones or []
    pcb.get_board_outline_segments.return_value = _outline_segments()
    pcb.board_origin = (0.0, 0.0)
    return pcb


# ---------------------------------------------------------------------------
# Tests: _CLEARANCE_EPSILON_MM constant
# ---------------------------------------------------------------------------

class TestEpsilonConstant:
    """Sanity-checks on the epsilon constant itself."""

    def test_epsilon_is_positive(self):
        assert _CLEARANCE_EPSILON_MM > 0

    def test_epsilon_is_sub_micron(self):
        """Epsilon must be much smaller than any real clearance."""
        assert _CLEARANCE_EPSILON_MM < 0.001  # less than 1 micron


# ---------------------------------------------------------------------------
# Tests: _check_segments
# ---------------------------------------------------------------------------

class TestCheckSegmentsEpsilon:
    """Edge clearance for trace segments with boundary-exact distances."""

    def _segment(self, start, end, width=0.2):
        return SimpleNamespace(
            start=start, end=end, width=width, layer="F.Cu", net_number=1,
        )

    def test_segment_at_exact_clearance_passes(self):
        """Distance == min_clearance must NOT be a violation."""
        min_clearance = 0.3
        # Place segment centerline at distance = min_clearance + half_width
        # from the left edge (x=0).  So actual_clearance == min_clearance.
        half_width = 0.1
        x = min_clearance + half_width  # 0.4
        seg = self._segment(start=(x, 15.0), end=(x, 16.0), width=half_width * 2)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_segments(
            _make_pcb(segments=[seg]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0

    def test_segment_slightly_below_epsilon_passes(self):
        """Distance just under min_clearance by less than epsilon must pass."""
        min_clearance = 0.3
        half_width = 0.1
        # Actual clearance = min_clearance - (epsilon / 2), should pass
        x = min_clearance + half_width - (_CLEARANCE_EPSILON_MM / 2)
        seg = self._segment(start=(x, 15.0), end=(x, 16.0), width=half_width * 2)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_segments(
            _make_pcb(segments=[seg]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0

    def test_segment_well_below_clearance_fails(self):
        """Distance clearly below min_clearance must be a violation."""
        min_clearance = 0.3
        half_width = 0.1
        # Actual clearance = 0.25 (well below 0.3)
        x = 0.25 + half_width  # 0.35
        seg = self._segment(start=(x, 15.0), end=(x, 16.0), width=half_width * 2)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_segments(
            _make_pcb(segments=[seg]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) > 0


# ---------------------------------------------------------------------------
# Tests: _check_vias
# ---------------------------------------------------------------------------

class TestCheckViasEpsilon:
    """Edge clearance for vias with boundary-exact distances."""

    def _via(self, position, size=0.6, drill=0.3):
        return SimpleNamespace(
            position=position, size=size, drill=drill,
            layers=["F.Cu", "B.Cu"], net_number=1,
        )

    def test_via_at_exact_clearance_passes(self):
        min_clearance = 0.5
        half_size = 0.3
        x = min_clearance + half_size
        via = self._via(position=(x, 15.0), size=half_size * 2)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_vias(
            _make_pcb(vias=[via]),
            _outline_segments(),
            _design_rules(hole_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0

    def test_via_below_clearance_by_2x_epsilon_fails(self):
        min_clearance = 0.5
        half_size = 0.3
        x = min_clearance + half_size - 2 * _CLEARANCE_EPSILON_MM
        via = self._via(position=(x, 15.0), size=half_size * 2)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_vias(
            _make_pcb(vias=[via]),
            _outline_segments(),
            _design_rules(hole_edge=min_clearance),
            results,
        )
        assert len(results.errors) > 0


# ---------------------------------------------------------------------------
# Tests: _check_pads
# ---------------------------------------------------------------------------

class TestCheckPadsEpsilon:
    """Edge clearance for pads with boundary-exact distances."""

    def _footprint_with_pad(self, pad_x, pad_size=0.8, pad_type="smd"):
        pad = SimpleNamespace(
            position=(0.0, 0.0), size=(pad_size, pad_size),
            type=pad_type, number="1",
        )
        fp = SimpleNamespace(
            position=(pad_x, 15.0), rotation=0.0,
            layer="F.Cu", reference="U1", pads=[pad],
        )
        return fp

    def test_pad_at_exact_clearance_passes(self):
        min_clearance = 0.3
        half_size = 0.4
        # Pad center at x = min_clearance + half_size from left edge
        fp = self._footprint_with_pad(min_clearance + half_size)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_pads(
            _make_pcb(footprints=[fp]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0

    def test_pad_well_below_clearance_fails(self):
        min_clearance = 0.3
        half_size = 0.4
        # Actual clearance ~ 0.1 (well below 0.3)
        fp = self._footprint_with_pad(0.1 + half_size)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_pads(
            _make_pcb(footprints=[fp]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) > 0


# ---------------------------------------------------------------------------
# Tests: _check_zones
# ---------------------------------------------------------------------------

class TestCheckZonesEpsilon:
    """Edge clearance for zones with boundary-exact distances."""

    def _zone(self, polygon):
        return SimpleNamespace(
            polygon=polygon,
            filled_polygons=[polygon],
            layer="F.Cu",
            net_number=1,
            net_name="GND",
        )

    def test_zone_at_exact_clearance_passes(self):
        """Zone vertex at exactly min_clearance from edge must pass."""
        min_clearance = 0.3
        # Inset rectangle: each vertex is exactly 0.3mm from board edge
        inset = min_clearance
        polygon = [
            (inset, inset),
            (50.0 - inset, inset),
            (50.0 - inset, 30.0 - inset),
            (inset, 30.0 - inset),
        ]
        zone = self._zone(polygon)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_zones(
            _make_pcb(zones=[zone]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0

    def test_zone_slightly_inside_epsilon_passes(self):
        """Zone vertex at min_clearance - epsilon/2 must still pass."""
        min_clearance = 0.3
        inset = min_clearance - _CLEARANCE_EPSILON_MM / 2
        polygon = [
            (inset, inset),
            (50.0 - inset, inset),
            (50.0 - inset, 30.0 - inset),
            (inset, 30.0 - inset),
        ]
        zone = self._zone(polygon)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_zones(
            _make_pcb(zones=[zone]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0

    def test_zone_well_below_clearance_fails(self):
        """Zone vertex at 0.1mm from edge (min 0.3mm) must fail."""
        min_clearance = 0.3
        inset = 0.1  # well below 0.3
        polygon = [
            (inset, inset),
            (50.0 - inset, inset),
            (50.0 - inset, 30.0 - inset),
            (inset, 30.0 - inset),
        ]
        zone = self._zone(polygon)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_zones(
            _make_pcb(zones=[zone]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) > 0

    def test_zone_above_clearance_passes(self):
        """Zone vertex well above min_clearance must pass."""
        min_clearance = 0.3
        inset = 1.0  # well above 0.3
        polygon = [
            (inset, inset),
            (50.0 - inset, inset),
            (50.0 - inset, 30.0 - inset),
            (inset, 30.0 - inset),
        ]
        zone = self._zone(polygon)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults
        results = DRCResults()
        rule._check_zones(
            _make_pcb(zones=[zone]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )
        assert len(results.errors) == 0


# ---------------------------------------------------------------------------
# Tests: all four methods use epsilon (code-level verification)
# ---------------------------------------------------------------------------

class TestAllMethodsUseEpsilon:
    """Verify that the source code of all four check methods references epsilon."""

    def test_check_segments_uses_epsilon(self):
        import inspect
        src = inspect.getsource(EdgeClearanceRule._check_segments)
        assert "_CLEARANCE_EPSILON_MM" in src

    def test_check_vias_uses_epsilon(self):
        import inspect
        src = inspect.getsource(EdgeClearanceRule._check_vias)
        assert "_CLEARANCE_EPSILON_MM" in src

    def test_check_pads_uses_epsilon(self):
        import inspect
        src = inspect.getsource(EdgeClearanceRule._check_pads)
        assert "_CLEARANCE_EPSILON_MM" in src

    def test_check_zones_uses_epsilon(self):
        import inspect
        src = inspect.getsource(EdgeClearanceRule._check_zones)
        assert "_CLEARANCE_EPSILON_MM" in src
