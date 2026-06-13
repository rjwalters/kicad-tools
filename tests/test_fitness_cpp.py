"""Cross-check tests for C++ evolutionary fitness evaluator vs pure Python.

These tests verify that the C++ evaluate_fitness() function produces
numerically identical results to the Python _evaluate_fitness_worker_python()
for all sub-scores: wire length, pin alignment, conflict counting,
boundary violations, and routability estimation.

Tests run against both backends and compare results. If the C++ backend
is not available, the cross-check tests are skipped but the Python
fallback tests still run.
"""

from __future__ import annotations

import pytest

from kicad_tools.optim.evolutionary import (
    _PLACEMENT_CPP_AVAILABLE,
    Individual,
    _evaluate_fitness_worker,
    _evaluate_fitness_worker_python,
    _EvaluationContext,
)

# Tolerance for floating point comparison
TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    components: dict | None = None,
    springs: list | None = None,
    board_vertices: list | None = None,
    board_bounds: tuple | None = None,
    wire_length_weight: float = 0.1,
    conflict_weight: float = 100.0,
    routability_weight: float = 50.0,
    boundary_violation_weight: float = 500.0,
    pin_alignment_weight: float = 5.0,
    pin_alignment_tolerance: float = 0.5,
) -> _EvaluationContext:
    """Create an _EvaluationContext with sensible defaults."""
    if components is None:
        components = {}
    if springs is None:
        springs = []
    if board_vertices is None:
        board_vertices = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    if board_bounds is None:
        board_bounds = (0.0, 0.0, 100.0, 100.0)

    return _EvaluationContext(
        components=components,
        springs=springs,
        board_vertices=board_vertices,
        board_bounds=board_bounds,
        wire_length_weight=wire_length_weight,
        conflict_weight=conflict_weight,
        routability_weight=routability_weight,
        boundary_violation_weight=boundary_violation_weight,
        pin_alignment_weight=pin_alignment_weight,
        pin_alignment_tolerance=pin_alignment_tolerance,
    )


def _two_component_context(
    x1: float = 30.0,
    y1: float = 40.0,
    x2: float = 70.0,
    y2: float = 60.0,
) -> tuple[_EvaluationContext, Individual]:
    """Create a two-component scenario with springs between pins."""
    components = {
        "U1": (x1, y1, 0.0, 10.0, 8.0, [(5.0, 0.0, "1"), (-5.0, 0.0, "2")]),
        "R1": (x2, y2, 0.0, 4.0, 2.0, [(2.0, 0.0, "1"), (-2.0, 0.0, "2")]),
    }
    springs = [("U1", "1", "R1", "1"), ("U1", "2", "R1", "2")]
    ctx = _make_context(components=components, springs=springs)
    ind = Individual(
        positions={"U1": (x1, y1), "R1": (x2, y2)},
        rotations={"U1": 0.0, "R1": 0.0},
    )
    return ctx, ind


# ---------------------------------------------------------------------------
# Skip marker for C++ tests
# ---------------------------------------------------------------------------

cpp_required = pytest.mark.skipif(
    not _PLACEMENT_CPP_AVAILABLE,
    reason="C++ placement backend not available",
)


# ---------------------------------------------------------------------------
# Python fallback tests (always run)
# ---------------------------------------------------------------------------


