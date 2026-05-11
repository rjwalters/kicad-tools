"""Per-pair diff-pair length-match (skew) tests (Issue #2647, Epic #2556 Phase 3H).

This module tests:

* :class:`kicad_tools.router.diffpair_length.DiffPairLengthTracker` for
  symmetric / asymmetric / via-traversing / unrouted scenarios.
* :meth:`kicad_tools.router.rules.NetClassRouting.effective_skew_tolerance`
  per-class round-trip + default-fallback semantics.
* The Phase 3I-facing :meth:`Autorouter.update_diffpair_skew` production
  path -- the dormant-signal gate (Issue #2587 lesson).
"""

from __future__ import annotations

import pytest

from kicad_tools.core.types import CopperLayer
from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import NetClassRouting

# =============================================================================
# Test helpers
# =============================================================================


def _make_signal(name: str, net_id: int, polarity: str) -> DifferentialSignal:
    """Construct a :class:`DifferentialSignal` with sensible defaults."""
    return DifferentialSignal(
        net_name=name,
        net_id=net_id,
        base_name=name.rstrip("+-_PN"),
        polarity=polarity,
        notation="plus_minus",
    )


def _make_detected_pair(
    base_name: str = "USB_D",
    p_name: str = "USB_D+",
    n_name: str = "USB_D-",
    p_id: int = 1,
    n_id: int = 2,
) -> DetectedPair:
    """Construct a :class:`DetectedPair` for skew measurement."""
    return DetectedPair(
        pair=DifferentialPair(
            name=base_name,
            positive=_make_signal(p_name, p_id, "P"),
            negative=_make_signal(n_name, n_id, "N"),
            pair_type=DifferentialPairType.USB2,
        ),
        source=DetectionSource.EXPLICIT,
    )


def _make_straight_route(
    net_id: int, net_name: str, length_mm: float, layer: Layer = Layer.F_CU
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


def _make_two_segment_route(
    net_id: int,
    net_name: str,
    leg1_mm: float,
    leg2_mm: float,
    layer: Layer = Layer.F_CU,
) -> Route:
    """Construct a 2-segment L-shaped route with total length leg1+leg2."""
    return Route(
        net=net_id,
        net_name=net_name,
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=leg1_mm,
                y2=0.0,
                width=0.2,
                layer=layer,
                net=net_id,
                net_name=net_name,
            ),
            Segment(
                x1=leg1_mm,
                y1=0.0,
                x2=leg1_mm,
                y2=leg2_mm,
                width=0.2,
                layer=layer,
                net=net_id,
                net_name=net_name,
            ),
        ],
    )


# =============================================================================
# 1. DiffPairLengthTracker -- basic record / query
# =============================================================================


class TestDiffPairLengthTrackerBasic:
    """Symmetric and asymmetric pair length / skew measurement."""

    def test_symmetric_pair_zero_skew(self):
        # L_p == L_n == 10mm -> skew = 0.0
        dp = _make_detected_pair(p_id=1, n_id=2)
        p_route = _make_two_segment_route(1, "USB_D+", 5.0, 5.0)
        n_route = _make_two_segment_route(2, "USB_D-", 5.0, 5.0)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([p_route, n_route], [dp])

        lengths = tracker.get_pair_lengths(dp)
        assert lengths is not None
        l_p, l_n = lengths
        assert abs(l_p - 10.0) < 1e-9
        assert abs(l_n - 10.0) < 1e-9
        assert tracker.get_skew(dp) == 0.0

    def test_asymmetric_pair_skew_two_mm(self):
        # L_p = 10mm, L_n = 12mm -> skew = 2.0
        dp = _make_detected_pair(p_id=1, n_id=2)
        p_route = _make_straight_route(1, "USB_D+", 10.0)
        n_route = _make_straight_route(2, "USB_D-", 12.0)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([p_route, n_route], [dp])

        assert tracker.get_pair_lengths(dp) == (10.0, 12.0)
        assert tracker.get_skew(dp) == 2.0

    def test_unrouted_p_side_returns_none(self):
        # Only the negative half has a Route -- positive is unrouted.
        dp = _make_detected_pair(p_id=1, n_id=2)
        n_route = _make_straight_route(2, "USB_D-", 12.0)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([n_route], [dp])

        assert tracker.get_pair_lengths(dp) is None
        assert tracker.get_skew(dp) is None

    def test_unrouted_n_side_returns_none(self):
        dp = _make_detected_pair(p_id=1, n_id=2)
        p_route = _make_straight_route(1, "USB_D+", 10.0)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([p_route], [dp])

        assert tracker.get_pair_lengths(dp) is None
        assert tracker.get_skew(dp) is None

    def test_non_pair_routes_ignored(self):
        # A net id that is not part of any detected pair must NOT
        # appear in the recorded lengths -- the tracker is per-pair.
        dp = _make_detected_pair(p_id=1, n_id=2)
        unrelated = _make_straight_route(99, "VCC", 100.0)
        p_route = _make_straight_route(1, "USB_D+", 5.0)
        n_route = _make_straight_route(2, "USB_D-", 5.0)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([unrelated, p_route, n_route], [dp])

        assert 99 not in tracker.lengths
        assert tracker.get_skew(dp) == 0.0


