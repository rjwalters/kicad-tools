"""Cross-check tests for C++ force engine vs pure Python.

These tests verify that the C++ force-directed placement engine produces
numerically identical results to the Python implementation in
optim/placement.py for component repulsion forces, torques, and
board boundary forces.

Tests run against both backends and compare results. If the C++ backend
is not available, the cross-check tests are skipped but the Python
fallback tests still run.
"""

from __future__ import annotations

import pytest

from kicad_tools.optim.components import Component, Pin
from kicad_tools.optim.config import PlacementConfig
from kicad_tools.optim.cpp_backend import is_cpp_available
from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.placement import PlacementOptimizer

# Tolerance for floating point comparison
TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_component(ref: str, x: float, y: float, w: float, h: float, fixed: bool = False) -> Component:
    """Create a rectangular component at (x, y) with given dimensions."""
    comp = Component(
        ref=ref,
        x=x,
        y=y,
        width=w,
        height=h,
        pins=[],
        fixed=fixed,
    )
    return comp


def _make_optimizer(
    components: list[Component],
    board_w: float = 100.0,
    board_h: float = 100.0,
    config: PlacementConfig | None = None,
) -> PlacementOptimizer:
    """Create a PlacementOptimizer with given components."""
    board = Polygon.rectangle(board_w / 2, board_h / 2, board_w, board_h)
    opt = PlacementOptimizer(board, config=config or PlacementConfig())
    for comp in components:
        opt.add_component(comp)
    return opt


def _force_python_optimizer(opt: PlacementOptimizer) -> PlacementOptimizer:
    """Force an optimizer to use Python-only CPU path."""
    opt._cpp_force_available = False
    opt._gpu_enabled = False
    return opt


def _force_cpp_optimizer(opt: PlacementOptimizer) -> PlacementOptimizer:
    """Force an optimizer to use C++ force engine."""
    opt._cpp_force_available = True
    opt._gpu_enabled = False
    return opt


# ---------------------------------------------------------------------------
# Python-only tests (always run)
# ---------------------------------------------------------------------------


class TestPythonForceEngine:
    """Test the Python force engine works standalone."""

    def test_single_component_no_repulsion(self):
        """A single component should have zero repulsion force."""
        comp = _make_component("U1", 50, 50, 10, 10)
        opt = _make_optimizer([comp])
        _force_python_optimizer(opt)
        forces, torques = opt._compute_component_repulsion_cpu()

        assert abs(forces["U1"].x) < TOLERANCE
        assert abs(forces["U1"].y) < TOLERANCE
        assert abs(torques["U1"]) < TOLERANCE

    def test_two_components_repel(self):
        """Two close components should repel each other."""
        comp1 = _make_component("U1", 45, 50, 10, 10)
        comp2 = _make_component("U2", 55, 50, 10, 10)
        opt = _make_optimizer([comp1, comp2])
        _force_python_optimizer(opt)
        forces, torques = opt._compute_component_repulsion_cpu()

        # U1 should be pushed left (negative x), U2 pushed right (positive x)
        assert forces["U1"].x < 0
        assert forces["U2"].x > 0

    def test_fixed_component_no_force(self):
        """Fixed components should not receive forces."""
        comp1 = _make_component("U1", 45, 50, 10, 10, fixed=True)
        comp2 = _make_component("U2", 55, 50, 10, 10)
        opt = _make_optimizer([comp1, comp2])
        _force_python_optimizer(opt)
        forces, torques = opt._compute_component_repulsion_cpu()

        # Fixed component should have zero force
        assert abs(forces["U1"].x) < TOLERANCE
        assert abs(forces["U1"].y) < TOLERANCE
        # Non-fixed should still be pushed away
        assert forces["U2"].x > 0

    def test_all_fixed_no_forces(self):
        """All fixed components should produce zero forces."""
        comp1 = _make_component("U1", 45, 50, 10, 10, fixed=True)
        comp2 = _make_component("U2", 55, 50, 10, 10, fixed=True)
        opt = _make_optimizer([comp1, comp2])
        _force_python_optimizer(opt)
        forces, torques = opt._compute_component_repulsion_cpu()

        assert abs(forces["U1"].x) < TOLERANCE
        assert abs(forces["U2"].x) < TOLERANCE


# ---------------------------------------------------------------------------
# Fallback behavior tests (always run)
# ---------------------------------------------------------------------------