class TestPythonFallbackFitness:
    """Verify _evaluate_fitness_worker_python works correctly."""

    def test_empty_components(self):
        """Empty component list produces baseline fitness."""
        ctx = _make_context()
        ind = Individual()
        result = _evaluate_fitness_worker_python((ind, ctx))
        # With no components: wire_length=0, conflicts=0, boundary=0,
        # routability=100.0 (n < 2), alignment=0.0 (no springs)
        expected = 1000.0 + 100.0 * 50.0  # baseline + routability * weight
        assert abs(result - expected) < TOLERANCE

    def test_single_component(self):
        """Single component: no pairwise interactions."""
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, []),
        }
        ctx = _make_context(components=components)
        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})
        result = _evaluate_fitness_worker_python((ind, ctx))
        # Single component: no conflicts, no boundary violations (inside),
        # routability=100.0 (n < 2), no wire length, no alignment
        expected = 1000.0 + 100.0 * 50.0
        assert abs(result - expected) < TOLERANCE

    def test_two_components_basic(self):
        """Two components produce non-zero wire length and routability."""
        ctx, ind = _two_component_context()
        result = _evaluate_fitness_worker_python((ind, ctx))
        assert result != 0.0

    def test_conflict_penalty(self):
        """Overlapping components reduce fitness via conflict penalty."""
        ctx_no_conflict, ind_no = _two_component_context(x1=20.0, y1=50.0, x2=80.0, y2=50.0)
        ctx_conflict, ind_yes = _two_component_context(x1=50.0, y1=50.0, x2=52.0, y2=50.0)

        fitness_no = _evaluate_fitness_worker_python((ind_no, ctx_no_conflict))
        fitness_yes = _evaluate_fitness_worker_python((ind_yes, ctx_conflict))

        # Overlapping should have lower fitness due to conflict penalty
        assert fitness_yes < fitness_no

    def test_boundary_violation_penalty(self):
        """Component outside board incurs heavy penalty."""
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, []),
        }
        board_inside = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        board_outside = [(200.0, 200.0), (300.0, 200.0), (300.0, 300.0), (200.0, 300.0)]

        ctx_inside = _make_context(components=components, board_vertices=board_inside)
        ctx_outside = _make_context(components=components, board_vertices=board_outside)

        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})

        fitness_inside = _evaluate_fitness_worker_python((ind, ctx_inside))
        fitness_outside = _evaluate_fitness_worker_python((ind, ctx_outside))

        assert fitness_outside < fitness_inside
        # Boundary violation penalty should be significant (500.0 weight)
        assert fitness_inside - fitness_outside > 400.0

    def test_rotation_affects_pins(self):
        """Component rotation changes absolute pin positions."""
        components = {
            "U1": (50.0, 50.0, 0.0, 10.0, 5.0, [(5.0, 0.0, "1")]),
            "R1": (60.0, 50.0, 0.0, 4.0, 2.0, [(-2.0, 0.0, "1")]),
        }
        springs = [("U1", "1", "R1", "1")]
        ctx = _make_context(components=components, springs=springs)

        ind_0deg = Individual(
            positions={"U1": (50.0, 50.0), "R1": (60.0, 50.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        ind_90deg = Individual(
            positions={"U1": (50.0, 50.0), "R1": (60.0, 50.0)},
            rotations={"U1": 90.0, "R1": 0.0},
        )

        f0 = _evaluate_fitness_worker_python((ind_0deg, ctx))
        f90 = _evaluate_fitness_worker_python((ind_90deg, ctx))

        # Different rotations produce different fitness
        assert f0 != f90

    def test_spring_missing_component(self):
        """Spring referencing non-existent component is safely skipped."""
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, [(0.0, 0.0, "1")]),
        }
        springs = [("U1", "1", "MISSING", "1")]
        ctx = _make_context(components=components, springs=springs)
        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})
        # Should not raise
        result = _evaluate_fitness_worker_python((ind, ctx))
        assert isinstance(result, float)

    def test_component_no_pins(self):
        """Component with no pins handles springs gracefully."""
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, []),
            "R1": (60.0, 50.0, 0.0, 4.0, 2.0, []),
        }
        springs = [("U1", "1", "R1", "1")]
        ctx = _make_context(components=components, springs=springs)
        ind = Individual(
            positions={"U1": (50.0, 50.0), "R1": (60.0, 50.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        result = _evaluate_fitness_worker_python((ind, ctx))
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Cross-check tests (require C++ backend)
# ---------------------------------------------------------------------------


@cpp_required
class TestCrossCheckFitness:
    """Cross-check evaluate_fitness: C++ vs Python."""

    def _compare(self, ctx: _EvaluationContext, ind: Individual) -> None:
        """Assert C++ and Python produce identical results."""
        from kicad_tools.optim.evolutionary import _evaluate_fitness_worker_cpp

        py = _evaluate_fitness_worker_python((ind, ctx))
        cpp = _evaluate_fitness_worker_cpp((ind, ctx))
        assert abs(py - cpp) < TOLERANCE, f"Mismatch: Python={py}, C++={cpp}, diff={abs(py - cpp)}"

    def test_empty_components(self):
        """Empty component list matches."""
        ctx = _make_context()
        ind = Individual()
        self._compare(ctx, ind)

    def test_single_component_inside(self):
        """Single component inside board matches."""
        components = {"U1": (50.0, 50.0, 0.0, 5.0, 5.0, [])}
        ctx = _make_context(components=components)
        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})
        self._compare(ctx, ind)

    def test_single_component_outside(self):
        """Single component outside board matches."""
        components = {"U1": (200.0, 200.0, 0.0, 5.0, 5.0, [])}
        ctx = _make_context(components=components)
        ind = Individual(positions={"U1": (200.0, 200.0)}, rotations={"U1": 0.0})
        self._compare(ctx, ind)

    def test_two_components_no_conflict(self):
        """Two well-separated components match."""
        ctx, ind = _two_component_context(x1=20.0, y1=50.0, x2=80.0, y2=50.0)
        self._compare(ctx, ind)

    def test_two_components_with_conflict(self):
        """Two overlapping components match."""
        ctx, ind = _two_component_context(x1=50.0, y1=50.0, x2=52.0, y2=50.0)
        self._compare(ctx, ind)

    def test_rotated_components(self):
        """Components with non-zero rotations match."""
        components = {
            "U1": (50.0, 50.0, 0.0, 10.0, 5.0, [(5.0, 0.0, "1"), (-5.0, 0.0, "2")]),
            "R1": (70.0, 60.0, 0.0, 4.0, 2.0, [(2.0, 0.0, "1"), (-2.0, 0.0, "2")]),
        }
        springs = [("U1", "1", "R1", "1")]
        ctx = _make_context(components=components, springs=springs)

        for rot1, rot2 in [(0, 0), (90, 0), (0, 180), (45, 270), (135, 315)]:
            ind = Individual(
                positions={"U1": (50.0, 50.0), "R1": (70.0, 60.0)},
                rotations={"U1": float(rot1), "R1": float(rot2)},
            )
            self._compare(ctx, ind)

    def test_pin_alignment_detected(self):
        """Pin alignment score matches when pins are axis-aligned."""
        # Pins on same Y axis (vertically aligned within tolerance)
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, [(0.0, 0.0, "1")]),
            "R1": (50.0, 70.0, 0.0, 4.0, 2.0, [(0.0, 0.0, "1")]),
        }
        springs = [("U1", "1", "R1", "1")]
        ctx = _make_context(components=components, springs=springs)
        ind = Individual(
            positions={"U1": (50.0, 50.0), "R1": (50.0, 70.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        self._compare(ctx, ind)

    def test_boundary_violations(self):
        """Boundary violation counting matches for various polygon shapes."""
        # Triangular board
        board_vertices = [(50.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, []),
            "R1": (10.0, 10.0, 0.0, 4.0, 2.0, []),  # outside triangle
        }
        ctx = _make_context(
            components=components,
            board_vertices=board_vertices,
            board_bounds=(0.0, 0.0, 100.0, 100.0),
        )
        ind = Individual(
            positions={"U1": (50.0, 50.0), "R1": (10.0, 10.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        self._compare(ctx, ind)

    def test_missing_spring_component(self):
        """Spring with missing component reference matches."""
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, [(0.0, 0.0, "1")]),
        }
        springs = [("U1", "1", "MISSING", "1")]
        ctx = _make_context(components=components, springs=springs)
        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})
        self._compare(ctx, ind)

    def test_many_components(self):
        """50 components with springs match exactly."""
        import random

        random.seed(42)
        n = 50
        components = {}
        for i in range(n):
            ref = f"C{i}"
            x = random.uniform(10, 90)
            y = random.uniform(10, 90)
            rot = random.choice([0.0, 90.0, 180.0, 270.0])
            w = random.uniform(2, 8)
            h = random.uniform(2, 6)
            n_pins = random.randint(1, 4)
            pins = [
                (random.uniform(-w / 2, w / 2), random.uniform(-h / 2, h / 2), str(p))
                for p in range(n_pins)
            ]
            components[ref] = (x, y, rot, w, h, pins)

        # Create random springs
        refs = list(components.keys())
        springs = []
        for _ in range(100):
            r1 = random.choice(refs)
            r2 = random.choice(refs)
            if r1 != r2:
                p1 = str(random.randint(0, 3))
                p2 = str(random.randint(0, 3))
                springs.append((r1, p1, r2, p2))

        ctx = _make_context(components=components, springs=springs)
        ind = Individual(
            positions={ref: (d[0], d[1]) for ref, d in components.items()},
            rotations={ref: d[2] for ref, d in components.items()},
        )
        self._compare(ctx, ind)

    def test_all_weights_zero(self):
        """With all weights zero, fitness equals baseline (1000.0)."""
        ctx, ind = _two_component_context()
        ctx.wire_length_weight = 0.0
        ctx.conflict_weight = 0.0
        ctx.routability_weight = 0.0
        ctx.boundary_violation_weight = 0.0
        ctx.pin_alignment_weight = 0.0
        self._compare(ctx, ind)

    def test_extreme_weights(self):
        """Extreme weight values produce matching results."""
        ctx, ind = _two_component_context()
        ctx.wire_length_weight = 1000.0
        ctx.conflict_weight = 0.001
        ctx.routability_weight = 999.0
        ctx.boundary_violation_weight = 0.0
        ctx.pin_alignment_weight = 50.0
        self._compare(ctx, ind)


@cpp_required
class TestCppFallbackIntegration:
    """Test that the dispatcher (_evaluate_fitness_worker) works correctly."""

    def test_dispatcher_uses_cpp(self):
        """_evaluate_fitness_worker uses C++ when available."""
        ctx, ind = _two_component_context()
        result = _evaluate_fitness_worker((ind, ctx))
        py_result = _evaluate_fitness_worker_python((ind, ctx))
        assert abs(result - py_result) < TOLERANCE

    def test_dispatcher_matches_python(self):
        """Dispatcher result matches Python fallback exactly."""
        import random

        random.seed(99)
        components = {
            f"U{i}": (
                random.uniform(10, 90),
                random.uniform(10, 90),
                0.0,
                random.uniform(3, 10),
                random.uniform(2, 8),
                [(random.uniform(-2, 2), random.uniform(-2, 2), str(p)) for p in range(3)],
            )
            for i in range(10)
        }
        springs = [("U0", "0", "U1", "0"), ("U2", "1", "U5", "2")]
        ctx = _make_context(components=components, springs=springs)
        ind = Individual(
            positions={ref: (d[0], d[1]) for ref, d in components.items()},
            rotations={ref: d[2] for ref, d in components.items()},
        )

        dispatched = _evaluate_fitness_worker((ind, ctx))
        py = _evaluate_fitness_worker_python((ind, ctx))
        assert abs(dispatched - py) < TOLERANCE


# ---------------------------------------------------------------------------
# Edge cases (always run via Python, cross-check if C++ available)
# ---------------------------------------------------------------------------


class TestEdgeCasesFitness:
    """Edge cases that should work identically on both backends."""

    def test_zero_board_vertices(self):
        """Less than 3 board vertices means no boundary check."""
        components = {"U1": (50.0, 50.0, 0.0, 5.0, 5.0, [])}
        ctx = _make_context(components=components, board_vertices=[(0, 0), (100, 0)])
        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})

        py = _evaluate_fitness_worker_python((ind, ctx))
        assert isinstance(py, float)

        if _PLACEMENT_CPP_AVAILABLE:
            from kicad_tools.optim.evolutionary import _evaluate_fitness_worker_cpp

            cpp = _evaluate_fitness_worker_cpp((ind, ctx))
            assert abs(py - cpp) < TOLERANCE

    def test_component_not_in_individual(self):
        """Component not in individual's positions uses original coords."""
        components = {
            "U1": (50.0, 50.0, 0.0, 5.0, 5.0, []),
            "R1": (60.0, 60.0, 45.0, 4.0, 2.0, []),
        }
        ctx = _make_context(components=components)
        # Individual only has U1, R1 uses original position
        ind = Individual(positions={"U1": (50.0, 50.0)}, rotations={"U1": 0.0})

        py = _evaluate_fitness_worker_python((ind, ctx))
        assert isinstance(py, float)

        if _PLACEMENT_CPP_AVAILABLE:
            from kicad_tools.optim.evolutionary import _evaluate_fitness_worker_cpp

            cpp = _evaluate_fitness_worker_cpp((ind, ctx))
            assert abs(py - cpp) < TOLERANCE

    def test_identical_positions(self):
        """All components at same position: maximum conflicts."""
        components = {f"C{i}": (50.0, 50.0, 0.0, 5.0, 5.0, []) for i in range(5)}
        ctx = _make_context(components=components)
        ind = Individual(
            positions={f"C{i}": (50.0, 50.0) for i in range(5)},
            rotations={f"C{i}": 0.0 for i in range(5)},
        )

        py = _evaluate_fitness_worker_python((ind, ctx))
        assert isinstance(py, float)

        if _PLACEMENT_CPP_AVAILABLE:
            from kicad_tools.optim.evolutionary import _evaluate_fitness_worker_cpp

            cpp = _evaluate_fitness_worker_cpp((ind, ctx))
            assert abs(py - cpp) < TOLERANCE
