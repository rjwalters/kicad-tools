"""Via conflict management for routing.

When routing fails due to existing vias blocking pad access, this module
provides strategies to resolve the conflicts:

- **Relocate**: Move the blocking via to a nearby position that maintains
  connectivity but doesn't block the pad.
- **Rip-reroute**: Remove the blocking via and its route, route the blocked
  net first, then re-route the affected net with new via positions.

The manager integrates with the existing failure analysis system to detect
via-related blocking and applies resolution strategies before retrying.

Example::

    from kicad_tools.router.via_conflict import (
        ViaConflictManager,
        ViaConflictStrategy,
    )

    manager = ViaConflictManager(grid, rules)
    conflicts = manager.find_blocking_vias(pad, net_id)

    for conflict in conflicts:
        resolved = manager.resolve(conflict, strategy=ViaConflictStrategy.RELOCATE)
        if resolved:
            print(f"Relocated via from {conflict.via_position} to {resolved.new_position}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .rules import DesignRules

from .layers import Layer
from .primitives import Pad, Route, Segment, Via


class ViaConflictStrategy(Enum):
    """Strategy for resolving via conflicts."""

    RELOCATE = auto()
    """Move the blocking via to a nearby non-conflicting position."""

    RIP_REROUTE = auto()
    """Remove blocking via's route and re-route after the blocked net."""

    NONE = auto()
    """Do not attempt to resolve via conflicts."""


@dataclass
class ViaConflict:
    """A detected conflict between a via and a pad.

    Attributes:
        via: The via that is blocking pad access.
        via_route: The route that the via belongs to.
        via_position: World coordinates of the via (x, y).
        blocked_pad: The pad whose access is blocked.
        blocked_net: Net ID of the pad that cannot be routed.
        blocking_net: Net ID of the net the via belongs to.
        blocking_net_name: Human-readable name of the blocking net.
        distance: Distance from the via to the blocked pad in mm.
        clearance_needed: Clearance required to resolve the conflict in mm.
    """

    via: Via
    via_route: Route | None
    via_position: tuple[float, float]
    blocked_pad: Pad
    blocked_net: int
    blocking_net: int
    blocking_net_name: str
    distance: float
    clearance_needed: float


@dataclass
class ViaRelocation:
    """Result of a via relocation attempt.

    Attributes:
        original_via: The original via that was relocated.
        new_position: New world coordinates (x, y) for the via.
        new_via: The via object at the new position.
        affected_route: The route that was modified.
        modified_segments: Segments that were updated to connect to the new via position.
        success: Whether the relocation was successful.
    """

    original_via: Via
    new_position: tuple[float, float]
    new_via: Via | None = None
    affected_route: Route | None = None
    modified_segments: list[Segment] = field(default_factory=list)
    success: bool = False


@dataclass
class RipRerouteResult:
    """Result of a rip-up and reroute attempt.

    Attributes:
        ripped_route: The route that was removed.
        ripped_net: Net ID of the route that was removed.
        blocked_net_routed: Whether the blocked net was successfully routed after rip-up.
        ripped_net_rerouted: Whether the ripped net was successfully re-routed.
        new_blocked_routes: New routes for the previously blocked net.
        new_ripped_routes: New routes for the re-routed net.
        success: Whether the full rip-reroute succeeded.
    """

    ripped_route: Route | None = None
    ripped_net: int = 0
    blocked_net_routed: bool = False
    ripped_net_rerouted: bool = False
    new_blocked_routes: list[Route] = field(default_factory=list)
    new_ripped_routes: list[Route] = field(default_factory=list)
    success: bool = False


@dataclass
class ViaConflictStats:
    """Statistics from via conflict resolution.

    Attributes:
        conflicts_found: Number of via conflicts detected.
        relocations_attempted: Number of relocation attempts made.
        relocations_succeeded: Number of successful relocations.
        rip_reroutes_attempted: Number of rip-reroute attempts made.
        rip_reroutes_succeeded: Number of successful rip-reroutes.
        nets_unblocked: Number of nets that were unblocked by conflict resolution.
    """

    conflicts_found: int = 0
    relocations_attempted: int = 0
    relocations_succeeded: int = 0
    rip_reroutes_attempted: int = 0
    rip_reroutes_succeeded: int = 0
    nets_unblocked: int = 0

    @property
    def total_resolved(self) -> int:
        """Total conflicts successfully resolved."""
        return self.relocations_succeeded + self.rip_reroutes_succeeded


