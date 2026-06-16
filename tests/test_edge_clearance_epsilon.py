"""Tests for floating-point epsilon tolerance in edge clearance DRC.

Verifies that the EdgeClearanceRule comparisons use a tolerance so that
distances at *exactly* the minimum clearance (or within floating-point
rounding) do not produce false-positive violations.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_tools.validate.rules.edge import _CLEARANCE_EPSILON_MM, EdgeClearanceRule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outline_segments() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A simple 50x30 rectangular board outline centred at (25, 15).

    Segments returned as (start, end) tuples forming the rectangle.
    """
    return [
        ((0.0, 0.0), (50.0, 0.0)),  # bottom edge
        ((50.0, 0.0), (50.0, 30.0)),  # right edge
        ((50.0, 30.0), (0.0, 30.0)),  # top edge
        ((0.0, 30.0), (0.0, 0.0)),  # left edge
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
            start=start,
            end=end,
            width=width,
            layer="F.Cu",
            net_number=1,
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
            position=position,
            size=size,
            drill=drill,
            layers=["F.Cu", "B.Cu"],
            net_number=1,
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
            position=(0.0, 0.0),
            size=(pad_size, pad_size),
            type=pad_type,
            number="1",
        )
        fp = SimpleNamespace(
            position=(pad_x, 15.0),
            rotation=0.0,
            layer="F.Cu",
            reference="U1",
            pads=[pad],
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
# Tests: _check_pads forward-rotation sign convention (issue #2788)
# ---------------------------------------------------------------------------


class TestCheckPadsRotationSignConvention:
    """Verify that _check_pads applies KiCad's negated-angle pad rotation.

    Regression coverage for issue #3739 (which OVERTURNED #2788/#738):
    KiCad's ``pcbnew`` 10.0.1 applies the footprint orientation as the
    NEGATED angle (``math.radians(-footprint.rotation)``) relative to
    standard CCW math.  Pure 0°/180° tests pass under BOTH sign
    conventions, so the parametrization MUST include {45°, 90°, 270°} to
    catch a divergence.

    The fixture uses ``board_origin = (0, 0)`` to isolate rotation from
    any origin-offset asymmetries, and the footprint is placed in the
    interior of the board with a pad offset chosen so the two sign
    conventions land the pad on opposite sides of the edge threshold.
    """

    @staticmethod
    def _footprint_with_offset_pad(
        fp_position: tuple[float, float],
        rotation: float,
        pad_local: tuple[float, float],
        pad_size: float = 0.4,
    ) -> SimpleNamespace:
        pad = SimpleNamespace(
            position=pad_local,
            size=(pad_size, pad_size),
            type="smd",
            number="1",
        )
        return SimpleNamespace(
            position=fp_position,
            rotation=rotation,
            layer="F.Cu",
            reference="U1",
            pads=[pad],
        )

    @staticmethod
    def _expected_pad_position(
        fp_position: tuple[float, float],
        rotation_deg: float,
        pad_local: tuple[float, float],
    ) -> tuple[float, float]:
        """KiCad forward transform (negated angle, matches schema/pcb.py)."""
        import math

        rot_rad = math.radians(-rotation_deg)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        px, py = pad_local
        return (
            fp_position[0] + px * cos_r - py * sin_r,
            fp_position[1] + px * sin_r + py * cos_r,
        )

    def test_negative_control_buggy_vs_canonical_differ(self):
        """Sanity check: the fixture coordinates DO differ between the two
        sign conventions at each chosen rotation, so the parametrized
        tests below can actually distinguish a correct fix from the bug.
        """
        import math

        pad_local = (3.0, 1.0)
        fp_pos = (10.0, 15.0)

        for rot in (45.0, 90.0, 270.0):
            rot_rad = math.radians(-rot)  # KiCad negated-angle convention
            cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
            correct = (
                fp_pos[0] + pad_local[0] * cos_r - pad_local[1] * sin_r,
                fp_pos[1] + pad_local[0] * sin_r + pad_local[1] * cos_r,
            )
            # Buggy (un-negated standard CCW, PR #738) → sin flipped sign
            buggy = (
                fp_pos[0] + pad_local[0] * cos_r + pad_local[1] * sin_r,
                fp_pos[1] - pad_local[0] * sin_r + pad_local[1] * cos_r,
            )
            # At least one coordinate must differ by a meaningful amount
            dx = abs(correct[0] - buggy[0])
            dy = abs(correct[1] - buggy[1])
            assert max(dx, dy) > 0.5, (
                f"rotation={rot}°: fixture too symmetric — correct={correct}, "
                f"buggy={buggy}; choose pad_local with nonzero, distinct "
                f"x/y components"
            )

    # Per-rotation fixtures: footprint x-position chosen so that, under
    # the CANONICAL KiCad (negated-angle) transform, the pad lands close
    # enough to the left board edge (x=0) to violate (or not violate) the
    # 0.3mm copper-to-edge minimum, while under the OLD un-negated CCW
    # rotation (PR #738) the pad lands with the OPPOSITE violation status.
    # This makes the violation *count* a clean discriminator between the
    # two sign conventions.
    #
    # pad_local = (3.0, 1.0) — nonzero, distinct x/y so 0°/180°
    # symmetries do not paper over the divergence.
    #
    # Pre-computed pad positions under the KiCad (negated) oracle
    # (the negative-control test proves the two conventions diverge):
    #   rot=45°,  fp_x=-1.9: kicad pad_x ~  0.928 (clears),
    #                        old-ccw   pad_x ~ -0.486 (violates)
    #   rot=90°,  fp_x= 0.6: kicad pad_x =  1.6   (clears),
    #                        old-ccw   pad_x = -0.4   (violates)
    #   rot=270°, fp_x= 0.6: kicad pad_x = -0.4   (violates),
    #                        old-ccw   pad_x =  1.6   (clears)
    @pytest.mark.parametrize(
        "rotation, fp_x, expect_canonical_violation",
        [
            (45.0, -1.9, False),
            (90.0, 0.6, False),
            (270.0, 0.6, True),
        ],
    )
    def test_violation_count_matches_canonical_convention(
        self, rotation, fp_x, expect_canonical_violation
    ):
        """Drive ``_check_pads`` with per-rotation fixtures where the
        canonical (CCW-positive) and buggy (negated) sign conventions
        disagree on whether the pad violates edge clearance.

        Under the bug, two of these three parametrized cases fail
        (violation count does not match canonical expectation).  Under
        the correct fix, all three pass.
        """
        min_clearance = 0.3
        pad_local = (3.0, 1.0)
        fp_pos = (fp_x, 15.0)

        fp = self._footprint_with_offset_pad(fp_pos, rotation, pad_local)
        expected_pad = self._expected_pad_position(fp_pos, rotation, pad_local)

        rule = EdgeClearanceRule()
        from kicad_tools.validate.violations import DRCResults

        results = DRCResults()
        rule._check_pads(
            _make_pcb(footprints=[fp]),
            _outline_segments(),
            _design_rules(copper_edge=min_clearance),
            results,
        )

        if expect_canonical_violation:
            assert len(results.errors) >= 1, (
                f"rotation={rotation} fp_x={fp_x}: expected canonical "
                f"violation at pad={expected_pad}, but rule reported "
                f"{len(results.errors)} errors -- likely the rule still "
                f"uses the buggy negated rotation."
            )
            # The reported violation location must match the canonical
            # pad world position to 1e-9.
            v = results.errors[0]
            loc_x, loc_y = v.location
            assert abs(loc_x - expected_pad[0]) < 1e-9, (
                f"rotation={rotation}: violation x={loc_x} differs "
                f"from canonical {expected_pad[0]}."
            )
            assert abs(loc_y - expected_pad[1]) < 1e-9, (
                f"rotation={rotation}: violation y={loc_y} differs "
                f"from canonical {expected_pad[1]}."
            )
        else:
            assert len(results.errors) == 0, (
                f"rotation={rotation} fp_x={fp_x}: canonical pad at "
                f"{expected_pad} is well inside the board (no violation "
                f"expected), but rule reported {len(results.errors)} "
                f"errors -- likely the rule still uses the buggy "
                f"negated rotation, placing the pad off-board."
            )


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
