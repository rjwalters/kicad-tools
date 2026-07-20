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

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class CostMode(Enum):
    """Scoring mode for the composite cost function."""

    WEIGHTED_SUM = "weighted_sum"
    LEXICOGRAPHIC = "lexicographic"


# Score offset added to any infeasible placement when running in
# :class:`CostMode.LEXICOGRAPHIC` mode. Used as a feasibility sentinel:
# any total score below this value indicates a feasible placement; any
# total score >= this value indicates an infeasible placement.
#
# Consumers like the CMA-ES convergence check can use this to decide
# whether the optimizer is still in the infeasible region.
INFEASIBILITY_OFFSET: float = 1e12


@dataclass(frozen=True)
class PlacementCostConfig:
    """Configuration for the composite cost function.

    Attributes:
        overlap_weight: Weight for component overlap penalty.
        drc_weight: Weight for DRC violation penalty.
        boundary_weight: Weight for board boundary violation penalty.
        wirelength_weight: Weight for total wirelength cost.
        area_weight: Weight for bounding-box area cost.
        block_boundary_weight: Weight for block boundary violation penalty.
        inter_block_spacing: Minimum spacing between block bounding boxes (mm).
        creepage_weight: Weight for the HV creepage-keepout penalty. Applied to
            :func:`compute_creepage_violation`. Like ``overlap``/``drc``/
            ``boundary`` it is treated as a hard-feasibility term: a non-zero
            creepage shortfall makes the placement infeasible.
        mode: Scoring mode (weighted_sum or lexicographic).
    """

    overlap_weight: float = 1e6
    drc_weight: float = 1e4
    boundary_weight: float = 1e5
    wirelength_weight: float = 1.0
    area_weight: float = 0.1
    block_boundary_weight: float = 1e5
    inter_block_spacing: float = 1.0
    creepage_weight: float = 1e5
    mode: CostMode = CostMode.WEIGHTED_SUM


@dataclass(frozen=True)
class BlockRegion:
    """Axis-aligned region assigned to a block.

    Attributes:
        block_id: Identifier matching a :class:`BlockGroupDef`.
        min_x: Left edge in mm.
        min_y: Top edge in mm.
        max_x: Right edge in mm.
        max_y: Bottom edge in mm.
    """

    block_id: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(frozen=True)
class CostBreakdown:
    """Per-component cost breakdown for debugging.

    Attributes:
        wirelength: Raw wirelength estimate (mm).
        overlap: Raw overlap penalty (sum of overlap areas, mm^2).
        boundary: Raw boundary violation penalty (sum of violation depths, mm).
        drc: Raw DRC violation count.
        area: Raw bounding-box area of all placements (mm^2).
        block_boundary: Raw block boundary violation penalty (mm).
        inter_block: Raw inter-block spacing violation penalty (mm).
        creepage: Raw HV creepage-keepout shortfall penalty (mm) -- the sum of
            required-minus-actual gaps across cross-domain footprint pairs that
            are closer than their required creepage.
    """

    wirelength: float = 0.0
    overlap: float = 0.0
    boundary: float = 0.0
    drc: float = 0.0
    area: float = 0.0
    block_boundary: float = 0.0
    inter_block: float = 0.0
    creepage: float = 0.0


