"""Tests for the router-side drill hole-to-hole guard (Issue #3855).

The router places vias at three sites without historically enforcing the
manufacturer ``min_hole_to_hole`` (canonical 0.5 mm) drill-to-drill floor:

1. diff-pair fan-out crossover (``diffpair_routing.py``)
2. escape staggered via fan (``escape.py::_can_place_via``)
3. plane stitching (``stitch_cmd.py::calculate_via_position``)

Each now rejects a candidate via whose DRILL would sit within
``min_hole_to_hole`` (edge-to-edge) of any existing drill (via OR
through-hole pad, any net), reusing the canonical edge-to-edge formula
from the DRC ``hole_to_hole_clearance`` rule.
"""

from __future__ import annotations

import math

from kicad_tools.cli.stitch_cmd import PadInfo, calculate_via_position
from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.mfr_limits import get_mfr_limits
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import (
    DEFAULT_MIN_HOLE_TO_HOLE,
    drill_hole_to_hole_clear,
)


class TestDrillHoleToHoleClear:
    """The shared edge-to-edge predicate (canonical formula)."""

    def test_no_registry_is_noop(self) -> None:
        assert drill_hole_to_hole_clear(0.0, 0.0, 0.25, None) is True
        assert drill_hole_to_hole_clear(0.0, 0.0, 0.25, []) is True

    def test_far_drill_is_clear(self) -> None:
        # center 2.0mm, edge = 2.0 - 0.125 - 0.125 = 1.75 >= 0.5
        assert drill_hole_to_hole_clear(0.0, 0.0, 0.25, [(2.0, 0.0, 0.25)]) is True

    def test_near_drill_is_rejected(self) -> None:
        # center 0.6mm, edge = 0.6 - 0.125 - 0.125 = 0.35 < 0.5
        assert drill_hole_to_hole_clear(0.0, 0.0, 0.25, [(0.6, 0.0, 0.25)]) is False

    def test_exactly_at_floor_is_clear(self) -> None:
        # Want edge == 0.5 exactly -> center = 0.5 + 0.125 + 0.125 = 0.75
        assert drill_hole_to_hole_clear(0.0, 0.0, 0.25, [(0.75, 0.0, 0.25)]) is True

    def test_just_below_floor_is_rejected(self) -> None:
        # center 0.74 -> edge 0.49 < 0.5 (beyond the 1e-3 tolerance)
        assert drill_hole_to_hole_clear(0.0, 0.0, 0.25, [(0.74, 0.0, 0.25)]) is False

    def test_matches_drc_formula(self) -> None:
        """Pre-check must agree with the DRC edge-to-edge formula."""
        cand = (1.0, 1.0, 0.3)
        existing = (1.4, 1.0, 0.3)
        center = math.hypot(existing[0] - cand[0], existing[1] - cand[1])
        edge = center - cand[2] / 2 - existing[2] / 2
        expected_clear = edge + 1e-3 >= DEFAULT_MIN_HOLE_TO_HOLE
        assert drill_hole_to_hole_clear(cand[0], cand[1], cand[2], [existing]) is expected_clear

    def test_custom_min_hole_to_hole(self) -> None:
        # With a tiny floor, a near drill that the default rejects is allowed.
        assert (
            drill_hole_to_hole_clear(0.0, 0.0, 0.25, [(0.6, 0.0, 0.25)], min_hole_to_hole=0.1)
            is True
        )


class TestRulesThreadHoleToHole:
    """``DesignRules`` carries / resolves the hole-to-hole spec (Issue #3855)."""

    def test_default_is_conservative_half_mm(self) -> None:
        assert DesignRules().min_hole_to_hole == 0.5

    def test_distinct_from_min_drill_clearance(self) -> None:
        # The same-net via-merge threshold must remain the tiny legacy value.
        assert DesignRules().min_drill_clearance == 0.102

    def test_populated_from_manufacturer(self) -> None:
        rules = DesignRules(manufacturer="jlcpcb")
        assert rules.min_hole_to_hole == get_mfr_limits("jlcpcb").min_hole_to_hole

    def test_explicit_override_preserved(self) -> None:
        rules = DesignRules(manufacturer="jlcpcb", min_hole_to_hole=0.7)
        assert rules.min_hole_to_hole == 0.7

    def test_unknown_manufacturer_falls_back_to_default(self) -> None:
        rules = DesignRules(manufacturer="not-a-real-mfr")
        assert rules.min_hole_to_hole == 0.5


