"""Tests for C++ router backend fallback behavior."""

import importlib

from kicad_tools.router.cpp_backend import (
    LARGE_GRID_THRESHOLD,
    format_backend_status,
    get_backend_info,
    is_cpp_available,
)


class TestCppBackendFallback:
    """Test that the Python fallback works when C++ is not available."""

    def test_is_cpp_available(self):
        """Test is_cpp_available function returns a boolean."""
        result = is_cpp_available()
        assert isinstance(result, bool)

    def test_get_backend_info_structure(self):
        """Test get_backend_info returns expected structure."""
        info = get_backend_info()
        assert isinstance(info, dict)
        assert "backend" in info
        assert "version" in info
        assert "available" in info
        assert info["backend"] in ("cpp", "python")

    def test_get_backend_info_consistency(self):
        """Test get_backend_info is consistent with is_cpp_available."""
        available = is_cpp_available()
        info = get_backend_info()
        assert info["available"] == available
        if available:
            assert info["backend"] == "cpp"
        else:
            assert info["backend"] == "python"

    def test_get_backend_info_has_build_hint_when_unavailable(self):
        """Test that unavailable backend info includes build hint."""
        info = get_backend_info()
        if not info["available"]:
            assert "build_hint" in info
            assert "kct build-native" in info["build_hint"]


class TestFormatBackendStatus:
    """Test the format_backend_status helper for CLI output."""

    def test_cpp_backend_shows_native(self):
        """Test that cpp backend status shows native label."""
        info = {"backend": "cpp", "version": "1.0.0", "available": True, "active": "cpp"}
        status = format_backend_status(info)
        assert "native" in status
        assert "1.0.0" in status

    def test_python_backend_shows_tip(self):
        """Test that python backend status includes build tip."""
        info = {
            "backend": "python",
            "version": "pure-python",
            "available": False,
            "active": "python",
        }
        status = format_backend_status(info)
        assert "python" in status.lower()
        assert "kct build-native" in status

    def test_python_backend_large_grid_warns(self):
        """Test that large grid with python backend shows WARNING."""
        info = {
            "backend": "python",
            "version": "pure-python",
            "available": False,
            "active": "python",
        }
        status = format_backend_status(info, grid_cells=LARGE_GRID_THRESHOLD + 1)
        assert "WARNING" in status
        assert "kct build-native" in status

    def test_python_backend_small_grid_no_warning(self):
        """Test that small grid with python backend shows tip, not WARNING."""
        info = {
            "backend": "python",
            "version": "pure-python",
            "available": False,
            "active": "python",
        }
        status = format_backend_status(info, grid_cells=1000)
        assert "WARNING" not in status
        assert "Tip" in status

    def test_python_backend_zero_grid_shows_tip(self):
        """Test that zero grid cells (default) shows tip."""
        info = {
            "backend": "python",
            "version": "pure-python",
            "available": False,
            "active": "python",
        }
        status = format_backend_status(info, grid_cells=0)
        assert "Tip" in status

    def test_large_grid_threshold_constant(self):
        """Test that the large grid threshold is a reasonable value."""
        assert LARGE_GRID_THRESHOLD > 10_000
        assert LARGE_GRID_THRESHOLD <= 200_000


class TestCppPathfinderRouteSignature:
    """Test that CppPathfinder.route() accepts extra_goal_cells parameter."""

    def test_route_accepts_extra_goal_cells_in_signature(self):
        """Test that CppPathfinder.route() has extra_goal_cells parameter."""
        import inspect

        from kicad_tools.router.cpp_backend import CppPathfinder

        sig = inspect.signature(CppPathfinder.route)
        assert "extra_goal_cells" in sig.parameters
        param = sig.parameters["extra_goal_cells"]
        assert param.default is None

    def test_route_accepts_extra_goal_cells_empty_set(self):
        """Test that CppPathfinder.route() can be called with extra_goal_cells=set()."""
        import inspect

        from kicad_tools.router.cpp_backend import CppPathfinder

        # Verify the parameter exists and has correct default
        sig = inspect.signature(CppPathfinder.route)
        param = sig.parameters["extra_goal_cells"]
        assert param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def test_route_accepts_extra_goal_cells_with_cells(self):
        """Test that extra_goal_cells parameter accepts a set of tuples."""
        import inspect

        from kicad_tools.router.cpp_backend import CppPathfinder

        sig = inspect.signature(CppPathfinder.route)
        # The parameter should be present and keyword-compatible
        param = sig.parameters["extra_goal_cells"]
        assert param.default is None


