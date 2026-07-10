"""Match-group length / skew measurement tests (Issue #2688, Epic #2661 Phase 1B).

This module tests:

* :class:`kicad_tools.router.match_group_length.MatchGroupTracker` for the
  per-group recording + query API on synthetic N-trace (DDR-style) groups.
* Reference-policy semantics (``None`` -> longest, explicit net id ->
  "pace-car").
* Via-length policy (mirrors PR #2654 / PR #2685 -- the same drift-prevention
  test pattern applied here gates segment summation against duplication).
* Static :meth:`measure_net_from_pcb` delegation to
  :class:`DiffPairLengthTracker.measure_net_from_pcb` (the Phase 2.5c sister
  primitive); a drift-prevention test asserts the two helpers stay byte-for-
  byte aligned.
* Phase 1D wiring (Issue #2690): ``Autorouter._finalize_routing`` populates
  the tracker after every ``route_all_*`` entry point; ``add_match_group``
  flows through detection to the tracker; the no-group / detection-failure
  paths preserve the working diff-pair path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from kicad_tools.core.types import CopperLayer
from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import (
    MatchGroup,
    MatchGroupSource,
    MatchGroupTracker,
)
from kicad_tools.router.primitives import Route, Segment, Via

# =============================================================================
# Test helpers
# =============================================================================


def _make_straight_route(
    net_id: int,
    net_name: str,
    length_mm: float,
    layer: Layer = Layer.F_CU,
) -> Route:
    """Construct a single-segment horizontal route of the given length."""
    return Route(
        net=net_id,
        net_name=net_name,
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=length_mm,
                y2=0.0,
                width=0.2,
                layer=layer,
                net=net_id,
                net_name=net_name,
            )
        ],
    )


def _make_ddr_group(
    name: str = "DDR_DATA",
    net_ids: list[int] | None = None,
    tolerance: float = 0.5,
    reference_net_id: int | None = None,
    source: MatchGroupSource = MatchGroupSource.LEGACY_API,
) -> MatchGroup:
    """Construct a :class:`MatchGroup` with sensible DDR-style defaults."""
    if net_ids is None:
        net_ids = [100, 101, 102, 103]
    return MatchGroup(
        name=name,
        net_ids=net_ids,
        tolerance=tolerance,
        reference_net_id=reference_net_id,
        source=source,
    )


# =============================================================================
# 1. MatchGroupTracker -- basic record / query
# =============================================================================


class TestMatchGroupTrackerBasic:
    """Per-group length / skew measurement on synthetic N-trace groups."""

    def test_synthetic_4_net_ddr_byte_skew(self):
        """4-net DDR-style group with deliberately mismatched lengths.

        Lengths: ``DQ0=10.0, DQ1=12.0, DQ2=11.5, DQ3=10.5``.
        Expected skew = ``12.0 - 10.0 = 2.0`` mm.
        """
        routes = [
            _make_straight_route(100, "DQ0", 10.0),
            _make_straight_route(101, "DQ1", 12.0),
            _make_straight_route(102, "DQ2", 11.5),
            _make_straight_route(103, "DQ3", 10.5),
        ]
        group = _make_ddr_group()

        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group])

        assert tracker.get_group_lengths(group) == {
            100: 10.0,
            101: 12.0,
            102: 11.5,
            103: 10.5,
        }
        assert tracker.get_group_skew(group) == 2.0
        # Default reference policy: longest in group.
        assert tracker.get_reference_length(group) == 12.0
        # Name-keyed cache populated.
        assert tracker.get_all_skews() == {"DDR_DATA": 2.0}

    def test_symmetric_group_zero_skew(self):
        """All members equal length -> skew == 0.0."""
        routes = [
            _make_straight_route(100, "DQ0", 10.0),
            _make_straight_route(101, "DQ1", 10.0),
            _make_straight_route(102, "DQ2", 10.0),
            _make_straight_route(103, "DQ3", 10.0),
        ]
        group = _make_ddr_group()
        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group])

        assert tracker.get_group_skew(group) == 0.0
        assert tracker.get_reference_length(group) == 10.0

    def test_non_member_routes_ignored(self):
        """A net that is not a member of any group must not appear in lengths."""
        unrelated = _make_straight_route(999, "VCC", 100.0)
        routes = [
            unrelated,
            _make_straight_route(100, "DQ0", 5.0),
            _make_straight_route(101, "DQ1", 5.0),
        ]
        group = _make_ddr_group(net_ids=[100, 101])

        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group])

        assert 999 not in tracker.lengths
        assert tracker.get_group_skew(group) == 0.0

    def test_record_routes_overwrites_previous_lengths(self):
        """A second record_routes call refreshes lengths and the skew cache."""
        group = _make_ddr_group(net_ids=[100, 101])

        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 10.0),
                _make_straight_route(101, "DQ1", 11.0),
            ],
            [group],
        )
        assert tracker.get_group_skew(group) == 1.0

        # Second call with different lengths -> updated skew + cache.
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 5.0),
                _make_straight_route(101, "DQ1", 7.0),
            ],
            [group],
        )
        assert tracker.get_group_skew(group) == 2.0
        assert tracker.get_all_skews() == {"DDR_DATA": 2.0}


# =============================================================================
# 2. Unrouted-member behavior
# =============================================================================


class TestUnroutedMembers:
    """``get_group_skew`` returns ``None`` when fewer than 2 members routed."""

    def test_fully_unrouted_group_returns_none_skew(self):
        group = _make_ddr_group()
        tracker = MatchGroupTracker()
        tracker.record_routes([], [group])

        assert tracker.get_group_skew(group) is None
        assert tracker.get_group_lengths(group) == {}
        assert tracker.get_reference_length(group) is None
        assert tracker.get_all_skews() == {}

    def test_single_member_routed_returns_none_skew(self):
        """1 of 4 members routed -> skew is undefined (need >=2 for max-min)."""
        group = _make_ddr_group()
        tracker = MatchGroupTracker()
        tracker.record_routes(
            [_make_straight_route(100, "DQ0", 10.0)],
            [group],
        )

        assert tracker.get_group_skew(group) is None
        # Partial-routing group excluded from the bulk cache.
        assert tracker.get_all_skews() == {}

    def test_partial_routing_two_of_four_returns_skew_but_omits_from_bulk(self):
        """2 of 4 members routed -> per-group skew is defined, bulk cache excludes."""
        group = _make_ddr_group()
        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 10.0),
                _make_straight_route(101, "DQ1", 11.0),
            ],
            [group],
        )

        # Per-group accessor still computes a skew over routed members.
        assert tracker.get_group_skew(group) == 1.0
        # Bulk get_all_skews omits the partially-routed group (mirrors
        # DiffPairLengthTracker.get_all_skews "all halves required" policy).
        assert tracker.get_all_skews() == {}


# =============================================================================
# 3. Reference-selection policy (Phase 1A length_match_reference)
# =============================================================================


class TestReferencePolicy:
    """``get_reference_length`` implements longest-default + pace-car override."""

    def test_default_policy_returns_longest(self):
        """``reference_net_id is None`` -> longest routed length."""
        group = _make_ddr_group()
        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 10.0),
                _make_straight_route(101, "DQ1", 12.0),
                _make_straight_route(102, "DQ2", 11.0),
                _make_straight_route(103, "DQ3", 10.5),
            ],
            [group],
        )

        assert tracker.get_reference_length(group) == 12.0

    def test_explicit_reference_returns_that_nets_length(self):
        """An explicit ``reference_net_id`` returns the pace-car's length."""
        # Pace-car is DQ2 (net 102), which is NOT the longest.
        group = _make_ddr_group(reference_net_id=102)
        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 10.0),
                _make_straight_route(101, "DQ1", 12.0),  # longest
                _make_straight_route(102, "DQ2", 11.0),  # pace-car
                _make_straight_route(103, "DQ3", 10.5),
            ],
            [group],
        )

        # The longest is 12.0 but the pace-car overrides to 11.0.
        assert tracker.get_reference_length(group) == 11.0

    def test_explicit_reference_unrouted_returns_none(self):
        """An unrouted explicit reference yields ``None`` regardless of other routes."""
        # Reference = net 102; we route 100 and 101 but not 102.
        group = _make_ddr_group(reference_net_id=102)
        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 10.0),
                _make_straight_route(101, "DQ1", 12.0),
            ],
            [group],
        )

        assert tracker.get_reference_length(group) is None

    def test_longest_policy_with_partial_routing(self):
        """Default policy returns longest of the *routed* subset."""
        group = _make_ddr_group()
        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "DQ0", 7.0),
                _make_straight_route(101, "DQ1", 9.0),
                # 102 and 103 unrouted.
            ],
            [group],
        )

        assert tracker.get_reference_length(group) == 9.0


