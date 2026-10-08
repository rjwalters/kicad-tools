"""Iterative overlap resolution (slide-off) for placement vectors.

Provides a self-contained, configurable settling pass that pushes
overlapping component bounding boxes apart until no overlaps remain
(or iteration/displacement limits are reached).

Key properties:

- Operates on :class:`PlacementVector` / :class:`ComponentDef` data
  (not on ``.kicad_pcb`` files via regex).
- Supports a configurable clearance margin (the Zeo-equivalent 0.5 mm).
- Caps total displacement per component per call.
- Re-clamps to board bounds after each settling iteration.
- Can be called as pure pre-processing or post-processing without
  modifying disk state.
- Components on opposite sides (front/back) are not pushed apart.
- Deterministic fallback direction for coincident components.
- Optional *violation-only* trigger (``min_clearance_mm``): a pair that is
  already legal (no overlap, edge gap >= the clearance rule) is never
  touched, even when it sits closer than ``margin_mm``.  Warm starts from a
  hand floorplan use this so a feasible layout comes back unchanged
  (issue #6250).
- Optional placement grid (``grid_mm``): every non-zero displacement is
  rounded away from zero to a whole number of grid steps, so a part that
  started on the grid stays on it.

Usage::

    from kicad_tools.placement.slide_off import slide_off_overlaps, SlideOffResult
    from kicad_tools.placement.cost import BoardOutline
    from kicad_tools.placement.vector import ComponentDef, PlacementVector

    new_vector, result = slide_off_overlaps(vector, component_defs, board)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .cost import BoardOutline
from .vector import (
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PlacementVector,
)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverlapDetail:
    """Detail of a single remaining overlap pair.

    Attributes:
        ref1: Reference designator of the first component.
        ref2: Reference designator of the second component.
        actual_clearance_mm: Actual clearance between the pair (negative
            means overlap).
        required_clearance_mm: Required clearance (i.e. the margin).
    """

    ref1: str
    ref2: str
    actual_clearance_mm: float
    required_clearance_mm: float

    def __str__(self) -> str:
        return (
            f"{self.ref1}/{self.ref2}: {self.actual_clearance_mm:+.3f}mm clearance, "
            f"required {self.required_clearance_mm:.3f}mm"
        )


@dataclass(frozen=True)
class SlideOffResult:
    """Result of running the slide-off overlap resolver.

    Attributes:
        iterations_run: Number of settling iterations executed.
        overlaps_resolved: Total number of overlap pairs resolved.
        overlaps_remaining: Number of overlap pairs still present after
            the final iteration.
        max_displacement_applied: Maximum Euclidean displacement applied
            to any single component (mm).
        overlap_details: Detailed information about each remaining overlap
            pair (empty when no overlaps remain).
    """

    iterations_run: int
    overlaps_resolved: int
    overlaps_remaining: int
    max_displacement_applied: float
    overlap_details: tuple[OverlapDetail, ...] = ()


# ---------------------------------------------------------------------------
# Spatial index (grid-based)
# ---------------------------------------------------------------------------


class _SpatialGrid:
    """Simple grid-based spatial index for fast neighbor lookup.

    Partitions the board into square cells and maps component indices
    to their occupied cells.  Querying potential overlap candidates
    for a given component returns only components sharing at least one
    cell, reducing the O(N^2) pairwise check for sparse boards.

    Args:
        cell_size: Side length of each grid cell in mm.
    """

    def __init__(self, cell_size: float = 10.0) -> None:
        self._cell_size = cell_size
        self._grid: dict[tuple[int, int], list[int]] = {}
        self._comp_cells: dict[int, list[tuple[int, int]]] = {}

    def _cell_range(
        self,
        min_val: float,
        max_val: float,
    ) -> range:
        """Return the range of cell indices spanning [min_val, max_val]."""
        lo = int(math.floor(min_val / self._cell_size))
        hi = int(math.floor(max_val / self._cell_size))
        return range(lo, hi + 1)

    def build(
        self,
        positions: np.ndarray,
        half_sizes: np.ndarray,
        margin: float,
    ) -> None:
        """Rebuild the grid from current positions.

        Args:
            positions: (N, 2) array of component center positions.
            half_sizes: (N, 2) array of component half-widths/heights.
            margin: Extra clearance margin to expand AABBs.
        """
        self._grid.clear()
        self._comp_cells.clear()
        n = positions.shape[0]
        for i in range(n):
            min_x = positions[i, 0] - half_sizes[i, 0] - margin
            max_x = positions[i, 0] + half_sizes[i, 0] + margin
            min_y = positions[i, 1] - half_sizes[i, 1] - margin
            max_y = positions[i, 1] + half_sizes[i, 1] + margin

            cells: list[tuple[int, int]] = []
            for cx in self._cell_range(min_x, max_x):
                for cy in self._cell_range(min_y, max_y):
                    key = (cx, cy)
                    cells.append(key)
                    self._grid.setdefault(key, []).append(i)
            self._comp_cells[i] = cells

    def potential_pairs(self) -> set[tuple[int, int]]:
        """Return all candidate overlap pairs (i < j)."""
        pairs: set[tuple[int, int]] = set()
        for cell_members in self._grid.values():
            for a_idx in range(len(cell_members)):
                for b_idx in range(a_idx + 1, len(cell_members)):
                    i = cell_members[a_idx]
                    j = cell_members[b_idx]
                    if i < j:
                        pairs.add((i, j))
                    else:
                        pairs.add((j, i))
        return pairs


# ---------------------------------------------------------------------------
# Deterministic fallback direction
# ---------------------------------------------------------------------------

# When two components are at the exact same position, we push them apart
# along this vector instead of producing NaN from a zero-length direction.
_FALLBACK_DX = 1.0
_FALLBACK_DY = 0.0

# Minimum push factor to avoid zero-push degenerate iterations
_MIN_PUSH = 1e-6

# Jitter radius for coincident components (mm) -- small enough to be
# invisible, large enough to break the axis-alignment degeneracy.
_JITTER_RADIUS = 0.01


# ---------------------------------------------------------------------------
# Coincident-component jitter
# ---------------------------------------------------------------------------


def _apply_coincident_jitter(
    positions: np.ndarray,
    sides: np.ndarray,
    n: int,
) -> None:
    """Add a small deterministic radial offset to coincident components.

    Groups of same-side components that share the exact same (x, y)
    position are spread in a circular pattern so that the subsequent
    pairwise push-apart has distinct axis differences to work with.

    Modifies *positions* in place.
    """
    # Group by (x, y, side) -- use rounded key to catch near-coincidences
    from collections import defaultdict

    groups: dict[tuple[float, float, int], list[int]] = defaultdict(list)
    for i in range(n):
        key = (
            round(positions[i, 0], 6),
            round(positions[i, 1], 6),
            int(sides[i]),
        )
        groups[key].append(i)

    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Spread members in a circle around their shared centre
        cx, cy = positions[members[0], 0], positions[members[0], 1]
        count = len(members)
        for rank, idx in enumerate(members):
            angle = 2.0 * math.pi * rank / count
            positions[idx, 0] = cx + _JITTER_RADIUS * math.cos(angle)
            positions[idx, 1] = cy + _JITTER_RADIUS * math.sin(angle)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def slide_off_overlaps(
    vector: PlacementVector,
    component_defs: Sequence[ComponentDef],
    board: BoardOutline,
    *,
    margin_mm: float = 0.5,
    max_iterations: int = 5,
    max_displacement_mm: float = 20.0,
    use_spatial_index: bool | None = None,
    min_clearance_mm: float | None = None,
    grid_mm: float = 0.0,
) -> tuple[PlacementVector, SlideOffResult]:
    """Resolve component overlaps by iteratively sliding components apart.

    For each pair of overlapping components (on the same board side), the
    minimum translation vector is computed and each component is pushed
    by half that vector in opposite directions.  After every push the
    positions are clamped to the board outline so components never leave
    the board.  Per-component cumulative displacement is tracked and
    capped at *max_displacement_mm*.

    Args:
        vector: Input placement vector.
        component_defs: Static component definitions (same order as
            encoded in *vector*).
        board: Rectangular board outline.
        margin_mm: Extra clearance margin to enforce beyond zero overlap.
            Each AABB is expanded by this amount before checking.
        max_iterations: Maximum number of settling iterations.
        max_displacement_mm: Maximum total Euclidean displacement per
            component per call.
        use_spatial_index: Whether to use a grid-based spatial index.
            ``None`` (default) enables it automatically when the number
            of components exceeds 50.
        min_clearance_mm: Violation threshold.  ``None`` (default) keeps
            the historical behaviour: any pair closer than *margin_mm* is
            pushed.  When set, a pair is only pushed once it is a real
            violation -- its bounding boxes overlap, or their edge gap
            (corner-to-corner when separated on both axes, the same metric
            as :func:`~kicad_tools.placement.cost.compute_drc_violations`)
            is below this value.  A violating pair is still pushed out to
            *margin_mm*; a legal pair inside the margin is left alone, so
            a layout with no violations comes back unchanged.  Remaining
            and resolved counts then count violations, not margin misses.
        grid_mm: Placement grid in mm (``0`` disables).  Each moved
            component's net displacement is rounded, per axis and away
            from zero, to a multiple of this step (pulled back a step at a
            time if that would leave the board), so a part placed on the
            grid stays on it.  Rounding can exceed *max_displacement_mm* by
            up to one grid step per axis.

    Returns:
        Tuple of ``(new_vector, result)`` where *new_vector* is the
        adjusted placement vector and *result* is a :class:`SlideOffResult`
        with diagnostic fields.
    """
    n = len(component_defs)
    if n != vector.num_components:
        raise ValueError(
            f"vector encodes {vector.num_components} components "
            f"but {n} component definitions provided"
        )

    if n < 2:
        return vector, SlideOffResult(
            iterations_run=0,
            overlaps_resolved=0,
            overlaps_remaining=0,
            max_displacement_applied=0.0,
        )

    # Work on a mutable copy of the underlying data
    data = vector.data.copy()

    # Extract positions and sides
    positions = np.empty((n, 2), dtype=np.float64)
    sides = np.empty(n, dtype=np.int32)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        positions[i, 0] = data[base]
        positions[i, 1] = data[base + 1]
        # Determine effective side
        sides[i] = int(round(data[base + 3]))

    # Compute rotation-aware half-sizes per component
    half_sizes = np.empty((n, 2), dtype=np.float64)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        rot_idx = int(round(data[base + 2])) % 4
        if rot_idx in (1, 3):
            half_sizes[i, 0] = component_defs[i].height / 2.0
            half_sizes[i, 1] = component_defs[i].width / 2.0
        else:
            half_sizes[i, 0] = component_defs[i].width / 2.0
            half_sizes[i, 1] = component_defs[i].height / 2.0

    # Pre-compute per-component safe bounds (component centre stays within)
    x_lo = np.array(
        [board.min_x + half_sizes[i, 0] for i in range(n)],
        dtype=np.float64,
    )
    x_hi = np.array(
        [board.max_x - half_sizes[i, 0] for i in range(n)],
        dtype=np.float64,
    )
    y_lo = np.array(
        [board.min_y + half_sizes[i, 1] for i in range(n)],
        dtype=np.float64,
    )
    y_hi = np.array(
        [board.max_y - half_sizes[i, 1] for i in range(n)],
        dtype=np.float64,
    )

    # Exact input positions, before the coincident jitter below: unmoved
    # components are restored to these and grid rounding is measured
    # from them.
    original_positions = positions.copy()

    # Pre-jitter: deterministically spread coincident components.
    # When multiple components share the exact same centre, the
    # axis-aligned push loop cannot spread them in 2D because every
    # pair picks the same push direction.  We apply a tiny radial
    # offset (proportional to component index) so that subsequent
    # pairwise pushes have distinct axes to work with.
    _apply_coincident_jitter(positions, sides, n)

    # Save initial positions to track cumulative displacement
    initial_positions = positions.copy()

    # Cumulative displacement per component
    cumulative_disp = np.zeros(n, dtype=np.float64)

    # Decide whether to use spatial index
    if use_spatial_index is None:
        use_spatial_index = n > 50

    spatial_grid: _SpatialGrid | None = None
    if use_spatial_index:
        spatial_grid = _SpatialGrid(cell_size=10.0)

    total_resolved = 0
    iterations_run = 0
    remaining_overlaps = 0

    # Pairs already found violating ``min_clearance_mm``: they keep being
    # pushed until they clear the full margin.
    active_pairs: set[tuple[int, int]] = set()

    for iteration in range(max_iterations):
        iterations_run = iteration + 1

        # Build spatial index if enabled
        if spatial_grid is not None:
            spatial_grid.build(positions, half_sizes, margin_mm)
            candidate_pairs = spatial_grid.potential_pairs()
        else:
            candidate_pairs = None

        any_overlap = False
        overlap_count = 0

        if candidate_pairs is not None:
            pairs_to_check = sorted(candidate_pairs)
        else:
            # Full pairwise check
            pairs_to_check = [(i, j) for i in range(n) for j in range(i + 1, n)]

        for i, j in pairs_to_check:
            # Skip components on different sides
            if sides[i] != sides[j]:
                continue

            # Check if both components have exhausted their displacement budget
            if (
                cumulative_disp[i] >= max_displacement_mm
                and cumulative_disp[j] >= max_displacement_mm
            ):
                continue

            # Compute overlap with margin
            dx = positions[j, 0] - positions[i, 0]
            dy = positions[j, 1] - positions[i, 1]

            combined_hw = half_sizes[i, 0] + half_sizes[j, 0] + margin_mm
            combined_hh = half_sizes[i, 1] + half_sizes[j, 1] + margin_mm

            overlap_x = combined_hw - abs(dx)
            overlap_y = combined_hh - abs(dy)

            if overlap_x > 0 and overlap_y > 0:
                if min_clearance_mm is not None and (i, j) not in active_pairs:
                    # Inside the margin but possibly legal: only a real
                    # violation is worth moving a part for (issue #6250).
                    if not _violates_clearance(
                        abs(dx) - half_sizes[i, 0] - half_sizes[j, 0],
                        abs(dy) - half_sizes[i, 1] - half_sizes[j, 1],
                        min_clearance_mm,
                    ):
                        continue
                    active_pairs.add((i, j))

                any_overlap = True
                overlap_count += 1

                # Compute push along the minimum-overlap axis.
                # Use a push factor > 1.0 to overshoot slightly,
                # preventing oscillation in dense configurations
                # (matches seed.py push_factor=1.1).
                _push_factor = 1.1

                # Determine which axis to push along.  When one axis
                # overlap is negligible (< 1e-3 mm) while the other is
                # substantial, pushing along the negligible axis produces
                # near-zero movement that never converges.  In that case
                # push along the larger axis instead.
                _NEGLIGIBLE = 1e-3
                if overlap_y < _NEGLIGIBLE < overlap_x:
                    # Y overlap is negligible; push in X
                    push_x = True
                elif overlap_x < _NEGLIGIBLE < overlap_y:
                    # X overlap is negligible; push in Y
                    push_x = False
                else:
                    push_x = overlap_x < overlap_y

                if push_x:
                    push_amount = _push_factor * overlap_x / 2.0
                    if push_amount < _MIN_PUSH:
                        push_amount = _MIN_PUSH
                    if dx >= 0:
                        push_i_x = -push_amount
                        push_j_x = push_amount
                    else:
                        push_i_x = push_amount
                        push_j_x = -push_amount
                    push_i_y = 0.0
                    push_j_y = 0.0
                else:
                    push_amount = _push_factor * overlap_y / 2.0
                    if push_amount < _MIN_PUSH:
                        push_amount = _MIN_PUSH
                    if dy >= 0:
                        push_i_y = -push_amount
                        push_j_y = push_amount
                    else:
                        push_i_y = push_amount
                        push_j_y = -push_amount
                    push_i_x = 0.0
                    push_j_x = 0.0

                # Apply displacement cap to component i
                remaining_i = max(0.0, max_displacement_mm - cumulative_disp[i])
                push_i_mag = math.sqrt(push_i_x * push_i_x + push_i_y * push_i_y)
                if push_i_mag > remaining_i and push_i_mag > 0:
                    scale = remaining_i / push_i_mag
                    push_i_x *= scale
                    push_i_y *= scale

                # Apply displacement cap to component j
                remaining_j = max(0.0, max_displacement_mm - cumulative_disp[j])
                push_j_mag = math.sqrt(push_j_x * push_j_x + push_j_y * push_j_y)
                if push_j_mag > remaining_j and push_j_mag > 0:
                    scale = remaining_j / push_j_mag
                    push_j_x *= scale
                    push_j_y *= scale

                # Apply pushes
                positions[i, 0] += push_i_x
                positions[i, 1] += push_i_y
                positions[j, 0] += push_j_x
                positions[j, 1] += push_j_y

                # Update cumulative displacement from initial position
                cumulative_disp[i] = math.sqrt(
                    (positions[i, 0] - initial_positions[i, 0]) ** 2
                    + (positions[i, 1] - initial_positions[i, 1]) ** 2
                )
                cumulative_disp[j] = math.sqrt(
                    (positions[j, 0] - initial_positions[j, 0]) ** 2
                    + (positions[j, 1] - initial_positions[j, 1]) ** 2
                )

        # Clamp all positions to board bounds
        for i in range(n):
            if x_lo[i] <= x_hi[i]:
                positions[i, 0] = max(x_lo[i], min(x_hi[i], positions[i, 0]))
            else:
                # Component wider than board: center it
                positions[i, 0] = (board.min_x + board.max_x) / 2.0
            if y_lo[i] <= y_hi[i]:
                positions[i, 1] = max(y_lo[i], min(y_hi[i], positions[i, 1]))
            else:
                positions[i, 1] = (board.min_y + board.max_y) / 2.0

        # Recompute cumulative displacement after clamping
        for i in range(n):
            cumulative_disp[i] = math.sqrt(
                (positions[i, 0] - initial_positions[i, 0]) ** 2
                + (positions[i, 1] - initial_positions[i, 1]) ** 2
            )

        if not any_overlap:
            # Count resolved overlaps from previous iterations
            total_resolved += 0  # No new overlaps to resolve
            remaining_overlaps = 0
            break
        else:
            # We attempted to resolve overlap_count pairs this iteration
            total_resolved += overlap_count
            remaining_overlaps = overlap_count

    # Snap displacements to the placement grid, then restore every
    # component that did not move to its exact input position.
    _finalize_positions(
        positions,
        original_positions,
        x_lo,
        x_hi,
        y_lo,
        y_hi,
        grid_mm,
    )
    for i in range(n):
        cumulative_disp[i] = math.sqrt(
            (positions[i, 0] - original_positions[i, 0]) ** 2
            + (positions[i, 1] - original_positions[i, 1]) ** 2
        )

    # Count what is left on the final (snapped) positions.
    remaining_overlaps = _count_overlaps(
        positions,
        half_sizes,
        sides,
        margin_mm,
        n,
        spatial_grid,
        min_clearance=min_clearance_mm,
    )

    # Compute final total_resolved as the initial overlaps minus remaining
    initial_overlaps = _count_overlaps(
        initial_positions,
        half_sizes,
        sides,
        margin_mm,
        n,
        spatial_grid=None,  # Use brute force for accurate initial count
        min_clearance=min_clearance_mm,
    )
    # Resolved = how many we started with minus how many remain
    actual_resolved = max(0, initial_overlaps - remaining_overlaps)

    # Build output vector
    out_data = data.copy()
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        out_data[base] = positions[i, 0]
        out_data[base + 1] = positions[i, 1]

    max_disp = float(np.max(cumulative_disp)) if n > 0 else 0.0

    # Collect detailed overlap information when overlaps remain
    overlap_details: tuple[OverlapDetail, ...] = ()
    if remaining_overlaps > 0:
        detail_list = _get_overlap_details(
            positions,
            half_sizes,
            sides,
            margin_mm,
            n,
            component_defs,
            spatial_grid,
            min_clearance=min_clearance_mm,
        )
        overlap_details = tuple(detail_list)

    return PlacementVector(data=out_data), SlideOffResult(
        iterations_run=iterations_run,
        overlaps_resolved=actual_resolved,
        overlaps_remaining=remaining_overlaps,
        max_displacement_applied=max_disp,
        overlap_details=overlap_details,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _violates_clearance(gap_x: float, gap_y: float, clearance: float) -> bool:
    """Whether two boxes with per-axis edge gaps *gap_x*/*gap_y* violate.

    Negative gaps mean the boxes overlap on that axis.  Mirrors
    :func:`~kicad_tools.placement.cost.compute_drc_violations`: overlapping
    boxes always violate; boxes separated on both axes are measured corner
    to corner; otherwise the gap on the separating axis is used.
    """
    if gap_x < 0 and gap_y < 0:
        return True
    if gap_x > 0 and gap_y > 0:
        gap = math.hypot(gap_x, gap_y)
    else:
        gap = max(gap_x, gap_y)
    return gap < clearance


def _pair_counts(
    gap_x: float,
    gap_y: float,
    margin: float,
    min_clearance: float | None,
) -> bool:
    """Whether a pair counts as an (unresolved) overlap for reporting."""
    if min_clearance is None:
        return gap_x < margin and gap_y < margin
    return _violates_clearance(gap_x, gap_y, min_clearance)


def _snap_away_from_zero(delta: float, grid: float) -> float:
    """Round *delta* away from zero to a whole number of *grid* steps."""
    steps = math.ceil(abs(delta) / grid - 1e-9)
    return math.copysign(steps * grid, delta)


def _finalize_positions(
    positions: np.ndarray,
    original_positions: np.ndarray,
    x_lo: np.ndarray,
    x_hi: np.ndarray,
    y_lo: np.ndarray,
    y_hi: np.ndarray,
    grid: float,
) -> None:
    """Grid-snap each component's displacement; restore unmoved ones.

    Displacements below 1e-9 mm are float noise and are dropped, so a
    component no pair pushed comes back with its exact input coordinates.
    With *grid* > 0 each remaining per-axis displacement is rounded away
    from zero to a multiple of *grid* (never shrinking a separation the
    push loop produced) and then stepped back toward zero, one grid step
    at a time, while the result would leave the board.  Components whose
    clamped range is empty (wider than the board) keep their centred
    position.  Modifies *positions* in place.
    """
    n = positions.shape[0]
    bounds = ((x_lo, x_hi), (y_lo, y_hi))
    for i in range(n):
        for axis in (0, 1):
            delta = positions[i, axis] - original_positions[i, axis]
            if abs(delta) < 1e-9:
                positions[i, axis] = original_positions[i, axis]
                continue
            if grid <= 0:
                continue
            lo, hi = bounds[axis][0][i], bounds[axis][1][i]
            if lo > hi:
                continue
            snapped = _snap_away_from_zero(delta, grid)
            candidate = original_positions[i, axis] + snapped
            while abs(snapped) > 0 and not (lo - 1e-9 <= candidate <= hi + 1e-9):
                snapped -= math.copysign(grid, snapped)
                if abs(snapped) < grid / 2:
                    snapped = 0.0
                candidate = original_positions[i, axis] + snapped
            if lo - 1e-9 <= candidate <= hi + 1e-9:
                positions[i, axis] = candidate
            # else: even the unmoved position is off-board; keep the clamp.


def _count_overlaps(
    positions: np.ndarray,
    half_sizes: np.ndarray,
    sides: np.ndarray,
    margin: float,
    n: int,
    spatial_grid: _SpatialGrid | None = None,
    min_clearance: float | None = None,
) -> int:
    """Count the number of overlapping pairs.

    With *min_clearance* set, counts pairs violating that clearance (see
    :func:`_violates_clearance`) instead of pairs inside *margin*.
    """
    count = 0

    if spatial_grid is not None:
        spatial_grid.build(positions, half_sizes, margin)
        pairs = sorted(spatial_grid.potential_pairs())
    else:
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]

    for i, j in pairs:
        if sides[i] != sides[j]:
            continue

        gap_x = abs(positions[j, 0] - positions[i, 0]) - half_sizes[i, 0] - half_sizes[j, 0]
        gap_y = abs(positions[j, 1] - positions[i, 1]) - half_sizes[i, 1] - half_sizes[j, 1]

        if _pair_counts(gap_x, gap_y, margin, min_clearance):
            count += 1

    return count


def _get_overlap_details(
    positions: np.ndarray,
    half_sizes: np.ndarray,
    sides: np.ndarray,
    margin: float,
    n: int,
    component_defs: Sequence[ComponentDef],
    spatial_grid: _SpatialGrid | None = None,
    min_clearance: float | None = None,
) -> list[OverlapDetail]:
    """Return detailed information about each remaining overlap pair.

    For each overlapping pair, computes the worst-axis clearance (the
    minimum of x-axis and y-axis clearance) and returns an
    :class:`OverlapDetail` with both component references and clearance
    values.
    """
    details: list[OverlapDetail] = []

    if spatial_grid is not None:
        spatial_grid.build(positions, half_sizes, margin)
        pairs = sorted(spatial_grid.potential_pairs())
    else:
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]

    for i, j in pairs:
        if sides[i] != sides[j]:
            continue

        gap_x = abs(positions[j, 0] - positions[i, 0]) - half_sizes[i, 0] - half_sizes[j, 0]
        gap_y = abs(positions[j, 1] - positions[i, 1]) - half_sizes[i, 1] - half_sizes[j, 1]

        if _pair_counts(gap_x, gap_y, margin, min_clearance):
            # Clearance = gap between edges on the axis that separates
            # them most (the minimum-overlap axis determines how far apart
            # they need to move); negative = overlap.
            actual_clearance = max(gap_x, gap_y)
            details.append(
                OverlapDetail(
                    ref1=component_defs[i].reference,
                    ref2=component_defs[j].reference,
                    actual_clearance_mm=round(actual_clearance, 3),
                    required_clearance_mm=(margin if min_clearance is None else min_clearance),
                )
            )

    return details