@dataclass(frozen=True)
class PlacementScore:
    """Result of evaluating a placement configuration.

    Attributes:
        total: Scalar score (lower is better).
        breakdown: Per-component raw costs before weighting.
        is_feasible: True if overlap=0, drc=0, boundary=0, block_boundary=0,
            and creepage=0 (HV keepout satisfied).
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
        weight: Per-net multiplier for wirelength cost contributions.
            Defaults to 1.0 (uniform weighting). Values >1.0 prioritise
            keeping this net short (useful when one or more pins are
            anchored to a fixed perimeter footprint and the optimizer
            could otherwise stretch the channel). A weight of 0.0
            removes the net from the wirelength sum entirely.
    """

    name: str
    pins: Sequence[tuple[str, str]]
    weight: float = 1.0


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

    Each net's HPWL contribution is multiplied by ``net.weight`` (default
    ``1.0``). Setting ``weight > 1.0`` prioritises keeping that net short
    (used by the anchor-aware path in ``optimize-placement``); setting
    ``weight = 0.0`` excludes the net from the wirelength sum entirely.

    Args:
        placements: Current component positions.
        nets: Net connectivity information.

    Returns:
        Total weighted HPWL across all nets (mm).
    """
    if not nets:
        return 0.0

    pos_map: dict[str, tuple[float, float]] = {p.reference: (p.x, p.y) for p in placements}

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
            total += net.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
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
            x_overlap = max(0.0, min(boxes[i][2], boxes[j][2]) - max(boxes[i][0], boxes[j][0]))
            y_overlap = max(0.0, min(boxes[i][3], boxes[j][3]) - max(boxes[i][1], boxes[j][1]))
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


def compute_block_boundary_violation(
    placements: Sequence[ComponentPlacement],
    block_regions: Sequence[BlockRegion],
    block_membership: dict[str, str],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute total boundary violation for block member components.

    For each component that belongs to a block, penalizes any portion of
    its bounding box that extends outside the assigned block region. Uses
    the same depth-based penalty approach as :func:`compute_boundary_violation`.

    Args:
        placements: Current component positions.
        block_regions: Block boundary regions.
        block_membership: Map from component reference to block_id.
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Sum of boundary violation depths across all block members (mm).
        Zero means all block members are within their assigned regions.
    """
    if not block_regions or not block_membership:
        return 0.0

    default_size = (1.0, 1.0)
    region_map = {r.block_id: r for r in block_regions}
    total = 0.0

    for p in placements:
        bid = block_membership.get(p.reference)
        if bid is None:
            continue
        region = region_map.get(bid)
        if region is None:
            continue

        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        half_w, half_h = w / 2, h / 2

        left = p.x - half_w
        right = p.x + half_w
        top = p.y - half_h
        bottom = p.y + half_h

        total += max(0.0, region.min_x - left)
        total += max(0.0, right - region.max_x)
        total += max(0.0, region.min_y - top)
        total += max(0.0, bottom - region.max_y)

    return total