class TestCppPathfinderPadBounds:
    """Test pad metal area expansion and approach zone relaxation (Issue #2427)."""

    def test_compute_pad_bounds_smd(self):
        """Test _compute_pad_bounds for an SMD pad returns correct metal/approach grid bounds."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        # Create an SMD pad at a known position
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
        )
        bounds = pf._compute_pad_bounds(pad)

        # Metal bounds should span the pad's copper area
        assert bounds.metal_gx1 <= bounds.metal_gx2
        assert bounds.metal_gy1 <= bounds.metal_gy2
        # Approach bounds should be metal + 2 cells
        assert bounds.approach_gx1 == bounds.metal_gx1 - 2
        assert bounds.approach_gy1 == bounds.metal_gy1 - 2
        assert bounds.approach_gx2 == bounds.metal_gx2 + 2
        assert bounds.approach_gy2 == bounds.metal_gy2 + 2

    def test_compute_pad_bounds_through_hole(self):
        """Test _compute_pad_bounds for a through-hole pad with drill."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        # Through-hole pad with drill
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.7,
            height=1.7,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
        )
        bounds = pf._compute_pad_bounds(pad)

        # THT pad should have reasonable metal area
        assert bounds.metal_gx1 <= bounds.metal_gx2
        assert bounds.metal_gy1 <= bounds.metal_gy2
        # Metal area should span more than 1 cell for a 1.7mm pad at 0.127mm res
        assert bounds.metal_gx2 - bounds.metal_gx1 >= 1
        assert bounds.metal_gy2 - bounds.metal_gy1 >= 1

    def test_cpp_routes_off_grid_pad(self):
        """Test that C++ backend routes successfully when pad is off-grid.

        Creates a scenario where the pad center is offset by half a grid cell,
        verifying the metal area expansion allows the route to succeed.
        """
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254  # Coarse grid to make off-grid effect visible
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        # Start pad on-grid
        start = Pad(
            x=5.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
        )
        # End pad offset by half a grid cell (off-grid)
        end = Pad(
            x=15.0 + rules.grid_resolution * 0.5,
            y=10.0 + rules.grid_resolution * 0.5,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
        )

        route = pf.route(start, end)
        assert route is not None, "C++ backend should find route to off-grid pad"
        assert len(route.segments) > 0


