"""TraceOptimizer class for PCB trace cleanup and simplification."""

from __future__ import annotations

import sys
from collections.abc import Callable

from ..layers import Layer
from ..pairwise_clearance import (
    PairwisePathChecker,
    PairwiseViolation,
    route_pairwise_violation,
    violation_pair_key,
)
from ..primitives import Route, Segment
from .algorithms import (
    _find_staircase_end,
    _optimal_path,
    compress_staircase,
    convert_corners_45,
    eliminate_zigzags,
    merge_collinear,
    pull_tight_pass,
)
from .chain import sort_into_chains
from .collision import CollisionChecker
from .config import OptimizationConfig, OptimizationStats
from .geometry import (
    angle_between,
    count_corners,
    is_90_degree_corner,
    is_connected,
    is_zigzag,
    same_direction,
    segment_direction,
    segments_touch,
    shorten_segment_end,
    shorten_segment_start,
    total_length,
)
from .pcb import optimize_pcb, parse_net_names, parse_segments, replace_segments
from .via_optimizer import ViaOptimizationConfig, ViaOptimizer


class PairwiseRouteGate:
    """Route-level pairwise-clearance accept predicate (issue #4766).

    The optimizer's own hazard gate (``TraceOptimizer._path_is_clear`` ->
    ``CollisionChecker.path_is_clear``) resolves clearance as the **scalar**
    ``grid.rules.trace_clearance`` in both checker implementations, so a
    ``--voltage-map`` run's HV creepage requirement was invisible to it: the
    pass could straighten, chamfer or pull-tight a trace straight through a gap
    the search had deliberately opened, and only the post-route #4588 audit
    would notice.

    This gate runs the SAME predicate the search and the audit run
    (:func:`~kicad_tools.router.pairwise_clearance.route_pairwise_violation`,
    with the layer-scoped #4506/#4699 attach-zone exemption) against the
    optimized geometry, and vetoes the move by keeping the pre-optimization
    route.  It is deliberately checked **before** the grid transaction in
    :func:`apply_route_transform_grid_synced`, so a vetoed route never enters
    the unmark/mark window at all.

    Its context comes from the single
    :meth:`~kicad_tools.router.pairwise_clearance.PairwisePathChecker.from_router`
    resolver (#4507), the same one the path-level predicate uses, so the two
    copper-moving post-passes and the predicate cannot resolve the table, the
    id->name map or the #4506 zones differently.

    Two properties worth stating plainly:

    * **The veto is coarse.**  A single bad move costs the whole route its
      optimization, not just the offending segment.  That is the
      correctness-first choice; the count is recorded (and reported) so the
      loss is measurable rather than silent.  Segment-level granularity is a
      follow-up and is *reachable today*: the checker's
      :meth:`~kicad_tools.router.pairwise_clearance.PairwisePathChecker.path_is_clear`
      is signature-compatible with :class:`~kicad_tools.router.optimizer.
      collision.CollisionChecker` and walks ``grid.routes`` directly, so it can
      be ``AND``-composed with the scalar checker inside ``TraceOptimizer``
      with no R-tree broad-phase change.  What it needs is a decision on the
      per-segment cost (an O(routes x segments) walk per candidate path,
      un-pruned by the R-tree), not a new primitive.
    * **It only vetoes what the pass would INTRODUCE.**  A route that already
      violates the same net pair before optimization keeps its optimization:
      inherited shortfalls are the audit's to report, and vetoing them would
      merely freeze the geometry of every HV route on a board that arrived
      dirty.  Anything else -- including a *different* pair appearing on an
      already-dirty route -- is vetoed.
    """

    def __init__(self, checker: PairwisePathChecker) -> None:
        self.checker = checker
        self.vetoes = 0
        self.worst: PairwiseViolation | None = None

    def _foreign_routes(self) -> list[Route]:
        """Committed copper to judge against.

        Comes from the checker's live ``foreign_routes`` view, i.e.
        ``grid.routes`` -- what the grid ``Pathfinder``'s own route validator
        uses.  At route *i* it holds post-optimize copper for ``0..i-1`` and
        pre-optimize copper for ``i+1..n``, the same incremental-visibility
        contract the pass already documents for its collision checker.  A stub
        router without a grid falls back to ``router.routes`` inside
        :meth:`PairwisePathChecker.from_router`.
        """
        return list(self.checker.foreign_routes())

    def _violation(self, route: Route, net: int, foreign: list[Route]) -> PairwiseViolation | None:
        return route_pairwise_violation(
            route,
            net,
            foreign,
            self.checker.table,
            id_to_name=self.checker.id_to_name,
            dru=self.checker.dru,
            attach_zones=self.checker.attach_zones,
            tolerance=self.checker.tolerance,
        )

    def __call__(self, original: Route, optimized: Route) -> bool:
        """True to accept ``optimized``; False to keep ``original``."""
        foreign = self._foreign_routes()
        violation = self._violation(optimized, original.net, foreign)
        if violation is None:
            return True
        # Only pay for the "was it already like this?" scan on a hit.
        prior = self._violation(original, original.net, foreign)
        if prior is not None and violation_pair_key(prior) == violation_pair_key(violation):
            # Inherited shortfall on the same net pair: not introduced here.
            return True
        self.vetoes += 1
        shortfall = violation.required_mm - violation.actual_mm
        if self.worst is None or shortfall > (self.worst.required_mm - self.worst.actual_mm):
            self.worst = violation
        return False

    def advisory(self) -> str | None:
        """One-line summary of what the gate cost, or ``None`` if it cost nothing."""
        if not self.vetoes or self.worst is None:
            return None
        return (
            f"  HV pairwise gate: {self.vetoes} route(s) kept pre-optimization "
            f"geometry (worst: {self.worst.net_a} vs {self.worst.net_b} "
            f"{self.worst.actual_mm:.3f}mm < {self.worst.required_mm:.3f}mm required "
            f"at ({self.worst.x:.3f}, {self.worst.y:.3f}))"
        )

    def report(self) -> None:
        """Surface the veto count (issue #4766 AC8).

        Printed to stderr, matching the router's existing advisory precedent
        (``_warn_layer_selection_advisories``, #4314): ``--quiet``'s contract is
        about stdout, and a silently-discarded optimization on a safety-critical
        HV board is exactly the thing that must not go unreported.  Nothing is
        printed when no route was vetoed, so every dormant (no ``--voltage-map``)
        run stays byte-identical.
        """
        line = self.advisory()
        if line is not None:
            print(line, file=sys.stderr)