# =============================================================================
# 4. Via-length policy (mirrors PR #2654 Phase 3H semantics)
# =============================================================================


class TestViaLengthPolicy:
    """Vias contribute to length only when board_thickness_mm is supplied."""

    def test_via_traversal_includes_drilled_length_when_thickness_supplied(self):
        """2-layer 1.6 mm board: F.Cu -> B.Cu via adds 1.6 mm."""
        # One member of a 2-net group traverses a via.  Segments total 10 mm,
        # via adds 1.6 mm -> measured length 11.6 mm.
        via_route = Route(
            net=100,
            net_name="DQ0",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=100, net_name="DQ0"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.B_CU, net=100, net_name="DQ0"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(CopperLayer.F_CU, CopperLayer.B_CU),
                    net=100,
                    net_name="DQ0",
                )
            ],
        )
        plain_route = _make_straight_route(101, "DQ1", 11.6)
        group = _make_ddr_group(net_ids=[100, 101])

        tracker = MatchGroupTracker()
        tracker.record_routes(
            [via_route, plain_route], [group], board_thickness_mm=1.6, num_copper_layers=2
        )

        lengths = tracker.get_group_lengths(group)
        assert abs(lengths[100] - 11.6) < 1e-9
        assert abs(lengths[101] - 11.6) < 1e-9
        # Skew = 0 because we sized the plain route to match.
        assert tracker.get_group_skew(group) < 1e-9

    def test_via_returns_zero_length_when_thickness_is_none(self):
        """When ``board_thickness_mm=None``, vias contribute 0.0 mm (documented)."""
        via_route = Route(
            net=100,
            net_name="DQ0",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=100, net_name="DQ0"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.B_CU, net=100, net_name="DQ0"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(CopperLayer.F_CU, CopperLayer.B_CU),
                    net=100,
                    net_name="DQ0",
                )
            ],
        )
        plain_route = _make_straight_route(101, "DQ1", 10.0)
        group = _make_ddr_group(net_ids=[100, 101])

        tracker = MatchGroupTracker()
        # board_thickness_mm defaults to None.
        tracker.record_routes([via_route, plain_route], [group])

        lengths = tracker.get_group_lengths(group)
        # Via contributes 0.0 mm -> measured = 10.0 mm (segments only).
        assert abs(lengths[100] - 10.0) < 1e-9
        assert abs(lengths[101] - 10.0) < 1e-9
        assert tracker.get_group_skew(group) == 0.0

    def test_blind_via_partial_thickness_on_four_layer_stack(self):
        """4-layer 1.6 mm board: F.Cu -> In1.Cu blind via adds ~0.5333 mm."""
        via_route = Route(
            net=100,
            net_name="DQ0",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=100, net_name="DQ0"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.IN1_CU, net=100, net_name="DQ0"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.2,
                    diameter=0.45,
                    layers=(CopperLayer.F_CU, CopperLayer.IN1_CU),
                    net=100,
                    net_name="DQ0",
                )
            ],
        )
        expected_via_mm = 1.6 * 1 / 3
        plain_route = _make_straight_route(101, "DQ1", 10.0 + expected_via_mm)
        group = _make_ddr_group(net_ids=[100, 101])

        tracker = MatchGroupTracker()
        tracker.record_routes(
            [via_route, plain_route],
            [group],
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )

        lengths = tracker.get_group_lengths(group)
        assert abs(lengths[100] - (10.0 + expected_via_mm)) < 1e-9
        assert abs(lengths[101] - (10.0 + expected_via_mm)) < 1e-9
        assert tracker.get_group_skew(group) < 1e-9


