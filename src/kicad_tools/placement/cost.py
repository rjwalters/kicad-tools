"""Composite cost function aggregator for placement scoring.

Aggregates individual placement metrics (wirelength, overlap, DRC violations,
boundary violations, area) into a single scalar score for the optimizer.

Supports two scoring modes:
- Weighted-sum: single weighted sum of all components
- Lexicographic: compare by (overlap, drc, boundary) first, then weighted sum
  of (wirelength, area) — ensures feasible placements always beat infeasible ones

Usage:
    config = PlacementCostConfig()
    score = evaluate_placement(placements, nets, rules, board, config)
    print(score.total, score.is_feasible)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class CostMode(Enum):
    """Scoring mode for the composite cost function."""

    WEIGHTED_SUM = "weighted_sum"
    LEXICOGRAPHIC = "lexicographic"


@dataclass(frozen=True)
class PlacementCostConfig:
    """Configuration for the composite cost function.

    Attributes:
        overlap_weight: Weight for component overlap penalty.
        drc_weight: Weight for DRC violation penalty.
        boundary_weight: Weight for board boundary violation penalty.
        wirelength_weight: Weight for total wirelength cost.
        area_weight: Weight for bounding-box area cost.
        mode: Scoring mode (weighted_sum or lexicographic).
    """

    overlap_weight: float = 1e6
    drc_weight: float = 1e4
    boundary_weight: float = 1e5
    wirelength_weight: float = 1.0
    area_weight: float = 0.1
    mode: CostMode = CostMode.WEIGHTED_SUM


@dataclass(frozen=True)
class CostBreakdown:
    """Per-component cost breakdown for debugging.

    Attributes:
        wirelength: Raw wirelength estimate (mm).
        overlap: Raw overlap penalty (sum of overlap areas, mm^2).
        boundary: Raw boundary violation penalty (sum of violation depths, mm).
        drc: Raw DRC violation count.
        area: Raw bounding-box area of all placements (mm^2).
    """

    wirelength: float = 0.0
    overlap: float = 0.0
    boundary: float = 0.0
    drc: float = 0.0
    area: float = 0.0


@dataclass(frozen=True)
class PlacementScore:
    """Result of evaluating a placement configuration.

    Attributes:
        total: Scalar score (lower is better).
        breakdown: Per-component raw costs before weighting.
        is_feasible: True if overlap=0, drc=0, and boundary=0.
    """

    total: float
    breakdown: CostBreakdown
    is_feasible: bool

    def __lt__(self, other: PlacementScore) -> bool:
        return self.total < other.total

    def __le__(self, other: PlacementScore) -> bool:
        return self.total <= other.total


@dataclass(frozen=True)
class ComponentPlacement:
    """Position of a single component.

    Attributes:
        reference: Component reference designator (e.g. "U1", "R3").
        x: X position in mm.
        y: Y position in mm.
        rotation: Rotation in degrees.
    """

    reference: str
    x: float
    y: float
    rotation: float = 0.0


@dataclass(frozen=True)
class Net:
    """A net connecting component pins.

    Attributes:
        name: Net name.
        pins: Sequence of (reference, pin_name) pairs in this net.
    """

    name: str
    pins: Sequence[tuple[str, str]]


@dataclass(frozen=True)
class DesignRuleSet:
    """Design rules for DRC checking.

    Attributes:
        min_clearance: Minimum copper-to-copper clearance in mm.
        min_hole_to_hole: Minimum hole-to-hole distance in mm.
        min_edge_clearance: Minimum component-to-board-edge clearance in mm.
    """

    min_clearance: float = 0.2
    min_hole_to_hole: float = 0.5
    min_edge_clearance: float = 0.3


@dataclass(frozen=True)
class BoardOutline:
    """Board boundary definition.

    Attributes:
        min_x: Left edge in mm.
        min_y: Top edge in mm.
        max_x: Right edge in mm.
        max_y: Bottom edge in mm.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