def pairwise_route_gate(router: object) -> PairwiseRouteGate | None:
    """Build the #4766 accept gate for ``router``, or ``None`` when dormant."""
    checker = PairwisePathChecker.from_router(router)
    if checker is None:
        return None
    return PairwiseRouteGate(checker)


def optimize_routes_grid_synced(
    router, optimizer: TraceOptimizer, skip_nets: set[int] | None = None
) -> list[Route]:
    """Optimize every route on ``router`` keeping the grid in sync.

    Issue #3507: the historical call-site pattern::

        optimized_routes = [optimizer.optimize_route(r) for r in router.routes]
        router.routes = optimized_routes

    replaces every Route object WITHOUT re-marking the routing grid, so

    * the optimizer's own collision checking for route *i* runs against
      the PRE-optimization copper of routes ``0..i-1``, and
    * every downstream grid consumer (the DRC nudge pass, targeted
      repair re-routes such as board 06's transactional solo re-route,
      future nets in multi-pass flows) operates on a stale grid.

    This helper is the grid-transactional replacement: after each route
    is optimized, its old geometry is unmarked and the new geometry is
    marked (cells, R-trees, ``grid.routes`` bookkeeping, and the paired
    C++ grid mirror) before the next route is optimized.  Routes whose
    optimized geometry is unchanged are skipped (the incremental
    unmark-old/mark-new perf shape).

    Issue #3511: the incremental per-route unmark is net-guarded but
    geometry-scoped -- ``grid.unmark_route(route)`` clears every cell the
    OLD envelope owned, including cells that a same-net SIBLING route
    (left geometry-unchanged by the optimizer) still occupies where the
    two envelopes overlapped.  The per-route ``mark_route`` only re-marks
    the mutated route's NEW geometry, so the sibling's cells inside the
    old overlap region stay cleared and the grid under-blocks real
    own-net copper.  To close that asymmetry we collect the
    ``(old, new)`` pair for every mutated route and issue ONE batched
    :meth:`RoutingGrid.resync_route_occupancy` after the loop: its step-4
    affected-net re-mark restores the sibling cells, and its single
    wholesale R-tree rebuild keeps the pass O(n) (calling resync per
    route would re-rebuild the R-trees n times -- the O(n^2) trap).

    Args:
        router: ``Autorouter`` whose ``routes`` will be optimized and
            replaced in place.
        optimizer: Configured :class:`TraceOptimizer`.
        skip_nets: Issue #3508: optional set of net IDs whose routes are
            passed through UNTOUCHED.  Coupled diff-pair routes carry
            intentional geometry the optimizer's simplification passes
            destroy: length-matching serpentines are exactly the
            "zigzags" ``eliminate_zigzags`` removes (measured on board
            06: PCIE_RX skew 0.097 mm after serpentine -> 1.652 mm in
            the final artifact), and straightening one side of a
            coupled pair breaks the constant-gap geometry.

    Issue #4766: under a ``--voltage-map`` run this pass also consults the
    pairwise (HV creepage) predicate before accepting a route -- see
    :class:`PairwiseRouteGate`.  Without a voltage map the gate is not built at
    all and the pass is byte-identical to the pre-#4766 path.

    Returns:
        The new ``router.routes`` list (also assigned on the router).
    """
    gate = pairwise_route_gate(router)
    routes = apply_route_transform_grid_synced(
        router, optimizer.optimize_route, skip_nets=skip_nets, accept=gate
    )
    if gate is not None:
        # Issue #4766 AC8: record the coarse-veto cost on the router (for
        # tests / callers) and surface it once per pass.
        router._pairwise_optimize_vetoes = gate.vetoes
        gate.report()
    return routes