# =============================================================================
# 4b. Through-via promotion (Issue #4007)
# =============================================================================


class TestThroughViaPromotion:
    """A standard blind via drills the full stack when blind vias are unsupported.

    Regression for Issue #4007: the match-group tuner measured board 07's
    ADDR_BUS A4/A6 escape vias as partial-thickness F.Cu->In1.Cu spans
    (0.533mm on a 1.6mm / 4-layer stack), converging the group to skew
    0.000mm.  But the board's stackup does not support blind/buried vias, so
    KiCad's post-route zone-fill re-save promoted every such via to a full
    F.Cu->B.Cu through-hole (1.6mm).  ``kct check`` then re-derived a 1.069mm
    skew from the promoted through-vias -- a 0.000-vs-1.069mm split.  The fix
    measures a standard (non-micro) via as a full through-via whenever
    ``blind_buried_supported`` is ``False``, so the tuner targets the same
    length the shipped board (and the checker) sees.
    """

    def _blind_via_route(self, net_id: int, net_name: str) -> Route:
        """A route with a single F.Cu->In1.Cu blind via (delta-1 span)."""
        return Route(
            net=net_id,
            net_name=net_name,
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=net_id, net_name=net_name),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.IN1_CU, net=net_id, net_name=net_name),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(CopperLayer.F_CU, CopperLayer.IN1_CU),
                    net=net_id,
                    net_name=net_name,
                )
            ],
        )

    def test_via_length_promotes_standard_via_to_through(self):
        """A non-micro F.Cu->In1.Cu via drills the full stack when unsupported."""
        via = Via(
            x=0.0,
            y=0.0,
            drill=0.3,
            diameter=0.6,
            layers=(CopperLayer.F_CU, CopperLayer.IN1_CU),
            net=1,
        )
        # Default (blind supported): partial span 1.6 * 1/3 = 0.5333mm.
        partial = DiffPairLengthTracker._via_length(via, 1.6, 4)
        assert abs(partial - 1.6 * 1 / 3) < 1e-9
        # Promoted (blind unsupported): full board thickness.
        promoted = DiffPairLengthTracker._via_length(via, 1.6, 4, blind_buried_supported=False)
        assert abs(promoted - 1.6) < 1e-9

    def test_via_length_micro_keeps_partial_span_when_unsupported(self):
        """A micro via is NOT promoted even when blind vias are unsupported."""
        micro = Via(
            x=0.0,
            y=0.0,
            drill=0.1,
            diameter=0.3,
            layers=(CopperLayer.F_CU, CopperLayer.IN1_CU),
            net=1,
            is_micro=True,
        )
        promoted = DiffPairLengthTracker._via_length(micro, 1.6, 4, blind_buried_supported=False)
        assert abs(promoted - 1.6 * 1 / 3) < 1e-9

    def test_tuner_measured_length_matches_pcb_through_via_measurement(self):
        """The route-object length (promoted) equals the promoted PCB measurement.

        This is the core Issue #4007 acceptance criterion: the via-inclusive
        length the tuner converges against (route-object form,
        blind-unsupported) must equal what the checker re-derives from the
        committed PCB after KiCad promotes the via to a full through-hole
        (PCB-segment form, blind-unsupported).
        """
        route = self._blind_via_route(100, "A4")

        # Route-object measurement (tuner side) with through-via promotion.
        route_len = MatchGroupTracker._measure_route_total(
            route, 1.6, 4, blind_buried_supported=False
        )

        # The committed board carries a PROMOTED through-via (F.Cu->B.Cu) --
        # this is what KiCad writes and what the checker reads.  Its
        # PCB-segment measurement (blind-unsupported) must match the route
        # measurement byte-for-byte.
        pcb = _StubPCB(
            _segments=[
                _StubSegment(start=(0.0, 0.0), end=(5.0, 0.0), net_number=100, layer="F.Cu"),
                _StubSegment(start=(5.0, 0.0), end=(10.0, 0.0), net_number=100, layer="In1.Cu"),
            ],
            _vias=[
                _StubVia(
                    position=(5.0, 0.0),
                    layers=["F.Cu", "B.Cu"],  # promoted through-via
                    net_number=100,
                )
            ],
        )
        pcb_len = MatchGroupTracker.measure_net_from_pcb(
            pcb, 100, board_thickness_mm=1.6, num_copper_layers=4, blind_buried_supported=False
        )

        # Both measure 10mm copper + 1.6mm through-via = 11.6mm.
        assert abs(route_len - 11.6) < 1e-9
        assert abs(route_len - pcb_len) < 1e-9

    def test_group_converges_to_matching_skew_under_promotion(self):
        """A group with a via-count imbalance stays skew-clean under promotion.

        A0 routes flat (no via); A4 has a promoted through-via.  Recorded
        with ``blind_buried_supported=False``, A4's via drills the full
        1.6mm; A0 must be that much longer to match -- mirroring the board 07
        ADDR_BUS geometry the tuner produces.
        """
        # A4: 10mm copper + 1.6mm promoted through-via = 11.6mm.
        a4 = self._blind_via_route(100, "A4")
        # A0: a flat 11.6mm route (no via) -- the length the tuner would
        # meander a planar member to, once it accounts for A4's through-via.
        a0 = _make_straight_route(101, "A0", 11.6)
        group = _make_ddr_group(net_ids=[100, 101])

        tracker = MatchGroupTracker()
        tracker.record_routes(
            [a4, a0],
            [group],
            board_thickness_mm=1.6,
            num_copper_layers=4,
            blind_buried_supported=False,
        )

        lengths = tracker.get_group_lengths(group)
        assert abs(lengths[100] - 11.6) < 1e-9
        assert abs(lengths[101] - 11.6) < 1e-9
        assert tracker.get_group_skew(group) < 1e-9

        # Sanity: with the legacy partial-span policy the SAME geometry
        # would read a nonzero skew (A4 = 10 + 0.533 = 10.533 vs A0 = 11.6),
        # which is exactly the tuner-vs-checker split Issue #4007 fixed.
        legacy = MatchGroupTracker()
        legacy.record_routes([a4, a0], [group], board_thickness_mm=1.6, num_copper_layers=4)
        assert legacy.get_group_skew(group) > 1.0