class TestMfrLimitsHoleToHole:
    def test_all_known_mfrs_carry_half_mm(self) -> None:
        for name in ("jlcpcb", "pcbway", "oshpark", "seeed"):
            assert get_mfr_limits(name).min_hole_to_hole == 0.5


def _escape_router() -> EscapeRouter:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.25,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer="jlcpcb",
    )
    grid = RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )
    return EscapeRouter(grid, rules)


class TestEscapeCanPlaceViaHoleToHole:
    """``_can_place_via`` rejects a candidate within ``min_hole_to_hole``
    of an existing drill, and is a no-op without a drill registry."""

    def test_no_existing_drills_is_back_compat(self) -> None:
        er = _escape_router()
        # No drill registry -> the hole-to-hole branch is skipped entirely.
        assert er._can_place_via(0.0, 0.0, net=1) is True

    def test_candidate_far_from_drill_is_placed(self) -> None:
        er = _escape_router()
        # existing drill 2mm away: edge = 2 - 0.125 - 0.15 = 1.725 >= 0.5
        assert (
            er._can_place_via(
                0.0,
                0.0,
                net=1,
                existing_drills=[(2.0, 0.0, 0.3)],
                via_drill=0.25,
            )
            is True
        )

    def test_candidate_too_close_to_drill_is_rejected(self) -> None:
        er = _escape_router()
        # existing drill 0.5mm away: edge = 0.5 - 0.125 - 0.15 = 0.225 < 0.5
        assert (
            er._can_place_via(
                0.0,
                0.0,
                net=1,
                existing_drills=[(0.5, 0.0, 0.3)],
                via_drill=0.25,
            )
            is False
        )


class TestStitchHoleToHoleGuard:
    """``calculate_via_position`` rejects a stitch via too close to an
    other-net through-hole drill (the J1-S2 case)."""

    def _gnd_pad(self) -> PadInfo:
        return PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100.0,
            y=100.0,
            layer="F.Cu",
            width=0.5,
            height=0.5,
        )

    def test_compliant_via_is_placed_without_drills(self) -> None:
        pos = calculate_via_position(
            self._gnd_pad(),
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            via_drill=0.2,
        )
        assert pos is not None

    def test_via_near_other_net_drill_is_rejected(self) -> None:
        """A through-hole drill surrounding the pad in every escape
        direction must block placement on hole-to-hole grounds even when
        copper would clear."""
        pad = self._gnd_pad()
        # Place an other-net drill (net 2) ringing the pad just inside the
        # 0.5mm hole-to-hole envelope of every cardinal/diagonal candidate.
        # The first offset ring is pad_radius(0.25)+offset(0.5)=0.75mm out.
        # An other-net drill 0.5mm out from each candidate would conflict;
        # to block ALL directions we surround densely.
        ring = []
        for ang in range(0, 360, 15):
            r = 0.75  # matches the first test_offset
            ring.append(
                (
                    pad.x + r * math.cos(math.radians(ang)),
                    pad.y + r * math.sin(math.radians(ang)),
                    0.6,  # large drill so hole-to-hole envelope is wide
                    2,
                )
            )
        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_drills=ring,
            via_drill=0.2,
            min_hole_to_hole=0.5,
        )
        assert pos is None

    def test_same_net_excluded_drill_does_not_block(self) -> None:
        """run_stitch excludes the stitch net's own drills; here we simply
        confirm an EMPTY other-net drill list never blocks placement."""
        pos = calculate_via_position(
            self._gnd_pad(),
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_drills=[],
            via_drill=0.2,
        )
        assert pos is not None

    def test_via_drill_zero_disables_check(self) -> None:
        """Back-compat: a caller that does not supply ``via_drill`` (0.0)
        skips the hole-to-hole drill check entirely."""
        pad = self._gnd_pad()
        ring = [(pad.x + 0.75, pad.y, 0.6, 2)]
        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_drills=ring,
            via_drill=0.0,
        )
        # With via_drill==0 the drill check is a no-op; placement succeeds
        # in a direction away from the single ring drill.
        assert pos is not None
