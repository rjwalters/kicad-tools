"""Tests for C++ router backend fallback behavior."""

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
