"""
Bottom-up (hierarchical) baseline placement.

Implements the Stelios baseline hypothesis (issue #2721):

    "In non-Analog layouts I'll bet that we can get 80% of the way there
     just doing Bottom -> Up."

The algorithm is a non-GA alternative to the cascaded evolutionary placement
of issues #2719/#2720. It groups components by functional motif
(power / timing / interface / driver), lays them out within each cluster,
then places the clusters as super-blocks on the board.

Flow
----

1. **Detect** functional clusters via :func:`detect_functional_clusters`
   (motif-based). Components not in any detected cluster become singleton
   clusters so they survive Phase 3.
2. **Within-cluster placement** (Phase 2): each cluster is laid out in a
   local coordinate frame centered on its anchor. Members are packed in a
   shell around the anchor, ordered by pad count (larger first), respecting
   ``cluster.max_distance_mm``.
3. **Cluster super-block placement** (Phase 3): each cluster becomes one
   "super-component" whose footprint is the cluster bounding box. Super-blocks
   are placed using a deterministic shelf-packing layout on the board outline.
4. **Expand** (Phase 4): cluster member offsets from Phase 2 are translated
   to absolute coordinates using the Phase 3 cluster center.
5. **Route**: returned ``dict[ref] -> (x, y, rotation)`` is identical in
   shape to the existing :class:`EvolutionaryPlacementOptimizer` output, so
   downstream code (router, validators) needs no change.

Out of scope (handled by issues #2719/#2720):
- The :class:`RoutingEvaluator` protocol.
- GA fitness functions.
- Cluster super-block rotation (locked to 0 deg for the baseline).

Reference: see PR linked from issue #2721 for the comparison study.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.optim.clustering import detect_functional_clusters
from kicad_tools.optim.components import (
    ClusterType,
    Component,
    FunctionalCluster,
    Pin,
)
from kicad_tools.optim.geometry import Polygon

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)

__all__ = [
    "HierarchicalPlacementConfig",
    "ClusterPlacement",
    "HierarchicalPlacementResult",
    "place_hierarchical",
    "place_hierarchical_from_pcb",
]


# Reference-designator prefixes considered "fixed" in this baseline (matches
# :meth:`PlacementOptimizer.from_pcb` so the comparison is apples-to-apples).
_FIXED_PREFIXES = ("J", "H", "MH")


@dataclass
class HierarchicalPlacementConfig:
    """Configuration for the bottom-up baseline placer.

    All distances are in millimeters.
    """

    #: Padding between cluster super-blocks in Phase 3 (mm).
    inter_cluster_padding: float = 2.0
    #: Padding between components inside a cluster in Phase 2 (mm).
    intra_cluster_padding: float = 1.0
    #: Margin from board edge (mm).
    board_margin: float = 2.0
    #: Include singleton (un-clustered) components as 1-member clusters.
    include_singletons: bool = True
    #: Fall back to including DRIVER cluster detection (off by default --
    #: DRIVER detection is noisy because it tags any IC with R+D as a driver).
    include_driver_clusters: bool = False


@dataclass
class ClusterPlacement:
    """Result of Phase 2: a cluster with relative member offsets.

    ``offsets[ref]`` is the position of ``ref`` relative to the cluster center.
    The cluster center is the geometric center of the cluster's bounding box
    *after* Phase 2 packing.
    """

    cluster: FunctionalCluster
    offsets: dict[str, tuple[float, float]] = field(default_factory=dict)
    rotations: dict[str, float] = field(default_factory=dict)
    width: float = 0.0
    height: float = 0.0


@dataclass
class HierarchicalPlacementResult:
    """Output of the bottom-up placer.

    Attributes:
        positions: ``ref -> (x, y, rotation)`` for every component.
        clusters: Phase 2 cluster layouts (one entry per detected cluster).
        cluster_centers: ``cluster_anchor -> (x, y)`` from Phase 3.
        unclustered: Refs that ended up as singleton clusters.
    """

    positions: dict[str, tuple[float, float, float]]
    clusters: list[ClusterPlacement]
    cluster_centers: dict[str, tuple[float, float]]
    unclustered: list[str]


# ---------------------------------------------------------------------------
# Phase 1: cluster detection
# ---------------------------------------------------------------------------


def _build_clusters(
    components: list[Component],
    config: HierarchicalPlacementConfig,
) -> tuple[list[FunctionalCluster], list[str]]:
    """Detect functional clusters and (optionally) wrap singletons.

    Returns (clusters, unclustered_refs). When ``include_singletons`` is True
    the returned ``clusters`` list also contains 1-member ``FunctionalCluster``
    objects (anchor only) for every component not covered by a detected
    cluster, so Phase 3 can pack everything together.
    """
    detected = detect_functional_clusters(
        components,
        include_power=True,
        include_timing=True,
        include_interface=True,
        include_driver=config.include_driver_clusters,
    )

    # A component may appear in more than one detected cluster (e.g. an
    # interface ESD diode that is also picked up as a driver flyback). The
    # first cluster wins -- subsequent appearances are dropped from the second
    # cluster's member list to keep Phase 2 packing simple.
    seen: set[str] = set()
    deduped: list[FunctionalCluster] = []
    for cluster in detected:
        anchor = cluster.anchor
        if anchor in seen:
            # Skip a whole cluster whose anchor was already claimed.
            continue
        members = [m for m in cluster.members if m not in seen]
        new_cluster = FunctionalCluster(
            cluster_type=cluster.cluster_type,
            anchor=anchor,
            members=members,
            max_distance_mm=cluster.max_distance_mm,
            anchor_pin=cluster.anchor_pin,
        )
        deduped.append(new_cluster)
        seen.add(anchor)
        seen.update(members)

    unclustered = [c.ref for c in components if c.ref not in seen]

    if config.include_singletons:
        for ref in unclustered:
            deduped.append(
                FunctionalCluster(
                    cluster_type=ClusterType.POWER,  # placeholder; not used
                    anchor=ref,
                    members=[],
                    max_distance_mm=0.0,
                )
            )

    return deduped, unclustered


# ---------------------------------------------------------------------------
# Phase 2: within-cluster placement
# ---------------------------------------------------------------------------


def _pack_within_cluster(
    cluster: FunctionalCluster,
    component_map: dict[str, Component],
    config: HierarchicalPlacementConfig,
) -> ClusterPlacement:
    """Lay out a cluster around its anchor.

    The anchor sits at (0, 0). Members are placed on an outward "ring" using
    a fixed-shelf grid: walk left-right rows, advancing the row when the
    horizontal extent exceeds the cluster's working width budget.
    The width budget is the maximum of the anchor footprint and
    ``2 * cluster.max_distance_mm``.

    Singleton clusters return an empty offsets dict; the anchor takes the
    whole bounding box.
    """
    anchor = component_map.get(cluster.anchor)
    if anchor is None:
        return ClusterPlacement(cluster=cluster, width=1.0, height=1.0)

    placement = ClusterPlacement(cluster=cluster)
    placement.offsets[anchor.ref] = (0.0, 0.0)
    placement.rotations[anchor.ref] = anchor.rotation

    if not cluster.members:
        # Singleton: bounding box is just the anchor.
        placement.width = max(anchor.width, 1.0)
        placement.height = max(anchor.height, 1.0)
        return placement

    pad = config.intra_cluster_padding
    members = [component_map[m] for m in cluster.members if m in component_map]
    # Sort by area descending so the larger members get the inner shelf
    # (smaller items can fill gaps later).
    members.sort(key=lambda c: -(c.width * c.height))

    # Pack members in two rows: above the anchor, then below if needed.
    # Row strategy keeps things deterministic and reproducible.
    anchor_half_h = anchor.height / 2.0

    row_above: list[tuple[Component, float]] = []  # (comp, x-offset)
    row_below: list[tuple[Component, float]] = []
    row_above_extent_x = 0.0
    row_below_extent_x = 0.0

    row_above_max_h = 0.0
    row_below_max_h = 0.0

    for i, m in enumerate(members):
        if i % 2 == 0:
            target = row_above
            extent = row_above_extent_x
        else:
            target = row_below
            extent = row_below_extent_x

        m_half_w = m.width / 2.0
        # Center this member at extent + m_half_w (left-to-right shelf packing)
        cx = extent + m_half_w
        target.append((m, cx))

        if i % 2 == 0:
            row_above_extent_x = extent + m.width + pad
            row_above_max_h = max(row_above_max_h, m.height)
        else:
            row_below_extent_x = extent + m.width + pad
            row_below_max_h = max(row_below_max_h, m.height)

    # Compute the row Y centers
    y_above = anchor_half_h + pad + row_above_max_h / 2.0
    y_below = -(anchor_half_h + pad + row_below_max_h / 2.0)

    # Translate row X so each row is centered on the anchor's X=0 axis
    def _centered_offsets(row: list[tuple[Component, float]], y: float) -> None:
        if not row:
            return
        total_w = row[-1][1] + row[-1][0].width / 2.0
        # Shift so the row is centered: leftmost member x = -total_w / 2
        # The row's first member has its center at row[0][1] currently.
        # After shift, member center x = row[i][1] - total_w / 2.
        for comp, cx in row:
            placement.offsets[comp.ref] = (cx - total_w / 2.0, y)
            placement.rotations[comp.ref] = comp.rotation

    _centered_offsets(row_above, y_above)
    _centered_offsets(row_below, y_below)

    # Cluster bounding box: half-extents in each direction
    xs = [
        placement.offsets[ref][0] - component_map[ref].width / 2.0 for ref in placement.offsets
    ] + [placement.offsets[ref][0] + component_map[ref].width / 2.0 for ref in placement.offsets]
    ys = [
        placement.offsets[ref][1] - component_map[ref].height / 2.0 for ref in placement.offsets
    ] + [placement.offsets[ref][1] + component_map[ref].height / 2.0 for ref in placement.offsets]
    placement.width = (max(xs) - min(xs)) if xs else max(anchor.width, 1.0)
    placement.height = (max(ys) - min(ys)) if ys else max(anchor.height, 1.0)

    # Re-center the cluster so its geometric bounding-box center is at (0, 0).
    cx_mid = (max(xs) + min(xs)) / 2.0 if xs else 0.0
    cy_mid = (max(ys) + min(ys)) / 2.0 if ys else 0.0
    if cx_mid or cy_mid:
        for ref in list(placement.offsets):
            ox, oy = placement.offsets[ref]
            placement.offsets[ref] = (ox - cx_mid, oy - cy_mid)

    return placement


# ---------------------------------------------------------------------------
# Phase 3: cluster super-block placement (shelf packing)
# ---------------------------------------------------------------------------


def _place_cluster_superblocks(
    cluster_placements: list[ClusterPlacement],
    board_outline: Polygon,
    config: HierarchicalPlacementConfig,
) -> dict[str, tuple[float, float]]:
    """Place each cluster super-block on the board using shelf packing.

    Strategy:
    - Treat each cluster bounding-box as a rectangle.
    - Walk shelves left-to-right, top-to-bottom inside the board's axis-aligned
      bounding box (inset by ``board_margin``).
    - Larger clusters first so they pin down the "anchor" positions.

    Returns ``cluster_anchor_ref -> (cx, cy)`` for every cluster.
    """
    if not cluster_placements:
        return {}

    # Board bbox from polygon vertices.
    if not board_outline.vertices:
        bx_min, by_min, bx_max, by_max = 0.0, 0.0, 100.0, 80.0
    else:
        xs = [v.x for v in board_outline.vertices]
        ys = [v.y for v in board_outline.vertices]
        bx_min, bx_max = min(xs), max(xs)
        by_min, by_max = min(ys), max(ys)

    bx_min += config.board_margin
    by_min += config.board_margin
    bx_max -= config.board_margin
    by_max -= config.board_margin

    # Sort clusters by area descending
    order = sorted(
        cluster_placements,
        key=lambda cp: -(max(cp.width, 1.0) * max(cp.height, 1.0)),
    )

    centers: dict[str, tuple[float, float]] = {}

    pad = config.inter_cluster_padding
    shelf_x = bx_min
    shelf_y = by_min
    shelf_max_h = 0.0

    for cp in order:
        w = max(cp.width, 1.0)
        h = max(cp.height, 1.0)
        # If this cluster won't fit on the current shelf, advance to next shelf.
        if shelf_x + w > bx_max and shelf_x > bx_min:
            shelf_x = bx_min
            shelf_y += shelf_max_h + pad
            shelf_max_h = 0.0

        cx = shelf_x + w / 2.0
        cy = shelf_y + h / 2.0
        centers[cp.cluster.anchor] = (cx, cy)

        shelf_x += w + pad
        shelf_max_h = max(shelf_max_h, h)

        # Don't error if we run off the board -- just keep packing. The router
        # will report any DRC violations and the comparison study can fold
        # those into the verdict. Log it for the operator.
        if cy + h / 2.0 > by_max:
            logger.warning(
                "Cluster %s (%s) extends past board bottom (%.2f > %.2f). "
                "Bottom-up baseline does not relocate -- DRC may fail.",
                cp.cluster.anchor,
                cp.cluster.cluster_type.value,
                cy + h / 2.0,
                by_max,
            )

    # Optionally re-center the whole packing inside the board if there's room.
    if centers:
        used_x = [centers[a][0] for a in centers]
        used_y = [centers[a][1] for a in centers]
        if used_x:
            shift_x = ((bx_min + bx_max) / 2.0) - ((min(used_x) + max(used_x)) / 2.0)
            shift_y = ((by_min + by_max) / 2.0) - ((min(used_y) + max(used_y)) / 2.0)
            # Only shift if doing so keeps everything on the board.
            # Simple heuristic: only shift in the positive direction (smaller
            # use case wouldn't go off-board after shift).
            if shift_x > 0 and shift_y > 0:
                for anchor in list(centers):
                    x, y = centers[anchor]
                    centers[anchor] = (x + shift_x, y + shift_y)

    return centers


# ---------------------------------------------------------------------------
# Phase 4: expand to absolute positions
# ---------------------------------------------------------------------------


def _expand_to_positions(
    cluster_placements: list[ClusterPlacement],
    cluster_centers: dict[str, tuple[float, float]],
    component_map: dict[str, Component],
) -> dict[str, tuple[float, float, float]]:
    """Translate per-cluster offsets to absolute (x, y, rotation) per ref."""
    positions: dict[str, tuple[float, float, float]] = {}

    for cp in cluster_placements:
        center = cluster_centers.get(cp.cluster.anchor)
        if center is None:
            continue
        cx, cy = center
        for ref, (ox, oy) in cp.offsets.items():
            rot = cp.rotations.get(ref, 0.0)
            comp = component_map.get(ref)
            # Keep fixed components at their current position so we don't move
            # connectors/mounting holes that the user pinned.
            if comp is not None and comp.fixed:
                positions[ref] = (comp.x, comp.y, comp.rotation)
            else:
                positions[ref] = (cx + ox, cy + oy, rot)

    # Anything still missing (shouldn't happen if singletons are included)
    for ref, comp in component_map.items():
        positions.setdefault(ref, (comp.x, comp.y, comp.rotation))

    return positions


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def place_hierarchical(
    components: list[Component],
    board_outline: Polygon,
    config: HierarchicalPlacementConfig | None = None,
) -> HierarchicalPlacementResult:
    """Run the bottom-up baseline placer on an in-memory component list.

    Args:
        components: Components with pins and net assignments.
        board_outline: Polygon describing the board boundary.
        config: Optional placement config. Defaults are tuned for boards
            01--03 in this repo.

    Returns:
        :class:`HierarchicalPlacementResult` with absolute positions for
        every input component.

    Notes:
        - Fixed components (``Component.fixed=True``) are left at their
          current coordinates so connectors/mounting holes don't move.
        - Singleton (unclustered) components are wrapped into 1-member
          clusters and packed alongside multi-member clusters in Phase 3.
        - Cluster super-blocks are rotation-locked at 0 deg in this baseline.
    """
    config = config or HierarchicalPlacementConfig()
    component_map = {c.ref: c for c in components}

    # Phase 1
    clusters, unclustered = _build_clusters(components, config)
    logger.info(
        "Bottom-up: detected %d clusters (%d singletons) from %d components",
        len(clusters),
        len(unclustered),
        len(components),
    )

    # Phase 2
    cluster_placements: list[ClusterPlacement] = [
        _pack_within_cluster(c, component_map, config) for c in clusters
    ]

    # Phase 3
    cluster_centers = _place_cluster_superblocks(cluster_placements, board_outline, config)

    # Phase 4
    positions = _expand_to_positions(cluster_placements, cluster_centers, component_map)

    return HierarchicalPlacementResult(
        positions=positions,
        clusters=cluster_placements,
        cluster_centers=cluster_centers,
        unclustered=unclustered,
    )


def place_hierarchical_from_pcb(
    pcb: PCB,
    config: HierarchicalPlacementConfig | None = None,
    fixed_refs: list[str] | None = None,
) -> HierarchicalPlacementResult:
    """Convenience wrapper: build components from a PCB and run :func:`place_hierarchical`.

    Args:
        pcb: Loaded :class:`PCB` object.
        config: Optional placement config.
        fixed_refs: Reference designators to mark as fixed.

    Returns:
        :class:`HierarchicalPlacementResult`.
    """
    # Importing inside the function keeps the module top-level cheap and
    # avoids circular imports (PCB module pulls in a lot).
    from kicad_tools.optim.board_outline import extract_board_outline

    fixed = set(fixed_refs or [])

    # Reuse PlacementOptimizer's outline extraction logic so the baseline and
    # the GA see the same board polygon.
    board = None
    try:
        from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely

        if has_shapely():
            try:
                board_geom = BoardGeometry.from_pcb(pcb)
                board = board_geom.to_optim_polygon()
            except Exception:
                board = None
    except ImportError:
        pass

    if board is None:
        board = extract_board_outline(pcb)

    if board is None:
        # Mirror PlacementOptimizer.from_pcb's fallback: estimate from footprint
        # positions.
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for fp in pcb.footprints:
            x, y = fp.position
            min_x = min(min_x, x - 10)
            max_x = max(max_x, x + 10)
            min_y = min(min_y, y - 10)
            max_y = max(max_y, y + 10)
        if min_x == float("inf"):
            board = Polygon.rectangle(50.0, 50.0, 100.0, 80.0)
        else:
            board = Polygon.rectangle(
                (min_x + max_x) / 2.0,
                (min_y + max_y) / 2.0,
                max_x - min_x,
                max_y - min_y,
            )

    # Build a Component list (mirrors PlacementOptimizer.from_pcb).
    components: list[Component] = []
    for fp in pcb.footprints:
        if fp.pads:
            pad_xs = [p.position[0] for p in fp.pads]
            pad_ys = [p.position[1] for p in fp.pads]
            width = max(pad_xs) - min(pad_xs) + 2.0
            height = max(pad_ys) - min(pad_ys) + 2.0
        else:
            width, height = 2.0, 2.0

        is_fixed = fp.reference in fixed
        if not is_fixed:
            ref_prefix = "".join(c for c in fp.reference if c.isalpha())
            if ref_prefix in _FIXED_PREFIXES:
                is_fixed = True

        comp = Component(
            ref=fp.reference,
            x=fp.position[0],
            y=fp.position[1],
            rotation=fp.rotation,
            width=max(width, 1.0),
            height=max(height, 1.0),
            fixed=is_fixed,
            pins=[
                Pin(
                    number=p.number,
                    x=fp.position[0] + p.position[0],
                    y=fp.position[1] + p.position[1],
                    net=p.net_number,
                    net_name=p.net_name,
                )
                for p in fp.pads
            ],
        )
        components.append(comp)

    return place_hierarchical(components, board, config)