def apply_route_transform_grid_synced(
    router,
    transform: Callable[[Route], Route],
    *,
    skip_nets: set[int] | None = None,
    accept: Callable[[Route, Route], bool] | None = None,
) -> list[Route]:
    """Replace every route with ``transform(route)`` keeping the grid in sync.

    The grid-transactional machinery
    :func:`optimize_routes_grid_synced` documents (#3507 incremental
    unmark/mark, #3511 batched ``resync_route_occupancy``) is not
    specific to the optimizer -- issue #4732's post-nudge consolidation
    pass needs exactly the same transaction.  This is that machinery,
    parameterized by the per-route transform; ``optimize_routes_grid_synced``
    is the ``optimizer.optimize_route`` specialization of it and its
    docstring remains the canonical explanation of *why* each step
    exists.

    Args:
        router: ``Autorouter`` whose ``routes`` are replaced in place.
        transform: Callable mapping a route to its replacement.  Returning
            the input object (or an equal one) marks the route unchanged
            and skips the grid transaction for it.
        skip_nets: Net IDs whose routes pass through untouched (#3508).
        accept: Issue #4766: optional ``(original, transformed) -> bool``
            veto hook, consulted BEFORE the grid transaction so a rejected
            transform never enters the unmark/mark window.  ``False`` keeps
            the original route object (identity-preserving, exactly as an
            unchanged transform would).  ``None`` -- the default, and what
            the copper-preserving consolidation pass uses -- disables the
            hook entirely, so callers that opt out are byte-identical to the
            pre-#4766 path.

    Returns:
        The new ``router.routes`` list (also assigned on the router).
    """
    grid = router.grid
    optimized_routes: list[Route] = []
    # Issue #3511: (old, new) pairs for every MUTATED route, replayed
    # through a single batched resync after the loop so same-net sibling
    # cells cleared by an incremental unmark are re-marked.
    mutated_pairs: list[tuple[Route, Route]] = []
    for route in router.routes:
        if skip_nets is not None and route.net in skip_nets:
            optimized_routes.append(route)
            continue
        optimized = transform(route)
        if accept is not None and optimized is not route and not accept(route, optimized):
            # Issue #4766: vetoed (e.g. the transform would close an HV
            # pairwise gap).  Fall back to the pre-transform route BEFORE the
            # grid transaction below, so the grid is never touched for it.
            optimized = route
        if optimized is not route and (
            optimized.segments == route.segments and optimized.vias == route.vias
        ):
            # Geometry unchanged: keep the ORIGINAL object so identity
            # stays aligned with ``grid.routes`` (mark_route bookkeeping)
            # -- a fresh-but-equal object would leave the grid holding a
            # stale twin that later resyncs would re-mark.
            optimized = route
        optimized_routes.append(optimized)
        if optimized is route:
            continue
        # Incremental grid transaction: rip the old copper (cells +
        # R-trees + grid.routes + C++ mirror + stored-route snapshot
        # invalidation), then commit the new copper on both grids.  Route
        # ``i`` must collision-check against the POST-opt copper of routes
        # ``0..i-1``, so this stays incremental -- the batched resync
        # below only repairs same-net sibling under-marking, it does not
        # replace the per-route transaction.
        grid.unmark_route(route)
        grid.mark_route(optimized)
        grid._mark_route_on_cpp_cells(optimized)
        mutated_pairs.append((route, optimized))
    router.routes = optimized_routes
    # Issue #3511: ONE batched repair after the loop.  The incremental
    # per-route ``unmark_route`` above clears every cell the old envelope
    # owned, which can blank cells still occupied by a geometry-unchanged
    # same-net sibling route where the envelopes overlapped; the per-route
    # ``mark_route`` only re-marks the mutated route's new geometry.
    # ``resync_route_occupancy``'s affected-net re-mark (its step 4)
    # restores those sibling cells, and its single wholesale R-tree
    # rebuild keeps the pass O(n) -- do NOT call it per route.
    if mutated_pairs:
        grid.resync_route_occupancy(mutated_pairs)
    return optimized_routes


