"""Via optimization for post-routing cleanup.

Provides algorithms to minimize via count in routed traces:
- Same-layer reroute: Replace via with single-layer detour
- Via pair elimination: Remove down-then-up via patterns

Via minimization improves:
- Signal integrity (fewer layer transitions)
- Manufacturing cost (fewer drill operations)
- Board aesthetics
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..layers import Layer
from ..primitives import Route, Segment, Via
from .collision import CollisionChecker

if TYPE_CHECKING:
    pass


@dataclass
class ViaContext:
    """Context for a via within a route.

    Tracks which segments connect to a via and on which layers,
    enabling analysis for potential via removal.
    """

    via: Via
    via_index: int
    # Segments ending at this via position (on the 'from' layer)
    segments_before: list[Segment]
    # Segments starting from this via position (on the 'to' layer)
    segments_after: list[Segment]

    @property
    def from_layer(self) -> Layer:
        """Layer the via transitions from."""
        return self.via.layers[0]

    @property
    def to_layer(self) -> Layer:
        """Layer the via transitions to."""
        return self.via.layers[1]


@dataclass
class ViaOptimizationConfig:
    """Configuration for via optimization."""

    enabled: bool = True
    """Enable via minimization."""

    max_detour_factor: float = 1.5
    """Maximum detour length as factor of direct distance."""

    via_pair_threshold: float = 2.0
    """Maximum distance between vias to consider as a pair (mm)."""

    min_segment_length: float = 0.05
    """Minimum segment length to keep (mm)."""

    tolerance: float = 1e-4
    """Tolerance for floating-point comparisons (mm)."""


@dataclass
class ViaOptimizationStats:
    """Statistics from via optimization."""

    vias_before: int = 0
    vias_after: int = 0
    vias_removed_single: int = 0
    vias_removed_pairs: int = 0

    @property
    def vias_removed(self) -> int:
        """Total vias removed."""
        return self.vias_removed_single + self.vias_removed_pairs

    @property
    def via_reduction_percent(self) -> float:
        """Percentage reduction in via count."""
        if self.vias_before == 0:
            return 0.0
        return (self.vias_removed / self.vias_before) * 100


@dataclass
class LayerConnectivityError:
    """Error from layer connectivity validation."""

    point: tuple[float, float]
    from_layer: Layer
    to_layer: Layer

    def __str__(self) -> str:
        return (
            f"Layer transition at ({self.point[0]:.4f}, {self.point[1]:.4f}) "
            f"from {self.from_layer.name} to {self.to_layer.name} has no via"
        )


class ViaOptimizer:
    """Optimizer for reducing via count in routed traces.

    Uses collision checking to ensure optimizations don't create
    DRC violations.

    Example::

        from kicad_tools.router.optimizer import ViaOptimizer, GridCollisionChecker

        checker = GridCollisionChecker(grid)
        optimizer = ViaOptimizer(collision_checker=checker)

        # Optimize a single route
        optimized = optimizer.optimize_route(route)

        # Get statistics
        stats = optimizer.get_stats()
        print(f"Removed {stats.vias_removed} vias")
    """

    def __init__(
        self,
        config: ViaOptimizationConfig | None = None,
        collision_checker: CollisionChecker | None = None,
    ):
        """Initialize the via optimizer.

        Args:
            config: Via optimization configuration.
            collision_checker: Collision checker for DRC-safe optimization.
        """
        self.config = config or ViaOptimizationConfig()
        self.collision_checker = collision_checker
        self._stats = ViaOptimizationStats()

    def get_stats(self) -> ViaOptimizationStats:
        """Get optimization statistics."""
        return self._stats

    def reset_stats(self) -> None:
        """Reset optimization statistics."""
        self._stats = ViaOptimizationStats()

    def optimize_route(self, route: Route) -> Route:
        """Optimize a route by minimizing vias.

        Applies via minimization strategies:
        1. Via pair elimination (down-then-up patterns)
        2. Single via removal (same-layer reroute)

        After optimization, validates that all layer transitions still have
        vias. If validation fails, returns the original route unchanged.

        Args:
            route: Route to optimize.

        Returns:
            New Route with minimized vias, or original route if optimization
            would break layer connectivity.
        """
        if not self.config.enabled or not route.vias:
            return route

        self._stats.vias_before += len(route.vias)

        # Build via contexts
        contexts = self._build_via_contexts(route)

        # Try via pair elimination first (removes 2 vias at once)
        optimized_segments = list(route.segments)
        optimized_vias = list(route.vias)

        # Process via pairs (reverse order to maintain indices)
        pairs_removed = self._eliminate_via_pairs(
            contexts, optimized_segments, optimized_vias, route
        )
        self._stats.vias_removed_pairs += pairs_removed * 2

        # Rebuild contexts after pair elimination
        if pairs_removed > 0:
            temp_route = Route(
                net=route.net,
                net_name=route.net_name,
                segments=optimized_segments,
                vias=optimized_vias,
            )
            contexts = self._build_via_contexts(temp_route)

        # Try single via removal (reverse order to maintain indices)
        singles_removed = self._remove_single_vias(
            contexts, optimized_segments, optimized_vias, route
        )
        self._stats.vias_removed_single += singles_removed

        optimized_route = Route(
            net=route.net,
            net_name=route.net_name,
            segments=optimized_segments,
            vias=optimized_vias,
        )

        # Validate layer connectivity after optimization
        errors = self.validate_layer_connectivity(optimized_route)
        if errors:
            # Optimization broke connectivity - restore original route
            # Reset stats since we're not applying the optimization
            self._stats.vias_removed_pairs -= pairs_removed * 2
            self._stats.vias_removed_single -= singles_removed
            self._stats.vias_after += len(route.vias)
            return route

        self._stats.vias_after += len(optimized_vias)

        return optimized_route

    def _build_via_contexts(self, route: Route) -> list[ViaContext]:
        """Build context information for each via.

        Maps segments to their connected vias based on position matching.
        """
        contexts: list[ViaContext] = []
        tol = self.config.tolerance

        for i, via in enumerate(route.vias):
            segments_before: list[Segment] = []
            segments_after: list[Segment] = []

            for seg in route.segments:
                # Check if segment ends at via position (before via)
                if (
                    abs(seg.x2 - via.x) < tol
                    and abs(seg.y2 - via.y) < tol
                    and seg.layer == via.layers[0]
                ):
                    segments_before.append(seg)

                # Check if segment starts at via position (after via)
                if (
                    abs(seg.x1 - via.x) < tol
                    and abs(seg.y1 - via.y) < tol
                    and seg.layer == via.layers[1]
                ):
                    segments_after.append(seg)

            contexts.append(
                ViaContext(
                    via=via,
                    via_index=i,
                    segments_before=segments_before,
                    segments_after=segments_after,
                )
            )

        return contexts

    def _eliminate_via_pairs(
        self,
        contexts: list[ViaContext],
        segments: list[Segment],
        vias: list[Via],
        route: Route,
    ) -> int:
        """Eliminate via pairs (down-then-up patterns).

        A via pair occurs when:
        - Two vias are close together
        - They transition to/from the same layers in opposite directions
        - A short segment connects them on the intermediate layer

        Returns:
            Number of via pairs removed.
        """
        if len(contexts) < 2:
            return 0

        pairs_removed = 0
        indices_to_remove: set[int] = set()

        # Find via pairs (process in order, mark for removal)
        for i in range(len(contexts) - 1):
            if i in indices_to_remove:
                continue

            ctx1 = contexts[i]
            ctx2 = contexts[i + 1]

            # Check if they form a pair (down-up or up-down)
            if not self._is_via_pair(ctx1, ctx2):
                continue

            # Check distance between vias
            dist = math.sqrt((ctx2.via.x - ctx1.via.x) ** 2 + (ctx2.via.y - ctx1.via.y) ** 2)
            if dist > self.config.via_pair_threshold:
                continue

            # Try to find alternative path on the original layer
            alt_path = self._find_same_layer_path(
                x1=ctx1.via.x,
                y1=ctx1.via.y,
                x2=ctx2.via.x,
                y2=ctx2.via.y,
                layer=ctx1.from_layer,
                width=route.segments[0].width if route.segments else 0.2,
                net=route.net,
                max_detour=dist * self.config.max_detour_factor,
            )

            if alt_path:
                # Remove the via pair and intermediate segment
                self._apply_via_pair_removal(ctx1, ctx2, alt_path, segments, vias, route)
                indices_to_remove.add(i)
                indices_to_remove.add(i + 1)
                pairs_removed += 1

        return pairs_removed

    def _is_via_pair(self, ctx1: ViaContext, ctx2: ViaContext) -> bool:
        """Check if two vias form a down-up or up-down pair."""
        # Via1 goes from A to B, Via2 goes from B to A
        return ctx1.from_layer == ctx2.to_layer and ctx1.to_layer == ctx2.from_layer

    def _remove_single_vias(
        self,
        contexts: list[ViaContext],
        segments: list[Segment],
        vias: list[Via],
        route: Route,
    ) -> int:
        """Try to remove single vias by same-layer reroute.

        For each via, try to find an alternative path on one layer
        that connects the segments before and after the via.

        Returns:
            Number of vias removed.
        """
        removed = 0

        # Process in reverse order to maintain valid indices
        for ctx in reversed(contexts):
            if ctx.via not in vias:
                continue  # Already removed

            # Need at least one segment before and after
            if not ctx.segments_before or not ctx.segments_after:
                continue

            # Get connection points
            seg_before = ctx.segments_before[0]
            seg_after = ctx.segments_after[0]

            # The start point is where the segment before the via comes from
            start_x, start_y = seg_before.x1, seg_before.y1
            # The end point is where the segment after the via goes to
            end_x, end_y = seg_after.x2, seg_after.y2

            # Calculate direct distance
            direct_dist = math.sqrt((end_x - start_x) ** 2 + (end_y - start_y) ** 2)

            # Try to find path on the 'before' layer
            alt_path = self._find_same_layer_path(
                x1=start_x,
                y1=start_y,
                x2=end_x,
                y2=end_y,
                layer=ctx.from_layer,
                width=seg_before.width,
                net=route.net,
                max_detour=direct_dist * self.config.max_detour_factor,
            )

            if alt_path:
                # Apply the optimization
                self._apply_single_via_removal(
                    ctx, seg_before, seg_after, alt_path, segments, vias, route
                )
                removed += 1
                continue

            # Try on the 'after' layer
            alt_path = self._find_same_layer_path(
                x1=start_x,
                y1=start_y,
                x2=end_x,
                y2=end_y,
                layer=ctx.to_layer,
                width=seg_after.width,
                net=route.net,
                max_detour=direct_dist * self.config.max_detour_factor,
            )

            if alt_path:
                self._apply_single_via_removal(
                    ctx, seg_before, seg_after, alt_path, segments, vias, route
                )
                removed += 1

        return removed

    def _find_same_layer_path(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        net: int,
        max_detour: float,
    ) -> list[Segment] | None:
        """Find a same-layer path between two points.

        Currently implements a simple direct path check.
        Future: Could use A* for smarter obstacle avoidance.

        Args:
            x1, y1: Start point.
            x2, y2: End point.
            layer: Layer to route on.
            width: Trace width.
            net: Net ID.
            max_detour: Maximum allowed path length.

        Returns:
            List of segments forming the path, or None if not possible.
        """
        # Simple direct path check
        direct_dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

        if direct_dist > max_detour:
            return None

        # Check if direct path is clear
        if self.collision_checker is not None:
            if not self.collision_checker.path_is_clear(
                x1=x1, y1=y1, x2=x2, y2=y2, layer=layer, width=width, exclude_net=net
            ):
                return None

        # Create direct segment
        return [
            Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=width,
                layer=layer,
                net=net,
            )
        ]

    def _apply_via_pair_removal(
        self,
        ctx1: ViaContext,
        ctx2: ViaContext,
        alt_path: list[Segment],
        segments: list[Segment],
        vias: list[Via],
        route: Route,
    ) -> None:
        """Apply via pair removal by replacing with alternative path."""
        tol = self.config.tolerance

        # Remove the two vias
        if ctx1.via in vias:
            vias.remove(ctx1.via)
        if ctx2.via in vias:
            vias.remove(ctx2.via)

        # Find and remove segments connected to these vias on the intermediate layer
        segs_to_remove: list[Segment] = []
        for seg in segments:
            # Check if segment is between the two via positions
            if seg.layer == ctx1.to_layer:  # Intermediate layer
                if (abs(seg.x1 - ctx1.via.x) < tol and abs(seg.y1 - ctx1.via.y) < tol) or (
                    abs(seg.x2 - ctx2.via.x) < tol and abs(seg.y2 - ctx2.via.y) < tol
                ):
                    segs_to_remove.append(seg)

        for seg in segs_to_remove:
            if seg in segments:
                segments.remove(seg)

        # Add alternative path segments
        for seg in alt_path:
            seg.net = route.net
            seg.net_name = route.net_name
            segments.append(seg)

    def _apply_single_via_removal(
        self,
        ctx: ViaContext,
        seg_before: Segment,
        seg_after: Segment,
        alt_path: list[Segment],
        segments: list[Segment],
        vias: list[Via],
        route: Route,
    ) -> None:
        """Apply single via removal by replacing with alternative path."""
        # Remove the via
        if ctx.via in vias:
            vias.remove(ctx.via)

        # Remove the original segments
        if seg_before in segments:
            segments.remove(seg_before)
        if seg_after in segments:
            segments.remove(seg_after)

        # Add alternative path segments
        for seg in alt_path:
            seg.net = route.net
            seg.net_name = route.net_name
            segments.append(seg)

    def validate_layer_connectivity(self, route: Route) -> list[LayerConnectivityError]:
        """Validate that all layer transitions have vias.

        Checks each segment pair for layer transitions. When two segments
        connect at a point but are on different layers, there must be a
        via at that connection point.

        Args:
            route: The route to validate.

        Returns:
            List of connectivity errors found. Empty list means valid.
        """
        errors: list[LayerConnectivityError] = []
        tol = self.config.tolerance

        # Build via position set for fast lookup
        via_positions: set[tuple[float, float]] = set()
        for via in route.vias:
            # Round to tolerance for matching
            via_positions.add((round(via.x / tol) * tol, round(via.y / tol) * tol))

        # Build segment endpoint map to find connected segments
        # Key: (x, y, layer) -> list of segments that end/start there
        segment_ends: dict[tuple[float, float, Layer], list[Segment]] = {}
        segment_starts: dict[tuple[float, float, Layer], list[Segment]] = {}

        for seg in route.segments:
            end_key = (round(seg.x2 / tol) * tol, round(seg.y2 / tol) * tol, seg.layer)
            start_key = (
                round(seg.x1 / tol) * tol,
                round(seg.y1 / tol) * tol,
                seg.layer,
            )
            segment_ends.setdefault(end_key, []).append(seg)
            segment_starts.setdefault(start_key, []).append(seg)

        # Check for layer transitions without vias
        # For each segment end point, check if there's a segment starting on a different layer
        for (x, y, layer1), segs_ending in segment_ends.items():
            # Check all segments starting at this point on different layers
            for seg_start in route.segments:
                start_x = round(seg_start.x1 / tol) * tol
                start_y = round(seg_start.y1 / tol) * tol
                layer2 = seg_start.layer

                # Same position, different layers?
                if start_x == x and start_y == y and layer1 != layer2:
                    # Check if there's a via at this point
                    point = (x, y)
                    if point not in via_positions:
                        # This is an error - layer transition without via
                        errors.append(
                            LayerConnectivityError(
                                point=point,
                                from_layer=layer1,
                                to_layer=layer2,
                            )
                        )

        # Deduplicate errors (same point may be found from both directions)
        seen: set[tuple[float, float]] = set()
        unique_errors: list[LayerConnectivityError] = []
        for error in errors:
            if error.point not in seen:
                seen.add(error.point)
                unique_errors.append(error)

        return unique_errors


def optimize_route_vias(
    route: Route,
    collision_checker: CollisionChecker | None = None,
    config: ViaOptimizationConfig | None = None,
) -> tuple[Route, ViaOptimizationStats]:
    """Convenience function to optimize vias in a route.

    Args:
        route: Route to optimize.
        collision_checker: Optional collision checker for DRC-safe optimization.
        config: Optional configuration.

    Returns:
        Tuple of (optimized route, statistics).
    """
    optimizer = ViaOptimizer(config=config, collision_checker=collision_checker)
    optimized = optimizer.optimize_route(route)
    return optimized, optimizer.get_stats()
