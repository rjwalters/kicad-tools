"""Tests for negotiated routing of HIGH_CURRENT_SIGNAL nets sharing a destination.

Issue #2475: Three motor phase nets (PHASE_A/B/C) all classified as
HIGH_CURRENT_SIGNAL contend at priority 1 for the same connector pin field.
The targeted-ripup logic must consider same-tier sibling nets that share a
destination component as blockers, even if they don't sit on the failed
net's direct A* line.

These tests exercise the helpers that were added in core.py and confirm
they correctly detect siblings, partial routes, and class priorities.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import (
    NET_CLASS_HIGH_CURRENT_SIGNAL,
    NET_CLASS_POWER,
    DesignRules,
    NetClassRouting,
)


def _make_autorouter_with_three_phase_nets() -> Autorouter:
    """Build a minimal autorouter with three HIGH_CURRENT_SIGNAL nets sharing J2.

    All three PHASE_A/B/C nets terminate at the J2 6-pin connector.  This
    is the canonical "three siblings sharing a destination" configuration
    that the rip-up logic needs to handle.
    """
    rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
    net_class_map = {
        "PHASE_A": NET_CLASS_HIGH_CURRENT_SIGNAL,
        "PHASE_B": NET_CLASS_HIGH_CURRENT_SIGNAL,
        "PHASE_C": NET_CLASS_HIGH_CURRENT_SIGNAL,
    }
    router = Autorouter(
        width=50.0,
        height=50.0,
        rules=rules,
        net_class_map=net_class_map,
    )

    # Each phase has a source pad on a half-bridge "Q?" component plus a
    # destination pad on the shared J2 connector.
    router.add_component("Q1", [
        {"number": "1", "x": 5.0, "y": 5.0, "width": 0.5, "height": 0.5,
         "net": 1, "net_name": "PHASE_A"},
    ])
    router.add_component("Q2", [
        {"number": "1", "x": 5.0, "y": 10.0, "width": 0.5, "height": 0.5,
         "net": 2, "net_name": "PHASE_B"},
    ])
    router.add_component("Q3", [
        {"number": "1", "x": 5.0, "y": 15.0, "width": 0.5, "height": 0.5,
         "net": 3, "net_name": "PHASE_C"},
    ])
    router.add_component("J2", [
        {"number": "1", "x": 30.0, "y": 5.0, "width": 0.5, "height": 0.5,
         "net": 1, "net_name": "PHASE_A"},
        {"number": "2", "x": 30.0, "y": 10.0, "width": 0.5, "height": 0.5,
         "net": 2, "net_name": "PHASE_B"},
        {"number": "3", "x": 30.0, "y": 15.0, "width": 0.5, "height": 0.5,
         "net": 3, "net_name": "PHASE_C"},
    ])
    return router


class TestGetNetClassPriority:
    """Helper: extract just the class priority from the 6-tuple sort key."""

    def test_high_current_signal_returns_one(self):
        router = _make_autorouter_with_three_phase_nets()
        assert router._get_net_class_priority(1) == 1
        assert router._get_net_class_priority(2) == 1
        assert router._get_net_class_priority(3) == 1

    def test_unmapped_net_returns_default_priority(self):
        router = Autorouter(width=10.0, height=10.0)
        # No net registered -- helper returns 10 (default).
        assert router._get_net_class_priority(99) == 10

    def test_pour_net_returns_99(self):
        rules = DesignRules()
        gnd_class = NetClassRouting(
            name="Ground", priority=1, trace_width=0.3,
            clearance=0.2, is_pour_net=True,
        )
        net_class_map = {"GND": gnd_class}
        router = Autorouter(width=10.0, height=10.0, rules=rules,
                            net_class_map=net_class_map)
        router.add_component("U1", [
            {"number": "1", "x": 1.0, "y": 1.0, "net": 5, "net_name": "GND"},
        ])
        # Pour nets get sentinel priority 99 so they sort to the back.
        assert router._get_net_class_priority(5) == 99


class TestGetNetDestinationComponents:
    """Helper: collect component refs touched by a net's pads."""

    def test_phase_a_destinations_include_q1_and_j2(self):
        router = _make_autorouter_with_three_phase_nets()
        comps = router._get_net_destination_components(1)
        assert comps == {"Q1", "J2"}

    def test_phase_c_destinations_include_q3_and_j2(self):
        router = _make_autorouter_with_three_phase_nets()
        comps = router._get_net_destination_components(3)
        assert comps == {"Q3", "J2"}

    def test_unknown_net_returns_empty(self):
        router = _make_autorouter_with_three_phase_nets()
        assert router._get_net_destination_components(999) == set()


