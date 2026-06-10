"""Tests for waypoint injection in A* pathfinding (Issue #2330).

Validates that off-grid pad positions are injected as waypoint nodes
into the A* search graph, enabling the pathfinder to route to/from
pads whose centers do not align with the routing grid.
"""

import math

import pytest

from kicad_tools.router import DesignRules, RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad


def _make_grid(
    width: float = 10.0,
    height: float = 10.0,
    resolution: float = 0.1,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    num_layers: int = 1,
) -> RoutingGrid:
    """Create a simple routing grid for testing."""
    from kicad_tools.router.layers import LayerStack

    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=0.1,
        trace_clearance=0.1,
    )
    layer_stack = LayerStack.two_layer() if num_layers <= 2 else LayerStack.four_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        origin_x=origin_x,
        origin_y=origin_y,
        layer_stack=layer_stack,
    )
    return grid


def _make_pad(
    x: float,
    y: float,
    net: int = 1,
    net_name: str = "NET1",
    layer: Layer = Layer.F_CU,
    width: float = 0.5,
    height: float = 0.5,
    through_hole: bool = False,
    ref: str = "U1",
    pin: str = "1",
) -> Pad:
    """Helper to create a Pad with sensible defaults."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        layer=layer,
        through_hole=through_hole,
        drill=0,
        ref=ref,
        pin=pin,
    )


class TestWaypointDetection:
    """Tests for _is_pad_off_grid detection."""

    def test_on_grid_pad(self):
        """Pad exactly on a grid point is not off-grid."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        # 1.0 is exactly on the 0.1mm grid
        pad = _make_pad(x=1.0, y=2.0)
        assert not router._is_pad_off_grid(pad)

    def test_off_grid_pad(self):
        """Pad between grid points is detected as off-grid."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        # 1.037 is 0.037mm from nearest grid point (1.0 or 1.1)
        # Tolerance is resolution/4 = 0.025mm, so this is off-grid.
        pad = _make_pad(x=1.037, y=2.0)
        assert router._is_pad_off_grid(pad)

    def test_near_grid_pad(self):
        """Pad very close to a grid point (within tolerance) is on-grid."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        # 1.02 is 0.02mm from nearest grid point (1.0)
        # Tolerance is resolution/4 = 0.025mm, so within tolerance.
        pad = _make_pad(x=1.02, y=2.0)
        assert not router._is_pad_off_grid(pad)


class TestWaypointCreation:
    """Tests for waypoint node creation and coordinate mapping."""

    def test_create_waypoint_returns_negative_indices(self):
        """Waypoint indices are negative to avoid grid cell collisions."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad = _make_pad(x=1.037, y=2.065)
        wp_key = router._create_waypoint(pad)

        assert wp_key[0] < 0
        assert wp_key[1] < 0

    def test_waypoint_stores_exact_coordinates(self):
        """Waypoint maps to exact pad world coordinates."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad = _make_pad(x=1.037, y=2.065)
        wp_key = router._create_waypoint(pad)

        world = router._waypoint_to_world(wp_key[0], wp_key[1])
        assert world is not None
        assert abs(world[0] - 1.037) < 1e-9
        assert abs(world[1] - 2.065) < 1e-9

    def test_multiple_waypoints_unique(self):
        """Each waypoint gets a unique key."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad1 = _make_pad(x=1.037, y=2.065)
        pad2 = _make_pad(x=3.043, y=4.078)

        wp1 = router._create_waypoint(pad1)
        wp2 = router._create_waypoint(pad2)

        assert wp1 != wp2

    def test_is_waypoint(self):
        """_is_waypoint correctly identifies waypoint nodes."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad = _make_pad(x=1.037, y=2.065)
        wp_key = router._create_waypoint(pad)

        assert router._is_waypoint(wp_key[0], wp_key[1])
        assert not router._is_waypoint(5, 10)
        assert not router._is_waypoint(0, 0)