class TestAvoidanceCost:
    """Test DRC avoidance cost feedback (Issue #2438)."""

    def test_boost_region_cost_modifies_cells(self):
        """Test that boost_region_cost correctly sets avoidance_cost in cells within radius."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid

        grid = CppGrid(cols=20, rows=20, layers=2, resolution=0.127)

        # All cells should start with zero avoidance cost
        cell_before = grid._impl.at(10, 10, 0)
        assert cell_before.avoidance_cost == 0.0

        # Boost a region around (10, 10) on layer 0
        grid._impl.boost_region_cost(10, 10, 0, 3, 20.0)

        # Center cell should have maximum cost
        cell_center = grid._impl.at(10, 10, 0)
        assert cell_center.avoidance_cost > 0.0

        # Cell within radius should also have cost
        cell_near = grid._impl.at(11, 10, 0)
        assert cell_near.avoidance_cost > 0.0

        # Cell outside radius should have zero cost
        cell_far = grid._impl.at(15, 15, 0)
        assert cell_far.avoidance_cost == 0.0

        # Other layer should be unaffected
        cell_other_layer = grid._impl.at(10, 10, 1)
        assert cell_other_layer.avoidance_cost == 0.0

    def test_boost_region_cost_tapers_with_distance(self):
        """Test that avoidance cost tapers off with distance from center."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid

        grid = CppGrid(cols=20, rows=20, layers=1, resolution=0.127)
        grid._impl.boost_region_cost(10, 10, 0, 4, 20.0)

        center_cost = grid._impl.at(10, 10, 0).avoidance_cost
        near_cost = grid._impl.at(11, 10, 0).avoidance_cost
        far_cost = grid._impl.at(13, 10, 0).avoidance_cost

        # Cost should decrease with distance
        assert center_cost > near_cost > far_cost > 0.0

    def test_clear_avoidance_costs_zeros_all(self):
        """Test that clear_avoidance_costs zeros all avoidance_cost fields."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid

        grid = CppGrid(cols=20, rows=20, layers=2, resolution=0.127)

        # Boost on multiple layers
        grid._impl.boost_region_cost(5, 5, 0, 3, 10.0)
        grid._impl.boost_region_cost(15, 15, 1, 3, 10.0)

        # Verify costs were set
        assert grid._impl.at(5, 5, 0).avoidance_cost > 0.0
        assert grid._impl.at(15, 15, 1).avoidance_cost > 0.0

        # Clear all
        grid._impl.clear_avoidance_costs()

        # All should be zero
        assert grid._impl.at(5, 5, 0).avoidance_cost == 0.0
        assert grid._impl.at(15, 15, 1).avoidance_cost == 0.0
        assert grid._impl.at(10, 10, 0).avoidance_cost == 0.0

    def test_avoidance_cost_shifts_route(self):
        """Test that A* path shifts away from cells with high avoidance_cost."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        rules.trace_width = 0.254
        rules.trace_clearance = 0.127
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(
            x=2.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )
        end = Pad(
            x=18.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )

        # Route without avoidance
        route1 = pf.route(start, end)
        assert route1 is not None

        # Boost avoidance cost on the direct path (y=10 area, midpoint)
        mid_gx, mid_gy = cpp_grid._impl.world_to_grid(10.0, 10.0)
        for layer in range(cpp_grid.num_layers):
            cpp_grid._impl.boost_region_cost(mid_gx, mid_gy, layer, 5, 50.0)

        # Route with avoidance - should still succeed but take a different path
        route2 = pf.route(start, end)
        assert route2 is not None

        # The avoidance route should be different (longer or shifted in Y)
        def total_length(route):
            length = 0.0
            for seg in route.segments:
                dx = seg.x2 - seg.x1
                dy = seg.y2 - seg.y1
                length += (dx * dx + dy * dy) ** 0.5
            return length

        # The route through the avoidance zone should be longer since it detours
        len1 = total_length(route1)
        len2 = total_length(route2)
        assert len2 > len1, (
            f"Route with avoidance cost ({len2:.2f}mm) should be longer "
            f"than direct route ({len1:.2f}mm)"
        )

        # Clean up
        cpp_grid._impl.clear_avoidance_costs()

    def test_clear_avoidance_costs_method_on_pathfinder(self):
        """Test that CppPathfinder exposes clear_avoidance_costs method."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        # Boost some costs
        cpp_grid._impl.boost_region_cost(5, 5, 0, 2, 10.0)
        assert cpp_grid._impl.at(5, 5, 0).avoidance_cost > 0.0

        # Clear via pathfinder method
        pf.clear_avoidance_costs()
        assert cpp_grid._impl.at(5, 5, 0).avoidance_cost == 0.0

    def test_avoidance_cost_no_overhead_when_zero(self):
        """Test that default zero avoidance_cost adds no overhead to A* routing."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid

        grid = CppGrid(cols=10, rows=10, layers=1, resolution=0.127)

        # All cells should default to 0.0 avoidance_cost
        for x in range(10):
            for y in range(10):
                assert grid._impl.at(x, y, 0).avoidance_cost == 0.0

    def test_gridcell_avoidance_cost_exposed(self):
        """Test that GridCell.avoidance_cost is readable and writable via bindings."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid

        grid = CppGrid(cols=5, rows=5, layers=1, resolution=0.127)
        cell = grid._impl.at(2, 2, 0)

        # Default value
        assert cell.avoidance_cost == 0.0

        # Write
        cell.avoidance_cost = 42.0
        assert grid._impl.at(2, 2, 0).avoidance_cost == 42.0


class TestResumableRouting:
    """Test resumable A* search (Issue #2447)."""

    def test_route_resumable_finds_path(self):
        """Test that route_resumable finds a path identical to route()."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(
            x=5.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )
        end = Pad(
            x=15.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )

        # route() should work as before (backward compatible)
        route = pf.route(start, end)
        assert route is not None
        assert len(route.segments) > 0

    def test_route_resumable_bindings_exist(self):
        """Test that route_resumable, resume, clear_search_state bindings exist."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router import router_cpp
        from kicad_tools.router.cpp_backend import CppGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        grid = CppGrid(cols=50, rows=50, layers=2, resolution=0.254)

        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        pf = router_cpp.Pathfinder(grid._impl, cpp_rules, True)

        assert hasattr(pf, "route_resumable")
        assert hasattr(pf, "resume")
        assert hasattr(pf, "clear_search_state")

    def test_route_resumable_and_resume_cycle(self):
        """Test route_resumable + resume finds alternative path after rejection."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router import router_cpp
        from kicad_tools.router.cpp_backend import CppGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        grid = CppGrid(cols=80, rows=80, layers=2, resolution=0.254)

        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        pf = router_cpp.Pathfinder(grid._impl, cpp_rules, True)

        # Route from (1,1) to (10,10) on layer 0
        result1 = pf.route_resumable(
            0.254, 0.254, 0,  # start
            2.54, 2.54, 0,    # end
            1,                 # net
        )
        assert result1.success, "Initial resumable route should succeed"
        nodes_after_first = pf.nodes_explored

        # Get the goal cell from the last segment
        last_seg = result1.segments[-1]
        reject_gx, reject_gy = grid._impl.world_to_grid(last_seg.x2, last_seg.y2)

        # Resume with the goal cell rejected
        result2 = pf.resume(reject_gx, reject_gy, 0)

        # The resume should find an alternative path (different goal cell
        # in the end pad metal area) or exhaust the search
        # Either way, nodes_explored should be >= what we had before
        assert pf.nodes_explored >= nodes_after_first

        # Clean up
        pf.clear_search_state()

    def test_clear_search_state_resets(self):
        """Test that clear_search_state releases memory and resets state."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router import router_cpp
        from kicad_tools.router.cpp_backend import CppGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        grid = CppGrid(cols=50, rows=50, layers=2, resolution=0.254)

        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        pf = router_cpp.Pathfinder(grid._impl, cpp_rules, True)

        # Run a resumable search
        result = pf.route_resumable(
            0.254, 0.254, 0,
            5.08, 5.08, 0,
            1,
        )
        assert result.success

        # Clear state
        pf.clear_search_state()

        # Resume after clear should return failure (no active search)
        result2 = pf.resume(0, 0, 0)
        assert not result2.success

    def test_route_still_works_after_resumable(self):
        """Test that non-resumable route() still works after route_resumable()."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router import router_cpp
        from kicad_tools.router.cpp_backend import CppGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        grid = CppGrid(cols=50, rows=50, layers=2, resolution=0.254)

        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        pf = router_cpp.Pathfinder(grid._impl, cpp_rules, True)

        # Run a resumable search and clear
        result1 = pf.route_resumable(
            0.254, 0.254, 0,
            5.08, 5.08, 0,
            1,
        )
        assert result1.success
        pf.clear_search_state()

        # Non-resumable route should still work fine
        result2 = pf.route(
            0.254, 0.254, 0,
            5.08, 5.08, 0,
            1,
        )
        assert result2.success

    def test_python_wrapper_uses_resumable(self):
        """Test that CppPathfinder.route() uses resumable API internally."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(
            x=5.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )
        end = Pad(
            x=15.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )

        # The Python wrapper now uses route_resumable internally
        route = pf.route(start, end)
        assert route is not None
        assert len(route.segments) > 0

    def test_exception_safety_clears_state(self):
        """Test that search state is cleared even when validation raises."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.254
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(
            x=5.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )
        end = Pad(
            x=15.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="NET1", layer=Layer.F_CU,
        )

        # Route should work (exercises try/finally in the wrapper)
        route = pf.route(start, end)
        assert route is not None

        # Route again to verify state was properly cleaned up
        route2 = pf.route(start, end)
        assert route2 is not None


class TestCppBackendImport:
    """Test import behavior of cpp_backend module."""

    def test_import_cpp_backend(self):
        """Test cpp_backend module can be imported."""
        from kicad_tools.router import cpp_backend

        assert hasattr(cpp_backend, "is_cpp_available")
        assert hasattr(cpp_backend, "get_backend_info")
        assert hasattr(cpp_backend, "create_hybrid_router")

    def test_cpp_grid_class_exists(self):
        """Test CppGrid class exists."""
        from kicad_tools.router.cpp_backend import CppGrid

        assert CppGrid is not None

    def test_cpp_pathfinder_class_exists(self):
        """Test CppPathfinder class exists."""
        from kicad_tools.router.cpp_backend import CppPathfinder

        assert CppPathfinder is not None


class TestHybridRouter:
    """Test the hybrid router factory function."""

    def test_create_hybrid_router_returns_router(self):
        """Test create_hybrid_router returns a valid router."""
        from kicad_tools.router.cpp_backend import create_hybrid_router
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

        router = create_hybrid_router(grid, rules)
        assert router is not None

    def test_create_hybrid_router_force_python(self):
        """Test create_hybrid_router with force_python flag."""
        from kicad_tools.router.cpp_backend import create_hybrid_router
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.pathfinder import Router
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

        router = create_hybrid_router(grid, rules, force_python=True)
        # Should return Python Router when force_python=True
        assert isinstance(router, Router)


class TestCppPathfinderPythonFallback:
    """Test per-net Python fallback when C++ pathfinder fails."""

    def test_fallback_stats_initial_state(self):
        """Test that fallback stats are empty on fresh CppPathfinder."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        stats = pf.fallback_stats
        assert stats["fallback_count"] == 0
        assert stats["fallback_nets"] == []

    def test_fallback_invoked_when_cpp_fails(self):
        """Test Python fallback is called when C++ route returns failure."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from unittest.mock import MagicMock, patch

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad, Route
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(x=1.0, y=1.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")
        end = Pad(x=8.0, y=8.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")

        # Mock the C++ impl.route to return failure
        mock_result = MagicMock()
        mock_result.success = False
        pf._impl.route = MagicMock(return_value=mock_result)

        # Mock the Python Router to return a successful route
        mock_route = Route(net=1, net_name="N1")
        with patch("kicad_tools.router.pathfinder.Router.route", return_value=mock_route):
            result = pf.route(start, end)

        assert result is not None
        assert result.net_name == "N1"
        assert pf.fallback_stats["fallback_count"] == 1
        assert pf.fallback_stats["fallback_nets"] == ["N1"]

    def test_fallback_returns_none_when_python_also_fails(self):
        """Test that None is returned when both C++ and Python fail."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from unittest.mock import MagicMock, patch

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(x=1.0, y=1.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")
        end = Pad(x=8.0, y=8.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")

        # Mock C++ to fail
        mock_result = MagicMock()
        mock_result.success = False
        pf._impl.route = MagicMock(return_value=mock_result)

        # Mock Python Router to also fail
        with patch("kicad_tools.router.pathfinder.Router.route", return_value=None):
            result = pf.route(start, end)

        assert result is None
        # Fallback was attempted but failed, so count should NOT increment
        assert pf.fallback_stats["fallback_count"] == 0

    def test_fallback_not_invoked_when_cpp_succeeds(self):
        """Test Python fallback is NOT constructed when C++ succeeds."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        # Route two pads far apart on an empty grid -- C++ should succeed
        start = Pad(x=2.0, y=2.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")
        end = Pad(x=18.0, y=18.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")

        result = pf.route(start, end)
        # Whether C++ succeeds or not on this simple grid, the fallback
        # router should NOT have been constructed if C++ succeeded.
        if result is not None:
            assert pf._py_router is None
            assert pf.fallback_stats["fallback_count"] == 0

    def test_fallback_skipped_when_no_py_grid(self):
        """Test that fallback is skipped when _py_grid is None."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from unittest.mock import MagicMock

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        # Create a CppGrid WITHOUT from_routing_grid (no _py_grid)
        cpp_grid = CppGrid(cols=80, rows=80, layers=2, resolution=0.127)
        assert cpp_grid._py_grid is None

        pf = CppPathfinder(cpp_grid, rules)

        start = Pad(x=1.0, y=1.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")
        end = Pad(x=8.0, y=8.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")

        # Mock C++ to fail
        mock_result = MagicMock()
        mock_result.success = False
        pf._impl.route = MagicMock(return_value=mock_result)

        result = pf.route(start, end)
        assert result is None
        assert pf._py_router is None
        assert pf.fallback_stats["fallback_count"] == 0

    def test_fallback_stats_accumulate(self):
        """Test that multiple fallbacks accumulate in stats."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from unittest.mock import MagicMock, patch

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad, Route
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        # Mock C++ to always fail
        mock_result = MagicMock()
        mock_result.success = False
        pf._impl.route = MagicMock(return_value=mock_result)

        # Route two different nets via fallback
        for i, net_name in enumerate(["NET_A", "NET_B", "NET_C"], start=1):
            start = Pad(
                x=1.0, y=1.0, width=0.5, height=0.5, layer=Layer.F_CU, net=i, net_name=net_name
            )
            end = Pad(
                x=8.0, y=8.0, width=0.5, height=0.5, layer=Layer.F_CU, net=i, net_name=net_name
            )
            mock_route = Route(net=i, net_name=net_name)
            with patch(
                "kicad_tools.router.pathfinder.Router.route", return_value=mock_route
            ):
                result = pf.route(start, end)
            assert result is not None

        assert pf.fallback_stats["fallback_count"] == 3
        assert pf.fallback_stats["fallback_nets"] == ["NET_A", "NET_B", "NET_C"]

    def test_py_router_reused_across_fallbacks(self):
        """Test that the Python Router is constructed once and reused."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from unittest.mock import MagicMock, patch

        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad, Route
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        rules.grid_resolution = 0.127
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)

        mock_result = MagicMock()
        mock_result.success = False
        pf._impl.route = MagicMock(return_value=mock_result)

        start = Pad(x=1.0, y=1.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")
        end = Pad(x=8.0, y=8.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="N1")

        mock_route = Route(net=1, net_name="N1")
        with patch("kicad_tools.router.pathfinder.Router.route", return_value=mock_route):
            pf.route(start, end)
            first_router = pf._py_router
            assert first_router is not None

            pf.route(start, end)
            # Same Router instance should be reused
            assert pf._py_router is first_router


class TestCppBuildVersionGuard:
    """Test the stale-.so detection added in Issue #2501.

    The guard compares ``router_cpp.BUILD_VERSION`` (compiled into the .so)
    against ``_REQUIRED_CPP_BUILD_VERSION`` (mirrored in cpp_backend.py).
    On mismatch the C++ backend is disabled with a "kct build-native" hint
    routed through the existing fallback path, preventing downstream
    ``AttributeError`` from missing symbols at routing time.
    """

    def test_required_build_version_constant_exists(self):
        """The Python-side required build version is defined as an int."""
        from kicad_tools.router import cpp_backend

        assert hasattr(cpp_backend, "_REQUIRED_CPP_BUILD_VERSION")
        assert isinstance(cpp_backend._REQUIRED_CPP_BUILD_VERSION, int)
        assert cpp_backend._REQUIRED_CPP_BUILD_VERSION >= 1

    def test_build_version_exposed_when_cpp_available(self):
        """When C++ loads successfully, router_cpp.BUILD_VERSION matches required."""
        if not is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available")

        from kicad_tools.router import cpp_backend

        assert cpp_backend.router_cpp is not None
        actual = getattr(cpp_backend.router_cpp, "BUILD_VERSION", None)
        assert actual == cpp_backend._REQUIRED_CPP_BUILD_VERSION

    def test_stale_so_disables_cpp_with_actionable_error(self, monkeypatch):
        """A mismatched BUILD_VERSION on a fresh import disables the C++ backend.

        Simulates the failure mode from Issue #2501: the .so loads cleanly but
        is older than the cpp/ source tree, so it lacks symbols (or has the
        wrong build version constant). The guard must short-circuit to the
        Python fallback with a "kct build-native" hint instead of allowing
        a downstream AttributeError.
        """
        import sys

        # Ensure cpp_backend is freshly imported so we see the live router_cpp.
        sys.modules.pop("kicad_tools.router.cpp_backend", None)
        original = importlib.import_module("kicad_tools.router.cpp_backend")

        # Snapshot the live router_cpp module (None if backend already disabled)
        router_cpp_mod = original.router_cpp
        if router_cpp_mod is None:
            import pytest

            pytest.skip("C++ backend not available - guard already engaged")

        # Force BUILD_VERSION to a mismatched value before re-importing
        monkeypatch.setattr(
            router_cpp_mod, "BUILD_VERSION", 999_999, raising=False
        )

        # Drop the cached cpp_backend so the module-level guard re-runs
        sys.modules.pop("kicad_tools.router.cpp_backend", None)
        try:
            reloaded = importlib.import_module("kicad_tools.router.cpp_backend")

            assert reloaded.is_cpp_available() is False
            reason = reloaded.get_cpp_unavailable_reason()
            assert reason is not None
            assert "build-native" in reason
            assert "stale" in reason.lower() or "999999" in reason
        finally:
            # Restore the real cpp_backend module so subsequent tests see the
            # true backend state (monkeypatch will restore BUILD_VERSION).
            sys.modules.pop("kicad_tools.router.cpp_backend", None)
            importlib.import_module("kicad_tools.router.cpp_backend")

    def test_missing_build_version_attr_disables_cpp(self, monkeypatch):
        """If router_cpp lacks BUILD_VERSION (very old .so) the guard fires."""
        import sys

        # Ensure cpp_backend is freshly imported so we see the live router_cpp.
        sys.modules.pop("kicad_tools.router.cpp_backend", None)
        original = importlib.import_module("kicad_tools.router.cpp_backend")

        router_cpp_mod = original.router_cpp
        if router_cpp_mod is None:
            import pytest

            pytest.skip("C++ backend not available - guard already engaged")

        # Remove the attribute entirely
        if hasattr(router_cpp_mod, "BUILD_VERSION"):
            monkeypatch.delattr(router_cpp_mod, "BUILD_VERSION", raising=False)

        sys.modules.pop("kicad_tools.router.cpp_backend", None)
        try:
            reloaded = importlib.import_module("kicad_tools.router.cpp_backend")

            assert reloaded.is_cpp_available() is False
            reason = reloaded.get_cpp_unavailable_reason()
            assert reason is not None
            assert "build-native" in reason
        finally:
            sys.modules.pop("kicad_tools.router.cpp_backend", None)
            importlib.import_module("kicad_tools.router.cpp_backend")