class TestFallbackBehavior:
    """Test that the optimizer falls back gracefully when C++ is unavailable."""

    def test_optimizer_works_without_cpp(self):
        """Optimizer produces valid results without C++ backend."""
        comp1 = _make_component("U1", 40, 50, 10, 10)
        comp2 = _make_component("U2", 60, 50, 10, 10)
        opt = _make_optimizer([comp1, comp2])
        _force_python_optimizer(opt)

        forces, torques = opt.compute_forces_and_torques()

        # Should have entries for both components
        assert "U1" in forces
        assert "U2" in forces
        assert "U1" in torques
        assert "U2" in torques

    def test_cpp_available_returns_bool(self):
        """is_cpp_available returns a boolean."""
        assert isinstance(is_cpp_available(), bool)


# ---------------------------------------------------------------------------
# Cross-check tests (require C++ backend)
# ---------------------------------------------------------------------------

cpp_required = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ force engine backend not available",
)


@cpp_required
class TestCrossCheckRepulsion:
    """Cross-check component repulsion: C++ vs Python."""

    def test_two_components_match(self):
        """Two components produce identical forces from both backends."""
        comp1 = _make_component("U1", 45, 50, 10, 10)
        comp2 = _make_component("U2", 55, 50, 10, 10)

        # Python
        opt_py = _make_optimizer([comp1, comp2])
        _force_python_optimizer(opt_py)
        py_forces, py_torques = opt_py._compute_component_repulsion_cpu()

        # C++
        comp1b = _make_component("U1", 45, 50, 10, 10)
        comp2b = _make_component("U2", 55, 50, 10, 10)
        opt_cpp = _make_optimizer([comp1b, comp2b])
        _force_cpp_optimizer(opt_cpp)
        cpp_forces, cpp_torques = opt_cpp._compute_component_repulsion_cpp()

        for ref in ["U1", "U2"]:
            assert abs(py_forces[ref].x - cpp_forces[ref].x) < TOLERANCE, (
                f"{ref} force_x: py={py_forces[ref].x}, cpp={cpp_forces[ref].x}"
            )
            assert abs(py_forces[ref].y - cpp_forces[ref].y) < TOLERANCE, (
                f"{ref} force_y: py={py_forces[ref].y}, cpp={cpp_forces[ref].y}"
            )
            assert abs(py_torques[ref] - cpp_torques[ref]) < TOLERANCE, (
                f"{ref} torque: py={py_torques[ref]}, cpp={cpp_torques[ref]}"
            )

    def test_ten_components_match(self):
        """Ten components with known positions produce identical results."""
        import random
        random.seed(42)

        components_py = []
        components_cpp = []
        for i in range(10):
            x = random.uniform(10, 90)
            y = random.uniform(10, 90)
            w = random.uniform(3, 12)
            h = random.uniform(3, 12)
            ref = f"C{i}"
            components_py.append(_make_component(ref, x, y, w, h))
            components_cpp.append(_make_component(ref, x, y, w, h))

        opt_py = _make_optimizer(components_py)
        _force_python_optimizer(opt_py)
        py_forces, py_torques = opt_py._compute_component_repulsion_cpu()

        opt_cpp = _make_optimizer(components_cpp)
        _force_cpp_optimizer(opt_cpp)
        cpp_forces, cpp_torques = opt_cpp._compute_component_repulsion_cpp()

        for ref in py_forces:
            assert abs(py_forces[ref].x - cpp_forces[ref].x) < TOLERANCE, (
                f"{ref} force_x mismatch"
            )
            assert abs(py_forces[ref].y - cpp_forces[ref].y) < TOLERANCE, (
                f"{ref} force_y mismatch"
            )
            assert abs(py_torques[ref] - cpp_torques[ref]) < TOLERANCE, (
                f"{ref} torque mismatch"
            )

    def test_with_fixed_components(self):
        """Mixed fixed/unfixed components produce identical results."""
        components_py = [
            _make_component("U1", 30, 50, 8, 8, fixed=True),
            _make_component("U2", 50, 50, 8, 8, fixed=False),
            _make_component("U3", 70, 50, 8, 8, fixed=False),
        ]
        components_cpp = [
            _make_component("U1", 30, 50, 8, 8, fixed=True),
            _make_component("U2", 50, 50, 8, 8, fixed=False),
            _make_component("U3", 70, 50, 8, 8, fixed=False),
        ]

        opt_py = _make_optimizer(components_py)
        _force_python_optimizer(opt_py)
        py_forces, py_torques = opt_py._compute_component_repulsion_cpu()

        opt_cpp = _make_optimizer(components_cpp)
        _force_cpp_optimizer(opt_cpp)
        cpp_forces, cpp_torques = opt_cpp._compute_component_repulsion_cpp()

        for ref in ["U1", "U2", "U3"]:
            assert abs(py_forces[ref].x - cpp_forces[ref].x) < TOLERANCE
            assert abs(py_forces[ref].y - cpp_forces[ref].y) < TOLERANCE
            assert abs(py_torques[ref] - cpp_torques[ref]) < TOLERANCE

    def test_single_component_both_zero(self):
        """Single component produces zero repulsion on both backends."""
        comp_py = _make_component("U1", 50, 50, 10, 10)
        comp_cpp = _make_component("U1", 50, 50, 10, 10)

        opt_py = _make_optimizer([comp_py])
        _force_python_optimizer(opt_py)
        py_forces, py_torques = opt_py._compute_component_repulsion_cpu()

        opt_cpp = _make_optimizer([comp_cpp])
        _force_cpp_optimizer(opt_cpp)
        cpp_forces, cpp_torques = opt_cpp._compute_component_repulsion_cpp()

        assert abs(py_forces["U1"].x) < TOLERANCE
        assert abs(cpp_forces["U1"].x) < TOLERANCE
        assert abs(py_torques["U1"]) < TOLERANCE
        assert abs(cpp_torques["U1"]) < TOLERANCE

    def test_custom_config_match(self):
        """Custom charge_density and edge_samples produce identical results."""
        config = PlacementConfig(
            charge_density=200.0,
            min_distance=1.0,
            edge_samples=8,
        )

        comp1_py = _make_component("U1", 40, 50, 10, 10)
        comp2_py = _make_component("U2", 55, 50, 10, 10)
        comp1_cpp = _make_component("U1", 40, 50, 10, 10)
        comp2_cpp = _make_component("U2", 55, 50, 10, 10)

        opt_py = _make_optimizer([comp1_py, comp2_py], config=config)
        _force_python_optimizer(opt_py)
        py_forces, py_torques = opt_py._compute_component_repulsion_cpu()

        opt_cpp = _make_optimizer([comp1_cpp, comp2_cpp], config=config)
        _force_cpp_optimizer(opt_cpp)
        cpp_forces, cpp_torques = opt_cpp._compute_component_repulsion_cpp()

        for ref in ["U1", "U2"]:
            assert abs(py_forces[ref].x - cpp_forces[ref].x) < TOLERANCE
            assert abs(py_forces[ref].y - cpp_forces[ref].y) < TOLERANCE
            assert abs(py_torques[ref] - cpp_torques[ref]) < TOLERANCE