def compute_inter_block_spacing_violation(
    placements: Sequence[ComponentPlacement],
    block_membership: dict[str, str],
    min_spacing: float,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute penalty for blocks placed closer than minimum spacing.

    Computes the bounding box of each block from its member positions, then
    checks pairwise edge-to-edge distance. If any pair is closer than
    *min_spacing*, the shortfall is added to the penalty.

    Args:
        placements: Current component positions.
        block_membership: Map from component reference to block_id.
        min_spacing: Minimum spacing between block bounding boxes (mm).
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Sum of spacing shortfalls across all block pairs (mm).
        Zero means all blocks are sufficiently spaced.
    """
    if not block_membership or min_spacing <= 0:
        return 0.0

    default_size = (1.0, 1.0)

    # Compute bounding box per block
    block_boxes: dict[str, list[float]] = {}  # block_id -> [min_x, min_y, max_x, max_y]
    for p in placements:
        bid = block_membership.get(p.reference)
        if bid is None:
            continue
        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        half_w, half_h = w / 2, h / 2
        left = p.x - half_w
        right = p.x + half_w
        top = p.y - half_h
        bottom = p.y + half_h

        if bid not in block_boxes:
            block_boxes[bid] = [left, top, right, bottom]
        else:
            bb = block_boxes[bid]
            bb[0] = min(bb[0], left)
            bb[1] = min(bb[1], top)
            bb[2] = max(bb[2], right)
            bb[3] = max(bb[3], bottom)

    # Pairwise spacing check
    block_ids = list(block_boxes.keys())
    total = 0.0
    for i in range(len(block_ids)):
        for j in range(i + 1, len(block_ids)):
            a = block_boxes[block_ids[i]]
            b = block_boxes[block_ids[j]]
            # Edge-to-edge gap on each axis
            gap_x = max(a[0], b[0]) - min(a[2], b[2])
            gap_y = max(a[1], b[1]) - min(a[3], b[3])

            if gap_x <= 0 and gap_y <= 0:
                # Overlapping -- gap is 0
                gap = 0.0
            elif gap_x > 0 and gap_y > 0:
                gap = (gap_x**2 + gap_y**2) ** 0.5
            else:
                gap = max(gap_x, gap_y)

            if gap < min_spacing:
                total += min_spacing - gap

    return total


def _domain_pair_key(a: str, b: str) -> tuple[str, str]:
    """Return an order-independent key for a pair of domain ids."""
    return (a, b) if a <= b else (b, a)


def compute_creepage_violation(
    placements: Sequence[ComponentPlacement],
    ref_domains: dict[str, str],
    required_mm_by_domain_pair: dict[tuple[str, str], float],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
    exempt_pairs: set[frozenset[str]] | None = None,
) -> float:
    """Compute the HV creepage-keepout shortfall between cross-domain footprints.

    This is the voltage-aware placement term (issue #4373). Each component is
    assigned an HV *domain* (e.g. ``"mains"``, ``"signal"``) via *ref_domains*.
    For every pair of footprints that belong to **different** domains, the
    edge-to-edge gap between their bounding boxes is compared against the
    required creepage for that domain pair (looked up in
    *required_mm_by_domain_pair*, keyed by the order-independent
    ``(min, max)`` domain-id tuple). When the gap is short of the requirement,
    the shortfall ``required - gap`` is accumulated.

    Same-domain pairs, pairs whose domain combination has no tabulated
    requirement, and pairs listed in *exempt_pairs* (guarded sense taps that are
    intentionally close to their parent HV net, issue #4373 Phase 3) contribute
    zero. Components absent from *ref_domains* are treated as domain-less and
    skipped.

    The required distances are supplied by the caller -- typically derived from
    ``kicad_tools.creepage.standards`` at the cross-domain ``|ΔV|`` -- so this
    function stays a pure numeric penalty with no standards dependency.

    Args:
        placements: Current component positions.
        ref_domains: Map from component reference to its HV domain id.
        required_mm_by_domain_pair: Map from an order-independent
            ``(domain_a, domain_b)`` tuple to the required creepage in mm.
        footprint_sizes: Map from reference to (width, height) in mm.
        exempt_pairs: Optional set of ``frozenset({ref_a, ref_b})`` pairs to
            exclude from the keepout (guarded sense taps).

    Returns:
        Sum of creepage shortfalls (mm) across all constrained cross-domain
        footprint pairs. Zero means every cross-domain pair meets its required
        creepage.
    """
    if not ref_domains or not required_mm_by_domain_pair:
        return 0.0

    default_size = (1.0, 1.0)
    exempt = exempt_pairs or set()

    boxes: list[tuple[str, str, float, float, float, float]] = []
    for p in placements:
        domain = ref_domains.get(p.reference)
        if domain is None:
            continue
        w, h = (footprint_sizes or {}).get(p.reference, default_size)
        half_w, half_h = w / 2, h / 2
        boxes.append(
            (
                p.reference,
                domain,
                p.x - half_w,
                p.y - half_h,
                p.x + half_w,
                p.y + half_h,
            )
        )

    total = 0.0
    n = len(boxes)
    for i in range(n):
        ref_i, dom_i, imin_x, imin_y, imax_x, imax_y = boxes[i]
        for j in range(i + 1, n):
            ref_j, dom_j, jmin_x, jmin_y, jmax_x, jmax_y = boxes[j]
            if dom_i == dom_j:
                continue
            required = required_mm_by_domain_pair.get(_domain_pair_key(dom_i, dom_j))
            if required is None or required <= 0.0:
                continue
            if frozenset((ref_i, ref_j)) in exempt:
                continue

            gap_x = max(imin_x, jmin_x) - min(imax_x, jmax_x)
            gap_y = max(imin_y, jmin_y) - min(imax_y, jmax_y)
            if gap_x <= 0 and gap_y <= 0:
                gap = 0.0
            elif gap_x > 0 and gap_y > 0:
                gap = (gap_x**2 + gap_y**2) ** 0.5
            else:
                gap = max(gap_x, gap_y)

            if gap < required:
                total += required - gap

    return total


def evaluate_placement(
    placements: Sequence[ComponentPlacement],
    nets: Sequence[Net],
    rules: DesignRuleSet,
    board: BoardOutline,
    config: PlacementCostConfig | None = None,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
    block_regions: Sequence[BlockRegion] | None = None,
    block_membership: dict[str, str] | None = None,
    ref_domains: dict[str, str] | None = None,
    required_mm_by_domain_pair: dict[tuple[str, str], float] | None = None,
    exempt_pairs: set[frozenset[str]] | None = None,
) -> PlacementScore:
    """Evaluate a placement configuration and return a composite score.

    This is a pure function with no side effects, safe to call from
    multiple threads for parallel evaluation.

    .. note::
        This is the **optimizer objective**. Overlap is measured as raw
        axis-aligned bounding-box overlap *area* (mm^2, see
        :func:`compute_overlap`) and DRC as a bbox-clearance *count* (see
        :func:`compute_drc_violations`) -- with **no courtyard margin** and
        **no KiCad-layer awareness**. This intentionally differs from
        ``kct placement check``
        (:class:`kicad_tools.placement.analyzer.PlacementAnalyzer`), which
        expands each footprint by a ``courtyard_margin`` and reports real
        KiCad DRC violations. The two metrics serve different audiences
        (optimizer search vs. user-facing diagnostics) and can disagree by
        the courtyard margin for touching footprints. See
        ``docs/placement-scoring.md`` for the full comparison (issue #3940).

    Args:
        placements: Current component positions.
        nets: Net connectivity information for wirelength estimation.
        rules: Design rules for DRC checking.
        board: Board outline for boundary checking.
        config: Cost function configuration. Uses defaults if None.
        footprint_sizes: Optional map from reference to (width, height) in mm.
            Used by overlap, boundary, and DRC sub-functions.
        block_regions: Optional block boundary regions for block-aware scoring.
        block_membership: Optional map from component reference to block_id.
        ref_domains: Optional map from component reference to its HV domain id.
            Enables the voltage-aware creepage-keepout term (issue #4373).
        required_mm_by_domain_pair: Optional map from an order-independent
            ``(domain_a, domain_b)`` tuple to the required creepage in mm.
            Required alongside *ref_domains* for the creepage term to fire.
        exempt_pairs: Optional set of ``frozenset({ref_a, ref_b})`` pairs
            exempted from the creepage keepout (guarded sense taps).

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

    # Block-aware cost components
    block_boundary = 0.0
    inter_block = 0.0
    if block_regions and block_membership:
        block_boundary = compute_block_boundary_violation(
            placements, block_regions, block_membership, footprint_sizes
        )
        inter_block = compute_inter_block_spacing_violation(
            placements, block_membership, config.inter_block_spacing, footprint_sizes
        )

    # HV creepage-keepout term (issue #4373). Only fires when both a domain
    # map and a required-distance table are supplied -- keeps the default
    # (voltage-blind) objective byte-identical.
    creepage = 0.0
    if ref_domains and required_mm_by_domain_pair:
        creepage = compute_creepage_violation(
            placements,
            ref_domains,
            required_mm_by_domain_pair,
            footprint_sizes,
            exempt_pairs,
        )

    breakdown = CostBreakdown(
        wirelength=wirelength,
        overlap=overlap,
        boundary=boundary,
        drc=drc,
        area=area,
        block_boundary=block_boundary,
        inter_block=inter_block,
        creepage=creepage,
    )

    is_feasible = (
        overlap == 0.0
        and drc == 0.0
        and boundary == 0.0
        and block_boundary == 0.0
        and creepage == 0.0
    )

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
        + config.block_boundary_weight * breakdown.block_boundary
        + config.block_boundary_weight * breakdown.inter_block
        + config.creepage_weight * breakdown.creepage
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
        return INFEASIBILITY_OFFSET + (
            config.overlap_weight * breakdown.overlap
            + config.drc_weight * breakdown.drc
            + config.boundary_weight * breakdown.boundary
            + config.creepage_weight * breakdown.creepage
        )
    else:
        return config.wirelength_weight * breakdown.wirelength + config.area_weight * breakdown.area
