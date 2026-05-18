"""
A* pathfinding for PCB routing (pure Python fallback).

NOTE: This is the pure Python implementation. A C++ backend is available
that provides 10-100x speedup. Build it with:

    kct build-native

The C++ backend is used automatically when available. This module serves
as the fallback when the C++ extension is not installed.

This module provides:
- AStarNode: Node for priority queue in A* search
- Router: A* pathfinder with multi-layer support and congestion awareness

The Router accepts a pluggable Heuristic for experimentation with
different routing strategies. See heuristics.py for available options.
"""

import heapq
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .geometry import segments_intersect as _geom_segments_intersect
from .grid import RoutingGrid
from .heuristics import DEFAULT_HEURISTIC, Heuristic, HeuristicContext
from .layers import Layer
from .primitives import Pad, Route, Segment, Via
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting
from .via_clearance import point_clear_of_copper, segment_clears_foreign_via


@dataclass(frozen=True)
class _SegmentAdapter:
    """Adapter exposing :class:`Segment` with the ``start_x/start_y/end_x/end_y``
    attribute names expected by :func:`point_clear_of_copper`.

    Issue #2947: The shared clearance helper consumes duck-typed track
    segments via the ``TrackSegmentLike`` protocol; :class:`Segment` uses
    ``x1, y1, x2, y2``.  Mirrors the identical adapter in
    :mod:`kicad_tools.router.escape` (PR #2945 for Issue #2944).
    """

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float


@dataclass(order=True)
class AStarNode:
    """Node for A* priority queue."""

    f_score: float
    g_score: float = field(compare=False)
    x: int = field(compare=False)
    y: int = field(compare=False)
    layer: int = field(compare=False)
    parent: Optional["AStarNode"] = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)
    direction: tuple[int, int] = field(compare=False, default=(0, 0))  # (dx, dy) from parent