class ViaConflictManager:
    """Manages via conflicts during routing.

    Detects when existing vias block pad access and applies strategies
    to resolve the conflicts, improving routing completion rate.

    Example::

        manager = ViaConflictManager(grid, rules)

        # Find vias blocking a specific pad
        conflicts = manager.find_blocking_vias(pad, net_id)

        # Try to relocate each blocking via
        for conflict in conflicts:
            result = manager.try_relocate(conflict)
            if result.success:
                print(f"Via relocated to {result.new_position}")

        # Or use rip-and-reroute for stubborn conflicts
        for conflict in conflicts:
            result = manager.try_rip_reroute(conflict, route_net_fn)
            if result.success:
                print(f"Net {conflict.blocking_net} rerouted")
    """

    # Search radius multiplier for finding candidate relocation positions
    RELOCATION_SEARCH_RADIUS = 3.0

    # Maximum relocation distance as multiple of via diameter
    MAX_RELOCATION_DISTANCE = 5.0

    # Number of candidate positions to evaluate around the blocking via
    RELOCATION_CANDIDATES = 16

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
    ):
        """Initialize the via conflict manager.

        Args:
            grid: The routing grid to work with.
            rules: Design rules for clearances and dimensions.
        """
        self.grid = grid
        self.rules = rules
        self._stats = ViaConflictStats()

    @property
    def stats(self) -> ViaConflictStats:
        """Get conflict resolution statistics."""
        return self._stats

    def reset_stats(self) -> None:
        """Reset conflict resolution statistics."""
        self._stats = ViaConflictStats()

    def find_blocking_vias(
        self,
        pad: Pad,
        pad_net: int,
        search_radius: float | None = None,
        net_names: dict[int, str] | None = None,
    ) -> list[ViaConflict]:
        """Find vias that block access to a pad.

        Searches the area around a pad to find vias from other nets whose
        clearance zones prevent routing to the pad.

        Args:
            pad: The pad whose access may be blocked.
            pad_net: Net ID of the pad.
            search_radius: Search radius in mm (defaults to calculated from rules).
            net_names: Optional mapping of net ID to net name.

        Returns:
            List of ViaConflict objects sorted by distance (closest first).
        """
        net_names = net_names or {}

        if search_radius is None:
            # Search radius covers via clearance + trace routing space
            search_radius = (
                self.rules.via_diameter / 2
                + self.rules.via_clearance
                + self.rules.trace_width
                + self.rules.trace_clearance
            ) * self.RELOCATION_SEARCH_RADIUS

        conflicts: list[ViaConflict] = []
        seen_vias: set[tuple[float, float]] = set()

        # Search through all routes on the grid for vias near this pad
        for route in self.grid.routes:
            if route.net == pad_net:
                continue  # Skip same-net vias

            for via in route.vias:
                via_key = (round(via.x, 4), round(via.y, 4))
                if via_key in seen_vias:
                    continue
                seen_vias.add(via_key)

                # Calculate distance from via to pad
                distance = math.sqrt(
                    (via.x - pad.x) ** 2 + (via.y - pad.y) ** 2
                )

                if distance > search_radius:
                    continue

                # Check if via's clearance zone actually blocks the pad
                min_clearance = (
                    via.diameter / 2 + self.rules.via_clearance
                )

                if distance < min_clearance + self.rules.trace_width:
                    conflict = ViaConflict(
                        via=via,
                        via_route=route,
                        via_position=(via.x, via.y),
                        blocked_pad=pad,
                        blocked_net=pad_net,
                        blocking_net=via.net,
                        blocking_net_name=net_names.get(via.net, f"Net_{via.net}"),
                        distance=distance,
                        clearance_needed=min_clearance + self.rules.trace_width - distance,
                    )
                    conflicts.append(conflict)
                    self._stats.conflicts_found += 1

        # Sort by distance (closest first - most impactful)
        conflicts.sort(key=lambda c: c.distance)
        return conflicts

    def find_all_via_conflicts(
        self,
        failed_nets: dict[int, list[Pad]],
        net_names: dict[int, str] | None = None,
    ) -> dict[int, list[ViaConflict]]:
        """Find via conflicts for all nets that failed to route.

        Args:
            failed_nets: Mapping of net ID to list of pads that failed to route.
            net_names: Optional mapping of net ID to net name.

        Returns:
            Mapping of net ID to list of ViaConflict objects.
        """
        all_conflicts: dict[int, list[ViaConflict]] = {}

        for net_id, pads in failed_nets.items():
            net_conflicts: list[ViaConflict] = []
            for pad in pads:
                conflicts = self.find_blocking_vias(
                    pad=pad,
                    pad_net=net_id,
                    net_names=net_names,
                )
                net_conflicts.extend(conflicts)

            if net_conflicts:
                # Deduplicate by via position
                seen: set[tuple[float, float]] = set()
                unique_conflicts: list[ViaConflict] = []
                for conflict in net_conflicts:
                    key = (round(conflict.via.x, 4), round(conflict.via.y, 4))
                    if key not in seen:
                        seen.add(key)
                        unique_conflicts.append(conflict)
                all_conflicts[net_id] = unique_conflicts

        return all_conflicts

    def try_relocate(
        self,
        conflict: ViaConflict,
    ) -> ViaRelocation:
        """Try to relocate a blocking via to a nearby non-conflicting position.

        Searches candidate positions around the via and finds one that:
        1. Does not block the target pad
        2. Does not violate DRC clearances
        3. Maintains connectivity for the via's net

        Args:
            conflict: The via conflict to resolve.

        Returns:
            ViaRelocation result (check .success for outcome).
        """
        self._stats.relocations_attempted += 1
        via = conflict.via
        pad = conflict.blocked_pad
        route = conflict.via_route

        if route is None:
            return ViaRelocation(original_via=via, new_position=conflict.via_position)

        # Generate candidate positions around the via
        max_dist = via.diameter * self.MAX_RELOCATION_DISTANCE
        candidates = self._generate_relocation_candidates(
            via_x=via.x,
            via_y=via.y,
            pad_x=pad.x,
            pad_y=pad.y,
            max_distance=max_dist,
            num_candidates=self.RELOCATION_CANDIDATES,
        )

        # Evaluate candidates
        best_pos: tuple[float, float] | None = None
        best_score = float("inf")

        for candidate_x, candidate_y in candidates:
            # Check that candidate doesn't block the pad
            pad_dist = math.sqrt(
                (candidate_x - pad.x) ** 2 + (candidate_y - pad.y) ** 2
            )
            min_clearance = via.diameter / 2 + self.rules.via_clearance + self.rules.trace_width
            if pad_dist < min_clearance:
                continue

            # Check that candidate position is within grid bounds
            gx, gy = self.grid.world_to_grid(candidate_x, candidate_y)
            if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                continue

            # Check that candidate position doesn't conflict with other obstacles
            if self._position_is_blocked(candidate_x, candidate_y, via.net):
                continue

            # Score the candidate: prefer closer to original position, farther from pad
            orig_dist = math.sqrt(
                (candidate_x - via.x) ** 2 + (candidate_y - via.y) ** 2
            )
            score = orig_dist - pad_dist * 0.5  # Prefer staying close but moving away from pad

            if score < best_score:
                best_score = score
                best_pos = (candidate_x, candidate_y)

        if best_pos is None:
            return ViaRelocation(original_via=via, new_position=conflict.via_position)

        # Apply relocation
        new_via = Via(
            x=best_pos[0],
            y=best_pos[1],
            drill=via.drill,
            diameter=via.diameter,
            layers=via.layers,
            net=via.net,
            net_name=via.net_name,
        )

        # Update the route's via and connected segments
        modified_segments = self._update_route_via_position(
            route=route,
            old_via=via,
            new_via=new_via,
        )

        self._stats.relocations_succeeded += 1

        return ViaRelocation(
            original_via=via,
            new_position=best_pos,
            new_via=new_via,
            affected_route=route,
            modified_segments=modified_segments,
            success=True,
        )

    def try_rip_reroute(
        self,
        conflict: ViaConflict,
        route_net_fn: RouteNetFunction | None = None,
    ) -> RipRerouteResult:
        """Try to rip up the blocking route and re-route after the blocked net.

        This is the more aggressive strategy:
        1. Remove the blocking via's entire route from the grid
        2. Route the blocked net (which should now succeed)
        3. Re-route the ripped-up net (which may find a different path)

        Args:
            conflict: The via conflict to resolve.
            route_net_fn: Function to route a net. Signature: (net_id) -> list[Route].
                If None, only the rip-up is performed.

        Returns:
            RipRerouteResult (check .success for outcome).
        """
        self._stats.rip_reroutes_attempted += 1
        result = RipRerouteResult()

        route = conflict.via_route
        if route is None:
            return result

        # Step 1: Rip up the blocking route
        self.grid.unmark_route(route)
        result.ripped_route = route
        result.ripped_net = conflict.blocking_net

        if route_net_fn is None:
            # Just the rip-up, no re-routing
            return result

        # Step 2: Route the blocked net
        blocked_routes = route_net_fn(conflict.blocked_net)
        if blocked_routes:
            result.blocked_net_routed = True
            result.new_blocked_routes = blocked_routes

        # Step 3: Re-route the ripped net
        ripped_routes = route_net_fn(conflict.blocking_net)
        if ripped_routes:
            result.ripped_net_rerouted = True
            result.new_ripped_routes = ripped_routes
        else:
            # Failed to re-route - restore original route
            self.grid.mark_route(route)
            # Also undo the blocked net routes
            for r in blocked_routes:
                self.grid.unmark_route(r)
            result.blocked_net_routed = False
            result.new_blocked_routes = []
            return result

        result.success = result.blocked_net_routed and result.ripped_net_rerouted
        if result.success:
            self._stats.rip_reroutes_succeeded += 1
            self._stats.nets_unblocked += 1

        return result

    def resolve_conflicts(
        self,
        conflicts: list[ViaConflict],
        strategy: ViaConflictStrategy = ViaConflictStrategy.RELOCATE,
        route_net_fn: RouteNetFunction | None = None,
    ) -> list[ViaRelocation | RipRerouteResult]:
        """Resolve a list of via conflicts using the specified strategy.

        Tries each conflict in order. For RELOCATE strategy, falls back
        to RIP_REROUTE if relocation fails (when route_net_fn is provided).

        Args:
            conflicts: List of via conflicts to resolve.
            strategy: Resolution strategy to use.
            route_net_fn: Function to route a net (required for RIP_REROUTE).

        Returns:
            List of resolution results.
        """
        results: list[ViaRelocation | RipRerouteResult] = []

        for conflict in conflicts:
            if strategy == ViaConflictStrategy.NONE:
                continue

            if strategy == ViaConflictStrategy.RELOCATE:
                relocation = self.try_relocate(conflict)
                if relocation.success:
                    results.append(relocation)
                    continue

                # Fall back to rip-reroute if relocation failed
                if route_net_fn is not None:
                    rip_result = self.try_rip_reroute(conflict, route_net_fn)
                    results.append(rip_result)

            elif strategy == ViaConflictStrategy.RIP_REROUTE:
                if route_net_fn is not None:
                    rip_result = self.try_rip_reroute(conflict, route_net_fn)
                    results.append(rip_result)

        return results

    def _generate_relocation_candidates(
        self,
        via_x: float,
        via_y: float,
        pad_x: float,
        pad_y: float,
        max_distance: float,
        num_candidates: int = 16,
    ) -> list[tuple[float, float]]:
        """Generate candidate positions for via relocation.

        Candidates are placed in a ring around the original via position,
        biased away from the blocked pad.

        Args:
            via_x, via_y: Original via position.
            pad_x, pad_y: Blocked pad position.
            max_distance: Maximum relocation distance.
            num_candidates: Number of candidates to generate.

        Returns:
            List of (x, y) candidate positions.
        """
        candidates: list[tuple[float, float]] = []

        # Direction from pad to via (move via further from pad)
        dx = via_x - pad_x
        dy = via_y - pad_y
        dist = math.sqrt(dx * dx + dy * dy)
        if dist > 0:
            dx /= dist
            dy /= dist
        else:
            dx, dy = 1.0, 0.0  # Default direction

        # Generate candidates at multiple distances and angles
        distances = [
            max_distance * 0.3,
            max_distance * 0.5,
            max_distance * 0.7,
            max_distance * 1.0,
        ]

        for d in distances:
            for i in range(num_candidates):
                angle = 2 * math.pi * i / num_candidates
                # Bias toward the direction away from the pad
                biased_angle = angle
                cx = via_x + d * math.cos(biased_angle)
                cy = via_y + d * math.sin(biased_angle)

                # Snap to grid
                gx, gy = self.grid.world_to_grid(cx, cy)
                snapped_x, snapped_y = self.grid.grid_to_world(gx, gy)
                candidates.append((snapped_x, snapped_y))

        # Deduplicate (grid snapping may merge candidates)
        seen: set[tuple[float, float]] = set()
        unique: list[tuple[float, float]] = []
        for pos in candidates:
            key = (round(pos[0], 4), round(pos[1], 4))
            if key not in seen:
                seen.add(key)
                unique.append(pos)

        return unique

    def _position_is_blocked(
        self,
        x: float,
        y: float,
        exclude_net: int,
    ) -> bool:
        """Check if a position is blocked by obstacles (excluding the given net).

        Args:
            x, y: World coordinates to check.
            exclude_net: Net ID to exclude from blocking check.

        Returns:
            True if the position is blocked by another net or obstacle.
        """
        gx, gy = self.grid.world_to_grid(x, y)
        radius = int(
            (self.rules.via_diameter / 2 + self.rules.via_clearance)
            / self.grid.resolution
        )

        for layer_idx in range(self.grid.num_layers):
            for dy_offset in range(-radius, radius + 1):
                for dx_offset in range(-radius, radius + 1):
                    nx, ny = gx + dx_offset, gy + dy_offset
                    if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                        continue
                    cell = self.grid.grid[layer_idx][ny][nx]
                    if cell.blocked and cell.net != exclude_net and cell.net != 0:
                        return True
                    if cell.is_obstacle:
                        return True

        return False

    def _update_route_via_position(
        self,
        route: Route,
        old_via: Via,
        new_via: Via,
    ) -> list[Segment]:
        """Update a route to use a new via position.

        Finds segments connected to the old via and updates their
        endpoints to connect to the new via position.

        Args:
            route: The route to modify.
            old_via: The original via being replaced.
            new_via: The new via with updated position.

        Returns:
            List of segments that were modified.
        """
        tol = 1e-4
        modified: list[Segment] = []

        # First, unmark the old route from the grid
        self.grid.unmark_route(route)

        # Replace the via in the route
        for i, via in enumerate(route.vias):
            if (
                abs(via.x - old_via.x) < tol
                and abs(via.y - old_via.y) < tol
            ):
                route.vias[i] = new_via
                break

        # Update connected segments
        for seg in route.segments:
            updated = False

            # Check if segment ends at old via position (on 'from' layer)
            if (
                abs(seg.x2 - old_via.x) < tol
                and abs(seg.y2 - old_via.y) < tol
                and seg.layer == old_via.layers[0]
            ):
                seg.x2 = new_via.x
                seg.y2 = new_via.y
                updated = True

            # Check if segment starts at old via position (on 'to' layer)
            if (
                abs(seg.x1 - old_via.x) < tol
                and abs(seg.y1 - old_via.y) < tol
                and seg.layer == old_via.layers[1]
            ):
                seg.x1 = new_via.x
                seg.y1 = new_via.y
                updated = True

            if updated:
                modified.append(seg)

        # Re-mark the updated route on the grid
        self.grid.mark_route(route)

        return modified


# Type alias for the route function callback
RouteNetFunction = type(None)  # Placeholder - actual type is Callable[[int], list[Route]]

try:
    from typing import Callable

    RouteNetFunction = Callable[[int], list[Route]]  # type: ignore[misc]
except ImportError:
    pass