# =============================================================================
# 5. get_all_skews -- ordering + omission of partial groups
# =============================================================================


class TestGetAllSkews:
    """Bulk skew query returns deterministic ordering and skips partial groups."""

    def test_returns_skew_for_all_fully_routed_groups(self):
        group_a = MatchGroup(name="DDR_LO", net_ids=[100, 101], tolerance=0.5)
        group_b = MatchGroup(name="DDR_HI", net_ids=[200, 201], tolerance=0.5)
        routes = [
            _make_straight_route(100, "DQ0", 10.0),
            _make_straight_route(101, "DQ1", 11.0),
            _make_straight_route(200, "DQ8", 5.0),
            _make_straight_route(201, "DQ9", 5.5),
        ]

        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group_a, group_b])

        skews = tracker.get_all_skews()
        assert skews["DDR_LO"] == 1.0
        assert abs(skews["DDR_HI"] - 0.5) < 1e-9

    def test_omits_partially_routed_groups(self):
        group_full = MatchGroup(name="DDR_LO", net_ids=[100, 101], tolerance=0.5)
        group_partial = MatchGroup(name="DDR_HI", net_ids=[200, 201], tolerance=0.5)
        routes = [
            _make_straight_route(100, "DQ0", 10.0),
            _make_straight_route(101, "DQ1", 11.0),
            # group_partial only has DQ8 routed; DQ9 missing.
            _make_straight_route(200, "DQ8", 5.0),
        ]

        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group_full, group_partial])

        skews = tracker.get_all_skews()
        assert "DDR_LO" in skews
        assert "DDR_HI" not in skews

    def test_ordering_is_deterministic_alphabetic_by_group_name(self):
        # Built in non-alphabetic order; get_all_skews must sort by name.
        group_z = MatchGroup(name="ZZ_BUS", net_ids=[200, 201], tolerance=0.5)
        group_a = MatchGroup(name="AA_BUS", net_ids=[100, 101], tolerance=0.5)
        routes = [
            _make_straight_route(100, "A0", 10.0),
            _make_straight_route(101, "A1", 10.0),
            _make_straight_route(200, "Z0", 10.0),
            _make_straight_route(201, "Z1", 10.0),
        ]
        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group_z, group_a])

        keys = list(tracker.get_all_skews().keys())
        assert keys == ["AA_BUS", "ZZ_BUS"]

    def test_record_routes_resets_skew_map(self):
        """A second record_routes call with different groups drops the previous cache."""
        group1 = MatchGroup(name="G1", net_ids=[100, 101], tolerance=0.5)
        group2 = MatchGroup(name="G2", net_ids=[200, 201], tolerance=0.5)

        tracker = MatchGroupTracker()
        tracker.record_routes(
            [
                _make_straight_route(100, "A0", 10.0),
                _make_straight_route(101, "A1", 11.0),
            ],
            [group1],
        )
        assert "G1" in tracker.get_all_skews()

        # Second call with a different group set -- previous cache must clear.
        tracker.record_routes(
            [
                _make_straight_route(200, "B0", 5.0),
                _make_straight_route(201, "B1", 6.0),
            ],
            [group2],
        )
        skews = tracker.get_all_skews()
        assert "G2" in skews
        assert "G1" not in skews


