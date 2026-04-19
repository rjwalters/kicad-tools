"""Tests for net-class-aware trace widths in segment creation.

Issue #1543: Verifies that the autorouter uses per-net-class trace widths
when creating segments, rather than always using the global rules.trace_width.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.path import _get_trace_width_for_net, create_intra_ic_routes
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import (
    NET_CLASS_DIGITAL,
    NET_CLASS_POWER,
    DesignRules,
    NetClassRouting,
)


class TestGetTraceWidthForNet:
    """Test the helper function for net-class trace width lookup."""

    def test_power_net_gets_wide_trace(self):
        rules = DesignRules(trace_width=0.2)
        net_class_map = {"+5V": NET_CLASS_POWER}
        width = _get_trace_width_for_net("+5V", rules, net_class_map)
        assert width == NET_CLASS_POWER.trace_width
        assert width == 0.5

    def test_digital_net_gets_standard_trace(self):
        rules = DesignRules(trace_width=0.2)
        net_class_map = {"SIG1": NET_CLASS_DIGITAL}
        width = _get_trace_width_for_net("SIG1", rules, net_class_map)
        assert width == NET_CLASS_DIGITAL.trace_width
        assert width == 0.2

    def test_unknown_net_falls_back_to_rules(self):
        rules = DesignRules(trace_width=0.15)
        net_class_map = {"+5V": NET_CLASS_POWER}
        width = _get_trace_width_for_net("UNKNOWN_NET", rules, net_class_map)
        assert width == 0.15

    def test_none_net_class_map_falls_back(self):
        rules = DesignRules(trace_width=0.25)
        width = _get_trace_width_for_net("ANY_NET", rules, None)
        assert width == 0.25

    def test_empty_net_class_map_falls_back(self):
        rules = DesignRules(trace_width=0.18)
        width = _get_trace_width_for_net("ANY_NET", rules, {})
        assert width == 0.18


class TestRouterNetClassWidth:
    """Test that Router._get_trace_width_for_net uses the net class map."""

    def test_router_uses_net_class_width(self):
        rules = DesignRules(trace_width=0.2)
        net_class_map = {"+5V": NET_CLASS_POWER, "GND": NET_CLASS_POWER}
        grid = RoutingGrid(10, 10, rules)
        router = Router(grid, rules, net_class_map=net_class_map)

        assert router._get_trace_width_for_net("+5V") == 0.5
        assert router._get_trace_width_for_net("GND") == 0.5
        assert router._get_trace_width_for_net("SIG1") == 0.2  # fallback

    def test_router_routes_with_net_class_width(self):
        """Integration test: route a power net and verify segment widths."""
        rules = DesignRules(trace_width=0.2, grid_resolution=0.5)
        net_class_map = {"+5V": NET_CLASS_POWER}
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules, net_class_map=net_class_map)

        # Create two pads on the +5V net
        pad1 = Pad(
            ref="C1", pin="1", x=2.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )
        pad2 = Pad(
            ref="C2", pin="1", x=8.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )

        grid.add_pad(pad1)
        grid.add_pad(pad2)

        route = router.route(pad1, pad2)
        assert route is not None, "Routing should succeed"
        assert len(route.segments) > 0, "Route should have segments"

        # All segments should use the POWER net class width (0.5mm),
        # not the default rules width (0.2mm)
        for seg in route.segments:
            assert seg.width == 0.5, (
                f"Segment width should be 0.5mm (POWER class), got {seg.width}mm"
            )

    def test_router_signal_net_uses_default_width(self):
        """Verify that signal nets (not in net_class_map) use rules.trace_width."""
        rules = DesignRules(trace_width=0.2, grid_resolution=0.5)
        net_class_map = {"+5V": NET_CLASS_POWER}
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules, net_class_map=net_class_map)

        # Create two pads on a signal net (not in net_class_map)
        pad1 = Pad(
            ref="U1", pin="1", x=2.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=2, net_name="DATA",
        )
        pad2 = Pad(
            ref="U2", pin="1", x=8.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=2, net_name="DATA",
        )

        grid.add_pad(pad1)
        grid.add_pad(pad2)

        route = router.route(pad1, pad2)
        assert route is not None, "Routing should succeed"
        assert len(route.segments) > 0, "Route should have segments"

        # All segments should use the default trace width (0.2mm)
        for seg in route.segments:
            assert seg.width == 0.2, (
                f"Segment width should be 0.2mm (default), got {seg.width}mm"
            )


class TestPerNetClearanceDuringPathfinding:
    """Issue #1674: A* search and grid marking use per-net-class trace widths."""

    def test_router_computes_per_net_clearance_radius(self):
        """Verify that the clearance radii cache includes per-net-class widths."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
        net_class_map = {"+5V": NET_CLASS_POWER}
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules, net_class_map=net_class_map)

        # Cache should contain entries for the global trace width (0.2)
        # AND the POWER net class trace width (0.5)
        assert (0.2, 0.2) in router._clearance_radii  # default width + default clearance
        assert (0.5, 0.2) in router._clearance_radii  # POWER width + default clearance

        # POWER radius should be larger than default
        default_radius = router._clearance_radii[(0.2, 0.2)]
        power_radius = router._clearance_radii[(0.5, 0.2)]
        assert power_radius > default_radius, (
            f"POWER radius ({power_radius}) should exceed default ({default_radius})"
        )

    def test_mark_route_uses_segment_width(self):
        """Verify grid.mark_route() uses seg.width, not rules.trace_width."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
        net_class_map = {"+5V": NET_CLASS_POWER}
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules, net_class_map=net_class_map)

        # Create two pads for a POWER net
        pad1 = Pad(
            ref="C1", pin="1", x=2.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )
        pad2 = Pad(
            ref="C2", pin="1", x=8.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )

        grid.add_pad(pad1)
        grid.add_pad(pad2)

        route = router.route(pad1, pad2)
        assert route is not None, "Routing should succeed for POWER net"

        # All segments should use the POWER net class width
        for seg in route.segments:
            assert seg.width == 0.5

        # Mark the route and check that the blocked zone uses the wider width
        grid.mark_route(route)

        # Expected clearance cells for 0.5mm trace:
        # (0.5/2 + 0.2) / 0.1 + 1 = 5.5 -> int(4.5) + 1 = 5, plus 1 safety = 6
        wide_clearance = int((0.5 / 2 + rules.trace_clearance) / grid.resolution) + 1 + 1
        narrow_clearance = int((0.2 / 2 + rules.trace_clearance) / grid.resolution) + 1 + 1
        assert wide_clearance > narrow_clearance, (
            f"POWER clearance ({wide_clearance}) must exceed default ({narrow_clearance})"
        )

    def test_unmark_route_uses_segment_width(self):
        """Verify grid.unmark_route() mirrors mark_route() clearance."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
        net_class_map = {"+5V": NET_CLASS_POWER}
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules, net_class_map=net_class_map)

        pad1 = Pad(
            ref="C1", pin="1", x=2.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )
        pad2 = Pad(
            ref="C2", pin="1", x=8.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )

        grid.add_pad(pad1)
        grid.add_pad(pad2)

        route = router.route(pad1, pad2)
        assert route is not None

        # Mark then unmark -- should not crash and should clear route cells
        grid.mark_route(route)
        grid.unmark_route(route)

    def test_get_clearance_radius_cells_with_custom_trace_width(self):
        """Test get_clearance_radius_cells with explicit trace_width parameter."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
        grid = RoutingGrid(10, 10, rules)
        router = Router(grid, rules)

        # Default trace width: (0.2/2 + 0.2) / 0.1 = 3 cells
        default = router.get_clearance_radius_cells(0.2)
        assert default == 3

        # Custom trace width 0.5: (0.5/2 + 0.2) / 0.1 = 4.5 -> ceil = 5 cells
        custom = router.get_clearance_radius_cells(0.2, trace_width=0.5)
        assert custom == 5
        assert custom > default


