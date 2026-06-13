"""Cross-check tests for C++ placement backend vs pure Python.

These tests verify that the C++ implementations produce numerically
identical results to the Python implementations for all AABB cost
functions (compute_overlap, compute_boundary_violation,
compute_drc_violations) and the BatchCostEvaluator.

Tests run against both backends and compare results. If the C++ backend
is not available, the cross-check tests are skipped but the Python
fallback tests still run.
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    DesignRuleSet,
    compute_boundary_violation,
    compute_drc_violations,
    compute_overlap,
)
from kicad_tools.placement.cpp_backend import (
    BatchCostEvaluatorWrapper,
    _build_boxes_from_placements,
    get_backend_info,
    is_cpp_available,
)

# Tolerance for floating point comparison
TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_placements(positions: list[tuple[str, float, float]]) -> list[ComponentPlacement]:
    """Create ComponentPlacement objects from (ref, x, y) tuples."""
    return [ComponentPlacement(reference=ref, x=x, y=y) for ref, x, y in positions]


def _make_footprint_sizes(
    refs: list[str],
    sizes: list[tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """Create footprint_sizes dict from parallel lists."""
    return dict(zip(refs, sizes, strict=False))


# ---------------------------------------------------------------------------
# Backend info tests (always run)
# ---------------------------------------------------------------------------


class TestBackendInfo:
    """Test backend info and availability reporting."""

    def test_get_backend_info_returns_dict(self):
        info = get_backend_info()
        assert isinstance(info, dict)
        assert "backend" in info
        assert "version" in info
        assert "available" in info
        assert "platform" in info

    def test_backend_info_has_platform_details(self):
        info = get_backend_info()
        plat = info["platform"]
        assert "system" in plat
        assert "machine" in plat
        assert "python_version" in plat

    def test_is_cpp_available_returns_bool(self):
        assert isinstance(is_cpp_available(), bool)

    def test_backend_info_consistency(self):
        info = get_backend_info()
        if is_cpp_available():
            assert info["backend"] == "cpp"
            assert info["available"] is True
        else:
            assert info["backend"] == "python"
            assert info["available"] is False
            assert "unavailable_reason" in info


# ---------------------------------------------------------------------------
# Python fallback tests (always run, no C++ required)
# ---------------------------------------------------------------------------


class TestPythonFallback:
    """Verify all functions work correctly with pure Python fallback."""

    def test_batch_evaluator_python_fallback(self):
        """BatchCostEvaluatorWrapper works with force_python=True."""
        board = BoardOutline(min_x=0, min_y=0, max_x=50, max_y=50)
        rules = DesignRuleSet(min_clearance=0.2)
        evaluator = BatchCostEvaluatorWrapper(board, rules, force_python=True)

        assert evaluator.backend == "python"

        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 12, 10),
            ]
        )
        sizes = {"U1": (5.0, 5.0), "U2": (5.0, 5.0)}

        overlap, boundary, drc = evaluator.evaluate(placements, sizes)
        # These two boxes overlap (centers 2mm apart, each 5mm wide)
        assert overlap > 0
        assert boundary == 0.0
        assert drc > 0

    def test_batch_evaluator_no_components(self):
        """Batch evaluator handles empty placement list."""
        board = BoardOutline(min_x=0, min_y=0, max_x=50, max_y=50)
        rules = DesignRuleSet(min_clearance=0.2)
        evaluator = BatchCostEvaluatorWrapper(board, rules, force_python=True)

        placements: list[ComponentPlacement] = []
        overlap, boundary, drc = evaluator.evaluate(placements)
        assert overlap == 0.0
        assert boundary == 0.0
        assert drc == 0.0

    def test_batch_evaluator_single_component(self):
        """Single component: no pairwise comparisons, check boundary only."""
        board = BoardOutline(min_x=0, min_y=0, max_x=50, max_y=50)
        rules = DesignRuleSet(min_clearance=0.2)
        evaluator = BatchCostEvaluatorWrapper(board, rules, force_python=True)

        placements = _make_placements([("U1", 25, 25)])
        sizes = {"U1": (5.0, 5.0)}

        overlap, boundary, drc = evaluator.evaluate(placements, sizes)
        assert overlap == 0.0
        assert boundary == 0.0
        assert drc == 0.0

    def test_batch_evaluator_boundary_violation(self):
        """Component extending beyond board edge."""
        board = BoardOutline(min_x=0, min_y=0, max_x=50, max_y=50)
        rules = DesignRuleSet(min_clearance=0.2)
        evaluator = BatchCostEvaluatorWrapper(board, rules, force_python=True)

        # Place component at left edge so it extends beyond
        placements = _make_placements([("U1", 1, 25)])
        sizes = {"U1": (5.0, 5.0)}

        overlap, boundary, drc = evaluator.evaluate(placements, sizes)
        assert overlap == 0.0
        assert boundary > 0.0  # Left edge violation: 0 - (1 - 2.5) = 1.5
        assert drc == 0.0

    def test_individual_methods_python_fallback(self):
        """Individual evaluate_* methods work via Python fallback."""
        board = BoardOutline(min_x=0, min_y=0, max_x=50, max_y=50)
        rules = DesignRuleSet(min_clearance=0.2)
        evaluator = BatchCostEvaluatorWrapper(board, rules, force_python=True)

        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 12, 10),
            ]
        )
        sizes = {"U1": (5.0, 5.0), "U2": (5.0, 5.0)}

        overlap = evaluator.evaluate_overlap(placements, sizes)
        boundary = evaluator.evaluate_boundary(placements, sizes)
        drc = evaluator.evaluate_drc(placements, sizes)

        assert overlap > 0
        assert boundary == 0.0
        assert drc > 0


# ---------------------------------------------------------------------------
# Cross-check tests (require C++ backend)
# ---------------------------------------------------------------------------

cpp_required = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ placement backend not available",
)


@cpp_required
class TestCrossCheckOverlap:
    """Cross-check compute_overlap: C++ vs Python."""

    def test_no_overlap(self):
        """Non-overlapping boxes produce identical zero results."""
        placements = _make_placements(
            [
                ("U1", 0, 0),
                ("U2", 20, 0),
                ("U3", 0, 20),
            ]
        )
        sizes = {"U1": (5, 5), "U2": (5, 5), "U3": (5, 5)}

        py_result = compute_overlap(placements, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100),
            DesignRuleSet(min_clearance=0.2),
            force_python=False,
        )
        cpp_result = cpp_evaluator.evaluate_overlap(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result == 0.0

    def test_full_overlap(self):
        """Identical positions produce identical overlap areas."""
        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 10, 10),
            ]
        )
        sizes = {"U1": (4, 6), "U2": (4, 6)}

        py_result = compute_overlap(placements, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100),
            DesignRuleSet(min_clearance=0.2),
            force_python=False,
        )
        cpp_result = cpp_evaluator.evaluate_overlap(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        # Full overlap of 4x6 box
        assert abs(py_result - 24.0) < TOLERANCE

    def test_partial_overlap(self):
        """Partially overlapping boxes produce identical results."""
        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 12, 11),
            ]
        )
        sizes = {"U1": (6, 6), "U2": (6, 6)}

        py_result = compute_overlap(placements, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100),
            DesignRuleSet(min_clearance=0.2),
            force_python=False,
        )
        cpp_result = cpp_evaluator.evaluate_overlap(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result > 0

    def test_many_components(self):
        """50 components in a grid produce identical overlap results."""
        import random

        random.seed(42)
        refs = [f"C{i}" for i in range(50)]
        positions = [(ref, random.uniform(0, 30), random.uniform(0, 30)) for ref in refs]
        placements = _make_placements(positions)
        sizes = dict.fromkeys(refs, (2.0, 1.0))

        py_result = compute_overlap(placements, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100),
            DesignRuleSet(min_clearance=0.2),
            force_python=False,
        )
        cpp_result = cpp_evaluator.evaluate_overlap(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE

    def test_default_sizes(self):
        """Default 1x1mm sizes when footprint_sizes is None."""
        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 10.5, 10),
            ]
        )

        py_result = compute_overlap(placements, None)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100),
            DesignRuleSet(min_clearance=0.2),
            force_python=False,
        )
        cpp_result = cpp_evaluator.evaluate_overlap(placements, None)

        assert abs(py_result - cpp_result) < TOLERANCE


@cpp_required
class TestCrossCheckBoundary:
    """Cross-check compute_boundary_violation: C++ vs Python."""

    def test_all_inside(self):
        """Components inside board produce zero violation."""
        board = BoardOutline(0, 0, 100, 100)
        placements = _make_placements(
            [
                ("U1", 50, 50),
                ("U2", 25, 75),
            ]
        )
        sizes = {"U1": (10, 10), "U2": (10, 10)}

        py_result = compute_boundary_violation(placements, board, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            board, DesignRuleSet(min_clearance=0.2), force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_boundary(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result == 0.0

    def test_left_edge_violation(self):
        """Component extending past left edge."""
        board = BoardOutline(0, 0, 100, 100)
        placements = _make_placements([("U1", 2, 50)])
        sizes = {"U1": (10, 10)}

        py_result = compute_boundary_violation(placements, board, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            board, DesignRuleSet(min_clearance=0.2), force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_boundary(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        # Left edge: board.min_x (0) - (2 - 5) = 3.0
        assert abs(py_result - 3.0) < TOLERANCE

    def test_multiple_edge_violations(self):
        """Component extending past multiple edges."""
        board = BoardOutline(10, 10, 40, 40)
        placements = _make_placements([("U1", 8, 42)])
        sizes = {"U1": (6, 6)}

        py_result = compute_boundary_violation(placements, board, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            board, DesignRuleSet(min_clearance=0.2), force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_boundary(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result > 0

    def test_many_components_boundary(self):
        """50 components with some outside board."""
        import random

        random.seed(123)
        board = BoardOutline(0, 0, 50, 50)
        refs = [f"R{i}" for i in range(50)]
        # Some positions deliberately outside
        positions = [(ref, random.uniform(-5, 55), random.uniform(-5, 55)) for ref in refs]
        placements = _make_placements(positions)
        sizes = dict.fromkeys(refs, (3.0, 2.0))

        py_result = compute_boundary_violation(placements, board, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            board, DesignRuleSet(min_clearance=0.2), force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_boundary(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE


@cpp_required
class TestCrossCheckDRC:
    """Cross-check compute_drc_violations: C++ vs Python."""

    def test_well_spaced(self):
        """Well-spaced components produce zero violations."""
        placements = _make_placements(
            [
                ("U1", 0, 0),
                ("U2", 20, 0),
            ]
        )
        sizes = {"U1": (5, 5), "U2": (5, 5)}
        rules = DesignRuleSet(min_clearance=0.2)

        py_result = compute_drc_violations(placements, rules, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100), rules, force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_drc(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result == 0.0

    def test_overlapping_boxes(self):
        """Overlapping boxes are always DRC violations."""
        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 10, 10),
            ]
        )
        sizes = {"U1": (5, 5), "U2": (5, 5)}
        rules = DesignRuleSet(min_clearance=0.2)

        py_result = compute_drc_violations(placements, rules, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100), rules, force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_drc(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result == 1.0

    def test_corner_to_corner(self):
        """Corner-to-corner distance uses Euclidean calculation."""
        # Boxes separated on both axes, corner-to-corner < min_clearance
        placements = _make_placements(
            [
                ("U1", 0, 0),
                ("U2", 5.1, 5.1),
            ]
        )
        sizes = {"U1": (5, 5), "U2": (5, 5)}
        # gap_x = 0.1, gap_y = 0.1, corner distance = sqrt(0.01+0.01) ~ 0.1414
        rules = DesignRuleSet(min_clearance=0.2)

        py_result = compute_drc_violations(placements, rules, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100), rules, force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_drc(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        assert py_result == 1.0  # 0.1414 < 0.2 clearance

    def test_edge_to_edge(self):
        """Edge-to-edge gap on one axis."""
        # Boxes separated on X axis but overlapping on Y axis
        placements = _make_placements(
            [
                ("U1", 0, 0),
                ("U2", 5.1, 0),
            ]
        )
        sizes = {"U1": (5, 5), "U2": (5, 5)}
        rules = DesignRuleSet(min_clearance=0.2)

        py_result = compute_drc_violations(placements, rules, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100), rules, force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_drc(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE
        # gap = 0.1 < 0.2 -> violation
        assert py_result == 1.0

    def test_many_components_drc(self):
        """50 components in tight space produce identical violation counts."""
        import random

        random.seed(99)
        refs = [f"Q{i}" for i in range(50)]
        positions = [(ref, random.uniform(0, 20), random.uniform(0, 20)) for ref in refs]
        placements = _make_placements(positions)
        sizes = dict.fromkeys(refs, (2.0, 2.0))
        rules = DesignRuleSet(min_clearance=0.5)

        py_result = compute_drc_violations(placements, rules, sizes)
        cpp_evaluator = BatchCostEvaluatorWrapper(
            BoardOutline(0, 0, 100, 100), rules, force_python=False
        )
        cpp_result = cpp_evaluator.evaluate_drc(placements, sizes)

        assert abs(py_result - cpp_result) < TOLERANCE


@cpp_required
class TestCrossCheckBatch:
    """Cross-check BatchCostEvaluator: C++ vs Python."""

    def test_batch_evaluate_matches_individual(self):
        """Batch evaluate matches individual Python function results."""
        board = BoardOutline(0, 0, 50, 50)
        rules = DesignRuleSet(min_clearance=0.3)

        placements = _make_placements(
            [
                ("U1", 5, 5),
                ("U2", 7, 5),
                ("U3", 48, 48),
            ]
        )
        sizes = {"U1": (4, 4), "U2": (4, 4), "U3": (6, 6)}

        # Python reference
        py_overlap = compute_overlap(placements, sizes)
        py_boundary = compute_boundary_violation(placements, board, sizes)
        py_drc = compute_drc_violations(placements, rules, sizes)

        # C++ batch
        evaluator = BatchCostEvaluatorWrapper(board, rules, force_python=False)
        cpp_overlap, cpp_boundary, cpp_drc = evaluator.evaluate(placements, sizes)

        assert abs(py_overlap - cpp_overlap) < TOLERANCE
        assert abs(py_boundary - cpp_boundary) < TOLERANCE
        assert abs(py_drc - cpp_drc) < TOLERANCE

    def test_batch_vs_python_fallback(self):
        """C++ batch evaluator matches Python fallback evaluator."""
        board = BoardOutline(0, 0, 100, 100)
        rules = DesignRuleSet(min_clearance=0.2)

        import random

        random.seed(7)
        refs = [f"D{i}" for i in range(30)]
        positions = [(ref, random.uniform(5, 95), random.uniform(5, 95)) for ref in refs]
        placements = _make_placements(positions)
        sizes = {ref: (random.uniform(1, 5), random.uniform(1, 5)) for ref in refs}

        cpp_eval = BatchCostEvaluatorWrapper(board, rules, force_python=False)
        py_eval = BatchCostEvaluatorWrapper(board, rules, force_python=True)

        cpp_overlap, cpp_boundary, cpp_drc = cpp_eval.evaluate(placements, sizes)
        py_overlap, py_boundary, py_drc = py_eval.evaluate(placements, sizes)

        assert abs(py_overlap - cpp_overlap) < TOLERANCE
        assert abs(py_boundary - cpp_boundary) < TOLERANCE
        assert abs(py_drc - cpp_drc) < TOLERANCE


# ---------------------------------------------------------------------------
# Edge case tests (always run via Python, cross-check if C++ available)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases that should work identically on both backends."""

    def test_zero_components(self):
        """Empty list produces all-zero costs."""
        board = BoardOutline(0, 0, 50, 50)
        rules = DesignRuleSet(min_clearance=0.2)

        py_eval = BatchCostEvaluatorWrapper(board, rules, force_python=True)
        py_result = py_eval.evaluate([], None)
        assert py_result == (0.0, 0.0, 0.0)

        if is_cpp_available():
            cpp_eval = BatchCostEvaluatorWrapper(board, rules, force_python=False)
            cpp_result = cpp_eval.evaluate([], None)
            assert cpp_result == (0.0, 0.0, 0.0)

    def test_single_component_no_pairwise(self):
        """Single component produces zero overlap and DRC."""
        board = BoardOutline(0, 0, 50, 50)
        rules = DesignRuleSet(min_clearance=0.2)
        placements = _make_placements([("U1", 25, 25)])
        sizes = {"U1": (5, 5)}

        py_eval = BatchCostEvaluatorWrapper(board, rules, force_python=True)
        py_overlap, py_boundary, py_drc = py_eval.evaluate(placements, sizes)
        assert py_overlap == 0.0
        assert py_drc == 0.0

        if is_cpp_available():
            cpp_eval = BatchCostEvaluatorWrapper(board, rules, force_python=False)
            cpp_overlap, cpp_boundary, cpp_drc = cpp_eval.evaluate(placements, sizes)
            assert abs(py_overlap - cpp_overlap) < TOLERANCE
            assert abs(py_boundary - cpp_boundary) < TOLERANCE
            assert abs(py_drc - cpp_drc) < TOLERANCE

    def test_asymmetric_sizes(self):
        """Components with different widths and heights."""
        board = BoardOutline(0, 0, 100, 100)
        rules = DesignRuleSet(min_clearance=0.2)
        placements = _make_placements(
            [
                ("U1", 10, 10),
                ("U2", 12, 10),
            ]
        )
        sizes = {"U1": (8, 2), "U2": (2, 8)}

        py_eval = BatchCostEvaluatorWrapper(board, rules, force_python=True)
        py_result = py_eval.evaluate(placements, sizes)

        if is_cpp_available():
            cpp_eval = BatchCostEvaluatorWrapper(board, rules, force_python=False)
            cpp_result = cpp_eval.evaluate(placements, sizes)
            for py_val, cpp_val in zip(py_result, cpp_result, strict=False):
                assert abs(py_val - cpp_val) < TOLERANCE


class TestBuildBoxesHelper:
    """Test the _build_boxes_from_placements helper function."""

    def test_basic_conversion(self):
        placements = _make_placements(
            [
                ("U1", 10, 20),
                ("U2", 30, 40),
            ]
        )
        sizes = {"U1": (5.0, 3.0), "U2": (2.0, 4.0)}

        xs, ys, widths, heights = _build_boxes_from_placements(placements, sizes)

        assert xs == [10, 30]
        assert ys == [20, 40]
        assert widths == [5.0, 2.0]
        assert heights == [3.0, 4.0]

    def test_default_sizes(self):
        placements = _make_placements([("U1", 5, 5)])

        xs, ys, widths, heights = _build_boxes_from_placements(placements, None)

        assert widths == [1.0]
        assert heights == [1.0]

    def test_empty_placements(self):
        xs, ys, widths, heights = _build_boxes_from_placements([], None)
        assert xs == []
        assert ys == []
