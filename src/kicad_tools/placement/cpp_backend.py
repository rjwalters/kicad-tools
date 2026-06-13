"""
C++ placement backend with Python fallback.

This module provides a unified interface to the placement AABB cost
functions that automatically uses the C++ implementation when available,
falling back to pure Python.

The C++ backend provides significant speedup for the O(N^2) pairwise
AABB loops in compute_overlap, compute_drc_violations, and
compute_boundary_violation.

If placement optimization is slow, the C++ backend is likely not
installed. Build it with:

    kct build-native
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .cost import BoardOutline, ComponentPlacement, DesignRuleSet

logger = logging.getLogger(__name__)

# Try to import C++ module with detailed error tracking
_CPP_IMPORT_ERROR: str | None = None
try:
    from . import placement_cpp

    _CPP_AVAILABLE = True
except ImportError as e:
    _CPP_AVAILABLE = False
    _CPP_IMPORT_ERROR = str(e)
    placement_cpp = None  # type: ignore


def is_cpp_available() -> bool:
    """Check if the C++ placement backend is available."""
    return _CPP_AVAILABLE


def get_cpp_unavailable_reason() -> str | None:
    """Get the reason why C++ backend is unavailable.

    Returns:
        Error message if C++ backend failed to load, None if available.
    """
    if _CPP_AVAILABLE:
        return None
    return _CPP_IMPORT_ERROR


def get_backend_info() -> dict:
    """Get information about the active backend.

    Returns a dictionary with:
        - backend: "cpp" or "python"
        - version: version string
        - available: True if C++ backend is available
        - unavailable_reason: Error message if C++ unavailable
        - platform: Current platform info (for diagnostics)
    """
    import platform
    import sys

    platform_info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
    }

    if _CPP_AVAILABLE:
        return {
            "backend": "cpp",
            "version": placement_cpp.version(),
            "available": True,
            "platform": platform_info,
        }

    reason = _CPP_IMPORT_ERROR or "Unknown error"

    build_hint = (
        "Build the C++ extension for faster placement evaluation:\n"
        "  kct build-native\n"
        "Or install with native support:\n"
        "  pip install kicad-tools[native]"
    )

    diagnostic_hint = None
    if "arm64" in platform.machine().lower() or "aarch64" in platform.machine().lower():
        if "darwin" in platform.system().lower():
            diagnostic_hint = (
                "On Apple Silicon, the C++ extension must be built locally. "
                "Run 'kct build-native' to build (requires Xcode Command Line Tools)."
            )
    elif "cannot open shared object" in reason.lower() or "dll" in reason.lower():
        diagnostic_hint = (
            "The compiled C++ extension was not found for this platform. "
            "Run 'kct build-native' to build from source."
        )

    result = {
        "backend": "python",
        "version": "pure-python",
        "available": False,
        "unavailable_reason": reason,
        "build_hint": build_hint,
        "platform": platform_info,
    }

    if diagnostic_hint:
        result["diagnostic_hint"] = diagnostic_hint

    return result


def _build_boxes_from_placements(
    placements: Sequence[ComponentPlacement],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Convert placements + footprint_sizes to flat arrays.

    Returns:
        Tuple of (xs, ys, widths, heights) lists.
    """
    default_size = (1.0, 1.0)
    xs: list[float] = []
    ys: list[float] = []
    widths: list[float] = []
    heights: list[float] = []

    for p in placements:
        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        xs.append(p.x)
        ys.append(p.y)
        widths.append(w)
        heights.append(h)

    return xs, ys, widths, heights


def compute_overlap_cpp(
    placements: Sequence[ComponentPlacement],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute overlap using C++ backend.

    Args:
        placements: Current component positions.
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Sum of pairwise overlap areas (mm^2).

    Raises:
        RuntimeError: If C++ backend is not available.
    """
    if not _CPP_AVAILABLE:
        raise RuntimeError("C++ placement backend not available")

    xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)

    # Build AABB list for the C++ free function
    boxes = []
    for i in range(len(xs)):
        half_w = widths[i] / 2.0
        half_h = heights[i] / 2.0
        boxes.append(
            placement_cpp.AABB(
                xs[i] - half_w,
                ys[i] - half_h,
                xs[i] + half_w,
                ys[i] + half_h,
            )
        )
    return placement_cpp.compute_overlap(boxes)


def compute_boundary_violation_cpp(
    placements: Sequence[ComponentPlacement],
    board: BoardOutline,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute boundary violation using C++ backend.

    Args:
        placements: Current component positions.
        board: Board outline.
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Sum of boundary violation depths (mm).

    Raises:
        RuntimeError: If C++ backend is not available.
    """
    if not _CPP_AVAILABLE:
        raise RuntimeError("C++ placement backend not available")

    xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)

    boxes = []
    for i in range(len(xs)):
        half_w = widths[i] / 2.0
        half_h = heights[i] / 2.0
        boxes.append(
            placement_cpp.AABB(
                xs[i] - half_w,
                ys[i] - half_h,
                xs[i] + half_w,
                ys[i] + half_h,
            )
        )

    board_aabb = placement_cpp.AABB(board.min_x, board.min_y, board.max_x, board.max_y)
    return placement_cpp.compute_boundary_violation(boxes, board_aabb)