class TestWaypointGridEdges:
    """Tests for waypoint-to-grid-cell edge generation."""

    def test_edges_generated(self):
        """Waypoint produces edges to nearby grid cells."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad = _make_pad(x=1.037, y=2.065, net=1)
        # Add pad to grid so cells are accessible
        grid.add_pad(pad)

        wp_key = router._create_waypoint(pad)
        edges = router._waypoint_grid_edges(wp_key, pad, net=1)

        assert len(edges) > 0, "Expected at least one edge from waypoint"

    def test_edge_costs_are_euclidean(self):
        """Edge costs correspond to Euclidean distance in grid-cell units."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad = _make_pad(x=1.037, y=2.065, net=1)
        grid.add_pad(pad)

        wp_key = router._create_waypoint(pad)
        edges = router._waypoint_grid_edges(wp_key, pad, net=1)

        for gx, gy, cost in edges:
            gw_x, gw_y = grid.grid_to_world(gx, gy)
            expected_dist = math.sqrt((1.037 - gw_x) ** 2 + (2.065 - gw_y) ** 2)
            expected_cost = expected_dist / grid.resolution
            assert abs(cost - expected_cost) < 1e-6, (
                f"Edge to ({gx},{gy}) cost {cost} != expected {expected_cost}"
            )

    def test_nearest_grid_cell_included(self):
        """The nearest grid cell to the pad is always included."""
        grid = _make_grid(resolution=0.1)
        rules = DesignRules(grid_resolution=0.1)
        router = Router(grid, rules)

        pad = _make_pad(x=1.037, y=2.065, net=1)
        grid.add_pad(pad)

        wp_key = router._create_waypoint(pad)
        edges = router._waypoint_grid_edges(wp_key, pad, net=1)

        nearest_gx, nearest_gy = grid.world_to_grid(1.037, 2.065)
        edge_cells = {(gx, gy) for gx, gy, _ in edges}
        assert (nearest_gx, nearest_gy) in edge_cells


class TestWaypointRouting:
    """Tests for end-to-end routing with waypoint injection."""

    def test_route_off_grid_start_pad(self):
        """Route from an off-grid start pad to an on-grid end pad."""
        grid = _make_grid(width=5.0, height=5.0, resolution=0.1)
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.1,
            trace_clearance=0.1,
        )
        router = Router(grid, rules)

        # Off-grid start pad at (1.037, 2.065)
        start = _make_pad(x=1.037, y=2.065, net=1, ref="U1", pin="1")
        # On-grid end pad at (3.0, 2.0)
        end = _make_pad(x=3.0, y=2.0, net=1, ref="R1", pin="1")

        grid.add_pad(start)
        grid.add_pad(end)

        route = router.route(start, end)
        assert route is not None, "Expected route to succeed with waypoint injection"
        assert len(route.segments) > 0

    def test_route_off_grid_end_pad(self):
        """Route from an on-grid start pad to an off-grid end pad."""
        grid = _make_grid(width=5.0, height=5.0, resolution=0.1)
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.1,
            trace_clearance=0.1,
        )
        router = Router(grid, rules)

        # On-grid start pad
        start = _make_pad(x=1.0, y=2.0, net=1, ref="U1", pin="1")
        # Off-grid end pad
        end = _make_pad(x=3.043, y=2.078, net=1, ref="R1", pin="1")

        grid.add_pad(start)
        grid.add_pad(end)

        route = router.route(start, end)
        assert route is not None, "Expected route to succeed with off-grid end pad"
        assert len(route.segments) > 0

    def test_route_both_pads_off_grid(self):
        """Route between two off-grid pads."""
        grid = _make_grid(width=5.0, height=5.0, resolution=0.1)
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.1,
            trace_clearance=0.1,
        )
        router = Router(grid, rules)

        start = _make_pad(x=1.037, y=2.065, net=1, ref="U1", pin="1")
        end = _make_pad(x=3.043, y=2.078, net=1, ref="R1", pin="1")

        grid.add_pad(start)
        grid.add_pad(end)

        route = router.route(start, end)
        assert route is not None, "Expected route between two off-grid pads"
        assert len(route.segments) > 0

    def test_route_on_grid_pads_still_works(self):
        """On-grid pads continue working without waypoints."""
        grid = _make_grid(width=5.0, height=5.0, resolution=0.1)
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.1,
            trace_clearance=0.1,
        )
        router = Router(grid, rules)

        start = _make_pad(x=1.0, y=2.0, net=1, ref="U1", pin="1")
        end = _make_pad(x=3.0, y=2.0, net=1, ref="R1", pin="1")

        grid.add_pad(start)
        grid.add_pad(end)

        route = router.route(start, end)
        assert route is not None, "On-grid routing should still work"
        assert len(route.segments) > 0

    def test_route_starts_at_pad_center(self):
        """Route from off-grid pad should start at exact pad coordinates."""
        grid = _make_grid(width=5.0, height=5.0, resolution=0.1)
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.1,
            trace_clearance=0.1,
        )
        router = Router(grid, rules)

        start = _make_pad(x=1.037, y=2.065, net=1, ref="U1", pin="1")
        end = _make_pad(x=3.0, y=2.0, net=1, ref="R1", pin="1")

        grid.add_pad(start)
        grid.add_pad(end)

        route = router.route(start, end)
        assert route is not None

        # First segment should start at the pad center
        first_seg = route.segments[0]
        assert abs(first_seg.x1 - start.x) < 0.001, (
            f"First segment starts at {first_seg.x1}, expected {start.x}"
        )
        assert abs(first_seg.y1 - start.y) < 0.001, (
            f"First segment starts at {first_seg.y1}, expected {start.y}"
        )

    def test_route_ends_at_pad_center(self):
        """Route to off-grid pad should end at exact pad coordinates."""
        grid = _make_grid(width=5.0, height=5.0, resolution=0.1)
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.1,
            trace_clearance=0.1,
        )
        router = Router(grid, rules)

        start = _make_pad(x=1.0, y=2.0, net=1, ref="U1", pin="1")
        end = _make_pad(x=3.043, y=2.078, net=1, ref="R1", pin="1")

        grid.add_pad(start)
        grid.add_pad(end)

        route = router.route(start, end)
        assert route is not None

        # Last segment should end at the pad center
        last_seg = route.segments[-1]
        assert abs(last_seg.x2 - end.x) < 0.001, (
            f"Last segment ends at {last_seg.x2}, expected {end.x}"
        )
        assert abs(last_seg.y2 - end.y) < 0.001, (
            f"Last segment ends at {last_seg.y2}, expected {end.y}"
        )


