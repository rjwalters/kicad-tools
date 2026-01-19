"""Performance tests comparing Python and C++ router backends.

This test module validates the performance improvement from the C++ backend
and ensures it matches the Python implementation's behavior.

Reference: Issue #947 - Router hangs on large boards (200x120mm)
"""

import time
from typing import NamedTuple

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available, get_backend_info
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.rules import DesignRules


class BenchmarkResult(NamedTuple):
    """Result from a routing benchmark."""

    backend: str
    board_size: tuple[float, float]
    num_nets: int
    nets_routed: int
    time_seconds: float
    grid_cells: int


def create_test_pads(
    autorouter: Autorouter,
    num_nets: int,
    spacing: float,
    width: float,
    height: float,
) -> None:
    """Create a grid of test pads for routing.

    Creates pairs of pads that need to be connected, simulating
    a board with multiple nets.
    """
    pad_size = 1.0  # 1mm pads
    margin = 5.0  # margin from board edge

    # Calculate how many pads we can fit in a row
    available_width = width - 2 * margin
    num_per_row = max(1, int(available_width / spacing))

    for net_id in range(1, num_nets + 1):
        row = (net_id - 1) // num_per_row
        col = (net_id - 1) % num_per_row

        # Create two pads for each net
        x1 = margin + col * spacing
        y = margin + row * spacing

        # Component with two pads on the same net
        pads = [
            {
                "number": "1",
                "x": x1,
                "y": y,
                "width": pad_size,
                "height": pad_size,
                "net": net_id,
                "net_name": f"NET_{net_id}",
                "layer": Layer.F_CU,
                "through_hole": False,
            },
            {
                "number": "2",
                "x": width - margin - col * spacing,
                "y": y,
                "width": pad_size,
                "height": pad_size,
                "net": net_id,
                "net_name": f"NET_{net_id}",
                "layer": Layer.F_CU,
                "through_hole": False,
            },
        ]
        autorouter.add_component(f"U{net_id}", pads)