# =============================================================================
# 6. Drift-prevention: segment summation MUST delegate to LengthTracker
# =============================================================================


class TestDriftPreventionSegmentSummation:
    """``record_routes`` MUST NOT reimplement segment summation -- it delegates.

    The single source of truth for segment summation is
    :meth:`LengthTracker.calculate_route_length` at ``length.py:138-153``.
    If a future contributor inlines a dx/dy/sqrt loop in
    ``match_group_length.py``, this test fails and points to the drift.

    Mirrors the drift-prevention scaffolding in PR #2654 / PR #2685.
    """

    def test_record_routes_delegates_to_calculate_route_length(self):
        """``MatchGroupTracker.record_routes`` calls ``LengthTracker.calculate_route_length``."""
        routes = [
            _make_straight_route(100, "DQ0", 10.0),
            _make_straight_route(101, "DQ1", 11.0),
        ]
        group = _make_ddr_group(net_ids=[100, 101])

        # Wrap the staticmethod with a side-effect-preserving spy.  If a
        # future change reimplements segment summation locally, call_count
        # stays 0 and the assertion fails fast.
        real_fn = LengthTracker.calculate_route_length
        with patch.object(
            LengthTracker,
            "calculate_route_length",
            wraps=real_fn,
        ) as spy:
            MatchGroupTracker().record_routes(routes, [group])

        # At least one delegation per measured route.
        assert spy.call_count >= len(routes)


# =============================================================================
# 7. Drift-prevention: measure_net_from_pcb MUST delegate to DiffPairLengthTracker
# =============================================================================


@dataclass
class _StubSegment:
    """PCB-schema-shape segment stub.

    Mirrors :class:`kicad_tools.schema.pcb.Segment`: ``start`` / ``end`` are
    ``tuple[float, float]`` (NOT the router-internal ``x1/y1/x2/y2``).
    """

    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubVia:
    """PCB-schema-shape via stub (``layers: list[str]``)."""

    position: tuple[float, float] = (0.0, 0.0)
    size: float = 0.6
    drill: float = 0.3
    layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""
    # Mirrors :attr:`kicad_tools.schema.pcb.Via.via_type` -- ``None`` for a
    # standard plated through-hole; ``"micro"`` / ``"blind"`` / ``"buried"``
    # for the corresponding KiCad via-type token (Issue #4007).
    via_type: str | None = None


@dataclass
class _StubPCB:
    """Minimal PCB stub implementing ``segments_in_net`` + ``vias_in_net``."""

    _segments: list[_StubSegment] = field(default_factory=list)
    _vias: list[_StubVia] = field(default_factory=list)

    def segments_in_net(self, net_number: int):
        for seg in self._segments:
            if seg.net_number == net_number:
                yield seg

    def vias_in_net(self, net_number: int):
        for via in self._vias:
            if via.net_number == net_number:
                yield via


class TestMeasureNetFromPcbDelegation:
    """``MatchGroupTracker.measure_net_from_pcb`` is a one-line forwarder.

    The drift-prevention contract: Phase 1B must NOT duplicate the body of
    :meth:`DiffPairLengthTracker.measure_net_from_pcb` (which would re-
    introduce the exact drift risk that PR #2685 wrote its own drift-
    prevention test to prevent).  This test asserts:

    1. The forwarder calls the diff-pair primitive (spy assertion).
    2. The forwarder returns the same value the diff-pair primitive does
       for the same input (round-trip equivalence).
    """

    def test_forwarder_delegates_to_diffpair_primitive(self):
        """``measure_net_from_pcb`` invokes ``DiffPairLengthTracker.measure_net_from_pcb``."""
        pcb = _StubPCB(
            _segments=[
                _StubSegment(start=(0.0, 0.0), end=(10.0, 0.0), net_number=100),
            ]
        )

        real_fn = DiffPairLengthTracker.measure_net_from_pcb
        with patch.object(
            DiffPairLengthTracker,
            "measure_net_from_pcb",
            wraps=real_fn,
        ) as spy:
            MatchGroupTracker.measure_net_from_pcb(pcb, 100)

        assert spy.call_count == 1
        # The forwarder must pass through all positional/kw args, including
        # the Issue #4007 ``blind_buried_supported`` through-via-promotion
        # flag (default True preserves the legacy partial-span behavior).
        spy.assert_called_with(
            pcb,
            100,
            board_thickness_mm=None,
            num_copper_layers=2,
            blind_buried_supported=True,
        )

    def test_forwarder_returns_same_value_as_diffpair_primitive(self):
        """For the same input, both helpers produce byte-for-byte identical results.

        This is the contract that lets a Phase 2G DRC producer import either
        primitive without surprise; if the two ever diverge this test fires.
        """
        pcb = _StubPCB(
            _segments=[
                _StubSegment(start=(0.0, 0.0), end=(10.0, 0.0), net_number=100),
                _StubSegment(start=(10.0, 0.0), end=(10.0, 5.0), net_number=100),
            ],
            _vias=[
                _StubVia(
                    position=(10.0, 5.0),
                    layers=["F.Cu", "B.Cu"],
                    net_number=100,
                )
            ],
        )

        diffpair_result = DiffPairLengthTracker.measure_net_from_pcb(
            pcb, 100, board_thickness_mm=1.6, num_copper_layers=2
        )
        match_group_result = MatchGroupTracker.measure_net_from_pcb(
            pcb, 100, board_thickness_mm=1.6, num_copper_layers=2
        )

        assert diffpair_result == match_group_result
        # Sanity: segments are 10 + 5 = 15 mm, via is 1.6 mm -> 16.6 mm total.
        assert abs(match_group_result - 16.6) < 1e-9

    def test_forwarder_zero_via_length_when_thickness_none(self):
        """Without ``board_thickness_mm``, vias contribute 0.0 (mirrors policy)."""
        pcb = _StubPCB(
            _segments=[
                _StubSegment(start=(0.0, 0.0), end=(10.0, 0.0), net_number=100),
            ],
            _vias=[
                _StubVia(
                    position=(10.0, 0.0),
                    layers=["F.Cu", "B.Cu"],
                    net_number=100,
                )
            ],
        )

        result = MatchGroupTracker.measure_net_from_pcb(pcb, 100)
        # 10 mm segments + 0 mm via = 10 mm.
        assert abs(result - 10.0) < 1e-9


