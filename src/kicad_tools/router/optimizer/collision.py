"""Collision checking for trace optimization."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ..geometry import point_to_segment_distance, segment_to_segment_distance
from ..layers import Layer
from ..primitives import Segment

if TYPE_CHECKING:
    from ..grid import RoutingGrid


class CollisionChecker(Protocol):
    """Protocol for checking if a path is clear of obstacles.

    Implementations can use different strategies:
    - Grid-based: Use RoutingGrid obstacle data
    - Segment intersection: Check for crossings with other nets
    - R-tree: Spatial indexing for efficient segment clearance queries
      (implemented in RoutingGrid via per-layer rtree.index.Index, Issue #1249)

    The collision checker should return True if the path is clear,
    False if it would cross obstacles or other nets.
    """

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path from (x1, y1) to (x2, y2) is clear of obstacles.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks (own net).

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        ...


class GridCollisionChecker:
    """Collision checker using the routing grid.

    Uses the RoutingGrid's obstacle data to check if paths are clear.
    This reuses the same collision detection logic as the autorouter.

    When ``ignore_overflow=True``, cells that are blocked by route
    occupation (another net passing through) but are *not* hard obstacles
    (pads, keepouts) are treated as clear.  This prevents the trace
    optimizer from fragmenting routes that pass through cells with minor
    overflow from negotiated routing.  Hard obstacles are always respected
    regardless of this flag.
    """

    def __init__(self, grid: RoutingGrid, ignore_overflow: bool = False):
        """Initialize with a routing grid.

        Args:
            grid: The routing grid with obstacle and net data.
            ignore_overflow: When True, treat cells blocked by route
                occupation (not hard obstacles) as clear.  This is used
                after negotiated routing with residual overflow so that
                the optimizer preserves connectivity instead of
                destroying segments that pass through overused cells.
        """
        self.grid = grid
        self.ignore_overflow = ignore_overflow

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using grid-based collision detection.

        Uses Bresenham's line algorithm to check all grid cells along the path,
        including a buffer for trace width and clearance.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Convert to grid coordinates
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        # Calculate clearance buffer in grid cells
        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Get layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False  # Invalid layer

        # Check all cells along the path using Bresenham's algorithm
        cells_to_check = self._get_path_cells(gx1, gy1, gx2, gy2, clearance_cells)

        for gx, gy in cells_to_check:
            if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                continue  # Out of bounds - skip but don't fail

            cell = self.grid.grid[layer_idx][gy][gx]

            # Check if blocked by another net
            if cell.blocked:
                # Issue #2963: own-net obstacle cells (destination pad
                # metal marked by PR #2928's first-touch) must remain
                # passable for the route's own net.  Foreign-net
                # obstacles still hard-reject.
                if cell.is_obstacle and cell.net != exclude_net:
                    return False  # Hard obstacle (pad, keepout) -- always block

                # Issue #2757: A pad on a skipped pour net (e.g. GND, +3V3)
                # has ``pad_blocked=True`` but ``is_obstacle=False`` and
                # ``cell.net=0`` because ``load_pcb_for_routing`` rewrites
                # skip-net pad nets to 0 (so they aren't routable) but still
                # registers the pad as a copper obstacle in the grid.  Before
                # this fix the optimizer's chamfer / collinear-merge passes
                # walked straight through those cells, producing post-route
                # ``clearance_pad_segment`` violations on every BGA/QFN edge
                # the new diagonal grazed (15 violations on board 06).
                # Treating pad-metal cells as hard obstacles -- except where
                # the cell already belongs to the optimised route's own net
                # (e.g. the route's destination pad) -- closes that hole
                # without affecting normal own-net pad anchoring.
                if cell.pad_blocked and cell.net != exclude_net:
                    return False

                # Cell is occupied by another net's route (soft block).
                # When ignore_overflow is set, skip this check so the
                # optimizer does not fragment routes through overused cells.
                #
                # Issue #3433: the tolerance is scoped to cells that are
                # GENUINELY overused (``usage_count > 1`` -- the same
                # predicate ``get_total_overflow`` uses).  The original
                # #2303 blanket skip made the checker blind to EVERY
                # foreign-net trace whenever the router finished with
                # ANY residual overflow: on board 04 (overflow=2 in an
                # unrelated OSC corridor) the staircase-compression pass
                # straightened SWO's B.Cu zigzag into a long diagonal
                # running straight across SWCLK's clearance-respecting
                # run, committing -0.200 mm full overlaps that no later
                # pass can repair.  The bug is environment-sensitive:
                # machines with the ``rtree`` package use
                # ``VectorCollisionChecker``, whose exact narrow phase
                # never honored ``ignore_overflow`` for foreign
                # segments -- only this grid fallback was blind.
                if cell.net != 0 and cell.net != exclude_net:
                    if not self.ignore_overflow:
                        return False  # Blocked by another net
                    if cell.usage_count <= 1:
                        return False  # Clean foreign trace -- never cross.

        return True

    def _get_path_cells(
        self, gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
    ) -> list[tuple[int, int]]:
        """Get all grid cells along a path with clearance buffer.

        Uses Bresenham's line algorithm with clearance expansion.

        Args:
            gx1, gy1: Start grid coordinates.
            gx2, gy2: End grid coordinates.
            clearance: Clearance buffer in grid cells.

        Returns:
            List of (gx, gy) grid coordinates to check.
        """
        cells: set[tuple[int, int]] = set()

        # Bresenham's line algorithm
        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy

        gx, gy = gx1, gy1
        while True:
            # Add cell and clearance buffer
            for cy in range(-clearance, clearance + 1):
                for cx in range(-clearance, clearance + 1):
                    cells.add((gx + cx, gy + cy))

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return list(cells)


class VectorCollisionChecker:
    """Collision checker using exact vector math and R-tree spatial indexing.

    Instead of discretizing the path onto a grid (O(L * W) cells), this
    checker performs broad-phase candidate filtering via the R-tree spatial
    index already maintained by ``RoutingGrid``, then applies exact
    segment-to-segment distance calculations for narrow-phase clearance
    checks.

    This is typically 10x+ faster than ``GridCollisionChecker`` for boards
    with many routed nets, because the R-tree query returns only nearby
    candidates and the analytical distance check is O(1) per candidate pair.

    Falls back to ``GridCollisionChecker`` when the R-tree is unavailable
    (e.g. rtree package not installed) or the segment count is below the
    R-tree activation threshold.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        ignore_overflow: bool = False,
    ):
        """Initialize with a routing grid that has R-tree spatial index data.

        Args:
            grid: The routing grid with R-tree index and obstacle data.
            ignore_overflow: When True, treat cells blocked by route
                occupation (not hard obstacles) as clear.
        """
        self.grid = grid
        self.ignore_overflow = ignore_overflow
        # Lazy-initialized fallback for when R-tree is unavailable on a layer
        self._grid_fallback: GridCollisionChecker | None = None

    def _get_grid_fallback(self) -> GridCollisionChecker:
        """Get or create the grid-based fallback checker."""
        if self._grid_fallback is None:
            self._grid_fallback = GridCollisionChecker(
                self.grid, ignore_overflow=self.ignore_overflow
            )
        return self._grid_fallback

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using R-tree + exact vector math.

        Broad phase: queries the per-layer R-tree for candidate segments
        whose bounding boxes overlap the query path's envelope (expanded
        by half-width + trace clearance).

        Narrow phase: computes exact segment-to-segment distance for each
        candidate and checks edge-to-edge clearance against the design
        rule minimum.

        Falls back to ``GridCollisionChecker`` when R-tree data is not
        available for the requested layer.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Resolve layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False

        # Check if R-tree is available and populated for this layer
        if not self.grid._rtree_available or layer_idx not in self.grid._seg_rtree:
            return self._get_grid_fallback().path_is_clear(
                x1, y1, x2, y2, layer, width, exclude_net
            )

        min_clearance = self.grid.rules.trace_clearance
        half_width = width / 2
        search_radius = half_width + min_clearance

        # Broad phase: query R-tree with expanded envelope
        query_envelope = (
            min(x1, x2) - search_radius,
            min(y1, y2) - search_radius,
            max(x1, x2) + search_radius,
            max(y1, y2) + search_radius,
        )
        candidate_ids = list(self.grid._seg_rtree[layer_idx].intersection(query_envelope))
        layer_items: dict[int, Any] = self.grid._seg_rtree_items.get(layer_idx, {})

        # Narrow phase: exact distance check for each candidate
        for cand_id in candidate_ids:
            other_seg: Segment | None = layer_items.get(cand_id)
            if other_seg is None:
                continue

            # Skip own-net segments
            if other_seg.net == exclude_net:
                continue

            # Exact center-to-center distance
            dist = segment_to_segment_distance(
                x1,
                y1,
                x2,
                y2,
                other_seg.x1,
                other_seg.y1,
                other_seg.x2,
                other_seg.y2,
            )

            # Edge-to-edge clearance
            clearance = dist - half_width - other_seg.width / 2

            if clearance < min_clearance:
                return False

        # Issue #2955 / #2960: Check against foreign-net vias.
        #
        # The segment R-tree above does not index vias, so without an
        # explicit via check the optimizer's ``compress_staircase`` /
        # ``convert_45_corners`` passes happily replace a
        # clearance-respecting zigzag with a diagonal that grazes or
        # punches a foreign-net through-hole via.  The canonical
        # board-03 failure was XTAL1's B.Cu trace rewritten to a single
        # off-grid segment running 0.14 mm from XTAL2's via at
        # (125.6, 128.3), producing ``clearance_segment_segment`` /
        # ``clearance_segment_via`` post-route DRC pairs.
        #
        # ``GridCollisionChecker`` is implicitly safe against this
        # because ``_mark_via`` paints ``cell.net = via.net`` on every
        # blocked cell in the via's clearance envelope, so the
        # Bresenham walk hits the via at the soft-block branch
        # (``cell.net != exclude_net``).  The vector path needs an
        # explicit check.
        #
        # PR #2958 (issue #2955) added a double-nested linear scan over
        # ``grid.routes × route.vias`` here, which the optimizer
        # invokes thousands of times per net.  On boards 06/07 this
        # produced a fleet-wide ~3x slowdown (issue #2960).
        #
        # Issue #2960 replaces that scan with an R-tree query against
        # the via index maintained by ``RoutingGrid`` (mirrored to
        # ``self.routes`` mutations in ``mark_route`` / ``unmark_route``).
        # The broad phase returns only vias whose AABB overlaps the
        # path's query envelope; the narrow phase keeps the existing
        # ``_via_on_layer`` + point-to-segment distance contract.
        #
        # Through-hole vias span ``layers[0]`` -> ``layers[1]`` inclusive
        # of everything in between (KiCad does not enumerate inner
        # layers in the S-expression).  ``validate_segment_clearance``
        # (grid.py) uses the same "check every via on every layer"
        # simplification -- it's conservative-safe (at most a handful
        # of false-positive rejections on multi-layer boards with
        # blind/buried vias, which kicad-tools does not currently
        # emit).
        via_rtree = getattr(self.grid, "_via_rtree", None)
        via_items: dict[int, Any] = getattr(self.grid, "_via_rtree_items", {})
        if via_rtree is not None and via_items:
            # Broad-phase query envelope: path AABB inflated by
            # half_width + min_clearance.  Each indexed via envelope is
            # already inflated by ``via_radius + max_clearance + max_trace_half_width``
            # (see ``RoutingGrid._compute_via_rtree_inflation``), so the
            # union of the two envelopes is a conservative superset of
            # the actual clearance check region for any via.
            query_envelope = (
                min(x1, x2) - search_radius,
                min(y1, y2) - search_radius,
                max(x1, x2) + search_radius,
                max(y1, y2) + search_radius,
            )
            for via_id in via_rtree.intersection(query_envelope):
                via = via_items.get(via_id)
                if via is None:
                    continue
                # Skip own-net vias (matches the pre-fix per-route filter).
                if via.net == exclude_net:
                    continue
                # Layer filter mirrors the linear-scan version.
                if not self._via_on_layer(via, layer_idx):
                    continue
                via_radius = via.diameter / 2
                dist = point_to_segment_distance(via.x, via.y, x1, y1, x2, y2)
                clearance = dist - half_width - via_radius
                if clearance < min_clearance:
                    return False
        else:
            # Fallback: index not built (e.g. mock grids in unit tests,
            # or rtree unavailable).  Use the original linear scan so
            # correctness from PR #2958 is preserved unconditionally.
            for route in self.grid.routes:
                if route.net == exclude_net:
                    continue
                for via in route.vias:
                    if not self._via_on_layer(via, layer_idx):
                        continue
                    via_radius = via.diameter / 2
                    dist = point_to_segment_distance(via.x, via.y, x1, y1, x2, y2)
                    clearance = dist - half_width - via_radius
                    if clearance < min_clearance:
                        return False

        # Also check hard obstacles (pads, keepouts) via the grid
        # The R-tree only indexes routed segments, not static obstacles,
        # so we use the grid's obstacle layer for pad/keepout checks.
        if not self._check_obstacles_clear(x1, y1, x2, y2, layer_idx, width, exclude_net):
            return False

        return True

    def _via_on_layer(self, via: Any, layer_idx: int) -> bool:
        """Return True if ``via`` blocks copper on ``layer_idx``.

        Through-hole vias (the common case in kicad-tools today) declare
        ``layers=(F.Cu, B.Cu)`` and physically block every layer in between
        as well.  Blind / buried vias declare a sub-range.  This helper maps
        the start / end layer enum values to grid layer indices and returns
        ``True`` iff ``layer_idx`` falls in the inclusive range.

        When the layer mapping cannot be resolved (unexpected Layer enum
        value, etc.) the helper returns ``True`` to preserve the conservative
        "assume blocking" behaviour of ``grid.validate_segment_clearance``
        which iterates every via without layer filtering.
        """
        try:
            start_idx = self.grid.layer_to_index(via.layers[0].value)
            end_idx = self.grid.layer_to_index(via.layers[1].value)
        except Exception:
            return True
        lo, hi = (start_idx, end_idx) if start_idx <= end_idx else (end_idx, start_idx)
        return lo <= layer_idx <= hi

    def _check_obstacles_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check that a path does not cross hard obstacles (pads, keepouts).

        Samples the path at grid resolution and checks each cell for hard
        obstacles.  This is lighter than a full Bresenham sweep because we
        only check obstacle status, not soft net occupation.

        Args:
            x1, y1: Start point coordinates (world).
            x2, y2: End point coordinates (world).
            layer_idx: Layer index.
            width: Trace width.
            exclude_net: Net ID to exclude.

        Returns:
            True if clear of hard obstacles.
        """
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Bresenham walk -- only check hard obstacles
        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy

        gx, gy = gx1, gy1
        while True:
            for cy in range(-clearance_cells, clearance_cells + 1):
                for cx in range(-clearance_cells, clearance_cells + 1):
                    check_x = gx + cx
                    check_y = gy + cy
                    if not (0 <= check_x < self.grid.cols and 0 <= check_y < self.grid.rows):
                        continue
                    cell = self.grid.grid[layer_idx][check_y][check_x]
                    if cell.blocked and (cell.is_obstacle or cell.pad_blocked):
                        # Hard obstacle (cross-net pad) OR pad-copper cell
                        # (Issue #2757: pads on skipped pour nets have
                        # pad_blocked=True but is_obstacle=False because
                        # their net was rewritten to 0 by
                        # load_pcb_for_routing; treat them as obstacles
                        # too so the optimizer doesn't chamfer through
                        # BGA GND / power pads).
                        if cell.net != 0 and cell.net == exclude_net:
                            continue  # Own-net pad is OK
                        if cell.pad_blocked and cell.net == exclude_net:
                            continue  # Own-net pad-metal cell (net match)
                        return False

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return True


def make_collision_checker(
    grid: RoutingGrid,
    ignore_overflow: bool = False,
) -> GridCollisionChecker | VectorCollisionChecker:
    """Select the best collision checker for the given grid.

    Returns a ``VectorCollisionChecker`` when the grid has an R-tree
    spatial index available and populated, otherwise falls back to
    ``GridCollisionChecker``.

    Args:
        grid: The routing grid with obstacle and net data.
        ignore_overflow: When True, treat cells blocked by route
            occupation (not hard obstacles) as clear.

    Returns:
        The most efficient collision checker for the given grid state.
    """
    if grid._rtree_available and grid._seg_rtree_count > 0:
        return VectorCollisionChecker(grid, ignore_overflow=ignore_overflow)
    return GridCollisionChecker(grid, ignore_overflow=ignore_overflow)