def compute_drc_violations_cpp(
    placements: Sequence[ComponentPlacement],
    rules: DesignRuleSet,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute DRC violations using C++ backend.

    Args:
        placements: Current component positions.
        rules: Design rules with clearance constraints.
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Number of pairwise clearance violations.

    Raises:
        RuntimeError: If C++ backend is not available.
    """
    if not _CPP_AVAILABLE:
        raise RuntimeError("C++ placement backend not available")

    xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)

    boxes = []
    for i in range(len(xs)):
        half_w = widths[i] / 2.0
        half_h = heights[i] / 2.0
        boxes.append(
            placement_cpp.AABB(
                xs[i] - half_w,
                ys[i] - half_h,
                xs[i] + half_w,
                ys[i] + half_h,
            )
        )
    return placement_cpp.compute_drc_violations(boxes, rules.min_clearance)


def create_batch_evaluator(
    board: BoardOutline,
    rules: DesignRuleSet,
) -> BatchCostEvaluatorWrapper:
    """Create a batch cost evaluator, preferring C++ if available.

    Args:
        board: Board outline.
        rules: Design rules with clearance constraints.

    Returns:
        BatchCostEvaluatorWrapper that uses C++ when available.
    """
    return BatchCostEvaluatorWrapper(board, rules)


class BatchCostEvaluatorWrapper:
    """Wrapper that delegates to C++ BatchCostEvaluator or Python fallback.

    This class provides a unified interface for batch cost evaluation.
    When the C++ backend is available, it delegates to the C++
    BatchCostEvaluator. Otherwise, it falls back to the pure Python
    implementations in cost.py.
    """

    def __init__(
        self,
        board: BoardOutline,
        rules: DesignRuleSet,
        force_python: bool = False,
    ):
        self._board = board
        self._rules = rules
        self._use_cpp = _CPP_AVAILABLE and not force_python

        if self._use_cpp:
            self._cpp_evaluator = placement_cpp.BatchCostEvaluator(
                board.min_x,
                board.min_y,
                board.max_x,
                board.max_y,
                rules.min_clearance,
            )
        else:
            self._cpp_evaluator = None
            if not force_python:
                logger.warning(
                    "C++ placement backend not available -- using pure Python (slower). "
                    "Build the native backend for faster placement: kct build-native"
                )

    @property
    def backend(self) -> str:
        """Return which backend is active: 'cpp' or 'python'."""
        return "cpp" if self._use_cpp else "python"

    def evaluate(
        self,
        placements: Sequence[ComponentPlacement],
        footprint_sizes: dict[str, tuple[float, float]] | None = None,
    ) -> tuple[float, float, float]:
        """Evaluate overlap, boundary, and DRC costs.

        Args:
            placements: Current component positions.
            footprint_sizes: Map from reference to (width, height) in mm.

        Returns:
            Tuple of (overlap, boundary, drc) costs.
        """
        if self._use_cpp and self._cpp_evaluator is not None:
            xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)
            result = self._cpp_evaluator.evaluate(xs, ys, widths, heights)
            return result.overlap, result.boundary, result.drc

        # Python fallback
        from .cost import (
            compute_boundary_violation,
            compute_drc_violations,
            compute_overlap,
        )

        overlap = compute_overlap(placements, footprint_sizes)
        boundary = compute_boundary_violation(placements, self._board, footprint_sizes)
        drc = compute_drc_violations(placements, self._rules, footprint_sizes)
        return overlap, boundary, drc

    def evaluate_overlap(
        self,
        placements: Sequence[ComponentPlacement],
        footprint_sizes: dict[str, tuple[float, float]] | None = None,
    ) -> float:
        """Compute only pairwise overlap area.

        Args:
            placements: Current component positions.
            footprint_sizes: Map from reference to (width, height) in mm.

        Returns:
            Sum of pairwise overlap areas (mm^2).
        """
        if self._use_cpp and self._cpp_evaluator is not None:
            xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)
            return self._cpp_evaluator.evaluate_overlap(xs, ys, widths, heights)

        from .cost import compute_overlap

        return compute_overlap(placements, footprint_sizes)

    def evaluate_boundary(
        self,
        placements: Sequence[ComponentPlacement],
        footprint_sizes: dict[str, tuple[float, float]] | None = None,
    ) -> float:
        """Compute only boundary violations.

        Args:
            placements: Current component positions.
            footprint_sizes: Map from reference to (width, height) in mm.

        Returns:
            Sum of boundary violation depths (mm).
        """
        if self._use_cpp and self._cpp_evaluator is not None:
            xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)
            return self._cpp_evaluator.evaluate_boundary(xs, ys, widths, heights)

        from .cost import compute_boundary_violation

        return compute_boundary_violation(placements, self._board, footprint_sizes)

    def evaluate_drc(
        self,
        placements: Sequence[ComponentPlacement],
        footprint_sizes: dict[str, tuple[float, float]] | None = None,
    ) -> float:
        """Compute only DRC violations.

        Args:
            placements: Current component positions.
            footprint_sizes: Map from reference to (width, height) in mm.

        Returns:
            Number of pairwise clearance violations.
        """
        if self._use_cpp and self._cpp_evaluator is not None:
            xs, ys, widths, heights = _build_boxes_from_placements(placements, footprint_sizes)
            return self._cpp_evaluator.evaluate_drc(xs, ys, widths, heights)

        from .cost import compute_drc_violations

        return compute_drc_violations(placements, self._rules, footprint_sizes)