# =============================================================================
# 8. MatchGroupSource enum -- members + cross-detector consistency
# =============================================================================


class TestMatchGroupSourceEnum:
    """The enum members match the Phase 1C detector contract."""

    def test_members_match_curator_spec(self):
        # Mirrors DetectionSource at diffpair_detection.py:49-54 plus
        # LEGACY_API for the pre-Epic-#2661 entry point.
        assert MatchGroupSource.EXPLICIT.value == "explicit"
        assert MatchGroupSource.KICAD_GROUP.value == "kicad_group"
        assert MatchGroupSource.SUFFIX.value == "suffix"
        assert MatchGroupSource.LEGACY_API.value == "legacy_api"
        # Exactly four members -- no extras snuck in.
        assert len(MatchGroupSource) == 4

    def test_suffix_member_byte_for_byte_matches_diffpair_detection_source(self):
        """``MatchGroupSource.SUFFIX`` is byte-for-byte aligned with ``DetectionSource.SUFFIX``.

        Cross-detector consistency -- if either side ever renames the
        member, this test fires.
        """
        from kicad_tools.router.diffpair_detection import DetectionSource

        assert MatchGroupSource.SUFFIX.value == DetectionSource.SUFFIX.value


# =============================================================================
# 9. MatchGroup dataclass -- field defaults + Phase 2F reservation
# =============================================================================


class TestMatchGroupDataclass:
    """Field defaults match the curator spec; pair_ids reserved for Phase 2F."""

    def test_default_tolerance_matches_phase_1a_accessor_default(self):
        """``MatchGroup.tolerance`` defaults align with ``effective_length_match_tolerance``.

        The literal ``0.5`` must appear in exactly two places: the
        dataclass default (here) and
        :meth:`NetClassRouting.effective_length_match_tolerance`'s
        ``default=`` arg (Phase 1A).  Any third copy is drift.
        """
        from kicad_tools.router.rules import NetClassRouting

        group = MatchGroup(name="G", net_ids=[1, 2])
        assert group.tolerance == 0.5

        nc = NetClassRouting(name="Default")
        # If Phase 1A's accessor default drifts away from 0.5 we want
        # to be loud about it -- the constraint is "must equal".
        assert nc.effective_length_match_tolerance() == group.tolerance

    def test_pair_ids_defaults_empty(self):
        """``pair_ids`` is Phase-2F reserved; default is an empty list."""
        group = MatchGroup(name="G", net_ids=[1, 2])
        assert group.pair_ids == []

    def test_source_default_is_legacy_api(self):
        """Constructor-shaped use without a kwarg attributes to LEGACY_API."""
        group = MatchGroup(name="G", net_ids=[1, 2])
        assert group.source is MatchGroupSource.LEGACY_API

    def test_pair_ids_members_measured_in_record_routes(self):
        """``pair_ids`` net halves are measured the same as singles (Phase 1B).

        Phase 2F-facing forward compat: the tracker measures BOTH halves of
        each pair member, populating ``lengths`` for both net ids.  The
        pair-aware reduction (which pair-length to use for skew) is left
        to Phase 2F's tuner.
        """
        group = MatchGroup(
            name="MIPI_LANE",
            net_ids=[],
            pair_ids=[(100, 101), (102, 103)],
        )
        routes = [
            _make_straight_route(100, "P0_P", 10.0),
            _make_straight_route(101, "P0_N", 10.0),
            _make_straight_route(102, "P1_P", 11.0),
            _make_straight_route(103, "P1_N", 11.0),
        ]
        tracker = MatchGroupTracker()
        tracker.record_routes(routes, [group])

        # All four halves measured.
        assert tracker.lengths[100] == 10.0
        assert tracker.lengths[101] == 10.0
        assert tracker.lengths[102] == 11.0
        assert tracker.lengths[103] == 11.0
        # Skew across all four members = 11 - 10 = 1.0.
        assert tracker.get_group_skew(group) == 1.0