class TestSubgridPrepassSkipped:
    """Tests that the sub-grid pre-pass is skipped when waypoints are active.

    Issue #3441: ``use_waypoint_injection`` is backend-aware.  Waypoint
    injection only exists in the pure-Python pathfinder, so the "enabled
    by default" contract holds only under ``force_python=True``.
    """

    def test_waypoint_injection_enabled_by_default(self):
        """Autorouter has waypoint injection enabled by default (Python backend)."""
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        assert ar.use_waypoint_injection is True

    def test_subgrid_prepass_skipped_with_waypoints(self):
        """_run_subgrid_prepass returns empty list when waypoints active."""
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        assert ar.use_waypoint_injection is True
        result = ar._run_subgrid_prepass()
        assert result == []

    def test_subgrid_prepass_runs_when_disabled(self):
        """_run_subgrid_prepass runs normally when waypoints disabled."""
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        ar.use_waypoint_injection = False
        # With no pads, subgrid should return empty (no off-grid pads)
        result = ar._run_subgrid_prepass()
        assert result == []


class TestWaypointBackendGating:
    """Issue #3441: ``use_waypoint_injection`` reflects backend capability.

    Waypoint injection (#2330) was implemented only in the pure-Python
    ``Router``; the C++ ``CppPathfinder`` has no waypoint support.  Before
    #3441 the flag was hardcoded True, which under the C++ backend
    simultaneously disabled the sub-grid escape pre-pass AND the
    PIN_ACCESS sub-grid retry while waypoints never actually ran -- all
    three off-grid recovery mechanisms were off at once (the board-07
    ``--grid 0.1`` 13/31 regression).
    """

    def test_python_backend_supports_waypoints(self):
        from kicad_tools.router.pathfinder import Router

        assert Router.supports_waypoint_injection is True

    def test_cpp_backend_does_not_support_waypoints(self):
        from kicad_tools.router.cpp_backend import CppPathfinder

        assert CppPathfinder.supports_waypoint_injection is False

    def test_effective_flag_false_under_cpp_backend(self):
        """Under the C++ backend the effective flag must be False so the
        sub-grid escape pre-pass and PIN_ACCESS retry stay available."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.cpp_backend import CppPathfinder, is_cpp_available

        if not is_cpp_available():
            pytest.skip("C++ backend not built")

        ar = Autorouter(width=10.0, height=10.0)
        assert isinstance(ar.router, CppPathfinder)
        # Request flag is True (historical default) ...
        assert ar._use_waypoint_injection is True
        # ... but the effective, backend-aware value is False.
        assert ar.use_waypoint_injection is False

    def test_subgrid_prepass_not_skipped_under_cpp_backend(self):
        """The waypoint gate must not skip the sub-grid pre-pass under cpp."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.cpp_backend import CppPathfinder, is_cpp_available

        if not is_cpp_available():
            pytest.skip("C++ backend not built")

        ar = Autorouter(width=10.0, height=10.0)
        assert isinstance(ar.router, CppPathfinder)
        # The pre-pass must actually run (here a no-op result because the
        # board has no pads -- the point is it is NOT short-circuited by
        # the waypoint gate; we verify by checking the gate evaluates
        # False rather than True).
        assert ar.use_waypoint_injection is False
        result = ar._run_subgrid_prepass()
        assert result == []  # no pads -> no escapes, but the pass ran

    def test_setter_can_disable_on_python_backend(self):
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        assert ar.use_waypoint_injection is True
        ar.use_waypoint_injection = False
        assert ar.use_waypoint_injection is False
        ar.use_waypoint_injection = True
        assert ar.use_waypoint_injection is True

    def test_setter_cannot_force_waypoints_on_cpp_backend(self):
        """Requesting waypoints on a backend without support stays False."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.cpp_backend import is_cpp_available

        if not is_cpp_available():
            pytest.skip("C++ backend not built")

        ar = Autorouter(width=10.0, height=10.0)
        ar.use_waypoint_injection = True
        assert ar.use_waypoint_injection is False

    def test_pin_access_subgrid_retry_gate_open_under_cpp(self):
        """The route_net PIN_ACCESS retry condition uses the effective flag.

        The retry at core.py is gated on ``not self.use_waypoint_injection``;
        under cpp this must evaluate True (retry available).
        """
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.cpp_backend import is_cpp_available

        if not is_cpp_available():
            pytest.skip("C++ backend not built")

        ar = Autorouter(width=10.0, height=10.0)
        assert (not ar.use_waypoint_injection) is True


class TestEscapeStubNotPrerouted:
    """Issue #3441 follow-on: escape stubs must not count as full routes.

    With the sub-grid pre-pass re-enabled under the C++ backend, the
    escape stubs it emits land in ``Autorouter.routes`` BEFORE the
    negotiated loop builds its #2464 pre-routed-net filter
    (``{r.net for r in self.routes}``).  Without the ``is_escape``
    exclusion, every net whose off-grid pad received a stub was skipped
    by the loop entirely -- board 07's six TMDS nets ended permanently
    at 1/2 pads connected.
    """

    def test_route_default_not_escape(self):
        from kicad_tools.router.primitives import Route

        r = Route(net=1, net_name="N1")
        assert r.is_escape is False

    def test_subgrid_escape_routes_are_marked(self):
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.primitives import Pad as CorePad

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        ar.use_waypoint_injection = False
        # Off-grid pad: 0.137 is off the default grid
        pad = CorePad(
            x=2.037,
            y=2.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="N1",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
        )
        ar.pads[("U1", "1")] = pad
        ar.nets[1] = [("U1", "1")]
        ar.net_names[1] = "N1"
        ar.grid.add_pad(pad)
        escape_routes = ar._run_subgrid_prepass()
        for r in escape_routes:
            assert r.is_escape is True

    def test_get_failed_nets_ignores_escape_only_routes(self):
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.primitives import Route

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        ar.nets[1] = [("U1", "1"), ("U2", "1")]
        ar.net_names[1] = "N1"
        # Net 1 has ONLY an escape stub -- it is not routed.
        ar.routes.append(Route(net=1, net_name="N1", is_escape=True))
        assert ar.get_failed_nets() == [1]
        # A real route clears it.
        ar.routes.append(Route(net=1, net_name="N1"))
        assert ar.get_failed_nets() == []

    def test_prerouted_filter_excludes_escape_stubs(self):
        """The #2464 filter expression must ignore escape stubs."""
        from kicad_tools.router.primitives import Route

        routes = [
            Route(net=1, net_name="TMDS_D0_P", is_escape=True),
            Route(net=2, net_name="USB_DP"),
        ]
        prerouted = {r.net for r in routes if not getattr(r, "is_escape", False)}
        assert prerouted == {2}

    def test_cleanup_removes_orphan_escape_stubs(self):
        """cleanup_artifacts drops escape stubs of nets with no real route
        so failed nets read as cleanly unrouted, not 'partially connected'
        via their stub copper (Issue #3441)."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.primitives import Route, Segment

        ar = Autorouter(width=10.0, height=10.0, force_python=True)
        ar.nets[1] = [("U1", "1"), ("U2", "1")]
        ar.net_names[1] = "N1"
        ar.nets[2] = [("U3", "1"), ("U4", "1")]
        ar.net_names[2] = "N2"

        def _stub(net: int, name: str, x: float) -> Route:
            return Route(
                net=net,
                net_name=name,
                segments=[
                    Segment(x, 2.0, x + 0.05, 2.0, 0.15, Layer.F_CU, net, name)
                ],
                is_escape=True,
            )

        # Net 1: stub only (failed net).  Net 2: stub + real route.
        ar.routes.append(_stub(1, "N1", 2.0))
        ar.routes.append(_stub(2, "N2", 5.0))
        ar.routes.append(
            Route(
                net=2,
                net_name="N2",
                segments=[
                    Segment(5.05, 2.0, 7.0, 2.0, 0.15, Layer.F_CU, 2, "N2")
                ],
            )
        )

        stats = ar.cleanup_artifacts()
        assert stats["orphan_escape_routes_removed"] == 1
        nets_left = {r.net for r in ar.routes}
        assert 1 not in nets_left, "failed net's stub must be removed"
        assert 2 in nets_left, "routed net keeps its copper (incl. stub)"
        # Net 2's escape stub survives (its net has a real route).
        assert any(r.is_escape for r in ar.routes if r.net == 2)