# =============================================================================
# 2. Via-length policy
# =============================================================================


class TestViaLengthPolicy:
    """Vias contribute to length only when board_thickness_mm is supplied."""

    def test_via_traversal_includes_drilled_length_when_thickness_supplied(self):
        # 2-layer 1.6 mm board, P traverses F.Cu -> B.Cu via.
        # Expected via length = 1.6 * |5 - 0| / (2 - 1) = 1.6 mm.
        # Two F.Cu segments totalling 10 mm + 1.6 mm via = 11.6 mm.
        dp = _make_detected_pair(p_id=1, n_id=2)
        p_route = Route(
            net=1,
            net_name="USB_D+",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.B_CU, net=1, net_name="USB_D+"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(CopperLayer.F_CU, CopperLayer.B_CU),
                    net=1,
                    net_name="USB_D+",
                )
            ],
        )
        n_route = _make_straight_route(2, "USB_D-", 11.6)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([p_route, n_route], [dp], board_thickness_mm=1.6, num_copper_layers=2)

        lengths = tracker.get_pair_lengths(dp)
        assert lengths is not None
        l_p, l_n = lengths
        assert abs(l_p - 11.6) < 1e-9
        assert abs(l_n - 11.6) < 1e-9
        # Skew should be ~0 because we sized n_route to match.
        assert abs(tracker.get_skew(dp)) < 1e-9

    def test_via_returns_zero_length_when_thickness_is_none(self):
        # When board_thickness_mm is None, vias contribute 0.0 mm.
        # P_segments = 10 mm + via -> 10 mm total when no thickness.
        # N is 10 mm -> skew should be 0, not 1.6.
        dp = _make_detected_pair(p_id=1, n_id=2)
        p_route = Route(
            net=1,
            net_name="USB_D+",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.B_CU, net=1, net_name="USB_D+"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(CopperLayer.F_CU, CopperLayer.B_CU),
                    net=1,
                    net_name="USB_D+",
                )
            ],
        )
        n_route = _make_straight_route(2, "USB_D-", 10.0)

        tracker = DiffPairLengthTracker()
        # board_thickness_mm defaults to None.
        tracker.record_routes([p_route, n_route], [dp])

        lengths = tracker.get_pair_lengths(dp)
        assert lengths is not None
        l_p, l_n = lengths
        assert abs(l_p - 10.0) < 1e-9
        assert abs(l_n - 10.0) < 1e-9
        assert tracker.get_skew(dp) == 0.0

    def test_blind_via_partial_thickness_on_four_layer_stack(self):
        # 4-layer 1.6 mm board with a F.Cu -> In1.Cu blind via.
        # Layer index delta = 1; divisor = (4 - 1) = 3.
        # Per-via length = 1.6 * 1 / 3 ~= 0.5333 mm.
        dp = _make_detected_pair(p_id=1, n_id=2)
        p_route = Route(
            net=1,
            net_name="USB_D+",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.IN1_CU, net=1, net_name="USB_D+"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.2,
                    diameter=0.45,
                    layers=(CopperLayer.F_CU, CopperLayer.IN1_CU),
                    net=1,
                    net_name="USB_D+",
                )
            ],
        )
        # N is segments only, same total: 10 mm + via_length.
        expected_via_mm = 1.6 * 1 / 3  # ~0.5333
        n_route = _make_straight_route(2, "USB_D-", 10.0 + expected_via_mm)

        tracker = DiffPairLengthTracker()
        tracker.record_routes([p_route, n_route], [dp], board_thickness_mm=1.6, num_copper_layers=4)

        lengths = tracker.get_pair_lengths(dp)
        assert lengths is not None
        l_p, l_n = lengths
        assert abs(l_p - (10.0 + expected_via_mm)) < 1e-9
        assert abs(l_n - (10.0 + expected_via_mm)) < 1e-9
        # Skew should be ~0 because we sized n_route to match P + via.
        assert tracker.get_skew(dp) < 1e-9