@cpp_required
class TestCrossCheckBoundaryForces:
    """Cross-check boundary forces: C++ vs Python."""

    def test_component_inside_board(self):
        """Component well inside board produces identical boundary forces."""
        comp_py = _make_component("U1", 50, 50, 10, 10)
        comp_cpp = _make_component("U1", 50, 50, 10, 10)

        opt_py = _make_optimizer([comp_py])
        _force_python_optimizer(opt_py)

        opt_cpp = _make_optimizer([comp_cpp])
        _force_cpp_optimizer(opt_cpp)

        py_forces, py_torques = opt_py.compute_forces_and_torques()
        cpp_forces, cpp_torques = opt_cpp.compute_forces_and_torques()

        assert abs(py_forces["U1"].x - cpp_forces["U1"].x) < TOLERANCE
        assert abs(py_forces["U1"].y - cpp_forces["U1"].y) < TOLERANCE
        assert abs(py_torques["U1"] - cpp_torques["U1"]) < TOLERANCE

    def test_component_near_edge(self):
        """Component near board edge produces identical boundary forces."""
        comp_py = _make_component("U1", 5, 50, 10, 10)
        comp_cpp = _make_component("U1", 5, 50, 10, 10)

        opt_py = _make_optimizer([comp_py])
        _force_python_optimizer(opt_py)

        opt_cpp = _make_optimizer([comp_cpp])
        _force_cpp_optimizer(opt_cpp)

        py_forces, py_torques = opt_py.compute_forces_and_torques()
        cpp_forces, cpp_torques = opt_cpp.compute_forces_and_torques()

        assert abs(py_forces["U1"].x - cpp_forces["U1"].x) < TOLERANCE
        assert abs(py_forces["U1"].y - cpp_forces["U1"].y) < TOLERANCE
        assert abs(py_torques["U1"] - cpp_torques["U1"]) < TOLERANCE

    def test_component_outside_board(self):
        """Component outside board produces identical strong repulsion forces."""
        comp_py = _make_component("U1", -10, 50, 10, 10)
        comp_cpp = _make_component("U1", -10, 50, 10, 10)

        opt_py = _make_optimizer([comp_py])
        _force_python_optimizer(opt_py)

        opt_cpp = _make_optimizer([comp_cpp])
        _force_cpp_optimizer(opt_cpp)

        py_forces, py_torques = opt_py.compute_forces_and_torques()
        cpp_forces, cpp_torques = opt_cpp.compute_forces_and_torques()

        assert abs(py_forces["U1"].x - cpp_forces["U1"].x) < TOLERANCE
        assert abs(py_forces["U1"].y - cpp_forces["U1"].y) < TOLERANCE
        assert abs(py_torques["U1"] - cpp_torques["U1"]) < TOLERANCE

    def test_multiple_components_boundary(self):
        """Multiple components with mixed inside/outside produce identical results."""
        comps_py = [
            _make_component("U1", 50, 50, 10, 10),
            _make_component("U2", 2, 50, 10, 10),
            _make_component("U3", 98, 98, 10, 10),
        ]
        comps_cpp = [
            _make_component("U1", 50, 50, 10, 10),
            _make_component("U2", 2, 50, 10, 10),
            _make_component("U3", 98, 98, 10, 10),
        ]

        opt_py = _make_optimizer(comps_py)
        _force_python_optimizer(opt_py)

        opt_cpp = _make_optimizer(comps_cpp)
        _force_cpp_optimizer(opt_cpp)

        py_forces, py_torques = opt_py.compute_forces_and_torques()
        cpp_forces, cpp_torques = opt_cpp.compute_forces_and_torques()

        for ref in ["U1", "U2", "U3"]:
            assert abs(py_forces[ref].x - cpp_forces[ref].x) < TOLERANCE, (
                f"{ref} force_x: py={py_forces[ref].x}, cpp={cpp_forces[ref].x}"
            )
            assert abs(py_forces[ref].y - cpp_forces[ref].y) < TOLERANCE, (
                f"{ref} force_y: py={py_forces[ref].y}, cpp={cpp_forces[ref].y}"
            )
            assert abs(py_torques[ref] - cpp_torques[ref]) < TOLERANCE, (
                f"{ref} torque: py={py_torques[ref]}, cpp={cpp_torques[ref]}"
            )


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for force computation."""

    def test_empty_components(self):
        """No components should produce empty results."""
        opt = _make_optimizer([])
        _force_python_optimizer(opt)
        forces, torques = opt._compute_component_repulsion_cpu()
        assert forces == {}
        assert torques == {}

    def test_coincident_components_no_crash(self):
        """Two components at same position should not crash."""
        comp1 = _make_component("U1", 50, 50, 10, 10)
        comp2 = _make_component("U2", 50, 50, 10, 10)
        opt = _make_optimizer([comp1, comp2])
        _force_python_optimizer(opt)

        # Should not raise
        forces, torques = opt._compute_component_repulsion_cpu()
        assert "U1" in forces
        assert "U2" in forces


@cpp_required
class TestEdgeCasesCpp:
    """Edge cases that should work identically on both backends."""

    def test_coincident_components_match(self):
        """Coincident components produce identical results (min_distance clamping)."""
        comps_py = [
            _make_component("U1", 50, 50, 10, 10),
            _make_component("U2", 50, 50, 10, 10),
        ]
        comps_cpp = [
            _make_component("U1", 50, 50, 10, 10),
            _make_component("U2", 50, 50, 10, 10),
        ]

        opt_py = _make_optimizer(comps_py)
        _force_python_optimizer(opt_py)
        py_forces, py_torques = opt_py._compute_component_repulsion_cpu()

        opt_cpp = _make_optimizer(comps_cpp)
        _force_cpp_optimizer(opt_cpp)
        cpp_forces, cpp_torques = opt_cpp._compute_component_repulsion_cpp()

        for ref in ["U1", "U2"]:
            assert abs(py_forces[ref].x - cpp_forces[ref].x) < TOLERANCE
            assert abs(py_forces[ref].y - cpp_forces[ref].y) < TOLERANCE
            assert abs(py_torques[ref] - cpp_torques[ref]) < TOLERANCE

    def test_empty_components_cpp(self):
        """No components should produce empty results from C++ backend."""
        from kicad_tools.optim.cpp_backend import compute_repulsion_cpp

        forces, torques = compute_repulsion_cpp([], PlacementConfig())
        assert forces == {}
        assert torques == {}
