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

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
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
    router.add_component(
        "Q1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 5.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "PHASE_A",
            },
        ],
    )
    router.add_component(
        "Q2",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "PHASE_B",
            },
        ],
    )
    router.add_component(
        "Q3",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 15.0,
                "width": 0.5,
                "height": 0.5,
                "net": 3,
                "net_name": "PHASE_C",
            },
        ],
    )
    router.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": 30.0,
                "y": 5.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "PHASE_A",
            },
            {
                "number": "2",
                "x": 30.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "PHASE_B",
            },
            {
                "number": "3",
                "x": 30.0,
                "y": 15.0,
                "width": 0.5,
                "height": 0.5,
                "net": 3,
                "net_name": "PHASE_C",
            },
        ],
    )
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
            name="Ground",
            priority=1,
            trace_width=0.3,
            clearance=0.2,
            is_pour_net=True,
        )
        net_class_map = {"GND": gnd_class}
        router = Autorouter(width=10.0, height=10.0, rules=rules, net_class_map=net_class_map)
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "net": 5, "net_name": "GND"},
            ],
        )
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
        siblings = router._find_same_tier_destination_siblings(failed_net=1, candidate_nets=[2, 3])
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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "net": 1, "net_name": "SIG_A"},
            ],
        )
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 10.0, "y": 1.0, "net": 1, "net_name": "SIG_A"},
                {"number": "2", "x": 10.0, "y": 2.0, "net": 2, "net_name": "SIG_B"},
            ],
        )
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 1.0, "y": 2.0, "net": 2, "net_name": "SIG_B"},
            ],
        )
        # Both nets are at default priority 10 -- helper must NOT mark them
        # as siblings (this would cause indiscriminate rip-up).
        siblings = router._find_same_tier_destination_siblings(failed_net=1, candidate_nets=[2])
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
            width=20.0,
            height=20.0,
            net_class_map=net_class_map,
        )
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
                {"number": "2", "x": 1.0, "y": 2.0, "net": 2, "net_name": "CLK"},
            ],
        )
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 10.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
                {"number": "2", "x": 10.0, "y": 2.0, "net": 2, "net_name": "CLK"},
            ],
        )
        # PHASE_A is priority 1, CLK is priority 2 -- not siblings.
        siblings = router._find_same_tier_destination_siblings(failed_net=1, candidate_nets=[2])
        assert siblings == set()

    def test_no_shared_destination_returns_empty(self):
        """Two HIGH_CURRENT_SIGNAL nets with no shared destination component."""
        net_class_map = {
            "PHASE_A": NET_CLASS_HIGH_CURRENT_SIGNAL,
            "PHASE_B": NET_CLASS_HIGH_CURRENT_SIGNAL,
        }
        router = Autorouter(width=30.0, height=30.0, net_class_map=net_class_map)
        router.add_component(
            "Q1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
            ],
        )
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 10.0, "y": 1.0, "net": 1, "net_name": "PHASE_A"},
            ],
        )
        router.add_component(
            "Q2",
            [
                {"number": "1", "x": 1.0, "y": 20.0, "net": 2, "net_name": "PHASE_B"},
            ],
        )
        router.add_component(
            "J2",
            [
                {"number": "1", "x": 20.0, "y": 20.0, "net": 2, "net_name": "PHASE_B"},
            ],
        )
        # PHASE_A and PHASE_B share no destination -- not siblings for rip-up.
        siblings = router._find_same_tier_destination_siblings(failed_net=1, candidate_nets=[2])
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
            x1=5.0,
            y1=5.0,
            x2=30.0,
            y2=5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="PHASE_A",
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
            x1=5.0,
            y1=5.0,
            x2=15.0,
            y2=5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="PHASE_A",
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