# =============================================================================
# 10. Exports -- public surface is reachable from kicad_tools.router
# =============================================================================


class TestPublicExports:
    """The acceptance criterion requires the trio to be importable from the package."""

    def test_match_group_tracker_exported(self):
        from kicad_tools.router import MatchGroupTracker as Exported

        assert Exported is MatchGroupTracker

    def test_match_group_exported(self):
        from kicad_tools.router import MatchGroup as Exported

        assert Exported is MatchGroup

    def test_match_group_source_exported(self):
        from kicad_tools.router import MatchGroupSource as Exported

        assert Exported is MatchGroupSource


# =============================================================================
# 11. Phase 1D wiring -- Autorouter._finalize_routing populates the tracker
#     (Issue #2690 / Epic #2661 Phase 1D)
# =============================================================================
#
# The #2587 dormant-signal lesson: a feature whose record_routes call is
# unreachable from the production code path silently regresses.  These
# tests pin the contract that every ``route_all_*`` strategy invokes
# ``_finalize_routing`` which in turn invokes ``update_match_group_skew``
# on the legacy-API-declared group below.
#
# The fixture uses a 4-net DDR-style group declared via the legacy
# ``Autorouter.add_match_group(...)`` API.  The Phase 1C detector's
# LEGACY_API source surfaces this group from ``LengthTracker.match_groups``;
# Phase 1D then populates the tracker with the routed lengths.


def _build_synthetic_ddr_group_autorouter():
    """Build a minimal Autorouter with a 4-net DDR-style group routable end-to-end.

    Two components (``J1`` and ``U1``) sit on opposite sides of a small
    30x30 mm board, with four matching ``DDR_DQ0..DDR_DQ3`` nets.  The
    legacy ``Autorouter.add_match_group("DDR_BUS", ...)`` call surfaces
    the group via the Phase 1C detector's LEGACY_API source, so any
    strategy that wires ``_finalize_routing`` correctly populates
    ``match_group_tracker.get_all_skews()``.
    """
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(width=30.0, height=30.0)
    ar.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 2.0,
                "y": 8.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "DDR_DQ0",
            },
            {
                "number": "2",
                "x": 2.0,
                "y": 12.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "DDR_DQ1",
            },
            {
                "number": "3",
                "x": 2.0,
                "y": 16.0,
                "width": 0.5,
                "height": 0.5,
                "net": 3,
                "net_name": "DDR_DQ2",
            },
            {
                "number": "4",
                "x": 2.0,
                "y": 20.0,
                "width": 0.5,
                "height": 0.5,
                "net": 4,
                "net_name": "DDR_DQ3",
            },
        ],
    )
    ar.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 28.0,
                "y": 8.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "DDR_DQ0",
            },
            {
                "number": "2",
                "x": 28.0,
                "y": 12.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "DDR_DQ1",
            },
            {
                "number": "3",
                "x": 28.0,
                "y": 16.0,
                "width": 0.5,
                "height": 0.5,
                "net": 3,
                "net_name": "DDR_DQ2",
            },
            {
                "number": "4",
                "x": 28.0,
                "y": 20.0,
                "width": 0.5,
                "height": 0.5,
                "net": 4,
                "net_name": "DDR_DQ3",
            },
        ],
    )
    # Legacy-API match group declaration -- the LEGACY_API source path.
    ar.add_match_group("DDR_BUS", [1, 2, 3, 4], tolerance=0.5)
    return ar