# =============================================================================
# 3. get_all_skews -- ordering + omission of unrouted pairs
# =============================================================================


class TestGetAllSkews:
    """Bulk skew query returns deterministic ordering and skips unrouted pairs."""

    def test_returns_skew_for_all_routed_pairs(self):
        dp1 = _make_detected_pair("USB_D", "USB_D+", "USB_D-", p_id=1, n_id=2)
        dp2 = _make_detected_pair("PCIE_TX", "PCIE_TX_P", "PCIE_TX_N", p_id=3, n_id=4)

        routes = [
            _make_straight_route(1, "USB_D+", 10.0),
            _make_straight_route(2, "USB_D-", 12.0),
            _make_straight_route(3, "PCIE_TX_P", 7.0),
            _make_straight_route(4, "PCIE_TX_N", 7.0),
        ]

        tracker = DiffPairLengthTracker()
        tracker.record_routes(routes, [dp1, dp2])

        skews = tracker.get_all_skews()
        assert skews[("PCIE_TX_P", "PCIE_TX_N")] == 0.0
        assert skews[("USB_D+", "USB_D-")] == 2.0

    def test_omits_pairs_with_unrouted_halves(self):
        # dp1 is fully routed; dp2 is missing its P half.
        dp1 = _make_detected_pair("USB_D", "USB_D+", "USB_D-", p_id=1, n_id=2)
        dp2 = _make_detected_pair("PCIE_TX", "PCIE_TX_P", "PCIE_TX_N", p_id=3, n_id=4)

        routes = [
            _make_straight_route(1, "USB_D+", 10.0),
            _make_straight_route(2, "USB_D-", 12.0),
            _make_straight_route(4, "PCIE_TX_N", 7.0),
        ]

        tracker = DiffPairLengthTracker()
        tracker.record_routes(routes, [dp1, dp2])

        skews = tracker.get_all_skews()
        assert ("USB_D+", "USB_D-") in skews
        assert ("PCIE_TX_P", "PCIE_TX_N") not in skews

    def test_ordering_is_deterministic_by_p_net_name(self):
        # Build pairs whose P names sort to a known order.
        dp_b = _make_detected_pair("BUS_B", "B+", "B-", p_id=1, n_id=2)
        dp_a = _make_detected_pair("BUS_A", "A+", "A-", p_id=3, n_id=4)

        routes = [
            _make_straight_route(1, "B+", 10.0),
            _make_straight_route(2, "B-", 10.0),
            _make_straight_route(3, "A+", 10.0),
            _make_straight_route(4, "A-", 10.0),
        ]
        tracker = DiffPairLengthTracker()
        tracker.record_routes(routes, [dp_b, dp_a])

        # The detected_pairs list was [dp_b, dp_a]; insertion would put
        # B+ before A+.  get_all_skews must sort by p_net_name so A+
        # comes first.
        skews = tracker.get_all_skews()
        keys = list(skews.keys())
        assert keys == [("A+", "A-"), ("B+", "B-")]

    def test_record_routes_resets_skew_map(self):
        # Calling record_routes twice with different detected_pairs lists
        # must not retain stale (p, n) entries from the previous call.
        dp1 = _make_detected_pair("USB_D", "USB_D+", "USB_D-", p_id=1, n_id=2)
        dp2 = _make_detected_pair("PCIE_TX", "PCIE_TX_P", "PCIE_TX_N", p_id=3, n_id=4)

        routes1 = [
            _make_straight_route(1, "USB_D+", 10.0),
            _make_straight_route(2, "USB_D-", 11.0),
        ]
        tracker = DiffPairLengthTracker()
        tracker.record_routes(routes1, [dp1])
        assert ("USB_D+", "USB_D-") in tracker.get_all_skews()

        routes2 = [
            _make_straight_route(3, "PCIE_TX_P", 7.0),
            _make_straight_route(4, "PCIE_TX_N", 8.0),
        ]
        tracker.record_routes(routes2, [dp2])
        skews = tracker.get_all_skews()
        assert ("PCIE_TX_P", "PCIE_TX_N") in skews
        assert ("USB_D+", "USB_D-") not in skews