class TestIntraIcNetClassWidth:
    """Test that intra-IC routes use net-class-aware trace widths."""

    def test_intra_ic_uses_net_class_width(self):
        """Intra-IC routes for power nets should use the power trace width."""
        rules = DesignRules(trace_width=0.2)
        net_class_map = {"+5V": NET_CLASS_POWER}

        # Create two pads on the same IC, same power net
        pad1 = Pad(
            ref="U1", pin="1", x=1.0, y=1.0, width=0.5, height=0.5,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )
        pad2 = Pad(
            ref="U1", pin="2", x=1.5, y=1.0, width=0.5, height=0.5,
            layer=Layer.F_CU, net=1, net_name="+5V",
        )

        pad_lookup = {("U1", "1"): pad1, ("U1", "2"): pad2}
        pads = [("U1", "1"), ("U1", "2")]

        routes, connected = create_intra_ic_routes(
            net=1, pads=pads, pad_lookup=pad_lookup, rules=rules,
            net_class_map=net_class_map,
        )

        assert len(routes) == 1
        assert routes[0].segments[0].width == 0.5  # POWER class width

    def test_intra_ic_signal_uses_default_width(self):
        """Intra-IC routes for signal nets should use default width."""
        rules = DesignRules(trace_width=0.2)
        net_class_map = {"+5V": NET_CLASS_POWER}

        pad1 = Pad(
            ref="U1", pin="3", x=1.0, y=2.0, width=0.5, height=0.5,
            layer=Layer.F_CU, net=2, net_name="SIG",
        )
        pad2 = Pad(
            ref="U1", pin="4", x=1.5, y=2.0, width=0.5, height=0.5,
            layer=Layer.F_CU, net=2, net_name="SIG",
        )

        pad_lookup = {("U1", "3"): pad1, ("U1", "4"): pad2}
        pads = [("U1", "3"), ("U1", "4")]

        routes, connected = create_intra_ic_routes(
            net=2, pads=pads, pad_lookup=pad_lookup, rules=rules,
            net_class_map=net_class_map,
        )

        assert len(routes) == 1
        assert routes[0].segments[0].width == 0.2  # default width