class TestFinalizeRoutingWiresMatchGroupTracker:
    """Drift-prevention gate -- ``_finalize_routing`` wires the match-group tracker.

    Mirrors ``TestFinalizeRoutingDriftPrevention`` in
    ``tests/test_diffpair_length.py:637-741`` byte-for-byte modulo the
    diff-pair -> match-group renames.  Without these tests, a future
    strategy that bypasses ``_finalize_routing`` would silently regress:
    its routes would land on ``self.routes`` but
    ``match_group_tracker.get_all_skews()`` would stay empty.
    """

    @pytest.mark.parametrize(
        "strategy_name",
        [
            "route_all",
            "route_all_negotiated",
            "route_all_interleaved",
        ],
    )
    def test_strategy_populates_match_group_tracker(self, strategy_name):
        ar = _build_synthetic_ddr_group_autorouter()

        # Each strategy is a top-level entry point on Autorouter that
        # must end with a _finalize_routing call.  Invoked through
        # getattr so the parametrize-id reflects which strategy regressed.
        strategy = getattr(ar, strategy_name)
        if strategy_name == "route_all_negotiated":
            # max_iterations=2 keeps the test fast; the contract is
            # invocation-shape, not convergence.
            strategy(max_iterations=2)
        else:
            strategy()

        # Acceptance criterion 1: tracker.lengths is non-empty.
        # The #2587 dormant-signal gate -- if this is empty, the
        # _finalize_routing call never reached update_match_group_skew.
        assert ar.match_group_tracker.lengths, (
            f"{strategy_name}: match_group_tracker.lengths is empty -- "
            f"_finalize_routing is not being invoked on this strategy's "
            f"return path, or update_match_group_skew is not reachable."
        )

        # Acceptance criterion 2: get_all_skews() returns a non-empty
        # dict containing the legacy-API-declared "DDR_BUS" group.
        skews = ar.match_group_tracker.get_all_skews()
        assert skews, f"{strategy_name}: match_group_tracker.get_all_skews() is empty"
        assert "DDR_BUS" in skews, f"{strategy_name}: DDR_BUS group missing from skews: {skews}"
        # Skew is a real non-negative float (the contract is "non-None
        # float", not a specific value -- routing is non-deterministic).
        skew_mm = skews["DDR_BUS"]
        assert isinstance(skew_mm, float)
        assert skew_mm >= 0.0  # max(L) - min(L) is always non-negative

    def test_finalize_routing_idempotent(self):
        """``_finalize_routing`` can be called multiple times safely.

        ``record_routes`` overwrites previously-recorded lengths, and
        ``update_match_group_skew`` re-derives ``detected_groups`` each
        call so repeated invocations leave the tracker in the same state
        as a single invocation (modulo non-determinism in detection,
        which is deterministic for these synthetic inputs).
        """
        ar = _build_synthetic_ddr_group_autorouter()
        ar.route_all()
        first_skew = dict(ar.match_group_tracker.get_all_skews())

        # Direct second invocation must not corrupt the tracker.
        ar._finalize_routing()
        second_skew = dict(ar.match_group_tracker.get_all_skews())

        assert first_skew == second_skew
        assert first_skew  # non-empty

    def test_finalize_routing_noop_when_no_groups(self):
        """When no match groups are declared, ``_finalize_routing`` is a no-op.

        Documents the contract that ``_finalize_routing`` does NOT
        spuriously populate the tracker for boards without match groups
        (no legacy API call, no explicit declaration, suffix inference off).
        """
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=20.0, height=20.0)
        ar.add_component(
            "J1",
            [
                {
                    "number": "1",
                    "x": 2.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "SIG_A",
                },
            ],
        )
        ar.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 18.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "SIG_A",
                },
            ],
        )
        ar.route_all()

        # No groups -> no skews recorded.  Tracker stays empty.
        assert ar.match_group_tracker.get_all_skews() == {}
        assert ar.match_group_tracker.lengths == {}

    def test_finalize_routing_detection_failure_preserves_diff_pair_path(self):
        """A failing match-group detector must NOT regress the diff-pair tracker.

        Phase 1D's block ordering contract: the diff-pair block runs
        FIRST.  If the match-group detector raises (ImportError, generic
        Exception, ...), ``_finalize_routing`` returns cleanly and the
        diff-pair tracker remains populated.  This is the regression
        gate against the Phase 1D additions breaking the existing
        Phase 3H-cont path.
        """
        from kicad_tools.router.core import Autorouter

        # Build a USB pair AND a DDR group so both detectors have work.
        ar = Autorouter(width=30.0, height=30.0)
        ar.add_component(
            "J1",
            [
                {
                    "number": "1",
                    "x": 2.0,
                    "y": 8.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "2",
                    "x": 2.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 2,
                    "net_name": "USB_D-",
                },
            ],
        )
        ar.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 28.0,
                    "y": 8.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "2",
                    "x": 28.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 2,
                    "net_name": "USB_D-",
                },
            ],
        )

        # Monkey-patch detect_match_groups to raise so the match-group
        # block enters its except branch.  The diff-pair block must run
        # to completion regardless.
        with patch(
            "kicad_tools.router.match_group_detection.detect_match_groups",
            side_effect=RuntimeError("synthetic Phase 1C detection failure"),
        ):
            ar.route_all()

        # The diff-pair path is unbroken: USB_D+/USB_D- pair was
        # detected and the diff-pair tracker is populated.
        diff_skews = ar.diffpair_length_tracker.get_all_skews()
        assert diff_skews, (
            "diff-pair tracker is empty -- the Phase 1D additions broke "
            "the Phase 3H-cont diff-pair path (block ordering violation)"
        )
        assert ("USB_D+", "USB_D-") in diff_skews

        # The match-group path silently no-ops on detector failure.
        assert ar.match_group_tracker.get_all_skews() == {}

    def test_legacy_add_match_group_flows_to_tracker(self):
        """Integration test: the legacy API populates the new tracker.

        ``Autorouter.add_match_group("TEST", [...])`` writes into
        ``self._length_tracker.match_groups``; the Phase 1C detector's
        LEGACY_API source surfaces this; Phase 1D's
        ``update_match_group_skew`` records routed lengths.  The whole
        chain must work end-to-end without caller code change.
        """
        ar = _build_synthetic_ddr_group_autorouter()
        ar.route_all()

        skews = ar.match_group_tracker.get_all_skews()
        assert "DDR_BUS" in skews
        # The legacy-API source produces a real float skew (not None).
        assert skews["DDR_BUS"] is not None
        assert isinstance(skews["DDR_BUS"], float)

        # And the underlying lengths are populated for all four members.
        assert 1 in ar.match_group_tracker.lengths
        assert 2 in ar.match_group_tracker.lengths
        assert 3 in ar.match_group_tracker.lengths
        assert 4 in ar.match_group_tracker.lengths

    def test_match_group_tracker_property_returns_internal_tracker(self):
        """The ``match_group_tracker`` property returns the live tracker.

        Mirrors the diff-pair property contract: the returned object
        IS the autorouter's internal tracker (no defensive copy).
        """
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=10.0, height=10.0)
        assert ar.match_group_tracker is ar._match_group_tracker
        # And it's a real MatchGroupTracker, not None.
        assert isinstance(ar.match_group_tracker, MatchGroupTracker)