class TraceOptimizer:
    """Optimizer for PCB trace cleanup and simplification.

    Optionally uses a collision checker to ensure optimizations don't
    create DRC violations (shorts, track crossings with other nets).
    When a collision checker is provided, optimizations that would create
    collisions are skipped, preserving the original path.

    Example::

        from kicad_tools.router import TraceOptimizer, OptimizationConfig

        # Optimize a route in memory (no collision checking)
        optimizer = TraceOptimizer()
        optimized_route = optimizer.optimize_route(route)

        # Optimize with collision checking (auto-selects best checker)
        from kicad_tools.router.optimizer import make_collision_checker
        checker = make_collision_checker(grid)
        optimizer = TraceOptimizer(collision_checker=checker)
        optimized_route = optimizer.optimize_route(route)

        # Optimize traces in a PCB file
        stats = optimizer.optimize_pcb("board.kicad_pcb", output="optimized.kicad_pcb")
        print(f"Reduced segments from {stats['before']} to {stats['after']}")
    """

    def __init__(
        self,
        config: OptimizationConfig | None = None,
        collision_checker: CollisionChecker | None = None,
    ):
        """Initialize the trace optimizer.

        Args:
            config: Optimization configuration. Uses defaults if None.
            collision_checker: Optional collision checker for DRC-safe optimization.
                When provided, optimizations that would create collisions are skipped.
                When None, no collision checking is performed (original behavior).
        """
        self.config = config or OptimizationConfig()
        self.collision_checker = collision_checker

        # Initialize via optimizer with matching config
        via_config = ViaOptimizationConfig(
            enabled=self.config.minimize_vias,
            max_detour_factor=self.config.via_max_detour_factor,
            via_pair_threshold=self.config.via_pair_threshold,
            min_segment_length=self.config.min_segment_length,
            tolerance=self.config.tolerance,
        )
        self._via_optimizer = ViaOptimizer(
            config=via_config,
            collision_checker=collision_checker,
        )

    def optimize_segments(self, segments: list[Segment]) -> list[Segment]:
        """Optimize a list of segments for a single net/layer.

        Applies enabled optimizations in order:
        1. Sort segments into connected chains (to avoid cross-chain shortcuts)
        2. Collinear segment merging
        3. Zigzag elimination
        4. Staircase compression
        5. 45-degree corner conversion
        6. PullTight perpendicular segment translation

        Args:
            segments: List of segments to optimize (may contain multiple chains).

        Returns:
            Optimized list of segments.
        """
        if not segments:
            return []

        # Sort segments into connected chains to prevent cross-chain shortcuts
        chains = self._sort_into_chains(segments)

        # Optimize each chain independently
        all_optimized: list[Segment] = []
        for chain in chains:
            result = list(chain)

            # Apply optimizations in order
            if self.config.merge_collinear:
                result = self.merge_collinear(result)

            if self.config.eliminate_zigzags:
                result = self.eliminate_zigzags(result)

            if self.config.compress_staircase:
                result = self.compress_staircase(result)

            if self.config.convert_45_corners:
                result = self.convert_corners_45(result)

            if self.config.pull_tight:
                result = self.pull_tight(result)

            all_optimized.extend(result)

        return all_optimized

    def _path_is_clear(self, seg: Segment) -> bool:
        """Check if a segment's path is clear using the collision checker.

        Args:
            seg: The segment to check.

        Returns:
            True if path is clear (or no collision checker), False if blocked.
        """
        if self.collision_checker is None:
            return True  # No collision checking - allow all paths

        return self.collision_checker.path_is_clear(
            x1=seg.x1,
            y1=seg.y1,
            x2=seg.x2,
            y2=seg.y2,
            layer=seg.layer,
            width=seg.width,
            exclude_net=seg.net,
        )

    def merge_collinear(self, segments: list[Segment]) -> list[Segment]:
        """Merge adjacent collinear segments.

        Combines segments that:
        - Are connected (end of one matches start of next)
        - Have the same direction
        - Are on the same layer
        - Would not cross obstacles (if collision checker provided)

        Args:
            segments: List of segments to merge.

        Returns:
            List with collinear segments merged.
        """
        return merge_collinear(segments, self.config, self._path_is_clear)

    def eliminate_zigzags(self, segments: list[Segment]) -> list[Segment]:
        """Remove unnecessary zigzag patterns.

        Identifies segments where the path backtracks and removes
        the unnecessary detour, but only if the shortcut path is clear.

        Args:
            segments: List of segments to process.

        Returns:
            List with zigzags eliminated.
        """
        return eliminate_zigzags(segments, self.config, self._path_is_clear)

    def compress_staircase(self, segments: list[Segment]) -> list[Segment]:
        """Compress staircase patterns into optimal diagonal+orthogonal paths.

        Identifies runs of segments alternating between two directions
        (e.g., horizontal and 45° diagonal) and replaces them with an
        optimal 2-3 segment path, but only if the replacement is clear.

        Args:
            segments: List of segments to process.

        Returns:
            List with staircase patterns compressed.
        """
        return compress_staircase(segments, self.config, self._path_is_clear)

    def convert_corners_45(self, segments: list[Segment]) -> list[Segment]:
        """Convert 90-degree corners to 45-degree chamfers.

        Replaces sharp 90-degree turns with smoother 45-degree entry/exit,
        but only if the chamfer path is clear of obstacles.

        Args:
            segments: List of segments to process.

        Returns:
            List with corners converted to 45 degrees.
        """
        return convert_corners_45(segments, self.config, self._path_is_clear)

    def pull_tight(self, segments: list[Segment]) -> list[Segment]:
        """PullTight post-processing: translate interior segments to shorten total length.

        Iteratively moves interior segments perpendicular to their direction
        toward the ideal straight-line path, respecting DRC clearances via
        the collision checker.

        Args:
            segments: List of connected segments (single chain).

        Returns:
            Optimised list of segments.
        """
        return pull_tight_pass(segments, self.config, self._path_is_clear)

    def optimize_route(self, route: Route) -> Route:
        """Optimize a complete route.

        Applies optimizations in order:
        1. Segment optimization (collinear merge, zigzag elimination, etc.)
        2. Via minimization (remove unnecessary layer transitions)

        Segments are grouped by layer and then sorted into connected chains
        before optimization. This prevents optimization from creating
        shortcuts between unconnected parts of the route.

        A connectivity-preserving guard runs after optimisation: if the
        optimised segment graph fails to reach every pad-like endpoint of
        the input route, the original (pre-optimisation) segments are
        retained instead.  This protects against optimiser bugs on
        Y/T-junction multi-pad nets (issue #2389) and serves as a safety
        net for future optimiser changes.

        Args:
            route: Route to optimize.

        Returns:
            New Route with optimized segments and minimized vias.
        """
        # Capture pad-like endpoints (degree-1 vertices) on the input
        # segments so we can verify connectivity is preserved after
        # optimisation.  We use the same per-layer grouping the optimiser
        # uses, since vias bridge layers separately.
        pre_endpoints = _collect_terminal_endpoints(route.segments)

        # Group segments by layer for optimization
        segments_by_layer: dict[Layer, list[Segment]] = {}
        for seg in route.segments:
            if seg.layer not in segments_by_layer:
                segments_by_layer[seg.layer] = []
            segments_by_layer[seg.layer].append(seg)

        # Optimize each layer's segments (chain sorting happens in optimize_segments)
        optimized_segments: list[Segment] = []
        for _layer, segs in segments_by_layer.items():
            optimized = self.optimize_segments(segs)
            optimized_segments.extend(optimized)

        # Connectivity-preserving guard: if optimisation dropped any
        # pad-like endpoint from the segment graph, revert to the
        # pre-optimisation segments for this route.  This protects
        # against branch-dropping bugs in chain sorting and downstream
        # linearisation passes.
        if not _endpoints_preserved(
            pre_endpoints,
            optimized_segments,
            list(route.vias),
            tolerance=self.config.tolerance,
        ):
            optimized_segments = list(route.segments)

        # Create route with optimized segments
        optimized_route = Route(
            net=route.net,
            net_name=route.net_name,
            segments=optimized_segments,
            vias=list(route.vias),
        )

        # Apply via minimization if enabled
        if self.config.minimize_vias:
            optimized_route = self._via_optimizer.optimize_route(optimized_route)

        # Re-run the connectivity-preserving guard after via minimization
        # so that via removals that break pad connectivity are caught and
        # the entire route is reverted to pre-optimisation state.  The
        # earlier guard (above) only checks segment optimisation; this one
        # covers via-induced disconnects (issue #2402).
        if not _endpoints_preserved(
            pre_endpoints,
            list(optimized_route.segments),
            list(optimized_route.vias),
            tolerance=self.config.tolerance,
        ):
            optimized_route = Route(
                net=route.net,
                net_name=route.net_name,
                segments=list(route.segments),
                vias=list(route.vias),
            )

        return optimized_route

    def optimize_pcb(
        self,
        pcb_path: str,
        output_path: str | None = None,
        net_filter: str | None = None,
        dry_run: bool = False,
    ) -> OptimizationStats:
        """Optimize traces in a PCB file.

        Args:
            pcb_path: Path to input .kicad_pcb file.
            output_path: Path for output file. If None, modifies in place.
            net_filter: Only optimize nets matching this pattern.
            dry_run: If True, calculate stats but don't write output.

        Returns:
            Statistics about the optimization.
        """
        return optimize_pcb(
            pcb_path=pcb_path,
            output_path=output_path,
            optimize_fn=self.optimize_segments,
            config=self.config,
            net_filter=net_filter,
            dry_run=dry_run,
        )

    # =========================================================================
    # Helper methods exposed for tests
    # =========================================================================

    def _is_connected(self, s1: Segment, s2: Segment) -> bool:
        """Check if end of s1 connects to start of s2."""
        return is_connected(s1, s2, self.config.tolerance)

    def _segments_touch(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments share any endpoint (regardless of direction)."""
        return segments_touch(s1, s2, self.config.tolerance)

    def _sort_into_chains(self, segments: list[Segment]) -> list[list[Segment]]:
        """Sort segments into connected chains."""
        return sort_into_chains(segments, self.config.tolerance)

    def _same_direction(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments have the same direction."""
        return same_direction(s1, s2, self.config.tolerance)

    def _is_zigzag(self, s1: Segment, s2: Segment, s3: Segment) -> bool:
        """Check if s2 is a zigzag (backtrack) between s1 and s3."""
        return is_zigzag(s1, s2, s3, self.config.tolerance)

    def _angle_between(self, s1: Segment, s2: Segment) -> float:
        """Calculate angle between two segments in degrees (0-180)."""
        return angle_between(s1, s2, self.config.tolerance)

    def _is_90_degree_corner(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments form a 90-degree corner."""
        return is_90_degree_corner(s1, s2)

    def _shorten_segment_end(self, seg: Segment, amount: float) -> Segment | None:
        """Shorten a segment from its end by the given amount."""
        return shorten_segment_end(seg, amount, self.config.min_segment_length)

    def _shorten_segment_start(self, seg: Segment, amount: float) -> Segment | None:
        """Shorten a segment from its start by the given amount."""
        return shorten_segment_start(seg, amount, self.config.min_segment_length)

    def _count_corners(self, segments: list[Segment]) -> int:
        """Count number of corners (direction changes) in a segment list."""
        return count_corners(segments, self.config.tolerance)

    def _total_length(self, segments: list[Segment]) -> float:
        """Calculate total length of segments."""
        return total_length(segments)

    def _segment_direction(self, seg: Segment) -> float:
        """Calculate the direction of a segment in degrees (0-360)."""
        return segment_direction(seg, self.config.tolerance)

    def _find_staircase_end(self, segments: list[Segment], start_idx: int) -> int:
        """Find the end index of a staircase pattern starting at start_idx."""
        return _find_staircase_end(segments, start_idx, self.config)

    def _optimal_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        template: Segment,
    ) -> list[Segment]:
        """Generate an optimal 2-3 segment path from start to end."""
        return _optimal_path(start, end, template, self.config)

    def _parse_net_names(self, pcb_text: str) -> dict[int, str]:
        """Parse net ID to name mapping from PCB file."""
        return parse_net_names(pcb_text)

    def _parse_segments(self, pcb_text: str) -> dict[str, list[Segment]]:
        """Parse segments from PCB file text, grouped by net name."""
        return parse_segments(pcb_text)

    def _replace_segments(
        self,
        pcb_text: str,
        original: dict[str, list[Segment]],
        optimized: dict[str, list[Segment]],
    ) -> str:
        """Replace original segments with optimized ones in PCB text."""
        return replace_segments(pcb_text, original, optimized)

    def get_via_stats(self) -> dict:
        """Get via optimization statistics.

        Returns:
            Dictionary with via optimization stats:
                - vias_before: Total vias before optimization
                - vias_after: Total vias after optimization
                - vias_removed: Total vias removed
                - via_reduction_percent: Percentage reduction
        """
        stats = self._via_optimizer.get_stats()
        return {
            "vias_before": stats.vias_before,
            "vias_after": stats.vias_after,
            "vias_removed": stats.vias_removed,
            "via_reduction_percent": stats.via_reduction_percent,
        }

    def reset_via_stats(self) -> None:
        """Reset via optimization statistics."""
        self._via_optimizer.reset_stats()


# ---------------------------------------------------------------------------
# Connectivity-preserving guard helpers (issue #2389)
# ---------------------------------------------------------------------------


def _vertex_key(x: float, y: float, layer: Layer, tolerance: float) -> tuple[int, int, int]:
    """Quantise (x, y, layer) so neighbouring endpoints share a key."""
    if tolerance <= 0:
        qx = int(round(x * 1e9))
        qy = int(round(y * 1e9))
    else:
        qx = int(round(x / tolerance))
        qy = int(round(y / tolerance))
    return (qx, qy, int(layer.value))


def _collect_terminal_endpoints(
    segments: list[Segment], tolerance: float = 1e-4
) -> set[tuple[int, int, int]]:
    """Return the per-layer degree-1 vertices in a segment list.

    These are the "pad-like" endpoints of a route: points where exactly one
    segment terminates.  Interior junction or chain-internal vertices have
    degree >= 2 and are excluded.
    """
    counts: dict[tuple[int, int, int], int] = {}
    for seg in segments:
        for x, y in (seg.start, seg.end):
            k = _vertex_key(x, y, seg.layer, tolerance)
            counts[k] = counts.get(k, 0) + 1
    return {k for k, c in counts.items() if c == 1}


def _endpoints_preserved(
    pre_endpoints: set[tuple[int, int, int]],
    optimized_segments: list[Segment],
    vias: list,
    tolerance: float = 1e-4,
) -> bool:
    """Verify every pre-optimisation pad-like endpoint is reachable.

    Builds a connectivity graph from ``optimized_segments`` (and the route's
    vias, which bridge layers at a shared (x, y)) and confirms that every
    vertex in ``pre_endpoints`` is present and lies in a single connected
    component.  Returns True if connectivity is preserved, False otherwise.

    The check is conservative: missing keys, or splitting endpoints across
    multiple components, both fail.
    """
    if not pre_endpoints:
        return True

    # Build adjacency between vertex keys.  Each segment contributes an
    # edge between its two endpoints on its layer.  Each via contributes
    # edges between (x, y) on every layer it spans.
    adjacency: dict[tuple[int, int, int], set[tuple[int, int, int]]] = {}

    def add_edge(a: tuple[int, int, int], b: tuple[int, int, int]) -> None:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    for seg in optimized_segments:
        a = _vertex_key(seg.x1, seg.y1, seg.layer, tolerance)
        b = _vertex_key(seg.x2, seg.y2, seg.layer, tolerance)
        add_edge(a, b)

    for via in vias:
        # Vias have an (x, y) and a tuple of two layers.  A via connects
        # the same (x, y) across both layers.
        try:
            via_layers = via.layers
            via_x = via.x
            via_y = via.y
        except AttributeError:
            continue
        if not via_layers or len(via_layers) < 2:
            continue
        a = _vertex_key(via_x, via_y, via_layers[0], tolerance)
        b = _vertex_key(via_x, via_y, via_layers[1], tolerance)
        add_edge(a, b)

    # Every pre-optimisation endpoint must appear in the adjacency map.
    if not pre_endpoints.issubset(adjacency.keys()):
        return False

    # All pre-optimisation endpoints must lie in the same connected
    # component (BFS from one of them).
    start = next(iter(pre_endpoints))
    visited: set[tuple[int, int, int]] = {start}
    queue = [start]
    while queue:
        node = queue.pop()
        for neighbor in adjacency.get(node, ()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return pre_endpoints.issubset(visited)