class TestFindSameTierDestinationSiblings:
    """Helper: identify same-priority same-destination sibling nets."""

    def test_phase_a_finds_phase_b_and_c_as_siblings(self):
        """All three PHASE_* nets share J2 at priority 1."""
        router = _make_autorouter_with_three_phase_nets()
        siblings = router._find_same_tier_destination_siblings(
            failed_net=1, candidate_nets=[2, 3]
        )
        assert siblings == {2, 3}

    def test_failed_net_excluded_from_own_siblings(self):
        router = _make_autorouter_with_three_phase_nets()
        # Pass net 1 itself in the candidate list -- it must not appear.
        siblings = router._find_same_tier_destination_siblings(
            failed_net=1, candidate_nets=[1, 2, 3]
        )
        assert 1 not in siblings
        assert siblings == {2, 3}

    def test_default_priority_returns_empty_set(self):
        """Priority 10 (default) is too broad for sibling detection."""
        rules = DesignRules()
        router = Autorouter(width=20.0, height=20.0, rules=rules)
        router.add_component("U1", [
            {"number": "1", "x": 1.0, "y": 1.0, "net": 1, "net_name": "SIG_A"},
        ])
        router.add_component("J1", [
            {"number": "1", "x": 10.0, "y": 1.0, "net": 1, "net_name": "SIG_A"},
            {"number": "2", "x": 10.0, "y": 2.0, "net": 2, "net_name": "SIG_B"},
        ])
        router.add_component("U2", [
            {"number": "1", "x": 1.0, "y": 2.0, "net": 2, "net_name": "SIG_B"},
        ])
        # Both nets are at default priority 10 -- helper must NOT mark them
        # as siblings (this would cause indiscriminate rip-up).
        siblings = router._find_same_tier_destination_siblings(
            failed_net=1, candidate_nets=[2]
        )
        assert siblings == set()

    def test_different_priority_excluded(self):
        """A POWER-tier net must not be flagged as a sibling of a CLOCK-tier net."""
        net_class_map = {
            "VCC": NET_CLASS_POWER,
            "PHASE_A": NET_CLASS_HIGH_CURRENT_SIGNAL,
        }
        # POWER is priority 1, HIGH_CURRENT_SIGNAL is also priority 1, so
        # they do match.  Use a 2-priority class (CLOCK) for a non-match.
        from kicad_tools.router.rules import NET_CLASS_CLOCK
        net_class_map["CLK"] = NET_CLASS_CLOCK
        router = Autorouter(
            width=20.0, height=20.0,
            net_class_map=net_class_map,
        )
        router.add_component("U1", [
            {"number": "1", "x": 1.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
            {"number": "2", "x": 1.0, "y": 2.0, "net": 2, "net_name": "CLK"},
        ])
        router.add_component("J1", [
            {"number": "1", "x": 10.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
            {"number": "2", "x": 10.0, "y": 2.0, "net": 2, "net_name": "CLK"},
        ])
        # PHASE_A is priority 1, CLK is priority 2 -- not siblings.
        siblings = router._find_same_tier_destination_siblings(
            failed_net=1, candidate_nets=[2]
        )
        assert siblings == set()

    def test_no_shared_destination_returns_empty(self):
        """Two HIGH_CURRENT_SIGNAL nets with no shared destination component."""
        net_class_map = {
            "PHASE_A": NET_CLASS_HIGH_CURRENT_SIGNAL,
            "PHASE_B": NET_CLASS_HIGH_CURRENT_SIGNAL,
        }
        router = Autorouter(width=30.0, height=30.0, net_class_map=net_class_map)
        router.add_component("Q1", [
            {"number": "1", "x": 1.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
        ])
        router.add_component("J1", [
            {"number": "1", "x": 10.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
        ])
        router.add_component("Q2", [
            {"number": "1", "x": 1.0, "y": 20.0, "net": 2, "net_name": "PHASE_B"},
        ])
        router.add_component("J2", [
            {"number": "1", "x": 20.0, "y": 20.0, "net": 2, "net_name": "PHASE_B"},
        ])
        # PHASE_A and PHASE_B share no destination -- not siblings for rip-up.
        siblings = router._find_same_tier_destination_siblings(
            failed_net=1, candidate_nets=[2]
        )
        assert siblings == set()


class TestGetPartiallyRoutedNets:
    """Helper: detect nets that have routes but didn't connect every pad."""

    def test_no_routes_returns_empty(self):
        router = _make_autorouter_with_three_phase_nets()
        partial = router._get_partially_routed_nets({}, {})
        assert partial == set()

    def test_fully_connected_net_not_partial(self):
        router = _make_autorouter_with_three_phase_nets()
        # Build a route from Q1 (5,5) to J2-pad1 (30,5).
        seg = Segment(
            x1=5.0, y1=5.0, x2=30.0, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=1, net_name="PHASE_A",
        )
        route = Route(net=1, net_name="PHASE_A", segments=[seg], vias=[])
        net_routes = {1: [route]}
        pads_by_net = {
            1: [router.pads[("Q1", "1")], router.pads[("J2", "1")]],
        }
        partial = router._get_partially_routed_nets(net_routes, pads_by_net)
        assert partial == set()

    def test_unconnected_pad_is_partial(self):
        """A 2-pad net whose route doesn't reach one pad is flagged partial."""
        router = _make_autorouter_with_three_phase_nets()
        # Route ends at (29.0, 5.0) -- 1mm short of J2:1 at (30.0, 5.0).
        # Default tolerance in validate_net_connectivity is 2mm so this
        # would actually be considered close enough.  Use a larger gap.
        seg = Segment(
            x1=5.0, y1=5.0, x2=15.0, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=1, net_name="PHASE_A",
        )
        route = Route(net=1, net_name="PHASE_A", segments=[seg], vias=[])
        net_routes = {1: [route]}
        pads_by_net = {
            1: [router.pads[("Q1", "1")], router.pads[("J2", "1")]],
        }
        partial = router._get_partially_routed_nets(net_routes, pads_by_net)
        assert 1 in partial


class TestHighCurrentSignalPriority:
    """Sanity: confirm HIGH_CURRENT_SIGNAL priority matches POWER tier (#2465)."""

    def test_high_current_signal_priority_is_one(self):
        assert NET_CLASS_HIGH_CURRENT_SIGNAL.priority == 1

    def test_high_current_signal_matches_power_tier(self):
        """Both classes share priority 1 so motor phases route alongside power."""
        assert NET_CLASS_HIGH_CURRENT_SIGNAL.priority == NET_CLASS_POWER.priority