# =============================================================================
# 4. NetClassRouting.effective_skew_tolerance round-trip
# =============================================================================


class TestEffectiveSkewTolerance:
    """Per-class override semantics for the skew tolerance accessor."""

    def test_default_unset_returns_half_mm(self):
        # The literal default arg is 0.5; this anchors the drift-prevention
        # cross-check with Issue J's DEFAULT_SKEW_TOLERANCE_MM (which must
        # equal 0.5 byte-for-byte once the J-side module exists).
        nc = NetClassRouting(name="Default")
        assert nc.effective_skew_tolerance() == 0.5

    def test_override_returns_explicit_value(self):
        nc = NetClassRouting(name="USB_HS", skew_tolerance_mm=0.3)
        assert nc.effective_skew_tolerance() == 0.3
        # The explicit default arg must be ignored when the field is set.
        assert nc.effective_skew_tolerance(default=99.0) == 0.3

    def test_default_arg_overrides_module_default_when_field_unset(self):
        # Mirrors :meth:`effective_coupled_continuity_threshold` semantics:
        # the default arg is the fallback when the field is None.
        nc = NetClassRouting(name="Default")
        assert nc.effective_skew_tolerance(default=1.0) == 1.0

    def test_field_round_trip_via_dataclass_construction(self):
        # Plain dataclass construction with the field must round-trip
        # through repr() and equality.
        nc1 = NetClassRouting(name="HS", skew_tolerance_mm=0.25)
        nc2 = NetClassRouting(name="HS", skew_tolerance_mm=0.25)
        assert nc1 == nc2
        assert nc1.skew_tolerance_mm == 0.25
        assert "skew_tolerance_mm=0.25" in repr(nc1)


# =============================================================================
# 5. Drift-prevention test (anticipating Issue J's DEFAULT_SKEW_TOLERANCE_MM)
# =============================================================================


class TestDriftPrevention:
    """The H accessor default must equal Issue J's DRC-rule constant byte-for-byte.

    Until Issue J lands the J-side module does not exist; the H-side test
    asserts the accessor returns ``0.5``.  The J-side PR will add the
    import-and-cross-check.  This mirrors the #2521 / #2640 alias-drift
    failure mode where two literal copies of the same threshold drifted.
    """

    def test_accessor_default_is_half_mm(self):
        # The literal 0.5 must appear in exactly two places per repo:
        # router/rules.py (accessor default arg, this assertion) and
        # validate/rules/diffpair_length_skew.py (DEFAULT_SKEW_TOLERANCE_MM,
        # added in Issue J).  Any third copy is drift.
        nc = NetClassRouting(name="Default")
        assert nc.effective_skew_tolerance() == 0.5

    def test_accessor_default_matches_issue_j_constant_when_available(self):
        # Best-effort cross-check: if Issue J has already landed,
        # import the constant and assert byte-for-byte equality.
        try:
            from kicad_tools.validate.rules.diffpair_length_skew import (
                DEFAULT_SKEW_TOLERANCE_MM,
            )
        except ImportError:
            # Issue J not yet merged -- the H-side test (above) anchors
            # the 0.5 default unilaterally.
            return
        nc = NetClassRouting(name="Default")
        assert nc.effective_skew_tolerance() == DEFAULT_SKEW_TOLERANCE_MM


# =============================================================================
# 6. Length-critical flag does NOT gate measurement (acceptance criterion)
# =============================================================================