def compute_wirelength(
    placements: Sequence[ComponentPlacement],
    nets: Sequence[Net],
) -> float:
    """Compute total half-perimeter wirelength (HPWL) estimate.

    For each net, computes the half-perimeter of the bounding box of all
    connected component positions. This is the standard HPWL estimator
    used in placement optimization.

    Args:
        placements: Current component positions.
        nets: Net connectivity information.

    Returns:
        Total HPWL across all nets (mm).
    """
    if not nets:
        return 0.0

    pos_map: dict[str, tuple[float, float]] = {
        p.reference: (p.x, p.y) for p in placements
    }

    total = 0.0
    for net in nets:
        xs: list[float] = []
        ys: list[float] = []
        for ref, _ in net.pins:
            if ref in pos_map:
                x, y = pos_map[ref]
                xs.append(x)
                ys.append(y)
        if len(xs) >= 2:
            total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def compute_overlap(
    placements: Sequence[ComponentPlacement],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute total pairwise overlap area between component bounding boxes.

    Args:
        placements: Current component positions.
        footprint_sizes: Map from reference to (width, height) in mm.
            If None, uses a default 1x1mm bounding box per component.

    Returns:
        Sum of pairwise overlap areas (mm^2). Zero means no overlaps.
    """
    default_size = (1.0, 1.0)

    boxes: list[tuple[float, float, float, float]] = []
    for p in placements:
        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        half_w, half_h = w / 2, h / 2
        boxes.append((p.x - half_w, p.y - half_h, p.x + half_w, p.y + half_h))

    total_overlap = 0.0
    n = len(boxes)
    for i in range(n):
        for j in range(i + 1, n):
            x_overlap = max(
                0.0, min(boxes[i][2], boxes[j][2]) - max(boxes[i][0], boxes[j][0])
            )
            y_overlap = max(
                0.0, min(boxes[i][3], boxes[j][3]) - max(boxes[i][1], boxes[j][1])
            )
            total_overlap += x_overlap * y_overlap
    return total_overlap


def compute_boundary_violation(
    placements: Sequence[ComponentPlacement],
    board: BoardOutline,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute total boundary violation depth.

    For each component that extends beyond the board outline, sums the
    depth of violation on each edge.

    Args:
        placements: Current component positions.
        board: Board outline.
        footprint_sizes: Map from reference to (width, height) in mm.
            If None, uses a default 1x1mm bounding box per component.

    Returns:
        Sum of boundary violation depths across all components (mm).
        Zero means all components are within bounds.
    """
    default_size = (1.0, 1.0)
    total = 0.0

    for p in placements:
        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        half_w, half_h = w / 2, h / 2

        left = p.x - half_w
        right = p.x + half_w
        top = p.y - half_h
        bottom = p.y + half_h

        # Violation on each edge (positive means outside board)
        total += max(0.0, board.min_x - left)
        total += max(0.0, right - board.max_x)
        total += max(0.0, board.min_y - top)
        total += max(0.0, bottom - board.max_y)

    return total


def compute_drc_violations(
    placements: Sequence[ComponentPlacement],
    rules: DesignRuleSet,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute count of DRC clearance violations.

    Checks pairwise clearance between component bounding boxes against
    the minimum clearance rule.

    Args:
        placements: Current component positions.
        rules: Design rules with clearance constraints.
        footprint_sizes: Map from reference to (width, height) in mm.
            If None, uses a default 1x1mm bounding box per component.

    Returns:
        Number of pairwise clearance violations (float for consistency
        with other cost components).
    """
    default_size = (1.0, 1.0)
    min_gap = rules.min_clearance

    boxes: list[tuple[float, float, float, float]] = []
    for p in placements:
        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        half_w, half_h = w / 2, h / 2
        boxes.append((p.x - half_w, p.y - half_h, p.x + half_w, p.y + half_h))

    violations = 0.0
    n = len(boxes)
    for i in range(n):
        for j in range(i + 1, n):
            # Compute edge-to-edge gap (negative means overlap)
            gap_x = max(boxes[i][0], boxes[j][0]) - min(boxes[i][2], boxes[j][2])
            gap_y = max(boxes[i][1], boxes[j][1]) - min(boxes[i][3], boxes[j][3])

            # If boxes overlap on both axes, gap is negative on both
            # If separated, the gap is the distance on the separating axis
            if gap_x <= 0 and gap_y <= 0:
                # Overlapping — clearance is 0 (definitely a violation)
                gap = 0.0
            elif gap_x > 0 and gap_y > 0:
                # Separated on both axes — corner-to-corner distance
                gap = (gap_x**2 + gap_y**2) ** 0.5
            else:
                # Separated on one axis — edge-to-edge distance
                gap = max(gap_x, gap_y)

            if gap < min_gap:
                violations += 1.0
    return violations


def compute_area(
    placements: Sequence[ComponentPlacement],
) -> float:
    """Compute bounding-box area enclosing all component centers.

    This is a simple proxy for placement compactness.

    Args:
        placements: Current component positions.

    Returns:
        Area of the bounding box of all component centers (mm^2).
    """
    if not placements:
        return 0.0

    xs = [p.x for p in placements]
    ys = [p.y for p in placements]

    width = max(xs) - min(xs)
    height = max(ys) - min(ys)

    return width * height


def evaluate_placement(
    placements: Sequence[ComponentPlacement],
    nets: Sequence[Net],
    rules: DesignRuleSet,
    board: BoardOutline,
    config: PlacementCostConfig | None = None,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> PlacementScore:
    """Evaluate a placement configuration and return a composite score.

    This is a pure function with no side effects, safe to call from
    multiple threads for parallel evaluation.

    Args:
        placements: Current component positions.
        nets: Net connectivity information for wirelength estimation.
        rules: Design rules for DRC checking.
        board: Board outline for boundary checking.
        config: Cost function configuration. Uses defaults if None.
        footprint_sizes: Optional map from reference to (width, height) in mm.
            Used by overlap, boundary, and DRC sub-functions.

    Returns:
        PlacementScore with total score, per-component breakdown, and
        feasibility flag.
    """
    if config is None:
        config = PlacementCostConfig()

    # Compute individual cost components
    wirelength = compute_wirelength(placements, nets)
    overlap = compute_overlap(placements, footprint_sizes)
    boundary = compute_boundary_violation(placements, board, footprint_sizes)
    drc = compute_drc_violations(placements, rules, footprint_sizes)
    area = compute_area(placements)

    breakdown = CostBreakdown(
        wirelength=wirelength,
        overlap=overlap,
        boundary=boundary,
        drc=drc,
        area=area,
    )

    is_feasible = overlap == 0.0 and drc == 0.0 and boundary == 0.0

    if config.mode == CostMode.LEXICOGRAPHIC:
        total = _lexicographic_score(breakdown, config, is_feasible)
    else:
        total = _weighted_sum_score(breakdown, config)

    return PlacementScore(
        total=total,
        breakdown=breakdown,
        is_feasible=is_feasible,
    )


def _weighted_sum_score(breakdown: CostBreakdown, config: PlacementCostConfig) -> float:
    """Compute weighted sum of all cost components."""
    return (
        config.overlap_weight * breakdown.overlap
        + config.drc_weight * breakdown.drc
        + config.boundary_weight * breakdown.boundary
        + config.wirelength_weight * breakdown.wirelength
        + config.area_weight * breakdown.area
    )


def _lexicographic_score(
    breakdown: CostBreakdown,
    config: PlacementCostConfig,
    is_feasible: bool,
) -> float:
    """Compute lexicographic score.

    Infeasible placements get a large penalty base ensuring they always
    score worse than any feasible placement. The penalty is the weighted
    sum of the infeasibility components (overlap, DRC, boundary) plus a
    large constant offset.

    Feasible placements are scored by the weighted sum of wirelength and
    area only.
    """
    if not is_feasible:
        # Large offset ensures any infeasible score > any feasible score.
        # The infeasibility components are added so the optimizer can still
        # differentiate between "slightly infeasible" and "very infeasible".
        infeasibility_offset = 1e12
        return infeasibility_offset + (
            config.overlap_weight * breakdown.overlap
            + config.drc_weight * breakdown.drc
            + config.boundary_weight * breakdown.boundary
        )
    else:
        return (
            config.wirelength_weight * breakdown.wirelength
            + config.area_weight * breakdown.area
        )