class Router:
    """A* pathfinder with multi-layer support and congestion awareness.

    The heuristic parameter allows experimentation with different routing
    strategies. Available heuristics include:
    - ManhattanHeuristic: Simple baseline (fast, may explore more nodes)
    - DirectionBiasHeuristic: Prefers straight paths
    - CongestionAwareHeuristic: Avoids congested areas (default)
    - WeightedCongestionHeuristic: Stronger congestion avoidance
    - GreedyHeuristic: Fast but suboptimal
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        net_class_map: dict[str, NetClassRouting] | None = None,
        heuristic: Heuristic | None = None,
        diagonal_routing: bool = True,
    ):
        """
        Args:
            grid: The routing grid
            rules: Design rules for routing
            net_class_map: Mapping of net names to NetClassRouting
            heuristic: Heuristic for A* search (default: CongestionAwareHeuristic)
            diagonal_routing: Enable 45° diagonal routing (default: True).
                              When True, routes can use diagonal moves for shorter paths.
                              When False, routes use only orthogonal (Manhattan) moves.
        """
        self.grid = grid
        self.rules = rules
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.heuristic = heuristic or DEFAULT_HEURISTIC
        self.diagonal_routing = diagonal_routing

        # Neighbor offsets: (dx, dy, dlayer, cost_multiplier)
        # Same layer moves - orthogonal directions
        self.neighbors_2d = [
            (1, 0, 0, 1.0),  # Right
            (-1, 0, 0, 1.0),  # Left
            (0, 1, 0, 1.0),  # Down
            (0, -1, 0, 1.0),  # Up
        ]

        # Add diagonal directions if enabled (45° routing)
        # Diagonal moves travel √2 ≈ 1.414x the distance of orthogonal moves
        if diagonal_routing:
            self.neighbors_2d.extend(
                [
                    (1, 1, 0, 1.414),  # Down-Right
                    (-1, 1, 0, 1.414),  # Down-Left
                    (1, -1, 0, 1.414),  # Up-Right
                    (-1, -1, 0, 1.414),  # Up-Left
                ]
            )

        # Pre-calculate trace clearance radius in grid cells
        # This is the total radius from trace centerline that must be clear:
        # - trace_width/2: half-width of the trace copper
        # - trace_clearance: required clearance from trace edge to obstacles
        # This enforces clearance as a hard constraint during routing.
        # Issue #553: Previously only checked trace_width/2, causing DRC violations
        # when traces were placed too close to obstacles.
        # Issue #864: Use round() before ceil() to avoid floating point errors
        # causing an extra cell of clearance (e.g., 0.30000000000000004 -> 4 cells
        # instead of 3 cells).
        self._trace_half_width_cells = max(
            1,
            math.ceil(
                round(
                    (self.rules.trace_width / 2 + self.rules.trace_clearance)
                    / self.grid.resolution,
                    6,
                )
            ),
        )

        # Pre-calculate via blocking radius in grid cells
        # Via needs diameter/2 + clearance from other objects (pads, traces, vias)
        # Issue #864: Use round() before ceil() to avoid floating point errors.
        self._via_half_cells = max(
            1,
            math.ceil(
                round(
                    (self.rules.via_diameter / 2 + self.rules.via_clearance) / self.grid.resolution,
                    6,
                )
            ),
        )

        # Pre-compute neighbor arrays for batch cost computation (Issue #963)
        # Store offsets and cost multipliers as NumPy arrays for vectorized operations
        self._neighbor_dx = np.array([dx for dx, _, _, _ in self.neighbors_2d], dtype=np.int32)
        self._neighbor_dy = np.array([dy for _, dy, _, _ in self.neighbors_2d], dtype=np.int32)
        self._neighbor_cost_mult = np.array(
            [cost_mult for _, _, _, cost_mult in self.neighbors_2d], dtype=np.float64
        )

        # Pre-compute via checking offsets for vectorized blocking check (Issue #966)
        # Store all (dx, dy) pairs within via radius for batch cell lookup
        via_r = self._via_half_cells
        via_offsets = [
            (dx, dy) for dy in range(-via_r, via_r + 1) for dx in range(-via_r, via_r + 1)
        ]
        self._via_offset_dx = np.array([dx for dx, _ in via_offsets], dtype=np.int32)
        self._via_offset_dy = np.array([dy for _, dy in via_offsets], dtype=np.int32)

        # Layer priority cache for via checks: check most-congested layers first
        # This enables faster rejection when via is blocked on congested layer
        self._layer_priority: list[int] | None = None

        # Via validity cache (Issue #966): caches whether via can be placed at (x, y, net)
        # Key: (gx, gy, net), Value: True if valid, False if blocked
        # Cache is cleared when routes are modified (invalidates blocking state)
        self._via_cache: dict[tuple[int, int, int, int], bool] = {}
        self._via_cache_enabled: bool = True

        # Issue #2947: World-coord foreign-net clearance context for via
        # placement.  The coarse-grid obstacle map consulted by
        # ``_is_via_blocked`` can admit a via that sits within
        # ``via_radius + obstacle_radius + clearance`` of a foreign-net pad
        # or trace in world coordinates -- the same bug class PR #2945
        # patched in ``EscapeRouter._can_place_via``.  When this context is
        # populated via :meth:`set_via_foreign_context`,
        # ``_check_via_placement_cached`` consults
        # :func:`point_clear_of_copper` after the per-layer grid check
        # passes.  Empty by default so behavior matches pre-#2947 when no
        # caller wires the context up.  Cache invariant: the setter
        # CLEARS ``_via_cache`` so subsequent checks re-evaluate against
        # the new context (same pattern as ``add_routed_segments``).
        # Issue #2951: stored as 5-tuples ``(x, y, width, height, net)`` so
        # ``point_clear_of_copper`` uses rect-distance for oblong fine-pitch
        # pads (the previous ``(x, y, max(w,h)/2, net)`` disc-bound was the
        # last remaining via-clearance bug pattern for non-square pads).
        self._foreign_pad_tuples: list[tuple[float, float, float, float, int]] = []
        self._foreign_track_adapters: list[_SegmentAdapter] = []

        # Issue #3002: Symmetric to ``_foreign_pad_tuples`` /
        # ``_foreign_track_adapters`` (Issue #2947), but for the OPPOSITE
        # direction -- a NEW segment vs FOREIGN-net vias.  Populated by
        # :meth:`set_segment_foreign_context` (sibling of
        # :meth:`set_via_foreign_context`).  Consumed by callers that
        # validate a candidate segment against foreign vias via
        # :func:`segment_clears_foreign_via` before committing the
        # segment to ``grid.routes``.  Empty by default so behavior
        # matches pre-#3002 when no caller wires the context up.
        self._foreign_vias: list[Via] = []

        # Issue #1016: Component pitch cache for per-component clearance
        # Computed lazily on first use
        self._component_pitches: dict[str, float] | None = None

        # Issue #1016: Pre-compute trace clearance radii for component-specific clearances
        # Maps clearance value (mm) to grid cell radius
        self._clearance_radii: dict[tuple[float, float], int] = {}
        self._precompute_clearance_radii()

        # Issue #1019: Via impact scoring for fine-pitch IC routing
        # Stores unrouted net pad positions for via impact calculation
        self._unrouted_pad_positions: list[tuple[float, float, int]] = []  # (x, y, net)
        self._fine_pitch_pad_positions: list[tuple[float, float, str]] = []  # (x, y, ref)
        self._via_exclusion_cells: int = 0  # Grid cells for via exclusion zone
        self._via_impact_enabled: bool = False
        self._init_via_impact_scoring()

        # Issue #2275: Layer utilization balancing
        # Cached per-layer fill ratios for the A* cost function.  Updated
        # after each net routes via ``update_layer_fill_ratios()``.
        self._layer_fill_ratios: np.ndarray = np.zeros(self.grid.num_layers, dtype=np.float64)

        # Issue #1250: Crossing-aware routing
        # Stores previously routed segments for crossing detection.
        # Each entry is (x1, y1, x2, y2, layer_index, net_id) in grid coordinates.
        self._routed_segments: list[tuple[int, int, int, int, int, int]] = []

        # Issue #2430: Grid-based spatial index for crossing detection.
        # Built lazily at the start of each route() call when segments exist.
        # Replaces O(S) linear scan with O(B) bucket lookup.
        self._crossing_grid: dict[int, dict[tuple[int, int], list[int]]] | None = None
        self._crossing_bucket_size: int = 8
        self._crossing_grid_cols: int = 0
        self._crossing_grid_rows: int = 0

        # Issue #2325: Via placement diagnostic counters.
        # These track via placement attempts and rejections to help diagnose
        # zero-via routing failures on multi-layer boards.
        self._via_diag_attempts: int = 0
        self._via_diag_blocked: int = 0
        self._via_diag_zone_blocked: int = 0
        self._via_diag_exclusion_blocked: int = 0
        self._via_diag_eligible: int = 0

        # Issue #2330: Waypoint injection for off-grid pad routing.
        # Maps virtual (negative) grid indices to exact world coordinates.
        # Populated per-route call; cleared at the start of each route.
        self._waypoint_world_coords: dict[tuple[int, int], tuple[float, float]] = {}
        # Counter for assigning unique negative waypoint indices.
        self._waypoint_id_counter: int = 0

        # Issue #2559 / Epic #2556 Phase 1C: Differential-pair within-pair
        # clearance threading.  Net-name -> net-id reverse map populated by
        # the autorouter (or test harness) so the pathfinder can resolve
        # ``NetClassRouting.diffpair_partner`` (a name) to an integer net id
        # for the per-cell partner branch in ``_is_trace_blocked``.  Empty
        # by default -- when unset, the partner branch is dormant and
        # behavior matches pre-#2559 (single-clearance) routing.
        self._net_name_to_id: dict[str, int] = {}

        # Issue #2929: Per-A*-call wall-clock instrumentation.  When
        # ``_per_call_timing_enabled`` is True, every ``route()`` invocation
        # appends a dict to ``_per_call_timings`` recording the elapsed wall
        # time and the deadline budget used for that call.  Disabled by
        # default to keep zero overhead on the production hot path; the
        # autorouter / fleet-status command flips it on for audits.  Drained
        # via :meth:`get_and_clear_per_call_timings`.
        #
        # Schema for each record (see :meth:`route` for population):
        #   {
        #     "net": int,                  # source net id
        #     "net_name": str,
        #     "elapsed": float,            # wall-clock seconds for THIS call
        #     "per_net_timeout": float|None,  # deadline supplied by caller
        #     "deadline_violated": bool,   # True if elapsed > 1.2 * timeout
        #     "succeeded": bool,           # True iff route() returned non-None
        #   }
        #
        # This is the diagnostic surface for the deadline-enforcement audit
        # (Issue #2929 acceptance criterion 1).  Cumulative per-net wall
        # time across rip-up retries is computed by the caller by summing
        # records grouped by net id; a single record represents ONE A*
        # invocation and is what ``per_net_timeout`` actually brackets.
        self._per_call_timing_enabled: bool = False
        self._per_call_timings: list[dict] = []

    def enable_per_call_timing(self, enabled: bool = True) -> None:
        """Enable or disable per-A*-call wall-clock instrumentation.

        Issue #2929: When enabled, every ``route()`` call appends a timing
        record to an internal list that can be drained with
        :meth:`get_and_clear_per_call_timings`.  Disabled by default so the
        production routing loop pays zero overhead.

        Args:
            enabled: True to start recording; False to stop and drop any
                accumulated records.
        """
        self._per_call_timing_enabled = bool(enabled)
        if not enabled:
            self._per_call_timings = []

    def get_and_clear_per_call_timings(self) -> list[dict]:
        """Drain and return the recorded per-A*-call timing records.

        Issue #2929: Each record is a dict with keys
        ``net``, ``net_name``, ``elapsed``, ``per_net_timeout``,
        ``deadline_violated``, and ``succeeded`` (see ``__init__`` for the
        full schema).  After draining, the internal list is cleared so
        subsequent audit windows only see fresh records.

        Returns:
            The list of timing records since the last drain (or since
            instrumentation was enabled).  Empty list if instrumentation
            is disabled or no calls have been made.
        """
        result = self._per_call_timings
        self._per_call_timings = []
        return result

    # ------------------------------------------------------------------
    # Waypoint helpers (Issue #2330)
    # ------------------------------------------------------------------

    def _is_pad_off_grid(self, pad: Pad) -> bool:
        """Check whether a pad's center falls off the routing grid.

        A pad is considered off-grid when the distance from its center to
        the nearest grid point exceeds ``resolution / 4`` in either axis.
        """
        gx, gy = self.grid.world_to_grid(pad.x, pad.y)
        snap_x, snap_y = self.grid.grid_to_world(gx, gy)
        tolerance = self.grid.resolution / 4
        return abs(pad.x - snap_x) > tolerance or abs(pad.y - snap_y) > tolerance

    def _create_waypoint(self, pad: Pad) -> tuple[int, int]:
        """Allocate a unique virtual grid index pair for *pad*.

        The returned ``(wx, wy)`` are negative integers that will never
        collide with real grid indices (which are >= 0).  The mapping
        from ``(wx, wy)`` to the pad's world coordinates is stored in
        ``self._waypoint_world_coords``.
        """
        self._waypoint_id_counter -= 1
        wp_id = self._waypoint_id_counter
        # Use the same negative value for both axes — uniqueness comes
        # from the single counter, and the pair is used as a dict key.
        key = (wp_id, wp_id)
        self._waypoint_world_coords[key] = (pad.x, pad.y)
        return key

    def _waypoint_grid_edges(
        self, wp_key: tuple[int, int], pad: Pad, net: int, allow_sharing: bool = False,
    ) -> list[tuple[int, int, float]]:
        """Return grid-cell neighbors reachable from a waypoint node.

        For each of the nearest grid cells (within a 3-cell radius of the
        pad's grid-snapped position), compute the Euclidean edge cost from
        the waypoint's world position to the grid cell's world position.
        Cells that are blocked by other nets are skipped unless they fall
        within the pad's metal area.

        Returns:
            List of ``(gx, gy, edge_cost)`` tuples.
        """
        wx, wy = self._waypoint_world_coords[wp_key]
        center_gx, center_gy = self.grid.world_to_grid(wx, wy)
        radius = 3  # search radius in grid cells
        edges: list[tuple[int, int, float]] = []

        # Precompute pad metal bounds for same-net passability check.
        metal_bounds = self._get_pad_metal_bounds(pad)
        mgx1, mgy1, mgx2, mgy2 = metal_bounds

        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                gx = center_gx + dx
                gy = center_gy + dy
                if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                    continue

                # Check accessibility on any layer the pad can use
                if pad.through_hole:
                    check_layers = self.grid.get_routable_indices()
                else:
                    check_layers = [self.grid.layer_to_index(pad.layer.value)]

                accessible = False
                for li in check_layers:
                    cell = self.grid.grid[li][gy][gx]
                    # Inside pad metal — always accessible
                    if mgx1 <= gx <= mgx2 and mgy1 <= gy <= mgy2:
                        accessible = True
                        break
                    if not cell.blocked:
                        accessible = True
                        break
                    if cell.net == net:
                        accessible = True
                        break
                    # Clearance-zone cell (not actual copper) — accessible
                    if not cell.pad_blocked:
                        accessible = True
                        break
                if not accessible:
                    continue

                # Euclidean distance in world units
                gw_x, gw_y = self.grid.grid_to_world(gx, gy)
                dist = math.sqrt((wx - gw_x) ** 2 + (wy - gw_y) ** 2)
                # Convert to grid-cell cost units
                edge_cost = dist / self.grid.resolution
                edges.append((gx, gy, edge_cost))

        return edges

    def _waypoint_to_world(self, x: int, y: int) -> tuple[float, float] | None:
        """Resolve a node's grid indices to world coordinates.

        If ``(x, y)`` is a waypoint (negative indices), return the stored
        world position.  Otherwise return ``None`` (caller should use
        ``grid_to_world``).
        """
        key = (x, y)
        return self._waypoint_world_coords.get(key)

    def _is_waypoint(self, x: int, y: int) -> bool:
        """Return True if ``(x, y)`` represents a waypoint node."""
        return x < 0 and y < 0 and (x, y) in self._waypoint_world_coords

    # ------------------------------------------------------------------
    # Escape-hint waypoints (Issue #2974)
    # ------------------------------------------------------------------
    #
    # IC perimeter pads (LQFP-48 NRST/SWO/SWDIO/SWCLK on board-04) sit in
    # a narrow channel between flanking foreign-net pins.  Pure
    # octile/Manhattan heuristics fan out around the chip body for tens
    # of seconds before discovering the escape direction.
    # ``_detect_escape_hint`` is a grid-density predicate that returns
    # the escape unit vector when the pad's geometry matches the corner-
    # flank signature; ``_escape_hint_cells`` produces cells along that
    # ray for seeding into the A* open set, reusing the #2330 waypoint
    # straight-line edge cost contract (admissibility preserved).

    # Wedge sampling skips the pad's own clearance ring (same-net cells
    # at radius 1..2) and looks for foreign blockers at radius 2..6.
    _ESCAPE_HINT_RADIUS_MIN: int = 2
    _ESCAPE_HINT_RADIUS_MAX: int = 6
    # Body wedge must be ~fully blocked (4 of 5 cells foreign).
    _ESCAPE_HINT_BODY_MIN: int = 4
    # Each perpendicular wedge must show flanking neighbour pins.
    _ESCAPE_HINT_FLANK_MIN: int = 2
    # Body wedge must dominate the escape wedge by this many cells.
    _ESCAPE_HINT_ASYMMETRY: int = 2
    # Initial walk-out step before falling back to closer/farther cells.
    _ESCAPE_HINT_STEP: int = 3
    # Per-net deadline multiplier applied ONLY when the predicate fires
    # (Issue #2974 secondary fallback).  3x is enough to lift the 30s
    # caller budget to 90s, matching the curator-recommended ceiling
    # for perimeter-corner-flanked nets without touching the global
    # deadline or any other net's budget.
    _ESCAPE_HINT_DEADLINE_MULT: float = 3.0

    def _cell_is_foreign_blocker(self, cell, net: int) -> bool:
        """Return True if ``cell`` is a foreign pad/zone blocker for ``net``.

        Mirrors the static-foreign-obstacle classifier at
        pathfinder.py:1480 (``is_obstacle and cell_net != net``) so
        :meth:`_detect_escape_hint` counts the same cells the main A*
        loop would reject.  ``cell.net`` records the first pad that
        claimed the cell; on overlap the second touch flips
        ``is_obstacle = True`` (grid.py:1357), which we honour here.
        """
        if cell.net == net:
            return False
        if cell.is_zone and cell.net != 0:
            return True
        if cell.is_obstacle:
            return True
        return bool(cell.blocked and cell.pad_blocked)

    def _detect_escape_hint(
        self, pad: Pad, layers: list[int]
    ) -> tuple[int, int] | None:
        """Return an escape direction ``(dx, dy)`` for a corner-flanked pad.

        Samples foreign-blocker density in each cardinal direction.  A
        pad is corner-flanked when one wedge (chip body) is densely
        blocked, the opposite wedge (escape) is comparatively open, and
        both perpendicular wedges show flanking blockers.  Returns the
        escape unit vector or ``None`` if the geometry doesn't match --
        typical for centre-of-board components, large THT pads, and
        connectors with generous keepouts.
        """
        if not layers:
            return None
        gx, gy = self.grid.world_to_grid(pad.x, pad.y)
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return None

        # Pad's primary layer is representative; perimeter geometry is
        # symmetrical across copper layers in practice.
        layer = layers[0]
        r_min, r_max = self._ESCAPE_HINT_RADIUS_MIN, self._ESCAPE_HINT_RADIUS_MAX
        dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        counts: dict[tuple[int, int], int] = {d: 0 for d in dirs}

        for d in dirs:
            dx, dy = d
            for step in range(r_min, r_max + 1):
                cx, cy = gx + dx * step, gy + dy * step
                if not (0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows):
                    break
                if self._cell_is_foreign_blocker(self.grid.grid[layer][cy][cx], pad.net):
                    counts[d] += 1

        body_dir = max(counts, key=counts.get)
        if counts[body_dir] < self._ESCAPE_HINT_BODY_MIN:
            return None
        escape_dir = (-body_dir[0], -body_dir[1])
        if counts[body_dir] - counts[escape_dir] < self._ESCAPE_HINT_ASYMMETRY:
            return None
        # Perpendicular wedges must show flanking neighbour pins.
        perp_dirs = [(-body_dir[1], body_dir[0]), (body_dir[1], -body_dir[0])]
        for pd in perp_dirs:
            if counts[pd] < self._ESCAPE_HINT_FLANK_MIN:
                return None
        return escape_dir

    def _escape_hint_cells(
        self,
        pad: Pad,
        escape_dir: tuple[int, int],
        net: int,
        layers: list[int],
    ) -> list[tuple[int, int, int, float]]:
        """Produce ``(gx, gy, layer, edge_cost)`` seeds for an escape hint.

        Walks outward from the pad along ``escape_dir`` and returns the
        first cells that are not foreign-blocked -- the first toehold
        A* can land on in the escape corridor.  ``edge_cost`` is the
        Euclidean distance from the pad's world position to the cell,
        in grid-cell units, mirroring :meth:`_waypoint_grid_edges` so
        admissibility is preserved.  Returns an empty list when the
        escape ray runs into a wall, in which case the route falls
        back to the unmodified A* search.
        """
        gx, gy = self.grid.world_to_grid(pad.x, pad.y)
        dx, dy = escape_dir
        seeds: list[tuple[int, int, int, float]] = []

        # Prefer the closest clear cell along the ray.  A farther seed
        # would inflate edge_cost and bias A* against revisiting closer
        # alternatives if the corridor turns out to need a detour.
        start_step = max(1, self._ESCAPE_HINT_STEP - 1)
        for step in range(start_step, self._ESCAPE_HINT_RADIUS_MAX + 1):
            cx, cy = gx + dx * step, gy + dy * step
            if not (0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows):
                break
            wcx, wcy = self.grid.grid_to_world(cx, cy)
            edge_cost = math.sqrt((pad.x - wcx) ** 2 + (pad.y - wcy) ** 2) / self.grid.resolution

            usable = False
            for layer in layers:
                cell = self.grid.grid[layer][cy][cx]
                if self._cell_is_foreign_blocker(cell, net):
                    continue
                # Skip routed cells from other nets (not obstacles, but
                # not safe to seed into).
                if cell.blocked and cell.net not in (0, net):
                    continue
                seeds.append((cx, cy, layer, edge_cost))
                usable = True
            if usable:
                break
        return seeds

    def add_routed_segments(self, segments: list[Segment]) -> None:
        """Add committed route segments for crossing detection.

        Issue #1250: Called after each route is committed so that subsequent
        A* searches can penalize edges that cross these segments.

        Args:
            segments: List of Segment objects from a committed route.
        """
        for seg in segments:
            gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
            gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
            layer_idx = self.grid.layer_to_index(seg.layer.value)
            self._routed_segments.append((gx1, gy1, gx2, gy2, layer_idx, seg.net))

    def update_layer_fill_ratios(self) -> None:
        """Refresh the cached per-layer fill ratios from the grid.

        Issue #2275: Should be called after each net routes so that
        subsequent A* searches see up-to-date layer utilization.
        """
        self._layer_fill_ratios = self.grid.get_layer_fill_ratios()

    def clear_routed_segments(self) -> None:
        """Clear the routed segments list.

        Call this when starting a fresh routing session or when all routes
        have been removed.
        """
        self._routed_segments.clear()

    def get_via_diagnostics(self) -> dict[str, int]:
        """Return via placement diagnostic counters (Issue #2325).

        Returns:
            Dictionary with keys ``attempts``, ``blocked``, ``zone_blocked``,
            ``exclusion_blocked``, and ``eligible`` (candidates that passed all
            placement checks but may still be pruned by closed-set or g-score
            dominance).
        """
        return {
            "attempts": self._via_diag_attempts,
            "blocked": self._via_diag_blocked,
            "zone_blocked": self._via_diag_zone_blocked,
            "exclusion_blocked": self._via_diag_exclusion_blocked,
            "eligible": self._via_diag_eligible,
        }

    def reset_via_diagnostics(self) -> None:
        """Reset via placement diagnostic counters to zero."""
        self._via_diag_attempts = 0
        self._via_diag_blocked = 0
        self._via_diag_zone_blocked = 0
        self._via_diag_exclusion_blocked = 0
        self._via_diag_eligible = 0

    @staticmethod
    def _segments_intersect(
        ax1: int,
        ay1: int,
        ax2: int,
        ay2: int,
        bx1: int,
        by1: int,
        bx2: int,
        by2: int,
    ) -> bool:
        """Test whether two line segments intersect using cross-product sign changes.

        Uses the standard computational geometry approach: two segments intersect
        if and only if each segment straddles the line containing the other.

        Args:
            ax1, ay1, ax2, ay2: Endpoints of segment A (grid coords).
            bx1, by1, bx2, by2: Endpoints of segment B (grid coords).

        Returns:
            True if the segments properly intersect (share an interior point).
            Shared endpoints are NOT counted as intersections.
        """
        return _geom_segments_intersect(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)

    def _count_edge_crossings(
        self,
        cx: int,
        cy: int,
        nx: int,
        ny: int,
        nlayer: int,
        current_net: int,
    ) -> int:
        """Count how many routed segments the candidate edge crosses.

        Only counts crossings on the same layer with a different net.

        Issue #2430: When a crossing grid index is available (built by
        ``_build_crossing_grid``), uses spatial bucketing for O(B) lookup
        instead of O(S) linear scan.

        Args:
            cx, cy: Current node grid coordinates (edge start).
            nx, ny: Neighbor node grid coordinates (edge end).
            nlayer: Layer index of the candidate edge.
            current_net: Net ID of the route being searched.

        Returns:
            Number of crossing segments.
        """
        # Fast path: use spatial grid index when available
        if self._crossing_grid is not None:
            return self._count_edge_crossings_indexed(
                cx, cy, nx, ny, nlayer, current_net
            )

        count = 0
        for sx1, sy1, sx2, sy2, seg_layer, seg_net in self._routed_segments:
            # Only same layer, different net
            if seg_layer != nlayer or seg_net == current_net:
                continue
            if self._segments_intersect(cx, cy, nx, ny, sx1, sy1, sx2, sy2):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Crossing grid spatial index (Issue #2430)
    # ------------------------------------------------------------------

    def _build_crossing_grid(self, bucket_size: int = 8) -> None:
        """Build a grid-based spatial index for routed segments.

        Partitions the grid into square buckets and stores segment indices
        per bucket per layer.  ``_count_edge_crossings_indexed`` then only
        checks segments whose buckets overlap the query edge's bounding box.

        Args:
            bucket_size: Side length of each bucket in grid cells.
        """
        self._crossing_bucket_size = bucket_size
        cols_b = (self.grid.cols + bucket_size - 1) // bucket_size
        rows_b = (self.grid.rows + bucket_size - 1) // bucket_size

        # Dict[layer, Dict[(bx, by), List[int]]] where int = index into
        # _routed_segments.
        grid_idx: dict[int, dict[tuple[int, int], list[int]]] = {}

        for seg_i, (sx1, sy1, sx2, sy2, seg_layer, _seg_net) in enumerate(
            self._routed_segments
        ):
            bx1 = min(sx1, sx2) // bucket_size
            by1 = min(sy1, sy2) // bucket_size
            bx2 = max(sx1, sx2) // bucket_size
            by2 = max(sy1, sy2) // bucket_size

            bx1 = max(0, min(bx1, cols_b - 1))
            by1 = max(0, min(by1, rows_b - 1))
            bx2 = max(0, min(bx2, cols_b - 1))
            by2 = max(0, min(by2, rows_b - 1))

            layer_grid = grid_idx.setdefault(seg_layer, {})
            for by in range(by1, by2 + 1):
                for bx in range(bx1, bx2 + 1):
                    layer_grid.setdefault((bx, by), []).append(seg_i)

        self._crossing_grid = grid_idx
        self._crossing_grid_cols = cols_b
        self._crossing_grid_rows = rows_b

    def _count_edge_crossings_indexed(
        self,
        cx: int,
        cy: int,
        nx: int,
        ny: int,
        nlayer: int,
        current_net: int,
    ) -> int:
        """Count edge crossings using the spatial grid index.

        Same semantics as ``_count_edge_crossings`` but O(B) where B is the
        number of segments in overlapping buckets, instead of O(S) total.
        """
        assert self._crossing_grid is not None
        bs = self._crossing_bucket_size

        layer_grid = self._crossing_grid.get(nlayer)
        if layer_grid is None:
            return 0

        # Compute bounding box of the edge in bucket coordinates
        bx1 = min(cx, nx) // bs
        by1 = min(cy, ny) // bs
        bx2 = max(cx, nx) // bs
        by2 = max(cy, ny) // bs

        bx1 = max(0, bx1)
        by1 = max(0, by1)
        bx2 = min(bx2, self._crossing_grid_cols - 1)
        by2 = min(by2, self._crossing_grid_rows - 1)

        # Collect unique segment indices from overlapping buckets
        seen: set[int] = set()
        count = 0
        for by in range(by1, by2 + 1):
            for bx in range(bx1, bx2 + 1):
                bucket = layer_grid.get((bx, by))
                if bucket is None:
                    continue
                for seg_i in bucket:
                    if seg_i in seen:
                        continue
                    seen.add(seg_i)
                    sx1, sy1, sx2, sy2, _seg_layer, seg_net = self._routed_segments[seg_i]
                    if seg_net == current_net:
                        continue
                    if self._segments_intersect(cx, cy, nx, ny, sx1, sy1, sx2, sy2):
                        count += 1
        return count

    def _init_via_impact_scoring(self) -> None:
        """Initialize via impact scoring based on design rules.

        Issue #1019: Sets up via exclusion zones and impact scoring when
        via_exclusion_from_fine_pitch or via_impact_weight are configured.
        """
        # Calculate via exclusion zone in grid cells
        if self.rules.via_exclusion_from_fine_pitch > 0:
            self._via_exclusion_cells = max(
                1,
                math.ceil(
                    round(self.rules.via_exclusion_from_fine_pitch / self.grid.resolution, 6)
                ),
            )

        # Enable via impact scoring if weight is positive
        self._via_impact_enabled = self.rules.via_impact_weight > 0

    def set_unrouted_pads(self, unrouted_pads: list[Pad]) -> None:
        """Set the list of unrouted pad positions for via impact scoring.

        Issue #1019: Called by Autorouter before routing each net to update
        which pads haven't been connected yet. This enables the via impact
        scoring to consider whether a via would block access to unrouted pins.

        Args:
            unrouted_pads: List of Pad objects that haven't been routed yet.
        """
        self._unrouted_pad_positions = [(pad.x, pad.y, pad.net) for pad in unrouted_pads]

        # Also identify fine-pitch pads for exclusion zone checking
        self._fine_pitch_pad_positions = []
        component_pitches = self.component_pitches
        for pad in unrouted_pads:
            ref = pad.ref
            if ref and ref in component_pitches:
                pitch = component_pitches[ref]
                if pitch < self.rules.fine_pitch_threshold:
                    self._fine_pitch_pad_positions.append((pad.x, pad.y, ref))

    def set_via_foreign_context(
        self,
        foreign_pads: list[Pad] | None = None,
        foreign_tracks: list[Segment] | None = None,
    ) -> None:
        """Set foreign-net pad / track context for world-coord via clearance.

        Issue #2947: ``_check_via_placement_cached`` historically only
        consulted the coarse-grid obstacle map via ``_is_via_blocked``.
        A via that lands on a "free" grid cell can still sit within
        ``via_radius + foreign_obstacle_radius + clearance`` of an
        adjacent foreign-net pad / trace in world coordinates -- the
        coarse grid's resolution loses this distinction.  When this
        context is populated, the via predicate consults
        :func:`point_clear_of_copper` after the grid check passes.

        Cache-key safety: the via cache key is
        ``(gx, gy, net, effective_radius)`` -- already net-keyed, so
        cross-net stale positives are impossible.  Within a single
        net, however, a positive cache entry from a stale foreign
        context could now be wrong (a foreign track may have committed
        since).  This setter therefore CLEARS the via cache so subsequent
        checks re-evaluate against the new context.  Same invariant
        ``add_routed_segments`` / ``clear_via_cache`` already maintain.

        Args:
            foreign_pads: Board pads (any net) the via must clear.
                Same-net pads are filtered per-call so a superset is
                fine.
            foreign_tracks: Committed track segments (any net) the via
                must clear.  Same-net segments are filtered per-call.
        """
        # Issue #2951: pass (x, y, width, height, net) 5-tuples so
        # ``point_clear_of_copper`` uses rect-distance for oblong
        # fine-pitch pads (mirrors the same change in
        # ``EscapeRouter._can_place_via``).
        pad_tuples: list[tuple[float, float, float, float, int]] = []
        if foreign_pads:
            for p in foreign_pads:
                pad_tuples.append((p.x, p.y, p.width, p.height, p.net))

        track_adapters: list[_SegmentAdapter] = []
        if foreign_tracks:
            for s in foreign_tracks:
                track_adapters.append(
                    _SegmentAdapter(
                        start_x=s.x1, start_y=s.y1,
                        end_x=s.x2, end_y=s.y2,
                        width=s.width,
                    )
                )

        self._foreign_pad_tuples = pad_tuples
        self._foreign_track_adapters = track_adapters

        # Foreign context affects via blocking results -- invalidate cache.
        self.clear_via_cache()

    def set_segment_foreign_context(
        self,
        foreign_vias: list[Via] | None = None,
    ) -> None:
        """Set foreign-net via context for new-segment clearance gating.

        Issue #3002: Symmetric sibling of
        :meth:`set_via_foreign_context` (PR #2952 / Issue #2947).  Where
        ``set_via_foreign_context`` protects a NEW via from foreign
        segments / pads, this setter protects a NEW segment from
        foreign-net VIAs.

        Background: the main router commits segments via
        :meth:`_mark_route` (called from :meth:`route_net` and the
        negotiated rip-up path).  Pre-commit validation flows through
        :meth:`_validate_route_clearance`, which already walks
        ``grid.routes`` vias via :meth:`Grid.validate_segment_clearance`
        -- but only for vias ALREADY committed when the segment is
        validated.  Cross-net ordering bugs slip through when net A's
        segment commits BEFORE net B's via is placed in the same
        negotiated iteration (the board-04 SWDIO/BOOT0 site at PCB
        (143.8, 119.7) on B.Cu).  This setter lets the
        :class:`Autorouter` push a richer foreign-via list -- including
        vias that the negotiated post-iteration re-validation hook
        (algorithms/negotiated.py) will surface -- so the predicate is
        consulted with up-to-date geometry.

        Same-net filtering is the CALLER's responsibility (mirrors the
        boundary convention of :meth:`set_via_foreign_context`).

        Cache invariant: any cached per-segment validity results would
        be invalidated by a change in foreign-via geometry.  The router
        does not currently cache segment-clearance lookups (the via
        cache is the only one affected by world-coord geometry), so no
        additional cache clear is required here.  We still invalidate
        the via cache for symmetry with :meth:`set_via_foreign_context`
        in case a future patch introduces a per-segment cache that is
        keyed similarly.

        Args:
            foreign_vias: List of :class:`Via` objects whose net differs
                from the segment's own net.  Pass ``None`` to clear the
                context.
        """
        self._foreign_vias = list(foreign_vias) if foreign_vias else []

        # Foreign-via geometry can affect via-cache validity indirectly
        # (e.g. when a new via is added the via cache for nearby cells
        # must re-evaluate against the updated geometry).  Mirrors the
        # invariant in :meth:`set_via_foreign_context`.
        self.clear_via_cache()

    def _get_via_impact_cost(self, wx: float, wy: float, current_net: int) -> float:
        """Calculate the impact cost of placing a via at the given position.

        Issue #1019: Scores via placement based on how many unrouted net pins
        would be blocked or have their routing options constrained.

        Args:
            wx, wy: World coordinates of the proposed via position
            current_net: Net ID of the current route (excluded from impact)

        Returns:
            Impact cost (0 if no impact, positive value based on blocked pins)
        """
        if not self._via_impact_enabled:
            return 0.0

        impact = 0.0
        via_radius = self.rules.via_diameter / 2 + self.rules.via_clearance

        # Count unrouted pins that would be affected by this via
        for px, py, net in self._unrouted_pad_positions:
            if net == current_net:
                continue  # Same net, not impacted

            # Calculate distance from via to pad
            dist = math.sqrt((wx - px) ** 2 + (wy - py) ** 2)

            # Via blocks routing if it's within via_radius + trace_clearance + trace_width/2
            # of the pad (prevents traces from reaching the pad)
            blocking_dist = via_radius + self.rules.trace_clearance + self.rules.trace_width / 2

            if dist < blocking_dist:
                # Via would directly block access to this pad
                impact += 10.0
            elif dist < blocking_dist * 2:
                # Via constrains routing options but doesn't fully block
                # Impact decreases linearly with distance
                impact += 5.0 * (1 - (dist - blocking_dist) / blocking_dist)

        return impact * self.rules.via_impact_weight

    def _is_via_in_exclusion_zone(self, gx: int, gy: int) -> bool:
        """Check if a via position is within the exclusion zone of any fine-pitch pad.

        Issue #1019: Prevents via placement too close to fine-pitch IC pads,
        which would block routing to adjacent pins.

        Args:
            gx, gy: Grid coordinates of the proposed via position

        Returns:
            True if via is in exclusion zone (should be avoided), False otherwise.
        """
        if self._via_exclusion_cells == 0:
            return False

        if not self._fine_pitch_pad_positions:
            return False

        # Convert grid position to world coordinates
        wx, wy = self.grid.grid_to_world(gx, gy)

        exclusion_dist = self._via_exclusion_cells * self.grid.resolution

        for px, py, _ref in self._fine_pitch_pad_positions:
            dist = math.sqrt((wx - px) ** 2 + (wy - py) ** 2)
            if dist < exclusion_dist:
                return True

        return False

    def _precompute_clearance_radii(self) -> None:
        """Pre-compute grid cell radii for all component-specific clearances.

        Issue #1016: Pre-computes clearance radii for:
        - Default trace clearance
        - Each per-component clearance
        - Fine-pitch clearance (if configured)

        This allows efficient lookup during routing.
        """
        # Always include default clearance
        clearances = {self.rules.trace_clearance}

        # Add per-component clearances
        for clearance in self.rules.component_clearances.values():
            clearances.add(clearance)

        # Add fine-pitch clearance if configured
        if self.rules.fine_pitch_clearance is not None:
            clearances.add(self.rules.fine_pitch_clearance)

        # Collect all trace widths that may be used (global + per-net-class)
        trace_widths = {self.rules.trace_width}
        for nc in self.net_class_map.values():
            trace_widths.add(nc.trace_width)

        # Compute grid cell radius for each (trace_width, clearance) pair
        for tw in trace_widths:
            for clearance in clearances:
                radius = max(
                    1,
                    math.ceil(
                        round(
                            (tw / 2 + clearance) / self.grid.resolution,
                            6,
                        )
                    ),
                )
                self._clearance_radii[(tw, clearance)] = radius

    def get_clearance_radius_cells(self, clearance_mm: float, trace_width: float | None = None) -> int:
        """Get the trace clearance radius in grid cells for a given clearance.

        Args:
            clearance_mm: Clearance value in mm
            trace_width: Trace width in mm.  When ``None``, uses
                ``rules.trace_width`` (the global default).  Issue #1674:
                per-net-class trace widths require computing radii with
                the actual net trace width, not just the global default.

        Returns:
            Radius in grid cells (at least 1)
        """
        tw = trace_width if trace_width is not None else self.rules.trace_width
        cache_key = (tw, clearance_mm)

        # Check cache first
        if cache_key in self._clearance_radii:
            return self._clearance_radii[cache_key]

        # Compute and cache
        radius = max(
            1,
            math.ceil(
                round(
                    (tw / 2 + clearance_mm) / self.grid.resolution,
                    6,
                )
            ),
        )
        self._clearance_radii[cache_key] = radius
        return radius

    @property
    def component_pitches(self) -> dict[str, float]:
        """Get component pin pitches for automatic fine-pitch detection.

        Issue #1016: Computed lazily on first access and cached.
        Used for per-component clearance validation.

        Returns:
            Dict mapping component reference to minimum pin pitch in mm.
        """
        if self._component_pitches is None:
            self._component_pitches = self.grid.compute_component_pitches()
        return self._component_pitches

    def invalidate_component_pitch_cache(self) -> None:
        """Invalidate the component pitch cache.

        Call this if pads are added or modified after Router initialization.
        """
        self._component_pitches = None

    def _get_net_class(self, net_name: str) -> NetClassRouting | None:
        """Get the net class for a net name."""
        return self.net_class_map.get(net_name)

    # ------------------------------------------------------------------
    # Diff-pair partner resolution (Issue #2559 / Epic #2556 Phase 1C)
    # ------------------------------------------------------------------

    def set_net_name_to_id(self, mapping: dict[str, int]) -> None:
        """Inject a net-name -> net-id reverse map for partner resolution.

        Phase 1C threads ``NetClassRouting.intra_pair_clearance`` through the
        A* search.  The clearance is configured by net-class on the source
        net, but applies only when the *other* net is the named partner.
        Resolving partner-name to partner-id requires a reverse map that the
        ``Autorouter`` builds from its ``net_names`` dict.

        The setter is idempotent and may be called multiple times (e.g. to
        refresh the map when new pads are loaded).  Passing an empty dict
        disables partner detection (everything falls back to ``clearance``).
        """
        self._net_name_to_id = dict(mapping)

    def _resolve_partner_net_id(self, net_name: str) -> int | None:
        """Look up the integer net id of the diff-pair partner of *net_name*.

        Reads ``NetClassRouting.diffpair_partner`` (the authoritative
        Phase 1B signal) and resolves the partner-name to a partner-id via
        :attr:`_net_name_to_id`.  Returns ``None`` when:

        * the source net has no net class (or the class has no
          ``diffpair_partner`` set), or
        * the partner-name is missing from ``_net_name_to_id`` (e.g. the
          autorouter has not populated the reverse map yet).

        ``None`` is the dormant signal for the four read sites: when partner
        is unknown, the search uses the wider ``clearance`` for every other
        net, matching pre-#2559 behavior.
        """
        net_class = self._get_net_class(net_name)
        if net_class is None or net_class.diffpair_partner is None:
            return None
        return self._net_name_to_id.get(net_class.diffpair_partner)

    def _get_trace_width_for_net(self, net_name: str) -> float:
        """Get the trace width for a net based on its net class.

        Looks up the net's class in the net_class_map and returns the
        class-specific trace width. Falls back to rules.trace_width if
        the net has no class mapping.

        Args:
            net_name: Name of the net

        Returns:
            Trace width in mm
        """
        net_class = self._get_net_class(net_name)
        if net_class is not None:
            return net_class.trace_width
        return self.rules.trace_width

    def _get_pad_metal_bounds(self, pad: Pad) -> tuple[int, int, int, int]:
        """Calculate the grid coordinate bounds of a pad's metal area.

        This is used to expand goal regions for off-grid pads, ensuring
        routes can reach pads even when their centers don't align with
        the routing grid (Issue #956).

        Returns:
            (gx_min, gy_min, gx_max, gy_max) grid coordinate bounds
        """
        # Calculate effective pad dimensions (same logic as grid._add_pad_unsafe)
        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif pad.drill > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        # Metal area bounds in world coordinates
        metal_x1 = pad.x - effective_width / 2
        metal_y1 = pad.y - effective_height / 2
        metal_x2 = pad.x + effective_width / 2
        metal_y2 = pad.y + effective_height / 2

        # Convert to grid coordinates using ceil/floor to ensure we only include
        # cells whose CENTER is inside the metal area (Issue #996).
        # Using round() would include cells that are merely nearby.
        resolution = self.grid.resolution
        origin_x = self.grid.origin_x
        origin_y = self.grid.origin_y

        gx1 = max(0, int(math.ceil((metal_x1 - origin_x) / resolution)))
        gy1 = max(0, int(math.ceil((metal_y1 - origin_y) / resolution)))
        gx2 = min(self.grid.cols - 1, int(math.floor((metal_x2 - origin_x) / resolution)))
        gy2 = min(self.grid.rows - 1, int(math.floor((metal_y2 - origin_y) / resolution)))

        return (gx1, gy1, gx2, gy2)

    def _is_trace_blocked(
        self, gx: int, gy: int, layer: int, net: int, allow_sharing: bool = False,
        radius: int | None = None,
        partner_net: int | None = None,
        partner_radius: int | None = None,
        partner_active: bool | None = None,
    ) -> bool:
        """Check if placing a trace at this position would conflict.

        Unlike is_blocked which checks a single cell, this accounts for
        trace width by checking adjacent cells the trace would occupy.

        Uses vectorized NumPy operations for performance (Issue #962).
        Pre-computed clearance masks enable single-operation blocking checks
        instead of iterating over (2r+1)² cells per neighbor.

        Args:
            allow_sharing: If True (negotiated mode), allow routing through
                          non-obstacle blocked cells (they'll get high cost instead)
            radius: Override the trace half-width in grid cells. When None,
                    uses the global ``_trace_half_width_cells`` (Issue #1674).
            partner_net: Issue #2559 / Phase 1C -- if set (non-None and >= 0)
                         and ``partner_radius`` is provided, cells belonging
                         to this net are checked against the *tighter*
                         ``partner_radius`` instead of the wider ``radius``.
                         This implements within-pair clearance for diff pairs.
                         When ``None``, the partner branch is dormant and
                         behavior matches pre-#2559 routing.
            partner_radius: Tighter half-width (in grid cells) to apply only
                            to ``cell.net == partner_net``.  When ``None``
                            but ``partner_net`` is set, the partner branch
                            falls back to ``radius`` (no tightening).
            partner_active: Issue #2715 -- pre-computed dormant/active state
                            for the partner branch.  Callers in the A* hot
                            path resolve this once per route and pass the
                            cached bool here so the per-call 4-condition
                            tuple evaluation is skipped.  When ``None``
                            (legacy callers), the boolean is derived from
                            ``partner_net``/``partner_radius`` for backward
                            compatibility.
        """
        if radius is None:
            radius = self._trace_half_width_cells

        # Calculate region bounds
        x1 = gx - radius
        y1 = gy - radius
        x2 = gx + radius + 1
        y2 = gy + radius + 1

        # Clamp region to grid bounds instead of rejecting out-of-bounds
        # entirely (Issue #2425).  Pads near the board periphery previously
        # had all neighbours rejected because the trace-width check region
        # extended outside the grid, making them unroutable.
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(self.grid.cols, x2)
        y2 = min(self.grid.rows, y2)
        if x1 >= x2 or y1 >= y2:
            return True  # degenerate region after clamping

        # Extract array slices for the region
        blocked_region = self.grid._blocked[layer, y1:y2, x1:x2]
        net_region = self.grid._net[layer, y1:y2, x1:x2]

        # Issue #2559 / Phase 1C: Build partner-relaxation mask.
        # When the partner branch is active, cells whose net matches
        # partner_net are checked against partner_radius instead of radius.
        # We OR the blocking mask with a "is partner cell outside the tight
        # radius" suppressor so partner-blocked cells in the slack ring
        # (>partner_radius && <=radius from gx,gy) are treated as passable.
        #
        # Issue #2715: When the caller provides a cached ``partner_active``
        # bool, use it directly to skip the per-call 4-condition tuple
        # evaluation.  This is the hot-path optimization for dormant-partner
        # routes (the common case for non-diff-pair nets).
        if partner_active is None:
            partner_active = (
                partner_net is not None
                and partner_net >= 0
                and partner_radius is not None
                and partner_radius < radius
            )
        partner_relax_mask = None
        if partner_active:
            # Compute Chebyshev distance from (gx, gy) to each cell in the
            # extracted region, then mark cells whose net == partner_net
            # AND distance > partner_radius as "ignore for blocking".
            ys = np.arange(y1, y2)
            xs = np.arange(x1, x2)
            cheb = np.maximum(
                np.abs(ys - gy)[:, None],
                np.abs(xs - gx)[None, :],
            )
            partner_relax_mask = (net_region == partner_net) & (cheb > partner_radius)

        if allow_sharing:
            # Negotiated mode: more complex logic
            # Block if any cell is:
            # 1. Blocked AND is_obstacle AND different net
            # 2. Blocked AND NOT is_obstacle AND different net AND usage_count == 0 (static)
            obstacle_region = self.grid._is_obstacle[layer, y1:y2, x1:x2]
            usage_region = self.grid._usage_count[layer, y1:y2, x1:x2]

            # Different net mask (includes net == 0 which are no-net obstacles)
            different_net = net_region != net

            # Case 1: Blocked obstacles with different net
            obstacle_blocks = blocked_region & obstacle_region & different_net

            # Case 2: Blocked non-obstacles with different net AND static (usage == 0)
            static_blocks = blocked_region & ~obstacle_region & different_net & (usage_region == 0)

            combined = obstacle_blocks | static_blocks
            if partner_relax_mask is not None:
                combined = combined & ~partner_relax_mask
            return bool(np.any(combined))
        else:
            # Standard mode: block if any cell is blocked AND has different net
            # Issue #864: Same-net cells are passable (even overlapping clearance)
            # but different-net cells and obstacles (net=0 blocked cells) must block.
            blocked_different_net = blocked_region & (net_region != net)
            if partner_relax_mask is not None:
                blocked_different_net = blocked_different_net & ~partner_relax_mask
            return bool(np.any(blocked_different_net))

    def _is_diagonal_corner_blocked(
        self, gx: int, gy: int, dx: int, dy: int, layer: int, net: int, allow_sharing: bool = False
    ) -> bool:
        """Check if diagonal move would cut through obstacle corners.

        When moving diagonally from (gx, gy) to (gx+dx, gy+dy), we must verify
        that both adjacent orthogonal cells are clear to prevent corner-cutting:

            B │ D      Moving from A to D diagonally requires
            ──┼──      both B (gx, gy+dy) and C (gx+dx, gy) to be clear
            A │ C

        Args:
            gx, gy: Current grid position
            dx, dy: Diagonal direction (both must be non-zero for diagonal move)
            layer: Current layer
            net: Net ID for same-net checking
            allow_sharing: If True, allow routing through non-obstacle blocked cells

        Returns:
            True if the diagonal move is blocked (would cut corner), False if clear.
        """
        # Only check for actual diagonal moves
        if dx == 0 or dy == 0:
            return False

        # Check the two adjacent orthogonal cells
        # Cell B: same x, new y
        # Cell C: new x, same y
        adjacent_cells = [
            (gx, gy + dy),  # B: vertical neighbor
            (gx + dx, gy),  # C: horizontal neighbor
        ]

        for cx, cy in adjacent_cells:
            # Check bounds
            if not (0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows):
                return True  # Out of bounds = blocked

            cell = self.grid.grid[layer][cy][cx]

            if cell.blocked:
                if allow_sharing and not cell.is_obstacle:
                    # In negotiated mode, non-obstacle cells can be shared
                    # No-net pads (cell.net == 0) must always block other nets
                    # See issue #317: routes incorrectly allowed through no-net pads
                    if cell.net == 0:
                        if cell.usage_count == 0:
                            return True  # Static no-net obstacle (pad) - block
                    elif cell.net != net:
                        # Only allow sharing if this cell has been used by routes
                        # (usage_count > 0). Cells with usage_count == 0 are static
                        # obstacles like pads that should never be shared.
                        # See issue #174: pad clearance zones must block other nets.
                        if cell.usage_count == 0:
                            return True  # Static obstacle (pad) - block
                        continue  # Allow with cost penalty (routed cell)
                else:
                    # Standard mode (same logic as _is_trace_blocked)
                    # Issue #864: Same-net cells are passable, different nets block
                    if cell.net == net:
                        pass  # Same net - passable
                    else:
                        return True  # Different net or obstacle - blocked

        return False

    def _is_via_blocked(
        self, gx: int, gy: int, layer: int, net: int, allow_sharing: bool = False,
        radius: int | None = None,
    ) -> bool:
        """Check if placing a via at this position would conflict.

        Similar to _is_trace_blocked but uses the larger via clearance radius.
        Through-hole vias must be checked on ALL layers.

        Issue #966: Uses vectorized NumPy operations for ~2-3x speedup over
        the original nested loop implementation.

        Args:
            allow_sharing: If True (negotiated mode), allow routing through
                          non-obstacle blocked cells (they'll get high cost instead)
            radius: Override the via half-width in grid cells. When None,
                    uses the pre-computed ``_via_half_cells`` (Issue #1692).
        """
        # Issue #1692: Support per-net-class via radius override.
        # When a custom radius is provided, compute offsets on the fly
        # rather than using the pre-computed arrays (which use the global
        # via diameter).
        if radius is not None and radius != self._via_half_cells:
            via_r = radius
            via_offset_dx = np.array(
                [dx for dy in range(-via_r, via_r + 1) for dx in range(-via_r, via_r + 1)],
                dtype=np.int32,
            )
            via_offset_dy = np.array(
                [dy for dy in range(-via_r, via_r + 1) for dx in range(-via_r, via_r + 1)],
                dtype=np.int32,
            )
        else:
            via_offset_dx = self._via_offset_dx
            via_offset_dy = self._via_offset_dy

        # Compute all cell coordinates within via radius using offsets
        cx_arr = gx + via_offset_dx
        cy_arr = gy + via_offset_dy

        # Check bounds - if any cell is out of bounds, via is blocked
        in_bounds = (
            (cx_arr >= 0) & (cx_arr < self.grid.cols) & (cy_arr >= 0) & (cy_arr < self.grid.rows)
        )
        if not np.all(in_bounds):
            return True  # Some cells out of bounds

        # Batch lookup cell attributes using fancy indexing
        blocked_arr = self.grid._blocked[layer, cy_arr, cx_arr]

        # Fast path: if no cells are blocked, via is not blocked
        if not np.any(blocked_arr):
            return False

        # Some cells are blocked - need detailed checking
        # Get indices of blocked cells only
        blocked_indices = np.where(blocked_arr)[0]

        # Batch lookup additional attributes for blocked cells
        blocked_cx = cx_arr[blocked_indices]
        blocked_cy = cy_arr[blocked_indices]
        net_arr = self.grid._net[layer, blocked_cy, blocked_cx]

        if allow_sharing:
            # Negotiated mode: allow sharing non-obstacle cells
            is_obstacle_arr = self.grid._is_obstacle[layer, blocked_cy, blocked_cx]
            usage_arr = self.grid._usage_count[layer, blocked_cy, blocked_cx]

            for i in range(len(blocked_indices)):
                cell_net = net_arr[i]
                is_obstacle = is_obstacle_arr[i]
                usage = usage_arr[i]

                # Issue #2963: own-net obstacle cells (destination pad
                # metal post-PR #2928 first-touch marking) must remain
                # passable for the routing net's own via.  Foreign-net
                # obstacles still hard-reject.
                if is_obstacle and cell_net != net:
                    return True  # Obstacles always block (foreign net)

                # No-net pads must always block
                if cell_net == 0:
                    if usage == 0:
                        return True  # Static no-net obstacle
                elif cell_net != net:
                    # Different net - only allow if cell was used by routes
                    if usage == 0:
                        return True  # Static obstacle (pad)
                # else: same net or routed cell - allow with cost
        else:
            # Standard mode: same-net passable, different nets block
            # Check if any blocked cell has different net
            different_net = net_arr != net
            if np.any(different_net):
                return True

        return False

    def _get_negotiated_cell_cost(
        self,
        gx: int,
        gy: int,
        layer: int,
        present_factor: float = 1.0,
        net: int | None = None,
    ) -> float:
        """Get negotiated congestion cost for a cell.

        Issue #2963: ``net`` plumbs the routing-net context to the
        cost function so own-net ``is_obstacle`` cells (destination
        pad metal marked by PR #2928's first-touch) are reachable.
        """
        return self.grid.get_negotiated_cost(gx, gy, layer, present_factor, net=net)

    def _get_layer_priority(self) -> list[int]:
        """Get layer indices sorted by congestion (most congested first).

        Issue #966: When checking if a via is blocked, checking congested
        layers first enables faster rejection since blocked cells are more
        likely on congested layers.

        Returns:
            List of layer indices sorted by decreasing congestion level.
        """
        if self._layer_priority is not None:
            return self._layer_priority

        # Calculate total congestion per layer
        congestion_per_layer = []
        for layer_idx in range(self.grid.num_layers):
            layer_congestion = np.sum(self.grid._congestion[layer_idx])
            congestion_per_layer.append((layer_idx, layer_congestion))

        # Sort by congestion (descending)
        congestion_per_layer.sort(key=lambda x: x[1], reverse=True)

        # Cache and return layer indices
        self._layer_priority = [layer_idx for layer_idx, _ in congestion_per_layer]
        return self._layer_priority

    def _invalidate_layer_priority(self) -> None:
        """Invalidate cached layer priority (call when congestion changes significantly)."""
        self._layer_priority = None

    def _check_via_placement_cached(
        self, gx: int, gy: int, net: int, allow_sharing: bool = False,
        radius: int | None = None,
    ) -> bool:
        """Check if a via can be placed at (gx, gy) for the given net, using cache.

        Issue #966: This method wraps via blocking checks with a cache to avoid
        redundant computation when the same position is checked multiple times
        during A* search.

        Args:
            gx, gy: Grid coordinates for via placement
            net: Net ID for the route
            allow_sharing: If True (negotiated mode), allow sharing
            radius: Override the via half-width in grid cells (Issue #1692).

        Returns:
            True if via CAN be placed (all layers clear), False if blocked.
        """
        # Try cache first (only in non-sharing mode since sharing state can change)
        # Issue #1692: Include radius in cache key so different net classes
        # don't collide in the cache.
        effective_radius = radius if radius is not None else self._via_half_cells
        if self._via_cache_enabled and not allow_sharing:
            cache_key = (gx, gy, net, effective_radius)
            if cache_key in self._via_cache:
                return self._via_cache[cache_key]

        # Check all layers using priority ordering.
        # Issue #2325: Skip plane layers when checking via blockage.  On plane
        # layers (GND/PWR), KiCad's zone fill creates the anti-pad or thermal
        # relief automatically.  Checking blocking on plane layers causes false
        # rejections from PTH pad clearance zones, which on dense boards can
        # prevent ALL via placement.
        for check_layer in self._get_layer_priority():
            if self.grid.is_plane_layer(check_layer):
                continue
            if self._is_via_blocked(gx, gy, check_layer, net, allow_sharing,
                                     radius=radius):
                # Cache the negative result
                if self._via_cache_enabled and not allow_sharing:
                    self._via_cache[(gx, gy, net, effective_radius)] = False
                return False

        # Issue #2947: World-coord foreign-net clearance check.  The
        # per-layer coarse-grid check above can admit a via that sits
        # within ``via_radius + foreign_obstacle_radius + clearance`` of
        # an adjacent foreign-net pad / trace in world coordinates -- the
        # grid resolution loses the sub-cell distinction.  Same bug
        # class PR #2945 patched in ``EscapeRouter._can_place_via``.
        # Only runs when ``set_via_foreign_context`` has populated the
        # context lists (otherwise behavior matches pre-#2947).  A* has
        # a fallback at both call sites (``continue`` on rejection ->
        # next neighbor / next via position), so a hard reject here is
        # safe -- unlike the QFP in-pad rescue path.
        if self._foreign_pad_tuples or self._foreign_track_adapters:
            wx, wy = self.grid.grid_to_world(gx, gy)
            # Effective via diameter from grid radius (cells -> mm).
            eff_diameter = 2 * effective_radius * self.grid.resolution
            # Filter same-net obstacles (passing a superset is allowed
            # by ``point_clear_of_copper``'s contract but the same-net
            # filter avoids spurious rejections on the routing net).
            # Net is at index 4 for the 5-tuple (x, y, w, h, net) shape
            # populated by ``set_via_foreign_context`` (Issue #2951).
            other_pads = [p for p in self._foreign_pad_tuples if p[4] != net]
            # Track adapter does not carry net id; the caller
            # (``Autorouter``) is responsible for excluding same-net
            # segments before populating the context.  This matches
            # ``EscapeRouter``'s pattern at the boundary.
            if not point_clear_of_copper(
                x=wx,
                y=wy,
                via_size=eff_diameter,
                clearance=self.rules.via_clearance,
                other_net_tracks=self._foreign_track_adapters,
                other_net_pads=other_pads,
            ):
                if self._via_cache_enabled and not allow_sharing:
                    self._via_cache[(gx, gy, net, effective_radius)] = False
                return False

        # Via is valid on all layers - cache positive result
        if self._via_cache_enabled and not allow_sharing:
            self._via_cache[(gx, gy, net, effective_radius)] = True
        return True

    def clear_via_cache(self) -> None:
        """Clear the via validity cache.

        Call this when grid state changes (routes added/removed) to ensure
        cache doesn't return stale results.
        """
        self._via_cache.clear()

    def set_via_cache_enabled(self, enabled: bool) -> None:
        """Enable or disable via caching.

        Args:
            enabled: True to enable caching, False to disable.
        """
        self._via_cache_enabled = enabled
        if not enabled:
            self._via_cache.clear()

    def _get_congestion_cost(self, gx: int, gy: int, layer: int) -> float:
        """Get additional cost based on congestion at this location."""
        congestion = self.grid.get_congestion(gx, gy, layer)
        if congestion > self.rules.congestion_threshold:
            # Exponential penalty for congested areas
            excess = congestion - self.rules.congestion_threshold
            return self.rules.cost_congestion * (1.0 + excess * 2.0)
        return 0.0

    def _batch_congestion_costs(self, current_x: int, current_y: int, layer: int) -> np.ndarray:
        """Batch compute congestion costs for all 2D neighbors using vectorized NumPy.

        Issue #963: Pre-compute congestion costs for all neighbors in a single
        batch operation to reduce per-neighbor function call overhead.

        Args:
            current_x: Current grid x coordinate
            current_y: Current grid y coordinate
            layer: Current layer index

        Returns:
            Array of congestion costs indexed by neighbor offset index.
            Out-of-bounds neighbors get cost 0 (will be filtered anyway).
        """
        # Compute neighbor coordinates
        nx_arr = current_x + self._neighbor_dx
        ny_arr = current_y + self._neighbor_dy

        # Bounds mask - identify valid neighbors
        valid = (
            (nx_arr >= 0) & (nx_arr < self.grid.cols) & (ny_arr >= 0) & (ny_arr < self.grid.rows)
        )

        # Convert to congestion grid coordinates
        congestion_size = self.grid.congestion_size
        cx_arr = np.minimum(nx_arr // congestion_size, self.grid.congestion_cols - 1)
        cy_arr = np.minimum(ny_arr // congestion_size, self.grid.congestion_rows - 1)

        # Initialize costs array
        costs = np.zeros(len(self.neighbors_2d), dtype=np.float64)

        # Get valid indices
        valid_indices = np.where(valid)[0]
        if len(valid_indices) == 0:
            return costs

        # Batch lookup congestion counts using fancy indexing
        max_cells = congestion_size * congestion_size
        congestion_counts = self.grid._congestion[
            layer, cy_arr[valid_indices], cx_arr[valid_indices]
        ]
        congestion_levels = np.minimum(1.0, congestion_counts / max_cells)

        # Compute costs where congestion exceeds threshold
        threshold = self.rules.congestion_threshold
        exceeds = congestion_levels > threshold
        excess = np.maximum(0, congestion_levels - threshold)
        valid_costs = np.where(exceeds, self.rules.cost_congestion * (1.0 + excess * 2.0), 0.0)
        costs[valid_indices] = valid_costs

        return costs

    def _batch_turn_costs(self, current_direction: tuple[int, int]) -> np.ndarray:
        """Batch compute turn costs for all 2D neighbors using vectorized NumPy.

        Issue #963: Pre-compute turn costs for all neighbors in a single
        batch operation.

        Args:
            current_direction: Current direction as (dx, dy) tuple

        Returns:
            Array of turn costs indexed by neighbor offset index.
        """
        if current_direction == (0, 0):
            # No current direction - no turn penalty
            return np.zeros(len(self.neighbors_2d), dtype=np.float64)

        # Check which neighbors match the current direction
        dx_match = self._neighbor_dx == current_direction[0]
        dy_match = self._neighbor_dy == current_direction[1]
        matches = dx_match & dy_match

        # Turn cost where direction doesn't match
        return np.where(matches, 0.0, self.rules.cost_turn)

    def _batch_negotiated_costs(
        self,
        current_x: int,
        current_y: int,
        layer: int,
        present_cost_factor: float,
        skip_mask: np.ndarray,
    ) -> np.ndarray:
        """Batch compute negotiated costs for all 2D neighbors using vectorized NumPy.

        Issue #963: Pre-compute negotiated costs for all neighbors in a single
        batch operation.

        Args:
            current_x: Current grid x coordinate
            current_y: Current grid y coordinate
            layer: Current layer index
            present_cost_factor: Multiplier for current sharing penalty
            skip_mask: Boolean array indicating neighbors to skip (e.g., near pads)

        Returns:
            Array of negotiated costs indexed by neighbor offset index.
        """
        # Compute neighbor coordinates
        nx_arr = current_x + self._neighbor_dx
        ny_arr = current_y + self._neighbor_dy

        # Bounds mask combined with skip mask
        valid = (
            (nx_arr >= 0)
            & (nx_arr < self.grid.cols)
            & (ny_arr >= 0)
            & (ny_arr < self.grid.rows)
            & ~skip_mask
        )

        # Initialize costs array
        costs = np.zeros(len(self.neighbors_2d), dtype=np.float64)

        # Get valid indices
        valid_indices = np.where(valid)[0]
        if len(valid_indices) == 0:
            return costs

        # Batch lookup usage counts and history costs using fancy indexing
        usage_counts = self.grid._usage_count[layer, ny_arr[valid_indices], nx_arr[valid_indices]]
        history_costs = self.grid._history_cost[layer, ny_arr[valid_indices], nx_arr[valid_indices]]

        # Compute present cost + history cost
        # Issue #2333: When EMA smoothing is active, use the smoothed
        # per-cell present cost instead of the raw usage * factor.
        if self.grid._present_cost_ema is not None:
            present_costs = self.grid._present_cost_ema[layer, ny_arr[valid_indices], nx_arr[valid_indices]]
        else:
            present_costs = present_cost_factor * usage_counts
        costs[valid_indices] = present_costs + history_costs

        return costs

    def _is_zone_cell(self, gx: int, gy: int, layer: int) -> bool:
        """Check if a cell is part of a zone (copper pour)."""
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return False
        return self.grid.grid[layer][gy][gx].is_zone

    def _get_zone_net(self, gx: int, gy: int, layer: int) -> int:
        """Get the net number of a zone cell, or 0 if not a zone."""
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return 0
        cell = self.grid.grid[layer][gy][gx]
        if cell.is_zone:
            return cell.net
        return 0

    def _is_zone_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if routing through this zone cell is blocked.

        Zone cells allow routing through same-net zones but block
        routing through other-net zones.

        Args:
            gx, gy: Grid coordinates
            layer: Grid layer index
            net: Net number of the route being planned

        Returns:
            True if blocked (other-net zone), False if passable
        """
        if not self._is_zone_cell(gx, gy, layer):
            return False  # Not a zone, use normal blocking logic

        zone_net = self._get_zone_net(gx, gy, layer)

        # Same net: passable (can route through own zone copper)
        if zone_net == net:
            return False

        # Different net: blocked (cannot route through other-net zone)
        return True

    def _get_zone_cost(self, gx: int, gy: int, layer: int, net: int) -> float:
        """Get routing cost adjustment for zone cells.

        Same-net zones have reduced cost (encourage using zone copper).
        Different-net zones are blocked (handled elsewhere).

        Args:
            gx, gy: Grid coordinates
            layer: Grid layer index
            net: Net number of the route being planned

        Returns:
            Cost adjustment (0.0 for normal, negative for same-net zone)
        """
        if not self._is_zone_cell(gx, gy, layer):
            return 0.0

        zone_net = self._get_zone_net(gx, gy, layer)

        if zone_net == net:
            # Same net - encourage using zone copper with reduced cost
            return self.rules.cost_zone_same_net - 1.0  # Net reduction
        else:
            # Different net - should be blocked, but return high cost as fallback
            return 100.0

    def _get_layer_preference_cost(self, layer: int, net_class: NetClassRouting | None) -> float:
        """Get routing cost based on layer preferences (Issue #625).

        Applies cost modifiers based on the net class's layer preferences:
        - Preferred layers get a discount (cost multiplier 0.5)
        - Avoided layers get a penalty (cost multiplier from net_class)
        - Neutral layers have no adjustment

        Args:
            layer: Grid layer index
            net_class: NetClassRouting with layer preferences

        Returns:
            Cost multiplier (< 1.0 for preferred, > 1.0 for avoided, 1.0 for neutral)
        """
        if net_class is None:
            return 1.0

        # Check if this is a preferred layer
        if net_class.preferred_layers is not None:
            if layer in net_class.preferred_layers:
                return 0.5  # Discount for preferred layer

        # Check if this is an avoided layer
        if net_class.avoid_layers is not None:
            if layer in net_class.avoid_layers:
                return net_class.layer_cost_multiplier  # Penalty for avoided layer

        return 1.0  # Neutral

    def _is_layer_allowed(self, layer_idx: int) -> bool:
        """Check if routing on this layer is allowed (Issue #715).

        When allowed_layers is set in DesignRules, only those layers
        can be used for routing. This provides a hard constraint for
        single-layer or restricted-layer routing.

        Args:
            layer_idx: Grid layer index

        Returns:
            True if layer is allowed (or no restriction), False if blocked
        """
        if self.rules.allowed_layers is None:
            return True  # No restriction

        # Convert grid index to Layer enum value, then to KiCad name for comparison
        layer_value = self.grid.index_to_layer(layer_idx)
        layer = Layer(layer_value)
        return layer.kicad_name in self.rules.allowed_layers

    def _can_place_via_in_zones(self, gx: int, gy: int, net: int) -> bool:
        """Check if via placement is legal considering zones on all layers.

        A via can be placed if:
        - No zone on any layer, OR
        - All zones are same-net (via connects through same-net zones), OR
        - Via is placed where there's no zone copper

        Args:
            gx, gy: Grid coordinates
            net: Net number of the route being planned

        Returns:
            True if via can be placed, False if blocked by other-net zone
        """
        for layer_idx in range(self.grid.num_layers):
            if self._is_zone_cell(gx, gy, layer_idx):
                zone_net = self._get_zone_net(gx, gy, layer_idx)
                if zone_net != net:
                    # Via would pierce an other-net zone
                    return False
        return True

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
        per_net_timeout: float | None = None,
        extra_goal_cells: set[tuple[int, int, int]] | None = None,
    ) -> Route | None:
        """Route between two pads using congestion-aware A*.

        Issue #2929: When per-A*-call timing instrumentation is enabled via
        :meth:`enable_per_call_timing`, this wrapper records the elapsed
        wall-clock time and whether the deadline was respected.  The actual
        search logic lives in :meth:`_route_impl`.
        """
        if not self._per_call_timing_enabled:
            return self._route_impl(
                start, end,
                net_class=net_class,
                negotiated_mode=negotiated_mode,
                present_cost_factor=present_cost_factor,
                weight=weight,
                per_net_timeout=per_net_timeout,
                extra_goal_cells=extra_goal_cells,
            )

        t0 = time.monotonic()
        succeeded = False
        try:
            result = self._route_impl(
                start, end,
                net_class=net_class,
                negotiated_mode=negotiated_mode,
                present_cost_factor=present_cost_factor,
                weight=weight,
                per_net_timeout=per_net_timeout,
                extra_goal_cells=extra_goal_cells,
            )
            succeeded = result is not None
            return result
        finally:
            elapsed = time.monotonic() - t0
            # Issue #2929 acceptance criterion 2 specifies a 1.2x slack
            # bound for "small fudge factor."  We add a 1s absolute floor
            # to accommodate one final 1024-iteration batch on the Python
            # path (the deadline is sampled every 1024 iterations; on a
            # dense grid that batch can take ~hundreds of ms).  For
            # production budgets (10-30s typical) the multiplicative
            # bound dominates; for tiny budgets (sub-second) the additive
            # floor prevents false positives from check granularity.
            deadline_violated = (
                per_net_timeout is not None
                and per_net_timeout > 0
                and elapsed > per_net_timeout * 1.2 + 1.0
            )
            self._per_call_timings.append({
                "net": start.net,
                "net_name": start.net_name,
                "elapsed": elapsed,
                "per_net_timeout": per_net_timeout,
                "deadline_violated": deadline_violated,
                "succeeded": succeeded,
            })

    def _route_impl(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
        per_net_timeout: float | None = None,
        extra_goal_cells: set[tuple[int, int, int]] | None = None,
    ) -> Route | None:
        """Inner A* search implementation -- see :meth:`route` for the public
        wrapper that adds optional per-call wall-clock instrumentation.

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: If True, allow sharing resources with cost penalty
            present_cost_factor: Multiplier for current sharing penalty (increases each iteration)
            weight: A* weight factor (1.0 = optimal A*, >1.0 = faster but suboptimal)
                    Higher values explore fewer nodes but may miss optimal paths.
            per_net_timeout: Optional wall-clock timeout in seconds for this A* search.
                    If exceeded, returns None (no route found within time budget).
            extra_goal_cells: Optional set of (gx, gy, layer) grid cells that
                    count as goals in addition to the end pad.  Used by incremental
                    Steiner routing (Issue #2306) so that A* can terminate early
                    when it reaches any cell of the previously-routed net tree.
        """
        # Issue #966: Clear via cache at start of route (grid state may have changed)
        # Keep cache valid within this route call for same-position checks
        self.clear_via_cache()

        # Issue #2330: Reset waypoint state for this route call
        self._waypoint_world_coords.clear()
        self._waypoint_id_counter = 0

        # Get net class if not provided
        if net_class is None:
            net_class = self._get_net_class(start.net_name)

        # Net class cost multiplier (lower = prefer this net's route)
        cost_mult = net_class.cost_multiplier if net_class else 1.0

        # Issue #1674: Compute per-net trace clearance radius for A* search.
        # Use the net class trace width (if available) instead of the global
        # rules.trace_width so wider nets (e.g. POWER at 0.5mm) correctly
        # reserve space during pathfinding, not just at segment creation.
        net_trace_width = net_class.trace_width if net_class else self.rules.trace_width
        net_trace_clearance = net_class.clearance if net_class else self.rules.trace_clearance
        net_trace_half_width_cells = max(
            1,
            math.ceil(
                round(
                    (net_trace_width / 2 + net_trace_clearance) / self.grid.resolution,
                    6,
                )
            ),
        )

        # Issue #2559 / Epic #2556 Phase 1C: Resolve diff-pair partner and
        # compute the tighter within-pair half-width radius.  When the
        # source net's NetClassRouting declares a ``diffpair_partner`` and
        # the partner net id is known via ``_net_name_to_id``, the search
        # uses ``effective_intra_pair_clearance`` as the tighter radius
        # for cells belonging to the partner net only.  All other foreign
        # nets continue to see the wider ``net_trace_clearance`` radius.
        partner_net_id = self._resolve_partner_net_id(start.net_name)
        if partner_net_id is not None and net_class is not None:
            net_intra_pair_clearance = net_class.effective_intra_pair_clearance()
            net_partner_half_width_cells = max(
                1,
                math.ceil(
                    round(
                        (net_trace_width / 2 + net_intra_pair_clearance) / self.grid.resolution,
                        6,
                    )
                ),
            )
        else:
            net_partner_half_width_cells = net_trace_half_width_cells

        # Issue #2715: Pre-compute the partner-active flag ONCE per route.
        # This is forwarded into ``compute_expanded_blocked`` so the dormant
        # path (the common case for non-diff-pair nets) skips the 4-condition
        # tuple evaluation in the grid hot path.
        partner_active_flag = (
            partner_net_id is not None
            and partner_net_id >= 0
            and net_partner_half_width_cells < net_trace_half_width_cells
        )

        # Issue #1692: Compute per-net via clearance radius.  Net classes
        # may specify larger via_size which requires a wider blocking check.
        net_via_size = net_class.via_size if net_class else self.rules.via_diameter
        net_via_half_cells = max(
            1,
            math.ceil(
                round(
                    (net_via_size / 2 + self.rules.via_clearance) / self.grid.resolution,
                    6,
                )
            ),
        )

        # In negotiated mode, allow resource sharing
        allow_sharing = negotiated_mode

        # Convert to grid coordinates
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)

        # Convert Layer enum values to grid indices
        # For PTH pads, we can start/end on any routable layer
        routable_layers = self.grid.get_routable_indices()
        start_layer = self.grid.layer_to_index(start.layer.value)
        end_layer = self.grid.layer_to_index(end.layer.value)

        # Get all valid start/end layers for this pad type
        start_layers = routable_layers if start.through_hole else [start_layer]
        end_layers = routable_layers if end.through_hole else [end_layer]

        # Issue #956/#977: Calculate pad metal area bounds for expanded start/goal regions
        # When pads don't align with the routing grid, we accept reaching any cell
        # within the pad's metal area, not just the grid-snapped center cell.
        # Issue #977: Apply same expansion to START pad - if the grid-snapped center
        # falls on a cell blocked by another net's clearance, we need alternate entry points.
        start_metal_gx1, start_metal_gy1, start_metal_gx2, start_metal_gy2 = (
            self._get_pad_metal_bounds(start)
        )
        end_metal_gx1, end_metal_gy1, end_metal_gx2, end_metal_gy2 = self._get_pad_metal_bounds(end)

        # Issue #1618: Precompute geometry-derived pad approach bounds.
        # The approach zone is the pad metal area expanded by a small escape margin
        # (2 cells beyond the metal edge). This replaces the old hardcoded
        # pad_approach_radius=6 which created an oversized clearance-free zone
        # around every pad regardless of actual pad geometry.
        pad_escape_margin = 2  # cells beyond pad metal edge
        start_approach_gx1 = start_metal_gx1 - pad_escape_margin
        start_approach_gy1 = start_metal_gy1 - pad_escape_margin
        start_approach_gx2 = start_metal_gx2 + pad_escape_margin
        start_approach_gy2 = start_metal_gy2 + pad_escape_margin
        end_approach_gx1 = end_metal_gx1 - pad_escape_margin
        end_approach_gy1 = end_metal_gy1 - pad_escape_margin
        end_approach_gx2 = end_metal_gx2 + pad_escape_margin
        end_approach_gy2 = end_metal_gy2 + pad_escape_margin

        # Filter start/end layers by allowed_layers constraint (Issue #715)
        if self.rules.allowed_layers is not None:
            start_layers = [l for l in start_layers if self._is_layer_allowed(l)]
            end_layers = [l for l in end_layers if self._is_layer_allowed(l)]
            # If no valid layers remain, routing is impossible
            if not start_layers or not end_layers:
                return None

        # A* setup
        open_set: list[AStarNode] = []

        # Issue #2430: Use NumPy arrays for closed_set and g_scores to avoid
        # Python dict/set overhead with 480K+ tuple keys.  Array indexing
        # (closed_arr[layer, y, x]) is significantly faster than dict hashing.
        closed_arr = np.zeros(
            (self.grid.num_layers, self.grid.rows, self.grid.cols), dtype=np.bool_
        )
        g_scores_arr = np.full(
            (self.grid.num_layers, self.grid.rows, self.grid.cols),
            np.inf,
            dtype=np.float64,
        )

        # Issue #2430: Pre-compute expanded blocked bitmap so that
        # _is_trace_blocked becomes a single array lookup per neighbor.
        # Issue #2559 / Phase 1C: Pass partner net id and tighter radius so
        # within-pair edges of a diff pair get the relaxed clearance.
        expanded_blocked = self.grid.compute_expanded_blocked(
            net_trace_half_width_cells,
            start.net,
            allow_sharing,
            partner_net=partner_net_id,
            partner_radius=net_partner_half_width_cells,
            partner_active=partner_active_flag,
        )

        # Issue #2430: Build crossing grid index if routed segments exist.
        if self.rules.crossing_penalty > 0.0 and self._routed_segments:
            self._build_crossing_grid()
        else:
            self._crossing_grid = None

        # Issue #2430: Pre-compute zone blocking and zone cost arrays.
        # Zone cells with a different net are blocked; same-net zones get
        # a cost discount.  This replaces per-neighbor Python object access
        # with direct NumPy lookups.
        zone_blocked_arr = self.grid._is_zone & (self.grid._net != start.net) & (self.grid._net != 0)
        zone_cost_arr = np.where(
            self.grid._is_zone & (self.grid._net == start.net),
            self.rules.cost_zone_same_net - 1.0,
            np.where(
                zone_blocked_arr,
                100.0,
                0.0,
            ),
        )

        # Create heuristic context - for PTH end pads, use closest routable layer
        # for heuristic estimation (the actual goal check will accept any)
        heuristic_goal_layer = end_layers[0] if end_layers else end_layer
        heuristic_context = HeuristicContext(
            goal_x=end_gx,
            goal_y=end_gy,
            goal_layer=heuristic_goal_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            diagonal_routing=self.diagonal_routing,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )

        # Issue #977: Start nodes - add for ALL cells within start pad's metal area
        # This handles off-grid start pads where the grid-snapped center may be blocked
        # by another net's clearance zone. By initializing from all metal area cells,
        # we ensure routing can begin even when some entry points are blocked.
        for sgx in range(start_metal_gx1, start_metal_gx2 + 1):
            for sgy in range(start_metal_gy1, start_metal_gy2 + 1):
                for sl in start_layers:
                    start_h = self.heuristic.estimate(sgx, sgy, sl, (0, 0), heuristic_context)
                    start_node = AStarNode(start_h, 0, sgx, sgy, sl)
                    heapq.heappush(open_set, start_node)
                    g_scores_arr[sl, sgy, sgx] = 0

        # Issue #2330: Waypoint injection for off-grid start pad.
        # Inject the exact pad world position as a waypoint node with edges
        # to nearby grid cells so A* can route from the precise pad location.
        start_wp_key: tuple[int, int] | None = None
        if self._is_pad_off_grid(start):
            start_wp_key = self._create_waypoint(start)
            wp_edges = self._waypoint_grid_edges(
                start_wp_key, start, start.net, allow_sharing,
            )
            for gx, gy, edge_cost in wp_edges:
                for sl in start_layers:
                    wp_g = edge_cost * self.rules.cost_straight
                    wp_h = self.heuristic.estimate(gx, gy, sl, (0, 0), heuristic_context)
                    wp_node = AStarNode(wp_g + weight * wp_h, wp_g, gx, gy, sl)
                    if wp_g < g_scores_arr[sl, gy, gx]:
                        g_scores_arr[sl, gy, gx] = wp_g
                        heapq.heappush(open_set, wp_node)

        # Issue #2330: Waypoint for off-grid end pad — used in goal check.
        end_wp_key: tuple[int, int] | None = None
        if self._is_pad_off_grid(end):
            end_wp_key = self._create_waypoint(end)
            end_wp_grid_edges = self._waypoint_grid_edges(
                end_wp_key, end, start.net, allow_sharing,
            )
            # Build set of grid cells reachable from end waypoint for goal check
            end_wp_goal_cells: set[tuple[int, int]] = {
                (gx, gy) for gx, gy, _cost in end_wp_grid_edges
            }
        else:
            end_wp_goal_cells = set()

        # Issue #2974: Escape-hint seed for corner-flanked perimeter
        # pads.  Reuses the #2330 waypoint edge-cost contract:
        # ``g_score = euclidean(pad -> seed_cell) * cost_straight``.
        # The seed inherits ``direction = escape_dir`` so its
        # descendants get the turn-cost bonus for continuing outward.
        # When ``_detect_escape_hint`` declines, the block is a no-op.
        escape_dir = self._detect_escape_hint(start, start_layers)
        if escape_dir is not None:
            for cx, cy, cl, edge_cost in self._escape_hint_cells(
                start, escape_dir, start.net, start_layers
            ):
                seed_g = edge_cost * self.rules.cost_straight
                seed_h = self.heuristic.estimate(cx, cy, cl, escape_dir, heuristic_context)
                if seed_g < g_scores_arr[cl, cy, cx]:
                    g_scores_arr[cl, cy, cx] = seed_g
                    heapq.heappush(
                        open_set,
                        AStarNode(seed_g + weight * seed_h, seed_g, cx, cy, cl,
                                  direction=escape_dir),
                    )

        iterations = 0
        max_iterations = self.grid.cols * self.grid.rows * 4  # Prevent infinite loops

        # Per-net wall-clock timeout (Issue #1605).
        #
        # Issue #2974 secondary fallback: when the start pad is corner-
        # flanked, give A* up to ``_ESCAPE_HINT_DEADLINE_MULT`` times
        # the caller's budget so the perimeter detour has room to
        # converge.  This is a TARGETED extension -- it never widens
        # the global deadline, and nets whose pads don't trip the
        # predicate continue to honour ``per_net_timeout`` exactly.
        effective_timeout = per_net_timeout
        if (
            per_net_timeout is not None
            and per_net_timeout > 0.0
            and escape_dir is not None
        ):
            effective_timeout = per_net_timeout * self._ESCAPE_HINT_DEADLINE_MULT
        deadline = (
            time.monotonic() + effective_timeout
            if effective_timeout is not None
            else None
        )
        timeout_check_interval = 1024

        while open_set and iterations < max_iterations:
            iterations += 1

            # Per-net timeout check (Issue #1605)
            if deadline is not None and iterations % timeout_check_interval == 0:
                if time.monotonic() >= deadline:
                    break

            current = heapq.heappop(open_set)

            if closed_arr[current.layer, current.y, current.x]:
                continue
            closed_arr[current.layer, current.y, current.x] = True

            # Goal check - accept any cell within end pad's metal area (Issue #956)
            # This handles off-grid pads where the center doesn't align with routing grid
            # Issue #2330: Also accept cells reachable from the end pad's waypoint
            is_in_end_metal = (
                end_metal_gx1 <= current.x <= end_metal_gx2
                and end_metal_gy1 <= current.y <= end_metal_gy2
                and current.layer in end_layers
            )
            is_end_waypoint_reachable = (
                end_wp_goal_cells
                and (current.x, current.y) in end_wp_goal_cells
                and current.layer in end_layers
            )
            if is_in_end_metal or is_end_waypoint_reachable:
                route = self._reconstruct_route(current, start, end)
                if route is not None:
                    return route
                # Geometric validation failed (Issue #750) - continue A* search
                # This allows finding alternate paths (e.g., B.Cu when F.Cu fails)
                # The node stays in closed_set, preventing re-exploration on this layer
                continue

            # Issue #2306: Incremental Steiner goal check - terminate early when
            # reaching any cell of the previously-routed net tree.  This avoids
            # running the full A* to the target pad when a shorter connection to
            # the existing tree exists, dramatically reducing search time for
            # high-fanout nets (e.g., GNDD with 7+ components).
            if extra_goal_cells and (current.x, current.y, current.layer) in extra_goal_cells:
                # Create a synthetic end pad at the reached grid cell so
                # _reconstruct_route can build a valid Route object.
                wx, wy = self.grid.grid_to_world(current.x, current.y)
                layer_val = self.grid.index_to_layer(current.layer)
                synthetic_end = Pad(
                    x=wx,
                    y=wy,
                    width=self.rules.trace_width,
                    height=self.rules.trace_width,
                    net=start.net,
                    net_name=start.net_name,
                    layer=Layer(layer_val),
                    ref="",
                    pin="",
                    through_hole=False,
                    steiner_point=True,
                )
                route = self._reconstruct_route(current, start, synthetic_end)
                if route is not None:
                    return route
                # Geometric validation failed - keep searching
                continue

            # Batch pre-compute costs for all neighbors (Issue #963)
            # This reduces per-neighbor function call overhead by computing all costs
            # in vectorized NumPy operations before the neighbor loop
            batch_congestion_costs = self._batch_congestion_costs(
                current.x, current.y, current.layer
            )
            batch_turn_costs = self._batch_turn_costs(current.direction)

            # Explore neighbors
            for neighbor_idx, (dx, dy, _dlayer, neighbor_cost_mult) in enumerate(self.neighbors_2d):
                nx, ny = current.x + dx, current.y + dy
                nlayer = current.layer

                # Check bounds and obstacles - account for trace width
                # A trace with width W extends W/2 on each side of centerline
                # Must check adjacent cells that trace would occupy
                #
                # EXCEPTION: Allow routing near pad centers if the adjacent cells
                # belong to the SAME NET. This handles TSSOP and other fine-pitch
                # components where pad clearance zones overlap.
                # But we MUST still block cells from OTHER nets (like GND pads).
                # Issue #1618: Use geometry-derived approach bounds instead of
                # hardcoded radius. The approach zone covers the pad metal area
                # plus a small escape margin (2 cells) to allow trace escape routing.
                # For PTH pads, allow approach from any valid layer
                is_start_adjacent = (
                    start_approach_gx1 <= nx <= start_approach_gx2
                    and start_approach_gy1 <= ny <= start_approach_gy2
                    and nlayer in start_layers
                )
                is_end_adjacent = (
                    end_approach_gx1 <= nx <= end_approach_gx2
                    and end_approach_gy1 <= ny <= end_approach_gy2
                    and nlayer in end_layers
                )

                # Issue #990: Check if CURRENT node is within a pad's metal area
                # When the entire metal area is blocked by other nets' clearance zones,
                # we still need to allow the first step outward from the pad.
                # This enables routing to start even when all metal area cells would
                # normally be blocked by adjacent components' clearance zones.
                is_exiting_start_pad = (
                    start_metal_gx1 <= current.x <= start_metal_gx2
                    and start_metal_gy1 <= current.y <= start_metal_gy2
                    and current.layer in start_layers
                )
                is_exiting_end_pad = (
                    end_metal_gx1 <= current.x <= end_metal_gx2
                    and end_metal_gy1 <= current.y <= end_metal_gy2
                    and current.layer in end_layers
                )

                # Check grid bounds first
                if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                    continue

                # For diagonal moves, check corner clearance to prevent cutting through obstacles
                # This ensures we don't route diagonally through a corner where two obstacles meet
                if dx != 0 and dy != 0:  # Diagonal move
                    if self._is_diagonal_corner_blocked(
                        current.x, current.y, dx, dy, nlayer, start.net, allow_sharing
                    ):
                        continue

                # Check blocked cells carefully
                # Allow routing through blocked cells that belong to OUR net
                # This enables THT pads to be entered/exited on any layer
                cell = self.grid.grid[nlayer][ny][nx]
                if cell.blocked:
                    # Issue #1764: Pad reachability - if the neighbor cell falls
                    # within either pad's metal area, allow entry regardless of blocked/net
                    # state. This ensures start/end pads are always reachable even when
                    # adjacent net=0 pads have marked their cells as blocked.
                    is_in_end_metal = (
                        end_metal_gx1 <= nx <= end_metal_gx2
                        and end_metal_gy1 <= ny <= end_metal_gy2
                        and nlayer in end_layers
                    )
                    is_in_start_metal = (
                        start_metal_gx1 <= nx <= start_metal_gx2
                        and start_metal_gy1 <= ny <= start_metal_gy2
                        and nlayer in start_layers
                    )
                    if is_in_end_metal or is_in_start_metal:
                        pass
                    elif cell.net == start.net:
                        # Same-net blocked cell (e.g., our THT pad area)
                        # Allow routing through it - this is key for THT routing
                        pass
                    elif cell.net == 0:
                        # No-net blocked cell - use pre-computed expanded bitmap
                        if expanded_blocked[nlayer, ny, nx]:
                            continue
                    else:
                        # Different net's blocked cell
                        # Issue #996: When exiting a pad's metal area, allow entering
                        # clearance zones (not actual pad copper). This enables sub-grid
                        # pad connections where the nearest grid cells are within another
                        # net's clearance zone but not its copper. The geometric validation
                        # during route reconstruction will catch actual DRC violations.
                        is_clearance_only = not cell.pad_blocked  # Not actual pad copper
                        is_pad_exit = is_exiting_start_pad or is_exiting_end_pad
                        if is_clearance_only and is_pad_exit:
                            # Clearance zone cell while exiting pad - allow this move
                            # to enable the first step out of the pad
                            pass
                        else:
                            # Actual pad copper or not exiting a pad - block
                            continue
                else:
                    # Issue #864: Even when center cell is unblocked, check trace clearance
                    # The trace has width and must not violate clearance to other nets
                    # within its radius. Skip this check near pads to allow approach.
                    # Issue #990: Also skip when exiting from within a pad's metal area.
                    # This handles dense layouts where ALL cells in the metal area are
                    # blocked by adjacent nets' clearance zones - we must allow the
                    # first step outward to escape the pad.
                    is_pad_exit_or_approach = (
                        is_start_adjacent
                        or is_end_adjacent
                        or is_exiting_start_pad
                        or is_exiting_end_pad
                    )
                    if not is_pad_exit_or_approach:
                        # Issue #2430: Use pre-computed expanded blocked bitmap
                        if expanded_blocked[nlayer, ny, nx]:
                            continue

                # Issue #2430: Use pre-computed zone blocking array
                if zone_blocked_arr[nlayer, ny, nx]:
                    continue

                if closed_arr[nlayer, ny, nx]:
                    continue

                # Calculate cost - use batch pre-computed values (Issue #963)
                new_direction = (dx, dy)

                # Use batch-computed turn and congestion costs
                turn_cost = batch_turn_costs[neighbor_idx]
                congestion_cost = batch_congestion_costs[neighbor_idx]

                # Add negotiated congestion cost if in negotiated mode
                # Skip for cells adjacent to start/end pads (they're obstacles)
                negotiated_cost = 0.0
                if negotiated_mode and not (is_start_adjacent or is_end_adjacent):
                    negotiated_cost = self._get_negotiated_cell_cost(
                        nx, ny, nlayer, present_cost_factor, net=start.net
                    )

                # Issue #2430: Use pre-computed zone cost array
                zone_cost = float(zone_cost_arr[nlayer, ny, nx])

                # Add layer preference cost (Issue #625)
                layer_pref_mult = self._get_layer_preference_cost(nlayer, net_class)

                # Issue #1250: Crossing penalty for edges crossing routed segments
                crossing_cost = 0.0
                if self.rules.crossing_penalty > 0.0 and self._routed_segments:
                    num_crossings = self._count_edge_crossings(
                        current.x, current.y, nx, ny, nlayer, start.net
                    )
                    crossing_cost = self.rules.crossing_penalty * num_crossings

                # Issue #2275: Layer utilization cost
                layer_util_cost = (
                    self._layer_fill_ratios[nlayer] * self.rules.cost_layer_utilization
                )

                # Issue #2288: Corridor deviation penalty from global routing
                corridor_cost = self.grid.get_corridor_cost(nx, ny, nlayer, start.net)

                # Issue #2911: Diff-pair / match-group corridor attractor.
                # Subtract a bonus when this cell is reserved for our net so
                # the pathfinder preferentially uses the reserved channel
                # established by ``EscapeRouter._reserve_pair_continuation_corridor``.
                # Clamped at the positive cost components so g_score stays
                # non-negative (preserves A* admissibility).
                attractor_bonus = self.grid.get_corridor_attractor_bonus(
                    nlayer, nx, ny, start.net, self.rules.cost_corridor_attractor,
                )

                positive_step_cost = (
                    neighbor_cost_mult * self.rules.cost_straight * layer_pref_mult
                    + turn_cost
                    + congestion_cost
                    + negotiated_cost
                    + zone_cost
                    + crossing_cost
                    + layer_util_cost
                    + corridor_cost
                )
                if attractor_bonus > 0.0:
                    positive_step_cost = max(0.0, positive_step_cost - attractor_bonus)

                new_g = (current.g_score + positive_step_cost) * cost_mult

                if new_g < g_scores_arr[nlayer, ny, nx]:
                    g_scores_arr[nlayer, ny, nx] = new_g
                    h = self.heuristic.estimate(nx, ny, nlayer, new_direction, heuristic_context)
                    f = new_g + weight * h  # Weighted A*

                    neighbor_node = AStarNode(
                        f, new_g, nx, ny, nlayer, current, False, new_direction
                    )
                    heapq.heappush(open_set, neighbor_node)

            # Try layer change (via) - use grid indices, not enum values
            # Only consider routable layers (skip planes)
            for new_layer in self.grid.get_routable_indices():
                if new_layer == current.layer:
                    continue

                # Check layer constraint (Issue #715)
                if not self._is_layer_allowed(new_layer):
                    continue

                # Check if via placement is valid on ALL layers (through-hole via)
                # Issue #966: Use cached via check with layer priority ordering
                # Issue #1692: Pass per-net via radius for wider net classes
                self._via_diag_attempts += 1
                if not self._check_via_placement_cached(
                    current.x, current.y, start.net, allow_sharing,
                    radius=net_via_half_cells,
                ):
                    self._via_diag_blocked += 1
                    continue

                # Check zone blocking for via (would pierce other-net zones)
                if not self._can_place_via_in_zones(current.x, current.y, start.net):
                    self._via_diag_zone_blocked += 1
                    continue

                # Issue #1019: Check via exclusion zone near fine-pitch pads
                # If via is in exclusion zone, skip this position (hard constraint)
                if self._is_via_in_exclusion_zone(current.x, current.y):
                    self._via_diag_exclusion_blocked += 1
                    continue

                self._via_diag_eligible += 1

                if closed_arr[new_layer, current.y, current.x]:
                    continue

                # Via cost + congestion at new layer
                congestion_cost = self._get_congestion_cost(current.x, current.y, new_layer)

                # Add negotiated congestion cost if in negotiated mode
                negotiated_cost = 0.0
                if negotiated_mode:
                    negotiated_cost = self._get_negotiated_cell_cost(
                        current.x, current.y, new_layer, present_cost_factor, net=start.net
                    )

                # Add layer preference cost for new layer (Issue #625)
                layer_pref_mult = self._get_layer_preference_cost(new_layer, net_class)

                # Issue #1019: Add via impact cost for blocking unrouted nets
                wx, wy = self.grid.grid_to_world(current.x, current.y)
                via_impact_cost = self._get_via_impact_cost(wx, wy, start.net)

                # Issue #2265: Apply cost_layer_inner when transitioning
                # to an inner layer.  On boards with > 2 layers, indices
                # between 0 (F.Cu) and num_layers-1 (B.Cu) are inner
                # layers and should carry the additional penalty so the
                # pathfinder actually considers them (previously the via
                # cost alone discouraged all layer transitions equally).
                inner_layer_cost = 0.0
                if self.grid.num_layers > 2 and 0 < new_layer < self.grid.num_layers - 1:
                    inner_layer_cost = self.rules.cost_layer_inner

                # Issue #2275: Layer utilization cost for target layer
                layer_util_cost = (
                    self._layer_fill_ratios[new_layer] * self.rules.cost_layer_utilization
                )

                # Issue #2288: Corridor deviation penalty from global routing
                corridor_cost = self.grid.get_corridor_cost(
                    current.x, current.y, new_layer, start.net
                )

                # Issue #2911: Corridor attractor bonus on the target layer.
                # A via that lands inside the reserved corridor is the
                # primary motion this fix is intended to encourage -- without
                # this bonus the pathfinder has zero reason to drop a via
                # onto an empty inner layer that "happens" to be reserved.
                attractor_bonus = self.grid.get_corridor_attractor_bonus(
                    new_layer, current.x, current.y, start.net,
                    self.rules.cost_corridor_attractor,
                )

                # Issue #2325: Cap the total incremental via cost to prevent
                # accumulated additive penalties from making vias prohibitively
                # expensive.  Without the cap, dense boards can accumulate
                # inner-layer, utilization, corridor, congestion, and impact
                # costs that exceed 20x the base movement cost, causing A* to
                # exhaust its iteration budget before ever considering a via.
                via_incremental = (
                    self.rules.cost_via * layer_pref_mult
                    + inner_layer_cost
                    + congestion_cost
                    + negotiated_cost
                    + via_impact_cost
                    + layer_util_cost
                    + corridor_cost
                )
                if self.rules.via_cost_cap_factor > 0.0:
                    via_cap = self.rules.via_cost_cap_factor * self.rules.cost_via
                    via_incremental = min(via_incremental, via_cap)

                # Issue #2911: Apply the attractor AFTER the cap so the
                # bonus is felt even when the via cost is at the ceiling.
                # Clamp at zero so g_scores remain non-negative.
                if attractor_bonus > 0.0:
                    via_incremental = max(0.0, via_incremental - attractor_bonus)

                new_g = (current.g_score + via_incremental) * cost_mult

                if new_g < g_scores_arr[new_layer, current.y, current.x]:
                    g_scores_arr[new_layer, current.y, current.x] = new_g
                    # Via doesn't change direction, use current direction
                    h = self.heuristic.estimate(
                        current.x, current.y, new_layer, current.direction, heuristic_context
                    )
                    f = new_g + weight * h  # Weighted A*

                    neighbor_node = AStarNode(
                        f, new_g, current.x, current.y, new_layer, current, True
                    )
                    heapq.heappush(open_set, neighbor_node)

        # No path found
        return None

    def get_last_failure_info(self) -> dict | None:
        """Return structured failure diagnostics from the most recent failed route().

        Issue #2476: API parity with ``CppPathfinder.get_last_failure_info``.
        The Python pathfinder does not currently expose structured via-blocked
        failure reasons -- callers always receive ``None`` here.  The
        negotiated strategy treats ``None`` as "no actionable diagnostic"
        and falls back to its existing rip-up logic.
        """
        return None

    def find_blocking_nets(
        self,
        start: Pad,
        end: Pad,
        layer: int | None = None,
    ) -> set[int]:
        """Find which nets block the direct path from start to end.

        Uses Bresenham's line algorithm to trace the ideal direct path,
        then identifies which net IDs are blocking cells along that path.
        This is used for targeted rip-up in negotiated routing.

        Issue #2587 / Epic #2556 Phase 1C-cont: When the source net has a
        diff-pair partner (resolvable via :meth:`_resolve_partner_net_id`),
        the partner net is excluded from the blocker set.  The partner's
        copper legitimately sits close to this route at the within-pair
        clearance; treating it as a blocker would trigger spurious rip-up
        of the partner during negotiated routing.  Mirror of the C++
        backend's filter at :meth:`CppPathfinder.find_blocking_nets`.

        Args:
            start: Source pad
            end: Destination pad
            layer: Optional layer index (uses pad layer if not specified)

        Returns:
            Set of net IDs that block the path (excluding net 0, the source
            net, and -- when configured -- the diff-pair partner net).
        """
        blocking_nets: set[int] = set()
        source_net = start.net

        # Issue #2587 / Phase 1C-cont: Resolve the diff-pair partner net id
        # (or -1 when no partner is configured).  Cells belonging to the
        # partner are skipped below so they are not flagged for rip-up.
        partner_net_id = self._resolve_partner_net_id(start.net_name)
        if partner_net_id is None:
            partner_net_id = -1

        # Convert to grid coordinates
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)

        if layer is None:
            layer = self.grid.layer_to_index(start.layer.value)

        # Trace a direct line from start to end using Bresenham's algorithm
        # and collect all blocking nets along the path
        gx1, gy1 = start_gx, start_gy
        gx2, gy2 = end_gx, end_gy

        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy
        gx, gy = gx1, gy1

        while True:
            # Check this cell and nearby cells (accounting for trace width)
            for check_dy in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                for check_dx in range(
                    -self._trace_half_width_cells, self._trace_half_width_cells + 1
                ):
                    cx, cy = gx + check_dx, gy + check_dy
                    if 0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows:
                        cell = self.grid.grid[layer][cy][cx]
                        if (
                            cell.blocked
                            and cell.net != source_net
                            and cell.net != 0
                            and cell.net != partner_net_id
                        ):
                            # This cell is blocked by another net's route
                            # Check usage_count to ensure it's a routed cell, not a static obstacle
                            if cell.usage_count > 0:
                                blocking_nets.add(cell.net)

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return blocking_nets

    def find_blocking_nets_relaxed(
        self,
        start: Pad,
        end: Pad,
        saved_blocked: "np.ndarray",
        saved_net: "np.ndarray",
        per_net_timeout: float | None = None,
    ) -> set[int]:
        """Find blocking nets using relaxed A* (Issue #2274).

        Runs A* with routed-net obstacles temporarily removed (the caller
        is responsible for invoking this inside a
        ``grid.temporarily_unblock_routed_nets()`` context manager).  If a
        path is found, examines the *original* blocked/net arrays to
        determine which routed nets occupy cells along that path.

        This replaces the Bresenham straight-line check for cases where the
        only viable path is not a straight line.

        Args:
            start: Source pad.
            end: Destination pad.
            saved_blocked: The *original* blocked array before unblocking.
            saved_net: The *original* net array before unblocking.
            per_net_timeout: Optional timeout for the relaxed A* search.

        Returns:
            Set of routed-net IDs whose cells lie along the relaxed path.
        """
        # Run normal A* (the grid has routed nets unblocked already)
        route = self.route(
            start,
            end,
            negotiated_mode=True,
            present_cost_factor=0.0,
            per_net_timeout=per_net_timeout,
        )
        if route is None:
            return set()

        blocking: set[int] = set()
        source_net = start.net

        # Walk every cell along every segment of the relaxed path and check
        # the *original* grid state to find which routed nets were there.
        for seg in route.segments:
            gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
            gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
            layer_idx = self.grid.layer_to_index(seg.layer.value)

            # Walk segment cells (Bresenham)
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                # Check this cell and clearance envelope
                for cdy in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                    for cdx in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                        cx, cy = gx + cdx, gy + cdy
                        if 0 <= cx < self.grid.cols and 0 <= cy < self.grid.rows:
                            was_blocked = bool(saved_blocked[layer_idx, cy, cx])
                            orig_net = int(saved_net[layer_idx, cy, cx])
                            if (
                                was_blocked
                                and orig_net != 0
                                and orig_net != source_net
                                and not self.grid._pad_blocked[layer_idx, cy, cx]
                            ):
                                blocking.add(orig_net)

                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        # Also check via locations
        for via in route.vias:
            vgx, vgy = self.grid.world_to_grid(via.x, via.y)
            for layer_idx in range(self.grid.num_layers):
                if 0 <= vgx < self.grid.cols and 0 <= vgy < self.grid.rows:
                    was_blocked = bool(saved_blocked[layer_idx, vgy, vgx])
                    orig_net = int(saved_net[layer_idx, vgy, vgx])
                    if (
                        was_blocked
                        and orig_net != 0
                        and orig_net != source_net
                        and not self.grid._pad_blocked[layer_idx, vgy, vgx]
                    ):
                        blocking.add(orig_net)

        return blocking

    def _convert_path_to_route(
        self,
        path: list[tuple[float, float, int, bool]],
        route: Route,
        start_pad: Pad,
        end_pad: Pad,
    ) -> None:
        """Convert path points to route segments and vias.

        This helper method handles the common logic of converting A* path points
        into Via and Segment objects, adding them to the route. Used by both
        unidirectional and bidirectional route reconstruction.

        Issue #972: Performance optimization - merge collinear segments inline
        during reconstruction instead of creating segment-per-cell and merging
        later. This reduces segment count from thousands to tens per net,
        significantly improving routing performance for large boards.

        Issue #1018: Automatic trace neck-down near fine-pitch pads. When
        min_trace_width is configured, traces taper from normal width to
        minimum width as they approach fine-pitch pads.

        Args:
            path: List of (world_x, world_y, layer_idx, is_via) tuples
            route: Route object to populate with segments and vias
            start_pad: Source pad (determines starting position)
            end_pad: Destination pad (determines final segment endpoint)
        """
        if len(path) < 2:
            return

        # Start from pad center on the A* start node's layer
        # Issue #977: With expanded start regions, the A* may start on a different
        # layer than start_pad.layer (e.g., when allowed_layers constrains routing).
        # Use the layer from the first path node, not start_pad.layer.
        # current_layer_idx is a grid index (0, 1, ...), not Layer enum value
        current_layer_idx = path[0][2]  # Layer from first A* node

        # Issue #972: Inline segment merging - track segment start point and direction
        # to merge collinear cells into single segments
        seg_start_x, seg_start_y = start_pad.x, start_pad.y
        current_x, current_y = seg_start_x, seg_start_y
        current_direction: tuple[float, float] | None = None  # (dx_normalized, dy_normalized)

        # Issue #1018: Get pin pitches for neck-down calculation
        start_pitch = self.component_pitches.get(start_pad.ref) if start_pad.ref else None
        end_pitch = self.component_pitches.get(end_pad.ref) if end_pad.ref else None

        # Determine if neck-down applies for each pad
        start_needs_neckdown = self.rules.should_apply_neck_down(start_pad.ref, start_pitch)
        end_needs_neckdown = self.rules.should_apply_neck_down(end_pad.ref, end_pitch)

        # Issue #1543: Use net-class-aware trace width as the base width.
        # Look up the net class for this net and use its trace_width if defined,
        # falling back to the global rules.trace_width for unclassified nets.
        base_trace_width = self._get_trace_width_for_net(start_pad.net_name)

        # Issue #1692: Use per-net-class via size when creating vias.
        _nc = self._get_net_class(start_pad.net_name)
        net_via_diameter = _nc.via_size if _nc else self.rules.via_diameter

        def _normalize_direction(dx: float, dy: float) -> tuple[float, float] | None:
            """Normalize direction vector, return None if no movement."""
            length = (dx * dx + dy * dy) ** 0.5
            if length < 0.001:
                return None
            return (dx / length, dy / length)

        def _same_direction(d1: tuple[float, float] | None, d2: tuple[float, float] | None) -> bool:
            """Check if two directions are the same (within tolerance)."""
            if d1 is None or d2 is None:
                return False
            # Check if normalized directions match (collinear)
            return abs(d1[0] - d2[0]) < 0.01 and abs(d1[1] - d2[1]) < 0.01

        def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
            """Calculate Euclidean distance between two points."""
            return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

        def _calculate_segment_width(x1: float, y1: float, x2: float, y2: float) -> float:
            """Calculate trace width for a segment based on net class and distance to pads.

            Issue #1543: Uses net-class-aware base width (e.g., POWER=0.5mm)
            instead of the global rules.trace_width.

            Issue #1018: For segments near fine-pitch pads, the width tapers
            from the base trace width to minimum trace width. The width is
            determined by the minimum distance from the segment endpoints
            to either pad that needs neck-down.
            """
            # If no neck-down needed at either end, use net-class base width
            if not start_needs_neckdown and not end_needs_neckdown:
                return base_trace_width

            # Calculate distances from segment endpoints to pads
            min_width = base_trace_width

            # Check start pad influence
            if start_needs_neckdown:
                dist_to_start_1 = _distance(x1, y1, start_pad.x, start_pad.y)
                dist_to_start_2 = _distance(x2, y2, start_pad.x, start_pad.y)
                # Use minimum distance from either endpoint
                min_dist_start = min(dist_to_start_1, dist_to_start_2)
                width_from_start = self.rules.get_neck_down_width(min_dist_start, start_pitch)
                min_width = min(min_width, width_from_start)

            # Check end pad influence
            if end_needs_neckdown:
                dist_to_end_1 = _distance(x1, y1, end_pad.x, end_pad.y)
                dist_to_end_2 = _distance(x2, y2, end_pad.x, end_pad.y)
                # Use minimum distance from either endpoint
                min_dist_end = min(dist_to_end_1, dist_to_end_2)
                width_from_end = self.rules.get_neck_down_width(min_dist_end, end_pitch)
                min_width = min(min_width, width_from_end)

            return min_width

        def _emit_segment(x1: float, y1: float, x2: float, y2: float, layer_idx: int) -> None:
            """Create and add a segment if there's meaningful distance."""
            if abs(x2 - x1) > 0.01 or abs(y2 - y1) > 0.01:
                # Issue #1018: Calculate width with neck-down support
                width = _calculate_segment_width(x1, y1, x2, y2)
                seg = Segment(
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=width,
                    layer=Layer(self.grid.index_to_layer(layer_idx)),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.segments.append(seg)

        for _i, (wx, wy, layer_idx, is_via) in enumerate(path):
            if is_via:
                # Emit pending segment before via
                _emit_segment(seg_start_x, seg_start_y, current_x, current_y, current_layer_idx)

                # Add via - convert grid indices back to Layer enum values
                via = Via(
                    x=current_x,
                    y=current_y,
                    drill=self.rules.via_drill,
                    diameter=net_via_diameter,
                    layers=(
                        Layer(self.grid.index_to_layer(current_layer_idx)),
                        Layer(self.grid.index_to_layer(layer_idx)),
                    ),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.vias.append(via)
                current_layer_idx = layer_idx

                # Reset segment tracking after via
                seg_start_x, seg_start_y = current_x, current_y
                current_direction = None
            else:
                # Check if we've moved
                dx = wx - current_x
                dy = wy - current_y
                new_direction = _normalize_direction(dx, dy)

                if new_direction is not None:
                    # Direction changed - emit current segment and start new one
                    if not _same_direction(current_direction, new_direction):
                        _emit_segment(
                            seg_start_x, seg_start_y, current_x, current_y, current_layer_idx
                        )
                        seg_start_x, seg_start_y = current_x, current_y
                        current_direction = new_direction

                    current_x, current_y = wx, wy
                    current_layer_idx = layer_idx

        # Emit final segment of the path
        _emit_segment(seg_start_x, seg_start_y, current_x, current_y, current_layer_idx)

        # Final segment to end pad (if needed)
        dx = end_pad.x - current_x
        dy = end_pad.y - current_y
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            # Check if final segment is collinear with last emitted segment
            if route.segments:
                last_seg = route.segments[-1]
                last_dx = last_seg.x2 - last_seg.x1
                last_dy = last_seg.y2 - last_seg.y1
                last_dir = _normalize_direction(last_dx, last_dy)
                end_dir = _normalize_direction(dx, dy)

                if _same_direction(last_dir, end_dir):
                    # Extend last segment to end pad
                    # Issue #1018: Recalculate width for the extended segment
                    extended_width = _calculate_segment_width(
                        last_seg.x1, last_seg.y1, end_pad.x, end_pad.y
                    )
                    route.segments[-1] = Segment(
                        x1=last_seg.x1,
                        y1=last_seg.y1,
                        x2=end_pad.x,
                        y2=end_pad.y,
                        width=extended_width,
                        layer=last_seg.layer,
                        net=start_pad.net,
                        net_name=start_pad.net_name,
                    )
                else:
                    _emit_segment(current_x, current_y, end_pad.x, end_pad.y, current_layer_idx)
            else:
                _emit_segment(current_x, current_y, end_pad.x, end_pad.y, current_layer_idx)

    def _validate_route_clearance(
        self,
        route: Route,
        exclude_net: int,
        component_pitches: dict[str, float] | None = None,
        exclude_refs: set[str] | None = None,
    ) -> bool:
        """Validate route segments and vias against geometric clearance constraints.

        Issue #750: Grid-based A* checking is approximate; diagonal segments can
        cut through obstacle corners. This method validates actual geometry to
        catch clearance violations that grid-based checking missed.

        Issue #1016: Now supports per-component clearance validation via
        component_pitches dict for automatic fine-pitch detection.

        Issue #1667: Now also validates vias against other-net segments to catch
        seg-via clearance violations where a via's annular ring is too close to
        an existing trace.

        Args:
            route: Route to validate
            exclude_net: Net ID to exclude from clearance checks (the route's own net)
            component_pitches: Optional dict mapping component ref to pin pitch in mm
            exclude_refs: Optional set of component references whose pads should be
                         excluded from clearance checks (Issue #1764).

        Returns:
            True if route passes clearance validation, False otherwise.
        """
        for seg in route.segments:
            is_valid, _clearance, _location = self.grid.validate_segment_clearance(
                seg, exclude_net=exclude_net, component_pitches=component_pitches,
                exclude_refs=exclude_refs
            )
            if not is_valid:
                return False

            # Issue #3002: Also validate each segment against the
            # router-level foreign-via context populated by
            # :meth:`Autorouter._update_router_segment_foreign_context`.
            # ``grid.validate_segment_clearance`` already walks
            # ``self.grid.routes`` vias, but the foreign-context list
            # can include vias the negotiated re-validation hook has
            # flagged that may not yet be present in ``grid.routes``
            # (or may have just been added by the current iteration).
            # The STANDARD threshold (hard_intersection_only=False)
            # mirrors the main-router commit policy described in
            # :meth:`set_segment_foreign_context`.
            if self._foreign_vias:
                for via in self._foreign_vias:
                    if via.net == exclude_net:
                        continue  # Same-net via -- skipped by convention.
                    if not segment_clears_foreign_via(
                        seg, via,
                        trace_clearance=self.rules.trace_clearance,
                        hard_intersection_only=False,
                    ):
                        return False

        # Issue #1667: Validate vias against other-net segments
        for via in route.vias:
            is_valid, _clearance, _location = self.grid.validate_via_clearance(
                via, exclude_net=exclude_net
            )
            if not is_valid:
                return False

        # Issue #1693: Validate vias against other-net vias
        for via in route.vias:
            is_valid, _clearance, _location = self.grid.validate_via_to_via_clearance(
                via, exclude_net=exclude_net
            )
            if not is_valid:
                return False

        # Issue #1782: Validate same-net drill-to-drill spacing
        for via in route.vias:
            is_valid, _clearance, _location = self.grid.validate_same_net_drill_spacing(
                via, same_net=exclude_net
            )
            if not is_valid:
                return False

        return True

    def _merge_same_net_vias(self, route: Route) -> None:
        """Merge vias in a new route that overlap with existing same-net vias.

        Issue #1782: When routing multi-pad nets via MST edges, independent
        sub-routes can place vias at nearby positions for the same net. If
        the drill-to-drill distance is below the merge threshold
        (via_diameter + min_drill_clearance), merge by keeping the midpoint
        and updating connected segment endpoints.

        Args:
            route: The route to merge vias in (modified in place).
        """
        import math

        merge_threshold = self.rules.via_diameter + self.rules.min_drill_clearance

        # Collect all existing same-net vias from already-routed routes
        existing_vias: list[Via] = []
        for existing_route in self.grid.routes:
            if existing_route.net == route.net:
                existing_vias.extend(existing_route.vias)

        if not existing_vias:
            return

        # Track which vias in the new route need merging
        vias_to_remove: set[int] = set()

        for i, new_via in enumerate(route.vias):
            for existing_via in existing_vias:
                distance = math.sqrt(
                    (new_via.x - existing_via.x) ** 2
                    + (new_via.y - existing_via.y) ** 2
                )
                if distance < merge_threshold and distance > 1e-6:
                    # Merge: move the new via to the existing via's position
                    # and update any segments that reference the old position
                    old_x, old_y = new_via.x, new_via.y
                    mid_x = existing_via.x
                    mid_y = existing_via.y

                    # Update segments that connect to this via
                    for seg in route.segments:
                        if abs(seg.x1 - old_x) < 1e-6 and abs(seg.y1 - old_y) < 1e-6:
                            seg.x1 = mid_x
                            seg.y1 = mid_y
                        if abs(seg.x2 - old_x) < 1e-6 and abs(seg.y2 - old_y) < 1e-6:
                            seg.x2 = mid_x
                            seg.y2 = mid_y

                    # Issue #1802: expand surviving via layers to cover
                    # all layers from both vias (cross-layer-pair merge)
                    min_layer = min(
                        existing_via.layers[0].value,
                        existing_via.layers[1].value,
                        new_via.layers[0].value,
                        new_via.layers[1].value,
                    )
                    max_layer = max(
                        existing_via.layers[0].value,
                        existing_via.layers[1].value,
                        new_via.layers[0].value,
                        new_via.layers[1].value,
                    )
                    if (min_layer != existing_via.layers[0].value
                            or max_layer != existing_via.layers[1].value):
                        existing_via.layers = (Layer(min_layer), Layer(max_layer))

                    # Remove the new via since we're reusing the existing one
                    vias_to_remove.add(i)
                    break

        # Remove merged vias (iterate in reverse to keep indices valid)
        for idx in sorted(vias_to_remove, reverse=True):
            route.vias.pop(idx)

    def _reconstruct_route(self, end_node: AStarNode, start_pad: Pad, end_pad: Pad) -> Route | None:
        """Reconstruct the route from A* result with geometric validation.

        Issue #750: After reconstructing the route from grid coordinates,
        validates each segment against original obstacle geometry to catch
        clearance violations that grid-based checking missed (particularly
        for diagonal segments that can cut through obstacle corners).

        Returns:
            Route if valid, None if geometric clearance validation fails.
        """
        route = Route(net=start_pad.net, net_name=start_pad.net_name)

        # Collect path points
        path: list[tuple[float, float, int, bool]] = []
        node: AStarNode | None = end_node
        while node:
            wx, wy = self.grid.grid_to_world(node.x, node.y)
            path.append((wx, wy, node.layer, node.via_from_parent))
            node = node.parent

        path.reverse()

        # Convert path to segments and vias
        self._convert_path_to_route(path, route, start_pad, end_pad)

        # Issue #2934: Reject Route objects with no segments.
        # ``_convert_path_to_route`` silently no-ops when ``len(path) < 2``.
        # This happens when the A* search terminates on the start cell itself,
        # which Issue #2306's incremental-Steiner early-termination can do when
        # ``extra_goal_cells`` already contains the start cell (e.g., a later
        # RSMT edge whose start pad shares a grid cell with the previously
        # routed tree).  The resulting ``Route(segments=[], vias=[])`` is
        # geometrically empty and provides no connectivity, but callers using
        # ``if route:`` (a truthy check) silently accept it and the connectivity
        # validator later reports the pad as un-routed.  Return ``None`` so the
        # caller falls back to its failure branch (e.g., recording the failure
        # in ``routing_failures`` and firing ``failure_callback``).
        if not route.segments and not route.vias:
            return None

        # Issue #1782: Merge vias that overlap with existing same-net vias
        self._merge_same_net_vias(route)

        # Validate layer transitions and insert any missing vias
        route.validate_layer_transitions(
            via_drill=self.rules.via_drill,
            via_diameter=self.rules.via_diameter,
        )

        # Geometric clearance validation (Issue #1016: per-component clearance support)
        # Issue #1764: Exclude pads on the same component as start/end pads from
        # clearance checks. Adjacent pads (especially net=0 unconnected pads) on the
        # same component should not block routing to their neighbors.
        exclude_refs: set[str] = set()
        if start_pad.ref:
            exclude_refs.add(start_pad.ref)
        if end_pad.ref:
            exclude_refs.add(end_pad.ref)
        if not self._validate_route_clearance(
            route, start_pad.net, component_pitches=self.component_pitches,
            exclude_refs=exclude_refs if exclude_refs else None
        ):
            # Route has clearance violations - reject it
            # The caller will report "no path found" which is preferable
            # to returning a route with DRC violations
            return None

        return route

    def route_bidirectional(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
    ) -> Route | None:
        """Route between two pads using bidirectional A* search.

        Bidirectional A* runs two simultaneous searches: one from start toward
        end, and one from end toward start. When the frontiers meet, the path
        is reconstructed by combining both directions.

        This can significantly reduce the search space for large paths, as
        the searches meet in the middle rather than one having to traverse
        the entire distance.

        Performance benefits (Issue #964):
        - For paths with N nodes, unidirectional searches O(N)
        - Bidirectional searches O(√N) in best case
        - Typically 50-75% speedup for paths >5000 nodes

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: If True, allow sharing resources with cost penalty
            present_cost_factor: Multiplier for current sharing penalty
            weight: A* weight factor (1.0 = optimal, >1.0 = faster but suboptimal)

        Returns:
            Route if path found, None otherwise
        """
        # Issue #966: Clear via cache at start of route (grid state may have changed)
        self.clear_via_cache()

        # Issue #2330: Reset waypoint state for this route call
        self._waypoint_world_coords.clear()
        self._waypoint_id_counter = 0

        # Get net class if not provided
        if net_class is None:
            net_class = self._get_net_class(start.net_name)

        cost_mult = net_class.cost_multiplier if net_class else 1.0
        allow_sharing = negotiated_mode

        # Issue #1692: Compute per-net trace clearance radius for A* search,
        # matching the logic in route() (lines 1156-1170).  Without this,
        # bidirectional A* falls back to the global _trace_half_width_cells
        # which under-reserves space for wider net classes (e.g. POWER).
        net_trace_width = net_class.trace_width if net_class else self.rules.trace_width
        net_trace_clearance = net_class.clearance if net_class else self.rules.trace_clearance
        net_trace_half_width_cells = max(
            1,
            math.ceil(
                round(
                    (net_trace_width / 2 + net_trace_clearance) / self.grid.resolution,
                    6,
                )
            ),
        )

        # Issue #2559 / Epic #2556 Phase 1C: Resolve diff-pair partner for
        # within-pair clearance threading -- mirrors the logic in route().
        # The bidirectional search expands neighbors via
        # ``_expand_bidirectional_neighbors``, which now accepts a
        # ``partner_net``/``partner_radius`` pair to forward into the
        # ``_is_trace_blocked`` check.
        partner_net_id = self._resolve_partner_net_id(start.net_name)
        if partner_net_id is not None and net_class is not None:
            net_intra_pair_clearance = net_class.effective_intra_pair_clearance()
            net_partner_half_width_cells = max(
                1,
                math.ceil(
                    round(
                        (net_trace_width / 2 + net_intra_pair_clearance) / self.grid.resolution,
                        6,
                    )
                ),
            )
        else:
            net_partner_half_width_cells = net_trace_half_width_cells

        # Issue #2715: Pre-compute the partner-active flag ONCE per route so
        # the hot-path ``_is_trace_blocked`` call (per A* neighbor) skips
        # the 4-condition tuple evaluation.  Mirrors the same expression
        # that lives in ``_is_trace_blocked`` and ``compute_expanded_blocked``.
        partner_active_flag = (
            partner_net_id is not None
            and partner_net_id >= 0
            and net_partner_half_width_cells < net_trace_half_width_cells
        )

        # Issue #1692: Compute per-net via clearance radius.
        net_via_size = net_class.via_size if net_class else self.rules.via_diameter
        net_via_half_cells = max(
            1,
            math.ceil(
                round(
                    (net_via_size / 2 + self.rules.via_clearance) / self.grid.resolution,
                    6,
                )
            ),
        )

        # Convert to grid coordinates
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)

        # Get valid layers for each pad
        routable_layers = self.grid.get_routable_indices()
        start_layer = self.grid.layer_to_index(start.layer.value)
        end_layer = self.grid.layer_to_index(end.layer.value)
        start_layers = routable_layers if start.through_hole else [start_layer]
        end_layers = routable_layers if end.through_hole else [end_layer]

        # Apply layer constraints
        if self.rules.allowed_layers is not None:
            start_layers = [l for l in start_layers if self._is_layer_allowed(l)]
            end_layers = [l for l in end_layers if self._is_layer_allowed(l)]
            if not start_layers or not end_layers:
                return None

        # Get pad metal bounds for goal checking (Issue #956)
        start_metal_bounds = self._get_pad_metal_bounds(start)
        end_metal_bounds = self._get_pad_metal_bounds(end)

        # Heuristic contexts for both directions
        forward_context = HeuristicContext(
            goal_x=end_gx,
            goal_y=end_gy,
            goal_layer=end_layers[0] if end_layers else end_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            diagonal_routing=self.diagonal_routing,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )
        backward_context = HeuristicContext(
            goal_x=start_gx,
            goal_y=start_gy,
            goal_layer=start_layers[0] if start_layers else start_layer,
            rules=self.rules,
            cost_multiplier=cost_mult,
            diagonal_routing=self.diagonal_routing,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=self._get_congestion_cost,
        )

        # Initialize forward search (start -> end)
        # Issue #977: Initialize from ALL cells within start pad's metal area
        forward_open: list[AStarNode] = []
        forward_closed: set[tuple[int, int, int]] = set()
        forward_g: dict[tuple[int, int, int], float] = {}
        forward_nodes: dict[tuple[int, int, int], AStarNode] = {}

        for sgx in range(start_metal_bounds[0], start_metal_bounds[2] + 1):
            for sgy in range(start_metal_bounds[1], start_metal_bounds[3] + 1):
                for sl in start_layers:
                    h = self.heuristic.estimate(sgx, sgy, sl, (0, 0), forward_context)
                    node = AStarNode(h, 0, sgx, sgy, sl)
                    heapq.heappush(forward_open, node)
                    key = (sgx, sgy, sl)
                    forward_g[key] = 0
                    forward_nodes[key] = node

        # Issue #2330: Waypoint injection for off-grid start pad (bidirectional)
        if self._is_pad_off_grid(start):
            bidir_start_wp = self._create_waypoint(start)
            for gx, gy, edge_cost in self._waypoint_grid_edges(
                bidir_start_wp, start, start.net, allow_sharing,
            ):
                for sl in start_layers:
                    wp_g = edge_cost * self.rules.cost_straight
                    wp_h = self.heuristic.estimate(gx, gy, sl, (0, 0), forward_context)
                    wp_node = AStarNode(wp_g + weight * wp_h, wp_g, gx, gy, sl)
                    fkey = (gx, gy, sl)
                    if fkey not in forward_g or wp_g < forward_g[fkey]:
                        forward_g[fkey] = wp_g
                        forward_nodes[fkey] = wp_node
                        heapq.heappush(forward_open, wp_node)

        # Initialize backward search (end -> start)
        # Issue #977: Initialize from ALL cells within end pad's metal area
        backward_open: list[AStarNode] = []
        backward_closed: set[tuple[int, int, int]] = set()
        backward_g: dict[tuple[int, int, int], float] = {}
        backward_nodes: dict[tuple[int, int, int], AStarNode] = {}

        for egx in range(end_metal_bounds[0], end_metal_bounds[2] + 1):
            for egy in range(end_metal_bounds[1], end_metal_bounds[3] + 1):
                for el in end_layers:
                    h = self.heuristic.estimate(egx, egy, el, (0, 0), backward_context)
                    node = AStarNode(h, 0, egx, egy, el)
                    heapq.heappush(backward_open, node)
                    key = (egx, egy, el)
                    backward_g[key] = 0
                    backward_nodes[key] = node

        # Issue #2330: Waypoint injection for off-grid end pad (bidirectional)
        if self._is_pad_off_grid(end):
            bidir_end_wp = self._create_waypoint(end)
            for gx, gy, edge_cost in self._waypoint_grid_edges(
                bidir_end_wp, end, end.net, allow_sharing,
            ):
                for el in end_layers:
                    wp_g = edge_cost * self.rules.cost_straight
                    wp_h = self.heuristic.estimate(gx, gy, el, (0, 0), backward_context)
                    wp_node = AStarNode(wp_g + weight * wp_h, wp_g, gx, gy, el)
                    bkey = (gx, gy, el)
                    if bkey not in backward_g or wp_g < backward_g[bkey]:
                        backward_g[bkey] = wp_g
                        backward_nodes[bkey] = wp_node
                        heapq.heappush(backward_open, wp_node)

        # Issue #2974: Escape-hint seeds (bidirectional).  Same
        # contract as the forward-only path above: each seed cell
        # carries the straight-line edge cost from its pad and the
        # ``escape_dir`` as its incoming direction.
        forward_escape = self._detect_escape_hint(start, start_layers)
        if forward_escape is not None:
            for cx, cy, cl, edge_cost in self._escape_hint_cells(
                start, forward_escape, start.net, start_layers
            ):
                seed_g = edge_cost * self.rules.cost_straight
                seed_h = self.heuristic.estimate(cx, cy, cl, forward_escape, forward_context)
                fkey = (cx, cy, cl)
                if fkey not in forward_g or seed_g < forward_g[fkey]:
                    seed_node = AStarNode(
                        seed_g + weight * seed_h, seed_g, cx, cy, cl,
                        direction=forward_escape,
                    )
                    forward_g[fkey] = seed_g
                    forward_nodes[fkey] = seed_node
                    heapq.heappush(forward_open, seed_node)

        backward_escape = self._detect_escape_hint(end, end_layers)
        if backward_escape is not None:
            for cx, cy, cl, edge_cost in self._escape_hint_cells(
                end, backward_escape, end.net, end_layers
            ):
                seed_g = edge_cost * self.rules.cost_straight
                seed_h = self.heuristic.estimate(cx, cy, cl, backward_escape, backward_context)
                bkey = (cx, cy, cl)
                if bkey not in backward_g or seed_g < backward_g[bkey]:
                    seed_node = AStarNode(
                        seed_g + weight * seed_h, seed_g, cx, cy, cl,
                        direction=backward_escape,
                    )
                    backward_g[bkey] = seed_g
                    backward_nodes[bkey] = seed_node
                    heapq.heappush(backward_open, seed_node)

        # Best meeting point tracking
        best_path_cost = float("inf")
        meeting_point: tuple[int, int, int] | None = None

        iterations = 0
        max_iterations = self.grid.cols * self.grid.rows * 4

        while (forward_open or backward_open) and iterations < max_iterations:
            iterations += 1

            # Alternate between forward and backward search
            # Process forward step
            if forward_open:
                forward_node = heapq.heappop(forward_open)
                fkey = (forward_node.x, forward_node.y, forward_node.layer)

                if fkey not in forward_closed:
                    forward_closed.add(fkey)
                    forward_nodes[fkey] = forward_node

                    # Check if backward search has reached this point
                    if fkey in backward_closed:
                        total_cost = forward_node.g_score + backward_g.get(fkey, float("inf"))
                        if total_cost < best_path_cost:
                            best_path_cost = total_cost
                            meeting_point = fkey

                    # Expand forward neighbors
                    self._expand_bidirectional_neighbors(
                        forward_node,
                        forward_open,
                        forward_closed,
                        forward_g,
                        forward_nodes,
                        forward_context,
                        start,
                        start_layers,
                        end_layers,
                        start_metal_bounds,  # Issue #990: source metal bounds
                        end_metal_bounds,
                        allow_sharing,
                        cost_mult,
                        weight,
                        trace_radius=net_trace_half_width_cells,
                        via_radius=net_via_half_cells,
                        partner_net=partner_net_id,
                        partner_radius=net_partner_half_width_cells,
                        partner_active=partner_active_flag,
                    )

            # Process backward step
            if backward_open:
                backward_node = heapq.heappop(backward_open)
                bkey = (backward_node.x, backward_node.y, backward_node.layer)

                if bkey not in backward_closed:
                    backward_closed.add(bkey)
                    backward_nodes[bkey] = backward_node

                    # Check if forward search has reached this point
                    if bkey in forward_closed:
                        total_cost = backward_node.g_score + forward_g.get(bkey, float("inf"))
                        if total_cost < best_path_cost:
                            best_path_cost = total_cost
                            meeting_point = bkey

                    # Expand backward neighbors
                    self._expand_bidirectional_neighbors(
                        backward_node,
                        backward_open,
                        backward_closed,
                        backward_g,
                        backward_nodes,
                        backward_context,
                        end,  # Backward search uses end pad as "start"
                        end_layers,
                        start_layers,
                        end_metal_bounds,  # Issue #990: source metal bounds
                        start_metal_bounds,
                        allow_sharing,
                        cost_mult,
                        weight,
                        trace_radius=net_trace_half_width_cells,
                        via_radius=net_via_half_cells,
                        partner_net=partner_net_id,
                        partner_radius=net_partner_half_width_cells,
                        partner_active=partner_active_flag,
                    )

            # Early termination: if we have a meeting point and both queues
            # have higher f-scores than the best path, we're done
            if meeting_point is not None:
                min_forward_f = forward_open[0].f_score if forward_open else float("inf")
                min_backward_f = backward_open[0].f_score if backward_open else float("inf")
                if min_forward_f >= best_path_cost and min_backward_f >= best_path_cost:
                    break

        # Reconstruct path if meeting point found
        if meeting_point is not None:
            return self._reconstruct_bidirectional_route(
                meeting_point,
                forward_nodes,
                backward_nodes,
                start,
                end,
            )

        return None

    def _expand_bidirectional_neighbors(
        self,
        current: AStarNode,
        open_set: list[AStarNode],
        closed_set: set[tuple[int, int, int]],
        g_scores: dict[tuple[int, int, int], float],
        nodes: dict[tuple[int, int, int], AStarNode],
        heuristic_context: HeuristicContext,
        source_pad: Pad,
        source_layers: list[int],
        target_layers: list[int],
        source_metal_bounds: tuple[int, int, int, int],
        target_metal_bounds: tuple[int, int, int, int],
        allow_sharing: bool,
        cost_mult: float,
        weight: float,
        trace_radius: int | None = None,
        via_radius: int | None = None,
        partner_net: int | None = None,
        partner_radius: int | None = None,
        partner_active: bool | None = None,
    ) -> None:
        """Expand neighbors for bidirectional A* search.

        This is a helper method that expands neighbors for either the forward
        or backward search direction. It handles 2D moves and via transitions.

        Args:
            trace_radius: Per-net-class trace half-width in grid cells.
                When None, falls back to the global ``_trace_half_width_cells``
                (Issue #1692).
            via_radius: Per-net-class via half-width in grid cells.
                When None, falls back to the global ``_via_half_cells``
                (Issue #1692).
            partner_net: Issue #2559 / Phase 1C -- diff-pair partner net id.
                When set, ``_is_trace_blocked`` calls below pass this id and
                ``partner_radius`` so the partner cells are treated as
                blockers only within the tighter intra-pair radius.
            partner_radius: Tighter half-width for partner cells.
            partner_active: Issue #2715 -- pre-computed dormant/active flag
                for the partner branch.  Forwarded to ``_is_trace_blocked``
                so the per-call 4-condition tuple evaluation is skipped on
                the hot path.
        """
        # Extract bounds (Issue #990: also need source bounds for pad exit check)
        src_gx1, src_gy1, src_gx2, src_gy2 = source_metal_bounds
        tgt_gx1, tgt_gy1, tgt_gx2, tgt_gy2 = target_metal_bounds
        source_gx, source_gy = self.grid.world_to_grid(source_pad.x, source_pad.y)

        # Issue #1618: Precompute geometry-derived pad approach bounds
        pad_escape_margin = 2  # cells beyond pad metal edge
        src_approach_gx1 = src_gx1 - pad_escape_margin
        src_approach_gy1 = src_gy1 - pad_escape_margin
        src_approach_gx2 = src_gx2 + pad_escape_margin
        src_approach_gy2 = src_gy2 + pad_escape_margin
        tgt_approach_gx1 = tgt_gx1 - pad_escape_margin
        tgt_approach_gy1 = tgt_gy1 - pad_escape_margin
        tgt_approach_gx2 = tgt_gx2 + pad_escape_margin
        tgt_approach_gy2 = tgt_gy2 + pad_escape_margin

        # Explore 2D neighbors (same layer moves)
        for dx, dy, _dlayer, neighbor_cost_mult in self.neighbors_2d:
            nx, ny = current.x + dx, current.y + dy
            nlayer = current.layer

            # Check bounds
            if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                continue

            # Check diagonal corner blocking
            if dx != 0 and dy != 0:
                if self._is_diagonal_corner_blocked(
                    current.x, current.y, dx, dy, nlayer, source_pad.net, allow_sharing
                ):
                    continue

            # Issue #1618: Use geometry-derived approach bounds instead of
            # hardcoded pad_approach_radius=6. The approach zone covers the pad
            # metal area plus a small escape margin.
            is_source_adjacent = (
                src_approach_gx1 <= nx <= src_approach_gx2
                and src_approach_gy1 <= ny <= src_approach_gy2
                and nlayer in source_layers
            )
            is_target_adjacent = (
                tgt_approach_gx1 <= nx <= tgt_approach_gx2
                and tgt_approach_gy1 <= ny <= tgt_approach_gy2
                and nlayer in target_layers
            )

            # Issue #990: Check if CURRENT node is within a pad's metal area
            # When entire metal area is blocked by clearance zones, allow first step out
            is_exiting_source_pad = (
                src_gx1 <= current.x <= src_gx2
                and src_gy1 <= current.y <= src_gy2
                and current.layer in source_layers
            )
            is_exiting_target_pad = (
                tgt_gx1 <= current.x <= tgt_gx2
                and tgt_gy1 <= current.y <= tgt_gy2
                and current.layer in target_layers
            )

            # Check blocking
            cell = self.grid.grid[nlayer][ny][nx]
            if cell.blocked:
                if cell.net == source_pad.net:
                    pass  # Same net - passable
                elif cell.net == 0:
                    if self._is_trace_blocked(nx, ny, nlayer, source_pad.net, allow_sharing,
                                              radius=trace_radius,
                                              partner_net=partner_net,
                                              partner_radius=partner_radius,
                                              partner_active=partner_active):
                        continue
                else:
                    # Different net's blocked cell
                    # Issue #996: When exiting a pad's metal area, allow entering
                    # clearance zones (not actual pad copper). This enables sub-grid
                    # pad connections where the nearest grid cells are within another
                    # net's clearance zone but not its copper.
                    is_clearance_only = not cell.pad_blocked
                    is_pad_exit = is_exiting_source_pad or is_exiting_target_pad
                    if is_clearance_only and is_pad_exit:
                        # Clearance zone cell while exiting pad - allow this move
                        pass
                    else:
                        continue  # Actual pad copper or not exiting a pad - block
            else:
                # Issue #990: Relax blocking check when exiting from pad metal area
                is_pad_exit_or_approach = (
                    is_source_adjacent
                    or is_target_adjacent
                    or is_exiting_source_pad
                    or is_exiting_target_pad
                )
                if not is_pad_exit_or_approach:
                    if self._is_trace_blocked(nx, ny, nlayer, source_pad.net, allow_sharing,
                                              radius=trace_radius,
                                              partner_net=partner_net,
                                              partner_radius=partner_radius,
                                              partner_active=partner_active):
                        continue

            # Check zone blocking
            if self._is_zone_blocked(nx, ny, nlayer, source_pad.net):
                continue

            neighbor_key = (nx, ny, nlayer)
            if neighbor_key in closed_set:
                continue

            # Calculate cost
            new_direction = (dx, dy)
            turn_cost = 0.0
            if current.direction != (0, 0) and current.direction != new_direction:
                turn_cost = self.rules.cost_turn

            congestion_cost = self._get_congestion_cost(nx, ny, nlayer)
            negotiated_cost = 0.0
            if allow_sharing and not (is_source_adjacent or is_target_adjacent):
                negotiated_cost = self._get_negotiated_cell_cost(
                    nx, ny, nlayer, 1.0, net=source_pad.net
                )

            zone_cost = self._get_zone_cost(nx, ny, nlayer, source_pad.net)
            net_class = self._get_net_class(source_pad.net_name)
            layer_pref_mult = self._get_layer_preference_cost(nlayer, net_class)

            # Issue #1250: Crossing penalty for edges crossing routed segments
            crossing_cost = 0.0
            if self.rules.crossing_penalty > 0.0 and self._routed_segments:
                num_crossings = self._count_edge_crossings(
                    current.x, current.y, nx, ny, nlayer, source_pad.net
                )
                crossing_cost = self.rules.crossing_penalty * num_crossings

            # Issue #2275: Layer utilization cost
            layer_util_cost = (
                self._layer_fill_ratios[nlayer] * self.rules.cost_layer_utilization
            )

            # Issue #2288: Corridor deviation penalty from global routing
            corridor_cost = self.grid.get_corridor_cost(nx, ny, nlayer, source_pad.net)

            # Issue #2911: Diff-pair / match-group corridor attractor (see
            # forward A* expansion for full rationale).
            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                nlayer, nx, ny, source_pad.net, self.rules.cost_corridor_attractor,
            )

            positive_step_cost = (
                neighbor_cost_mult * self.rules.cost_straight * layer_pref_mult
                + turn_cost
                + congestion_cost
                + negotiated_cost
                + zone_cost
                + crossing_cost
                + layer_util_cost
                + corridor_cost
            )
            if attractor_bonus > 0.0:
                positive_step_cost = max(0.0, positive_step_cost - attractor_bonus)

            new_g = (current.g_score + positive_step_cost) * cost_mult

            if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                g_scores[neighbor_key] = new_g
                h = self.heuristic.estimate(nx, ny, nlayer, new_direction, heuristic_context)
                f = new_g + weight * h

                neighbor_node = AStarNode(f, new_g, nx, ny, nlayer, current, False, new_direction)
                heapq.heappush(open_set, neighbor_node)
                nodes[neighbor_key] = neighbor_node

        # Try layer changes (vias)
        for new_layer in self.grid.get_routable_indices():
            if new_layer == current.layer:
                continue

            if not self._is_layer_allowed(new_layer):
                continue

            # Check via blocking on all layers
            # Issue #966: Use cached via check with layer priority ordering
            # Issue #1692: Pass per-net via radius for wider net classes
            self._via_diag_attempts += 1
            if not self._check_via_placement_cached(
                current.x, current.y, source_pad.net, allow_sharing,
                radius=via_radius,
            ):
                self._via_diag_blocked += 1
                continue

            if not self._can_place_via_in_zones(current.x, current.y, source_pad.net):
                self._via_diag_zone_blocked += 1
                continue

            self._via_diag_eligible += 1

            neighbor_key = (current.x, current.y, new_layer)
            if neighbor_key in closed_set:
                continue

            congestion_cost = self._get_congestion_cost(current.x, current.y, new_layer)
            negotiated_cost = 0.0
            if allow_sharing:
                negotiated_cost = self._get_negotiated_cell_cost(
                    current.x, current.y, new_layer, 1.0, net=source_pad.net
                )

            net_class = self._get_net_class(source_pad.net_name)
            layer_pref_mult = self._get_layer_preference_cost(new_layer, net_class)

            # Issue #2275: Layer utilization cost for target layer
            layer_util_cost = (
                self._layer_fill_ratios[new_layer] * self.rules.cost_layer_utilization
            )

            # Issue #2288: Corridor deviation penalty from global routing
            corridor_cost = self.grid.get_corridor_cost(
                current.x, current.y, new_layer, source_pad.net
            )

            # Issue #2911: Corridor attractor bonus on the target layer
            # (see forward A* via transition for full rationale).
            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                new_layer, current.x, current.y, source_pad.net,
                self.rules.cost_corridor_attractor,
            )

            # Issue #2325: Cap via incremental cost (same logic as forward A*)
            via_incremental = (
                self.rules.cost_via * layer_pref_mult
                + congestion_cost
                + negotiated_cost
                + layer_util_cost
                + corridor_cost
            )
            if self.rules.via_cost_cap_factor > 0.0:
                via_cap = self.rules.via_cost_cap_factor * self.rules.cost_via
                via_incremental = min(via_incremental, via_cap)

            # Issue #2911: Apply the attractor AFTER the cap so the bonus
            # is felt even when the via cost is at the ceiling.
            if attractor_bonus > 0.0:
                via_incremental = max(0.0, via_incremental - attractor_bonus)

            new_g = (current.g_score + via_incremental) * cost_mult

            if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                g_scores[neighbor_key] = new_g
                h = self.heuristic.estimate(
                    current.x, current.y, new_layer, current.direction, heuristic_context
                )
                f = new_g + weight * h

                neighbor_node = AStarNode(f, new_g, current.x, current.y, new_layer, current, True)
                heapq.heappush(open_set, neighbor_node)
                nodes[neighbor_key] = neighbor_node

    def _reconstruct_bidirectional_route(
        self,
        meeting_point: tuple[int, int, int],
        forward_nodes: dict[tuple[int, int, int], AStarNode],
        backward_nodes: dict[tuple[int, int, int], AStarNode],
        start_pad: Pad,
        end_pad: Pad,
    ) -> Route | None:
        """Reconstruct route from bidirectional A* meeting point.

        Combines the forward path (start -> meeting) and reversed backward path
        (meeting -> end) into a complete route.

        Issue #972: Uses inline segment merging for performance.
        """
        route = Route(net=start_pad.net, net_name=start_pad.net_name)

        # Collect forward path (start -> meeting point)
        forward_path: list[tuple[float, float, int, bool]] = []
        forward_node = forward_nodes.get(meeting_point)
        while forward_node:
            wx, wy = self.grid.grid_to_world(forward_node.x, forward_node.y)
            forward_path.append((wx, wy, forward_node.layer, forward_node.via_from_parent))
            forward_node = forward_node.parent
        forward_path.reverse()

        # Collect backward path (end -> meeting point), then reverse
        backward_path: list[tuple[float, float, int, bool]] = []
        backward_node = backward_nodes.get(meeting_point)
        if backward_node:
            backward_node = backward_node.parent  # Skip meeting point (already in forward)
        while backward_node:
            wx, wy = self.grid.grid_to_world(backward_node.x, backward_node.y)
            backward_path.append((wx, wy, backward_node.layer, backward_node.via_from_parent))
            backward_node = backward_node.parent
        # backward_path is now from meeting -> end, which is what we want

        # Combine paths
        full_path = forward_path + backward_path

        # Convert path to segments and vias using shared helper
        # Issue #972: Helper includes inline segment merging optimization
        self._convert_path_to_route(full_path, route, start_pad, end_pad)

        # Issue #2934: Reject empty Routes - same rationale as the
        # unidirectional path in ``_reconstruct_route``.
        if not route.segments and not route.vias:
            return None

        # Validate layer transitions
        route.validate_layer_transitions(
            via_drill=self.rules.via_drill,
            via_diameter=self.rules.via_diameter,
        )

        # Geometric clearance validation (Issue #1016: per-component clearance support)
        # Issue #1764: Exclude pads on start/end component from clearance checks
        bidir_exclude_refs: set[str] = set()
        if start_pad.ref:
            bidir_exclude_refs.add(start_pad.ref)
        if end_pad.ref:
            bidir_exclude_refs.add(end_pad.ref)
        if not self._validate_route_clearance(
            route, start_pad.net, component_pitches=self.component_pitches,
            exclude_refs=bidir_exclude_refs if bidir_exclude_refs else None
        ):
            return None

        return route

    def route_auto(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
    ) -> Route | None:
        """Route using automatic algorithm selection.

        Chooses between standard A* and bidirectional A* based on:
        - Grid size (bidirectional for large grids)
        - Distance between pads (bidirectional for long paths)
        - Configuration settings

        This is the recommended entry point for routing, as it automatically
        selects the best algorithm for the task.

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: If True, allow sharing resources with cost penalty
            present_cost_factor: Multiplier for current sharing penalty
            weight: A* weight factor

        Returns:
            Route if path found, None otherwise
        """
        # Check if bidirectional search is enabled
        if not self.rules.bidirectional_search:
            return self.route(start, end, net_class, negotiated_mode, present_cost_factor, weight)

        # Calculate Manhattan distance in grid cells
        start_gx, start_gy = self.grid.world_to_grid(start.x, start.y)
        end_gx, end_gy = self.grid.world_to_grid(end.x, end.y)
        manhattan_dist = abs(end_gx - start_gx) + abs(end_gy - start_gy)

        # Use bidirectional for paths exceeding threshold
        if manhattan_dist >= self.rules.bidirectional_threshold:
            result = self.route_bidirectional(
                start, end, net_class, negotiated_mode, present_cost_factor, weight
            )
            if result is not None:
                return result
            # Fall back to standard A* if bidirectional fails

        return self.route(start, end, net_class, negotiated_mode, present_cost_factor, weight)