class TestLengthCriticalFlagDoesNotGate:
    """Pairs whose net class has length_critical=False still get measured."""

    def test_length_critical_false_pair_still_measured(self):
        # length_critical defaults to False on most net classes; the
        # tracker must measure unconditionally (the flag is a routing
        # priority hint, not a skew-tracking gate).  The DRC rule (Issue J)
        # is responsible for deciding whether to fire.
        dp = _make_detected_pair(p_id=1, n_id=2)
        nc = NetClassRouting(name="Generic", length_critical=False)
        assert nc.length_critical is False

        p_route = _make_straight_route(1, "USB_D+", 10.0)
        n_route = _make_straight_route(2, "USB_D-", 12.0)
        tracker = DiffPairLengthTracker()
        tracker.record_routes([p_route, n_route], [dp])

        # The tracker doesn't know about net classes -- it always
        # measures.  This test documents the policy.
        assert tracker.get_skew(dp) == 2.0


# =============================================================================
# 7. Dormant-signal gate -- Autorouter.update_diffpair_skew (Issue #2587 lesson)
# =============================================================================


class TestUpdateDiffpairSkewProductionPath:
    """The production-code path that calls record_routes must be reachable.

    The #2587 lesson: a feature whose record-routes call is reached from
    no production code path is dormant and silently regresses.  This
    test invokes :meth:`Autorouter.update_diffpair_skew` and asserts the
    tracker is non-empty after the call.
    """

    def test_update_diffpair_skew_populates_tracker(self):
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.rules import DesignRules

        # Build a minimal Autorouter and inject synthetic routes.  We
        # don't actually invoke the routing engine -- this test isolates
        # the production-path wiring that update_diffpair_skew exercises.
        router = Autorouter(
            width=20.0,
            height=20.0,
            rules=DesignRules(),
        )
        router.routes = [
            _make_straight_route(1, "USB_D+", 10.0),
            _make_straight_route(2, "USB_D-", 12.0),
        ]

        dp = _make_detected_pair(p_id=1, n_id=2)
        tracker = router.update_diffpair_skew([dp])

        # Production path is reached and produces a non-empty tracker.
        assert tracker is router.diffpair_length_tracker
        assert tracker.lengths  # non-empty
        assert tracker.get_skew(dp) == 2.0

    def test_update_diffpair_skew_with_board_thickness(self):
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.rules import DesignRules

        router = Autorouter(
            width=20.0,
            height=20.0,
            rules=DesignRules(),
        )

        # P has a F.Cu -> B.Cu via on a 2-layer 1.6 mm board.
        p_route = Route(
            net=1,
            net_name="USB_D+",
            segments=[
                Segment(0.0, 0.0, 5.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+"),
                Segment(5.0, 0.0, 10.0, 0.0, 0.2, Layer.B_CU, net=1, net_name="USB_D+"),
            ],
            vias=[
                Via(
                    x=5.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(CopperLayer.F_CU, CopperLayer.B_CU),
                    net=1,
                    net_name="USB_D+",
                )
            ],
        )
        n_route = _make_straight_route(2, "USB_D-", 10.0)
        router.routes = [p_route, n_route]

        dp = _make_detected_pair(p_id=1, n_id=2)
        tracker = router.update_diffpair_skew([dp], board_thickness_mm=1.6, num_copper_layers=2)

        # P = 10 mm segments + 1.6 mm via = 11.6 mm; N = 10 mm.
        # Skew = 1.6 mm.
        assert abs(tracker.get_skew(dp) - 1.6) < 1e-9


# =============================================================================
# 8. Drift-prevention gate -- _finalize_routing wired into route_all_* strategies
#    (Issue #2657 / Epic #2556 Phase 3H-cont)
# =============================================================================


def _build_synthetic_usb_pair_autorouter():
    """Build a minimal Autorouter with a USB_D+/USB_D- pair routable end-to-end.

    Two components (``J1`` and ``U1``) sit on opposite sides of a small
    20x20 mm board, with matching ``USB_D+`` / ``USB_D-`` nets.  Suffix
    detection (``diffpair_detection.detect_diff_pairs``) picks them up
    as a pair, so any strategy that wires ``_finalize_routing``
    correctly will populate ``diffpair_length_tracker.get_all_skews()``.
    """
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(width=20.0, height=20.0)
    ar.add_component(
        "J1",
        [
            {
                "number": "1", "x": 2.0, "y": 10.0,
                "width": 0.5, "height": 0.5,
                "net": 1, "net_name": "USB_D+",
            },
            {
                "number": "2", "x": 2.0, "y": 12.0,
                "width": 0.5, "height": 0.5,
                "net": 2, "net_name": "USB_D-",
            },
        ],
    )
    ar.add_component(
        "U1",
        [
            {
                "number": "1", "x": 18.0, "y": 10.0,
                "width": 0.5, "height": 0.5,
                "net": 1, "net_name": "USB_D+",
            },
            {
                "number": "2", "x": 18.0, "y": 12.0,
                "width": 0.5, "height": 0.5,
                "net": 2, "net_name": "USB_D-",
            },
        ],
    )
    return ar


class TestFinalizeRoutingDriftPrevention:
    """Drift-prevention gate -- ``_finalize_routing`` wired into every strategy.

    Without this, an added strategy that bypasses ``_finalize_routing``
    would silently regress: its routes would land on ``self.routes`` but
    ``diffpair_length_tracker.get_all_skews()`` would stay empty.  The
    parametrized cases below pin the contract for each ``route_all_*``
    entry point so a missed wiring fails the suite immediately.

    See also: Issue #2587 (Phase 1C-cont) dormant-signal precedent and
    PR #2654's docstring guidance on the zero-via-length default.
    """

    @pytest.mark.parametrize(
        "strategy_name",
        [
            "route_all",
            "route_all_negotiated",
            "route_all_interleaved",
        ],
    )
    def test_strategy_populates_skew_tracker(self, strategy_name):
        ar = _build_synthetic_usb_pair_autorouter()

        # Each strategy is a top-level entry point on Autorouter that
        # must end with a _finalize_routing call.  We invoke through
        # getattr so the parametrize-id reflects which strategy regressed.
        strategy = getattr(ar, strategy_name)
        if strategy_name == "route_all_negotiated":
            # max_iterations=2 keeps the test fast; the contract is
            # invocation-shape, not convergence.
            strategy(max_iterations=2)
        else:
            strategy()

        # Acceptance criterion 1: get_all_skews() returns a non-empty
        # dict for a board with at least one diff pair routed.
        skews = ar.diffpair_length_tracker.get_all_skews()
        assert skews, (
            f"{strategy_name}: diffpair_length_tracker.get_all_skews() is empty -- "
            f"_finalize_routing is not being invoked on this strategy's return path"
        )

        # Acceptance criterion 2: the USB pair skew is a real float
        # (not None).  For these symmetric synthetic routes, the
        # measured skew is small; the contract is "non-None float",
        # not a specific value.
        assert ("USB_D+", "USB_D-") in skews
        skew_mm = skews[("USB_D+", "USB_D-")]
        assert isinstance(skew_mm, float)
        assert skew_mm >= 0.0  # |L_p - L_n| is always non-negative

    def test_finalize_routing_idempotent(self):
        """``_finalize_routing`` can be called multiple times safely.

        ``record_routes`` overwrites previously-recorded lengths, and
        ``update_diffpair_skew`` re-derives ``detected_pairs`` each call
        so repeated invocations leave the tracker in the same state as
        a single invocation (modulo non-determinism in detection, which
        is deterministic for these synthetic inputs).
        """
        ar = _build_synthetic_usb_pair_autorouter()
        ar.route_all()
        first_skew = dict(ar.diffpair_length_tracker.get_all_skews())

        # Direct second invocation must not corrupt the tracker.
        ar._finalize_routing()
        second_skew = dict(ar.diffpair_length_tracker.get_all_skews())

        assert first_skew == second_skew
        assert first_skew  # non-empty

    def test_finalize_routing_noop_when_no_pairs(self):
        """When no diff pairs are present, ``_finalize_routing`` is a no-op.

        Documents the contract that ``_finalize_routing`` does NOT
        spuriously populate the tracker for boards without diff pairs.
        """
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=20.0, height=20.0)
        ar.add_component(
            "J1",
            [
                {
                    "number": "1", "x": 2.0, "y": 10.0,
                    "width": 0.5, "height": 0.5,
                    "net": 1, "net_name": "SIG_A",
                },
            ],
        )
        ar.add_component(
            "U1",
            [
                {
                    "number": "1", "x": 18.0, "y": 10.0,
                    "width": 0.5, "height": 0.5,
                    "net": 1, "net_name": "SIG_A",
                },
            ],
        )
        ar.route_all()

        # No pairs -> no skews recorded.  Tracker stays empty.
        assert ar.diffpair_length_tracker.get_all_skews() == {}
