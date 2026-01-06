"""Tests for C++ router backend fallback behavior."""

from kicad_tools.router.cpp_backend import (
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