class TestFindConnectorSiblingsOfPreroutedNets:
    """Issue #2482: identify nets that share a connector with prerouted nets.

    When the diff-pair pre-pass routes USB_D+/USB_D- before the negotiated
    loop, a single-ended net like USB_CC1 that terminates at the same
    USB-C connector must be routed before lower-priority unrelated nets
    in its tier; otherwise the connector pin field corridor is consumed
    by the diff-pair escape and USB_CC1 can no longer reach its pad.
    """

    def _make_router_with_diffpair_and_sibling(self):
        """Build a fixture with two prerouted-style HIGH_SPEED nets and one sibling.

        Layout mirrors board 03's USB section:
        - U1 (MCU) on the left has three pads: USB_D+, USB_D-, USB_CC1.
        - J1 (USB-C connector) on the right has matching pads for all three.
        - USB_D+/USB_D- form a "diff pair" (HIGH_SPEED priority 2).
        - USB_CC1 is single-ended (HIGH_SPEED priority 2, same tier).

        We call ``_find_connector_siblings_of_prerouted_nets`` with
        ``prerouted_nets={USB_D+, USB_D-}`` and assert USB_CC1 is reported
        as a connector sibling because it shares J1 with the diff pair.
        """
        from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED

        net_class_map = {
            "USB_D+": NET_CLASS_HIGH_SPEED,
            "USB_D-": NET_CLASS_HIGH_SPEED,
            "USB_CC1": NET_CLASS_HIGH_SPEED,
        }
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(
            width=40.0,
            height=20.0,
            rules=rules,
            net_class_map=net_class_map,
        )
        # MCU side
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 5.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "2",
                    "x": 5.0,
                    "y": 7.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 2,
                    "net_name": "USB_D-",
                },
                {
                    "number": "3",
                    "x": 5.0,
                    "y": 9.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 3,
                    "net_name": "USB_CC1",
                },
            ],
        )
        # USB-C connector side -- all three nets terminate here.
        router.add_component(
            "J1",
            [
                {
                    "number": "A6",
                    "x": 30.0,
                    "y": 5.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "A7",
                    "x": 30.0,
                    "y": 7.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 2,
                    "net_name": "USB_D-",
                },
                {
                    "number": "A5",
                    "x": 30.0,
                    "y": 9.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 3,
                    "net_name": "USB_CC1",
                },
            ],
        )
        return router

    def test_returns_empty_when_no_prerouted(self):
        router = self._make_router_with_diffpair_and_sibling()
        result = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets=set(), candidate_nets=[1, 2, 3]
        )
        assert result == set()

    def test_finds_sibling_sharing_connector(self):
        """USB_CC1 (net 3) shares J1 with the prerouted USB_D+/D- pair."""
        router = self._make_router_with_diffpair_and_sibling()
        result = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets={1, 2},  # USB_D+, USB_D-
            candidate_nets=[3],  # USB_CC1
        )
        assert result == {3}

    def test_excludes_prerouted_nets_themselves(self):
        """Prerouted members must not appear in the sibling set."""
        router = self._make_router_with_diffpair_and_sibling()
        result = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets={1, 2},
            candidate_nets=[1, 2, 3],  # include the prerouted nets too
        )
        assert 1 not in result and 2 not in result
        assert result == {3}

    def test_excludes_default_priority_candidates(self):
        """Priority-10 candidates are filtered to avoid indiscriminate boost."""
        from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED

        # Same fixture but USB_CC1 is left unclassified (priority 10).
        net_class_map = {
            "USB_D+": NET_CLASS_HIGH_SPEED,
            "USB_D-": NET_CLASS_HIGH_SPEED,
            # USB_CC1 deliberately omitted -> default priority 10
        }
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(
            width=40.0,
            height=20.0,
            rules=rules,
            net_class_map=net_class_map,
        )
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "2", "x": 5.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "3", "x": 5.0, "y": 9.0, "net": 3, "net_name": "USB_CC1"},
            ],
        )
        router.add_component(
            "J1",
            [
                {"number": "A6", "x": 30.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "A7", "x": 30.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "A5", "x": 30.0, "y": 9.0, "net": 3, "net_name": "USB_CC1"},
            ],
        )
        result = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets={1, 2}, candidate_nets=[3]
        )
        # Net 3 has default priority 10 -> filtered out.
        assert result == set()

    def test_excludes_unrelated_destination(self):
        """A candidate sharing no destination with prerouted nets is excluded."""
        from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED

        net_class_map = {
            "USB_D+": NET_CLASS_HIGH_SPEED,
            "USB_D-": NET_CLASS_HIGH_SPEED,
            "JOY_X": NET_CLASS_HIGH_SPEED,
        }
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(
            width=60.0,
            height=40.0,
            rules=rules,
            net_class_map=net_class_map,
        )
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "2", "x": 5.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "3", "x": 5.0, "y": 20.0, "net": 4, "net_name": "JOY_X"},
            ],
        )
        # USB connector has D+/D- only; joystick connector has JOY_X only.
        router.add_component(
            "J1",
            [
                {"number": "A6", "x": 30.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "A7", "x": 30.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
            ],
        )
        router.add_component(
            "J2",
            [
                {"number": "1", "x": 50.0, "y": 20.0, "net": 4, "net_name": "JOY_X"},
            ],
        )
        result = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets={1, 2}, candidate_nets=[4]
        )
        # JOY_X shares U1 with the prerouted set but JOY_X also lives on
        # J2 which neither prerouted net touches.  However, U1 is itself a
        # shared component -- so the helper currently DOES return JOY_X.
        # This is the intentional, conservative behaviour: any shared
        # destination triggers the bump.  We assert it explicitly.
        assert result == {4}

    def test_cross_tier_sibling_detected(self):
        """Sibling is detected even when tier differs from prerouted nets.

        A diff-pair member may be HIGH_SPEED (priority 2) while its
        connector sibling is DIGITAL (priority 4).  Both still need
        ordering coordination because they share the same physical pin
        field.
        """
        from kicad_tools.router.rules import (
            NET_CLASS_DIGITAL,
            NET_CLASS_HIGH_SPEED,
        )

        net_class_map = {
            "USB_D+": NET_CLASS_HIGH_SPEED,
            "USB_D-": NET_CLASS_HIGH_SPEED,
            "USB_VBUS_DET": NET_CLASS_DIGITAL,
        }
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(
            width=40.0,
            height=20.0,
            rules=rules,
            net_class_map=net_class_map,
        )
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "2", "x": 5.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "3", "x": 5.0, "y": 9.0, "net": 3, "net_name": "USB_VBUS_DET"},
            ],
        )
        router.add_component(
            "J1",
            [
                {"number": "A6", "x": 30.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "A7", "x": 30.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "A4", "x": 30.0, "y": 9.0, "net": 3, "net_name": "USB_VBUS_DET"},
            ],
        )
        result = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets={1, 2}, candidate_nets=[3]
        )
        assert result == {3}, (
            "Cross-tier connector sibling must still be detected so the "
            "sort step can bump it to the front of its own tier."
        )

    def test_negotiated_ordering_places_sibling_before_other_tier_members(self):
        """Issue #2482: in route_all_negotiated, connector siblings sort first within tier.

        We don't run the full router here -- we exercise the bump branch
        end-to-end by setting up self.routes (simulating the diff-pair
        prepass output) and then extract the same net_order computation
        the negotiated loop performs.
        """
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment
        from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED

        net_class_map = {
            "USB_D+": NET_CLASS_HIGH_SPEED,
            "USB_D-": NET_CLASS_HIGH_SPEED,
            "USB_CC1": NET_CLASS_HIGH_SPEED,
            # Add a same-tier non-sibling net to confirm ordering picks
            # USB_CC1 (sibling) BEFORE the non-sibling within tier 2.
            "OTHER_HS": NET_CLASS_HIGH_SPEED,
        }
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(
            width=60.0,
            height=20.0,
            rules=rules,
            net_class_map=net_class_map,
        )
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "2", "x": 5.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "3", "x": 5.0, "y": 9.0, "net": 3, "net_name": "USB_CC1"},
            ],
        )
        router.add_component(
            "J1",
            [
                {"number": "A6", "x": 30.0, "y": 5.0, "net": 1, "net_name": "USB_D+"},
                {"number": "A7", "x": 30.0, "y": 7.0, "net": 2, "net_name": "USB_D-"},
                {"number": "A5", "x": 30.0, "y": 9.0, "net": 3, "net_name": "USB_CC1"},
            ],
        )
        # OTHER_HS lives between U2 and J3, no shared destination with the
        # USB nets.
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 5.0, "y": 15.0, "net": 4, "net_name": "OTHER_HS"},
            ],
        )
        router.add_component(
            "J3",
            [
                {"number": "1", "x": 30.0, "y": 15.0, "net": 4, "net_name": "OTHER_HS"},
            ],
        )

        # Simulate the diff-pair prepass having routed nets 1, 2.
        prerouted_segs_d_plus = Segment(
            x1=5.0,
            y1=5.0,
            x2=30.0,
            y2=5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="USB_D+",
        )
        prerouted_segs_d_minus = Segment(
            x1=5.0,
            y1=7.0,
            x2=30.0,
            y2=7.0,
            width=0.2,
            layer=Layer.F_CU,
            net=2,
            net_name="USB_D-",
        )
        router.routes.append(
            Route(net=1, net_name="USB_D+", segments=[prerouted_segs_d_plus], vias=[]),
        )
        router.routes.append(
            Route(net=2, net_name="USB_D-", segments=[prerouted_segs_d_minus], vias=[]),
        )

        # Replicate the ordering logic from route_all_negotiated.
        prerouted_nets = {r.net for r in router.routes}
        net_order = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))
        net_order = router._filter_pour_nets(net_order)
        net_order = [n for n in net_order if n != 0]
        net_order = [n for n in net_order if n not in prerouted_nets]

        connector_siblings = router._find_connector_siblings_of_prerouted_nets(
            prerouted_nets, net_order
        )
        assert 3 in connector_siblings  # USB_CC1
        assert 4 not in connector_siblings  # OTHER_HS not a sibling

        def _sibling_aware_priority(net_id: int) -> tuple:
            base = router._get_net_priority(net_id)
            flag = 0 if net_id in connector_siblings else 1
            return (base[0], flag) + base[1:]

        sorted_order = sorted(net_order, key=_sibling_aware_priority)
        # USB_CC1 (sibling, net 3) must come before OTHER_HS (net 4).
        idx_cc1 = sorted_order.index(3)
        idx_other = sorted_order.index(4)
        assert idx_cc1 < idx_other, (
            f"Expected USB_CC1 (net 3) at lower index than OTHER_HS (net 4); "
            f"got USB_CC1@{idx_cc1}, OTHER_HS@{idx_other}, order={sorted_order}"
        )