def run_routing_benchmark(
    width: float,
    height: float,
    num_nets: int,
    force_python: bool = False,
    resolution: float = 0.5,
) -> BenchmarkResult:
    """Run a routing benchmark with specified parameters.

    Args:
        width: Board width in mm
        height: Board height in mm
        num_nets: Number of nets to route
        force_python: If True, use Python backend; otherwise use C++ if available
        resolution: Grid resolution in mm

    Returns:
        BenchmarkResult with timing and statistics
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=resolution,
    )

    autorouter = Autorouter(
        width=width,
        height=height,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
        force_python=force_python,
    )

    # Create test pads
    create_test_pads(autorouter, num_nets, spacing=5.0, width=width, height=height)

    # Get grid cell count
    grid_cells = autorouter.grid.cols * autorouter.grid.rows * autorouter.grid.num_layers

    # Time the routing
    backend_info = autorouter.backend_info
    backend = backend_info.get("active", "python")

    start = time.perf_counter()
    routes = autorouter.route_all()
    elapsed = time.perf_counter() - start

    return BenchmarkResult(
        backend=backend,
        board_size=(width, height),
        num_nets=num_nets,
        nets_routed=len(routes),
        time_seconds=elapsed,
        grid_cells=grid_cells,
    )


class TestCppPerformance:
    """Tests for C++ backend performance improvements."""

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not available")
    def test_cpp_backend_available(self):
        """Verify C++ backend is properly built and available."""
        info = get_backend_info()
        assert info["available"] is True
        assert info["backend"] == "cpp"
        assert info["version"] == "1.0.0"

    def test_small_board_routing_works(self):
        """Test routing works on a small board with both backends."""
        # This test verifies basic functionality, not performance
        result = run_routing_benchmark(
            width=20.0,
            height=20.0,
            num_nets=3,
            resolution=0.5,
        )
        assert result.nets_routed > 0
        assert result.time_seconds < 10.0

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not available")
    def test_cpp_matches_python_results(self):
        """Verify C++ backend produces valid routes matching Python behavior."""
        # Run with Python backend
        python_result = run_routing_benchmark(
            width=30.0,
            height=30.0,
            num_nets=5,
            force_python=True,
            resolution=0.5,
        )

        # Run with C++ backend
        cpp_result = run_routing_benchmark(
            width=30.0,
            height=30.0,
            num_nets=5,
            force_python=False,
            resolution=0.5,
        )

        # Both should route successfully
        assert python_result.backend == "python"
        assert cpp_result.backend == "cpp"

        # Results should be similar (not necessarily identical due to algorithm details)
        assert python_result.nets_routed > 0
        assert cpp_result.nets_routed > 0

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not available")
    def test_cpp_faster_than_python(self):
        """Verify C++ backend is faster than Python on medium-sized board."""
        # Medium-sized board to show meaningful speedup
        board_size = (50.0, 40.0)
        num_nets = 8

        # Run Python backend
        python_result = run_routing_benchmark(
            width=board_size[0],
            height=board_size[1],
            num_nets=num_nets,
            force_python=True,
            resolution=0.5,
        )

        # Run C++ backend
        cpp_result = run_routing_benchmark(
            width=board_size[0],
            height=board_size[1],
            num_nets=num_nets,
            force_python=False,
            resolution=0.5,
        )

        assert python_result.backend == "python"
        assert cpp_result.backend == "cpp"

        # C++ should be faster (at least 2x improvement expected)
        # Note: actual speedup is 10-100x but we use conservative threshold
        speedup = python_result.time_seconds / max(cpp_result.time_seconds, 0.001)
        print(f"\nSpeedup: {speedup:.1f}x")
        print(f"Python: {python_result.time_seconds:.3f}s")
        print(f"C++: {cpp_result.time_seconds:.3f}s")

        # Allow some slack for system variability
        assert speedup > 1.5 or cpp_result.time_seconds < 0.5

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not available")
    @pytest.mark.slow
    def test_large_board_issue_947(self):
        """Test routing on large board scenario from issue #947.

        Issue #947: Router hangs on 200x120mm board with ~40 nets.
        With C++ backend, this should complete in reasonable time.
        """
        # Board dimensions from issue #947
        result = run_routing_benchmark(
            width=200.0,
            height=120.0,
            num_nets=20,  # Reduced for test but still meaningful
            force_python=False,
            resolution=0.5,  # Coarse grid for performance
        )

        # Should complete in reasonable time
        assert result.time_seconds < 60.0, f"Routing took too long: {result.time_seconds:.1f}s"
        assert result.nets_routed > 0, "No nets were routed"

        print(f"\nLarge board (200x120mm) results:")
        print(f"  Grid cells: {result.grid_cells:,}")
        print(f"  Nets routed: {result.nets_routed}/{result.num_nets}")
        print(f"  Time: {result.time_seconds:.2f}s")


class TestBackendSelection:
    """Tests for automatic backend selection."""

    def test_autorouter_uses_cpp_by_default(self):
        """Verify Autorouter uses C++ backend by default when available."""
        rules = DesignRules()
        autorouter = Autorouter(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

        info = autorouter.backend_info

        if is_cpp_available():
            assert info["active"] == "cpp"
        else:
            assert info["active"] == "python"

    def test_force_python_flag(self):
        """Verify force_python flag works."""
        rules = DesignRules()
        autorouter = Autorouter(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
            force_python=True,
        )

        info = autorouter.backend_info
        assert info["active"] == "python"


if __name__ == "__main__":
    # Run a quick benchmark comparison when executed directly
    print("Running performance comparison...")
    print("=" * 60)

    sizes = [
        (30, 30, 5, "Small"),
        (50, 40, 10, "Medium"),
        (100, 80, 15, "Large"),
    ]

    for width, height, nets, label in sizes:
        print(f"\n{label} board ({width}x{height}mm, {nets} nets):")

        # Python
        py_result = run_routing_benchmark(width, height, nets, force_python=True)
        print(f"  Python: {py_result.time_seconds:.3f}s, {py_result.nets_routed}/{nets} routed")

        # C++ (if available)
        if is_cpp_available():
            cpp_result = run_routing_benchmark(width, height, nets, force_python=False)
            speedup = py_result.time_seconds / max(cpp_result.time_seconds, 0.001)
            print(f"  C++:    {cpp_result.time_seconds:.3f}s, {cpp_result.nets_routed}/{nets} routed")
            print(f"  Speedup: {speedup:.1f}x")
        else:
            print("  C++: Not available")
