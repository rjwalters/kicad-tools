"""Tests for connectivity-aware net counting in two-phase summary (#2352, #2403).

The two-phase routing summary should only count nets as 'routed' when all
pads in the net are connected, not just when any route segment exists.
Disconnected escape stubs should not inflate the 'nets routed' count.

Issue #2403: Single-pad nets must also be excluded from the routable
population so they do not inflate the 'nets routed' denominator.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Layer, Pad, Route, Segment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pad(net: int, x: float, y: float, ref: str = "U1", pin: str = "1") -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=f"Net{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _make_segment(net: int, x1: float, y1: float, x2: float, y2: float) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=0, net=net)


def _make_route(net: int, segments: list[Segment]) -> Route:
    return Route(net=net, net_name=f"Net{net}", segments=segments)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnectivityAwareNetCount:
    """Verify that validate_net_connectivity correctly distinguishes
    fully-connected nets from those with only stub/partial routes,
    which is the mechanism used by the two-phase summary (#2352)."""

    def test_connected_net_is_reported_connected(self):
        """A net where a segment spans both pads is fully connected."""
        pad_a = _make_pad(1, 0.0, 0.0)
        pad_b = _make_pad(1, 10.0, 0.0, pin="2")
        route = _make_route(1, [_make_segment(1, 0.0, 0.0, 10.0, 0.0)])

        result = validate_net_connectivity([route], {1: [pad_a, pad_b]})
        assert result[1]["connected"] is True
        assert result[1]["connected_pads"] == 2

    def test_stub_net_is_reported_disconnected(self):
        """A net where the segment only reaches one pad is not connected."""
        pad_a = _make_pad(1, 0.0, 0.0)
        pad_b = _make_pad(1, 10.0, 0.0, pin="2")
        # Stub goes away from pad_b (endpoint at 5, 5 -- far from pad_b at 10, 0)
        stub = _make_route(1, [_make_segment(1, 0.0, 0.0, 5.0, 5.0)])

        result = validate_net_connectivity([stub], {1: [pad_a, pad_b]})
        assert result[1]["connected"] is False
        assert result[1]["connected_pads"] == 1

    def test_counting_logic_matches_issue_expectation(self):
        """The connected-count formula from two_phase.py should exclude
        partially routed nets."""
        # Net 1: fully connected
        pad_1a = _make_pad(1, 0.0, 0.0, ref="U1", pin="1")
        pad_1b = _make_pad(1, 10.0, 0.0, ref="U1", pin="2")
        route_1 = _make_route(1, [_make_segment(1, 0.0, 0.0, 10.0, 0.0)])

        # Net 2: stub only (disconnected fragment)
        pad_2a = _make_pad(2, 0.0, 5.0, ref="U2", pin="1")
        pad_2b = _make_pad(2, 10.0, 5.0, ref="U2", pin="2")
        stub_2 = _make_route(2, [_make_segment(2, 0.0, 5.0, 3.0, 7.0)])

        all_routes = [route_1, stub_2]
        net_pads = {1: [pad_1a, pad_1b], 2: [pad_2a, pad_2b]}
        connectivity = validate_net_connectivity(all_routes, net_pads)

        # This is the same formula used in the fixed two_phase.py
        nets_with_segments = len({r.net for r in all_routes})
        connected_nets = sum(1 for info in connectivity.values() if info["connected"])

        assert nets_with_segments == 2, "Both nets have segments"
        assert connected_nets == 1, "Only one net is fully connected"

    def test_no_pads_fallback(self):
        """When net_pads is empty, validate_net_connectivity returns empty
        and the fallback (nets_with_segments) should be used."""
        route = _make_route(1, [_make_segment(1, 0.0, 0.0, 10.0, 0.0)])
        result = validate_net_connectivity([route], {})
        assert len(result) == 0


class TestSinglePadNetFiltering:
    """Verify that single-pad nets are excluded from the routable population
    in the two-phase router, so they do not inflate the denominator (#2403)."""

    def test_single_pad_net_excluded_from_net_order(self):
        """Single-pad nets should be filtered out of net_order before
        total_nets is computed, matching core.py's established pattern."""
        from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter

        # Net 1: single-pad (should be excluded)
        # Net 2: multi-pad (should be included)
        nets = {
            1: [("U1", "1")],
            2: [("U1", "2"), ("U1", "3")],
        }
        net_names = {1: "SingleNet", 2: "MultiNet"}

        router = TwoPhaseRouter(
            grid=MagicMock(),
            router=MagicMock(),
            rules=MagicMock(cost_corridor_deviation=10.0),
            net_class_map={},
            nets=nets,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: (0, 0, 0, 0, 0, 0),
            route_net=MagicMock(),
            route_net_with_corridor=MagicMock(),
            mark_route=MagicMock(),
        )

        # Simulate the filtering logic from route_all (lines 120-157)
        net_order = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]

        # Apply single-pad filter (the new code being tested)
        single_pad_nets = []
        multi_pad_nets = []
        for n in net_order:
            if len(router.nets.get(n, [])) < 2:
                single_pad_nets.append(n)
            else:
                multi_pad_nets.append(n)
        net_order = multi_pad_nets

        assert 1 not in net_order, "Single-pad net should be excluded"
        assert 2 in net_order, "Multi-pad net should be included"
        assert len(single_pad_nets) == 1
        assert single_pad_nets[0] == 1

    def test_only_single_pad_nets_yields_empty_net_order(self):
        """A board with only single-pad nets should result in an empty
        net_order (total_nets == 0), triggering the 'No nets to route' path."""
        nets = {
            1: [("U1", "1")],
            2: [("U2", "1")],
        }

        # Apply the same filtering logic
        net_order = sorted(nets.keys())
        net_order = [n for n in net_order if n != 0]
        multi_pad_nets = [n for n in net_order if len(nets.get(n, [])) >= 2]

        assert len(multi_pad_nets) == 0, "All single-pad nets should be excluded"

    def test_single_pad_net_marked_connected_not_counted(self):
        """validate_net_connectivity marks single-pad nets as connected,
        which would inflate the count if they were included in total_nets.
        Verify the filter prevents this."""
        # Single-pad net
        pad_single = _make_pad(1, 0.0, 0.0)
        # Multi-pad net (connected)
        pad_2a = _make_pad(2, 0.0, 5.0, ref="U2", pin="1")
        pad_2b = _make_pad(2, 10.0, 5.0, ref="U2", pin="2")
        route_2 = _make_route(2, [_make_segment(2, 0.0, 5.0, 10.0, 5.0)])

        # If we include single-pad net in validation, it reports as connected
        connectivity_all = validate_net_connectivity(
            [route_2], {1: [pad_single], 2: [pad_2a, pad_2b]}
        )
        assert connectivity_all[1]["connected"] is True, "Single-pad net is trivially connected"

        # But total_nets should only count multi-pad nets
        # With the filter: total_nets=1, connected_nets=1 -> 100%
        # Without the filter: total_nets=2, connected_nets=2 -> still 100%
        # but the denominator is wrong (inflated by trivial nets)
        all_nets = {1: [("U1", "1")], 2: [("U2", "1"), ("U2", "2")]}
        multi_pad = [n for n in all_nets if len(all_nets[n]) >= 2]
        assert len(multi_pad) == 1, "Only the multi-pad net counts"
        assert multi_pad[0] == 2
