"""
High-level autorouter API.

This module provides:
- Autorouter: High-level autorouter for complete PCBs with net class awareness
- AdaptiveAutorouter: Autorouter that automatically increases layer count if needed
- RoutingResult: Result of a routing attempt with convergence metrics

For PCB file I/O, see the io module:
- route_pcb: Function to route a PCB given component placements
- load_pcb_for_routing: Function to load a KiCad PCB file for routing
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

from .bus import (
    BusGroup,
    BusRoutingConfig,
    BusRoutingMode,
    analyze_buses,
    detect_bus_signals,
    group_buses,
)
from .diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    LengthMismatchWarning,
    analyze_differential_pairs,
    detect_differential_pairs,
)
from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .pathfinder import Router
from .primitives import Obstacle, Pad, Route, Segment
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting
from .zones import ZoneManager


class Autorouter:
    """High-level autorouter for complete PCBs with net class awareness."""

    def __init__(
        self,
        width: float,
        height: float,
        origin_x: float = 0,
        origin_y: float = 0,
        rules: DesignRules | None = None,
        net_class_map: dict[str, NetClassRouting] | None = None,
        layer_stack: LayerStack | None = None,
    ):
        self.rules = rules or DesignRules()
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.layer_stack = layer_stack
        self.grid = RoutingGrid(
            width, height, self.rules, origin_x, origin_y, layer_stack=layer_stack
        )
        self.router = Router(self.grid, self.rules, self.net_class_map)

        # Zone management
        self.zone_manager = ZoneManager(self.grid, self.rules)

        self.pads: dict[tuple[str, str], Pad] = {}  # (ref, pin) -> Pad
        self.nets: dict[int, list[tuple[str, str]]] = {}  # net -> [(ref, pin), ...]
        self.net_names: dict[int, str] = {}  # net_id -> net_name
        self.routes: list[Route] = []

    def add_component(self, ref: str, pads: list[dict]):
        """Add a component's pads.

        Args:
            ref: Component reference (e.g., "U1")
            pads: List of pad dicts with keys: number, x, y, width, height, net, net_name, layer
        """
        for pad_info in pads:
            pad = Pad(
                x=pad_info["x"],
                y=pad_info["y"],
                width=pad_info.get("width", 0.5),
                height=pad_info.get("height", 0.5),
                net=pad_info.get("net", 0),
                net_name=pad_info.get("net_name", ""),
                layer=pad_info.get("layer", Layer.F_CU),
                ref=ref,
                through_hole=pad_info.get("through_hole", False),
                drill=pad_info.get("drill", 0.0),
            )
            key = (ref, str(pad_info["number"]))
            self.pads[key] = pad

            # Track nets and their names
            if pad.net > 0:
                if pad.net not in self.nets:
                    self.nets[pad.net] = []
                self.nets[pad.net].append(key)
                # Track net name for priority sorting
                if pad.net_name:
                    self.net_names[pad.net] = pad.net_name

            # Add as obstacle
            self.grid.add_pad(pad)

    def add_obstacle(
        self, x: float, y: float, width: float, height: float, layer: Layer = Layer.F_CU
    ):
        """Add an obstacle (keepout area, mounting hole, etc.)."""
        obs = Obstacle(x, y, width, height, layer)
        self.grid.add_obstacle(obs)

    # =========================================================================
    # ZONE (COPPER POUR) SUPPORT
    # =========================================================================

    def add_zones(self, zones: list) -> None:
        """Add zones (copper pours) to the router.

        Fills zones onto the routing grid with thermal reliefs for same-net pads.
        After calling this, routing will be zone-aware:
        - Routes can pass through same-net zones with reduced cost
        - Routes are blocked by other-net zones

        Args:
            zones: List of Zone objects from PCB schema
        """
        # Get all pad objects for thermal relief generation
        pad_list = list(self.pads.values())

        # Fill zones onto grid
        filled = self.zone_manager.fill_all_zones(zones, pad_list, apply_to_grid=True)

        zone_count = len(filled)
        total_cells = sum(len(z.filled_cells) for z in filled)
        print(f"  Zones: {zone_count} zones, {total_cells} cells filled")

    def clear_zones(self) -> None:
        """Remove all zone markings from the grid.

        Call this before re-adding zones or to disable zone-aware routing.
        """
        self.zone_manager.clear_all_zones()

    def get_zone_statistics(self) -> dict:
        """Get statistics about filled zones.

        Returns:
            Dictionary with zone statistics including:
            - zone_count: Number of zones
            - total_cells: Total cells filled
            - zones: Per-zone details (net, layer, cells, priority)
        """
        return self.zone_manager.get_zone_statistics()

    def _create_intra_ic_routes(
        self, net: int, pads: list[tuple[str, str]]
    ) -> tuple[list[Route], set[int]]:
        """Create direct routes for same-IC pins on the same net.

        For pins on the same IC that share a net (e.g., U10 pins 1,3,4 on SYNC_L),
        create direct short segments connecting them. This bypasses the A* router
        for these tight connections where blocking areas overlap.

        Args:
            net: Net ID
            pads: List of (ref, pin) tuples for this net

        Returns:
            Tuple of (routes created, set of pad indices that were connected)
        """
        routes: list[Route] = []
        connected_indices: set[int] = set()

        # Group pads by component reference
        by_ref: dict[str, list[int]] = {}
        for i, (ref, _pin) in enumerate(pads):
            if ref not in by_ref:
                by_ref[ref] = []
            by_ref[ref].append(i)

        # For each component with multiple same-net pins, create direct connections
        for ref, indices in by_ref.items():
            if len(indices) < 2:
                continue

            # Get pad objects
            pad_objs = [self.pads[pads[i]] for i in indices]
            net_name = pad_objs[0].net_name

            # Connect all pads on this component with short stubs
            # Use chain topology: pad0 -> pad1 -> pad2 -> ...
            # Sort by position to get sensible ordering
            sorted_pairs = sorted(
                zip(indices, pad_objs, strict=False), key=lambda p: (p[1].x, p[1].y)
            )

            for j in range(len(sorted_pairs) - 1):
                idx1, pad1 = sorted_pairs[j]
                idx2, pad2 = sorted_pairs[j + 1]

                # Create a direct segment between these pads
                # Check distance - only do this for close pins (< 3mm)
                # SOT-23-5 is ~2.5mm wide, TSSOP pins can be ~2mm apart
                dist = math.sqrt((pad2.x - pad1.x) ** 2 + (pad2.y - pad1.y) ** 2)
                if dist > 3.0:
                    continue  # Too far apart, let normal router handle it

                # Create route with single segment
                route = Route(net=net, net_name=net_name)
                seg = Segment(
                    x1=pad1.x,
                    y1=pad1.y,
                    x2=pad2.x,
                    y2=pad2.y,
                    width=self.rules.trace_width,
                    layer=pad1.layer,  # Use pad layer (typically F.Cu for SMD)
                    net=net,
                    net_name=net_name,
                )
                route.segments.append(seg)
                routes.append(route)

                # Mark these pads as connected
                connected_indices.add(idx1)
                connected_indices.add(idx2)

                print(
                    f"  Intra-IC route: {ref} pins {pads[idx1][1]}->{pads[idx2][1]} ({dist:.2f}mm)"
                )

        return routes, connected_indices

    def route_net(self, net: int, use_mst: bool = True) -> list[Route]:
        """Route all connections for a net.

        Args:
            net: Net ID to route
            use_mst: If True, use minimum spanning tree to minimize total length.
                     If False, use star topology from first pad.
        """
        if net not in self.nets:
            return []

        pads = self.nets[net]
        if len(pads) < 2:
            return []

        routes: list[Route] = []

        # First, handle intra-IC connections (same component, same net pins)
        # These are short stubs that bypass normal routing
        intra_routes, connected_indices = self._create_intra_ic_routes(net, pads)
        for route in intra_routes:
            self.grid.mark_route(route)
            routes.append(route)
            self.routes.append(route)

        # If all pads are connected within ICs, we may still need inter-IC routing
        # Build the reduced pad list for MST: use one representative pad per connected group
        if connected_indices:
            # Group connected pads by their component reference
            ref_to_indices: dict[str, list[int]] = {}
            for i in connected_indices:
                ref = pads[i][0]
                if ref not in ref_to_indices:
                    ref_to_indices[ref] = []
                ref_to_indices[ref].append(i)

            # Create reduced pads list: one representative per connected group + unconnected pads
            reduced_pad_indices: list[int] = []
            for ref, indices in ref_to_indices.items():
                # Use first pad from each intra-IC group as representative
                reduced_pad_indices.append(indices[0])

            # Add pads that weren't connected intra-IC
            for i in range(len(pads)):
                if i not in connected_indices:
                    reduced_pad_indices.append(i)

            # If we now have just one group, no more routing needed
            if len(reduced_pad_indices) < 2:
                return routes

            # Use the reduced pad list for inter-IC routing
            pads_for_routing = [pads[i] for i in reduced_pad_indices]
        else:
            pads_for_routing = pads

        if use_mst and len(pads_for_routing) > 2:
            # MST-based routing: connect nearest unconnected pads first
            # This minimizes total wirelength and often finds easier routes

            # Build list of pad objects from reduced pads list
            pad_objs = [self.pads[p] for p in pads_for_routing]
            n = len(pad_objs)

            # Prim's algorithm for MST
            connected: set[int] = {0}  # Start with first pad
            unconnected = set(range(1, n))
            mst_edges: list[tuple[int, int]] = []

            while unconnected:
                # Find shortest edge from connected to unconnected
                best_dist = float("inf")
                best_edge: tuple[int, int] | None = None

                for i in connected:
                    for j in unconnected:
                        dist = abs(pad_objs[i].x - pad_objs[j].x) + abs(
                            pad_objs[i].y - pad_objs[j].y
                        )
                        if dist < best_dist:
                            best_dist = dist
                            best_edge = (i, j)

                if best_edge:
                    i, j = best_edge
                    mst_edges.append((i, j))
                    connected.add(j)
                    unconnected.remove(j)

            # Route MST edges (shortest connections first)
            mst_edges.sort(
                key=lambda e: abs(pad_objs[e[0]].x - pad_objs[e[1]].x)
                + abs(pad_objs[e[0]].y - pad_objs[e[1]].y)
            )

            for i, j in mst_edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]
                route = self.router.route(source_pad, target_pad)

                if route:
                    self.grid.mark_route(route)
                    routes.append(route)
                    self.routes.append(route)
                else:
                    print(
                        f"  Warning: Could not route net {net} from {pads_for_routing[i]} to {pads_for_routing[j]}"
                    )
        else:
            # Star topology or 2-pin net
            first_pad = self.pads[pads_for_routing[0]]

            for i in range(1, len(pads_for_routing)):
                target_pad = self.pads[pads_for_routing[i]]
                route = self.router.route(first_pad, target_pad)

                if route:
                    self.grid.mark_route(route)
                    routes.append(route)
                    self.routes.append(route)
                else:
                    print(
                        f"  Warning: Could not route net {net} from {pads_for_routing[0]} to {pads_for_routing[i]}"
                    )

        return routes

    def _get_net_priority(self, net_id: int) -> tuple[int, int]:
        """Get routing priority for a net (lower = higher priority).

        Returns (priority, pad_count) for sorting.
        """
        net_name = self.net_names.get(net_id, "")
        net_class = self.net_class_map.get(net_name)

        if net_class:
            priority = net_class.priority
        else:
            priority = 10  # Default low priority

        pad_count = len(self.nets.get(net_id, []))
        return (priority, pad_count)

    def route_all(
        self,
        net_order: list[int] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route all nets in priority order.

        Args:
            net_order: Optional list specifying routing order.
                       If None, uses net class priority then pad count.
            progress_callback: Optional callback for progress reporting.
                Signature: (progress: float, message: str, cancelable: bool) -> bool
                Returns False to cancel, True to continue.
        """
        if net_order is None:
            # Sort by: (1) net class priority, (2) pad count
            # Higher priority nets (power, clock) route first to get best paths
            net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))

        # Filter out "no net"
        nets_to_route = [n for n in net_order if n != 0]
        total_nets = len(nets_to_route)

        all_routes: list[Route] = []
        for i, net in enumerate(nets_to_route):
            # Report progress
            if progress_callback is not None:
                progress = i / total_nets if total_nets > 0 else 0.0
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    # Cancelled
                    break

            routes = self.route_net(net)
            all_routes.extend(routes)
            if routes:
                print(
                    f"  Net {net}: {len(routes)} routes, "
                    f"{sum(len(r.segments) for r in routes)} segments, "
                    f"{sum(len(r.vias) for r in routes)} vias"
                )

        # Final progress report
        if progress_callback is not None:
            routed_count = len({r.net for r in all_routes})
            progress_callback(1.0, f"Routed {routed_count}/{total_nets} nets", False)

        return all_routes

    def route_all_negotiated(
        self,
        max_iterations: int = 10,
        initial_present_factor: float = 0.5,
        present_factor_increment: float = 0.5,
        history_increment: float = 1.0,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route all nets using PathFinder-style negotiated congestion.

        This algorithm:
        1. Routes all nets initially, allowing temporary resource sharing
        2. Identifies overused cells (conflicts)
        3. Rips up routes with conflicts
        4. Reroutes with increasing present cost factor
        5. Updates history costs for persistently congested cells
        6. Repeats until no overflow or max iterations reached

        Args:
            max_iterations: Maximum negotiation iterations
            initial_present_factor: Starting multiplier for present sharing cost
            present_factor_increment: How much to increase present factor each iteration
            history_increment: Amount to add to history cost for overused cells
            progress_callback: Optional callback for progress reporting.
                Signature: (progress: float, message: str, cancelable: bool) -> bool
                Returns False to cancel, True to continue.

        Returns:
            List of all successfully routed routes
        """
        print("\n=== Negotiated Congestion Routing ===")
        print(f"  Max iterations: {max_iterations}")
        print(f"  Present factor: {initial_present_factor} + {present_factor_increment}/iter")

        # Get priority-sorted net order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]  # Skip "no net"

        # Track routes per net for rip-up
        net_routes: dict[int, list[Route]] = {}

        # Initial routing pass with sharing allowed
        print("\n--- Iteration 0: Initial routing with sharing ---")
        present_factor = initial_present_factor

        # Report initial progress
        if progress_callback is not None:
            if not progress_callback(0.0, "Initial routing pass", True):
                return list(self.routes)

        for net in net_order:
            routes = self._route_net_negotiated(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        # Check initial overflow
        overflow = self.grid.get_total_overflow()
        overused = self.grid.find_overused_cells()
        print(f"  Routed {len(net_routes)}/{len(net_order)} nets, overflow: {overflow}")

        if overflow == 0:
            print("  No conflicts - routing complete!")
            if progress_callback is not None:
                progress_callback(1.0, "Routing complete - no conflicts", False)
            return list(self.routes)

        # Iterative negotiation
        for iteration in range(1, max_iterations + 1):
            # Report progress for this iteration
            if progress_callback is not None:
                progress = iteration / (max_iterations + 1)
                if not progress_callback(
                    progress, f"Iteration {iteration}/{max_iterations}: rip-up and reroute", True
                ):
                    break  # Cancelled

            print(f"\n--- Iteration {iteration}: Rip-up and reroute ---")
            present_factor += present_factor_increment

            # Update history costs for currently overused cells
            self.grid.update_history_costs(history_increment)

            # Find nets with routes through overused cells
            overused_set = {(x, y, layer) for x, y, layer, _ in overused}
            nets_to_reroute: list[int] = []

            for net, routes in net_routes.items():
                needs_reroute = False
                for route in routes:
                    for seg in route.segments:
                        # Check if segment passes through overused cell
                        gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                        gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                        layer = seg.layer.value

                        # Sample points along segment
                        steps = max(abs(gx2 - gx1), abs(gy2 - gy1), 1)
                        for i in range(steps + 1):
                            t = i / steps
                            gx = int(gx1 + t * (gx2 - gx1))
                            gy = int(gy1 + t * (gy2 - gy1))
                            if (gx, gy, layer) in overused_set:
                                needs_reroute = True
                                break
                        if needs_reroute:
                            break
                    if needs_reroute:
                        break

                if needs_reroute:
                    nets_to_reroute.append(net)

            print(f"  Ripping up {len(nets_to_reroute)} nets with conflicts")

            # Rip up conflicting nets
            for net in nets_to_reroute:
                for route in net_routes.get(net, []):
                    self.grid.unmark_route_usage(route)
                    self.grid.unmark_route(route)
                    if route in self.routes:
                        self.routes.remove(route)
                net_routes[net] = []

            # Reroute in priority order with higher present cost
            rerouted_count = 0
            for net in nets_to_reroute:
                routes = self._route_net_negotiated(net, present_factor)
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        self.routes.append(route)

            # Check new overflow
            overflow = self.grid.get_total_overflow()
            overused = self.grid.find_overused_cells()
            print(f"  Rerouted {rerouted_count}/{len(nets_to_reroute)} nets, overflow: {overflow}")

            if overflow == 0:
                print(f"  Convergence achieved at iteration {iteration}!")
                break

        # Final statistics
        successful_nets = sum(1 for routes in net_routes.values() if routes)
        print("\n=== Negotiated Routing Complete ===")
        print(f"  Total nets: {len(net_order)}")
        print(f"  Successful: {successful_nets}")
        print(f"  Final overflow: {overflow}")

        # Final progress report
        if progress_callback is not None:
            status = "converged" if overflow == 0 else f"overflow={overflow}"
            progress_callback(
                1.0, f"Routing complete: {successful_nets}/{len(net_order)} nets ({status})", False
            )

        return list(self.routes)

    def _route_net_negotiated(self, net: int, present_cost_factor: float) -> list[Route]:
        """Route a single net in negotiated mode.

        Similar to route_net but uses negotiated routing with sharing allowed.
        """
        if net not in self.nets:
            return []

        pads = self.nets[net]
        if len(pads) < 2:
            return []

        routes: list[Route] = []

        # Handle intra-IC connections first (these don't use negotiated mode)
        intra_routes, connected_indices = self._create_intra_ic_routes(net, pads)
        for route in intra_routes:
            self.grid.mark_route(route)
            routes.append(route)

        # Build reduced pad list
        if connected_indices:
            ref_to_indices: dict[str, list[int]] = {}
            for i in connected_indices:
                ref = pads[i][0]
                if ref not in ref_to_indices:
                    ref_to_indices[ref] = []
                ref_to_indices[ref].append(i)

            reduced_pad_indices: list[int] = []
            for ref, indices in ref_to_indices.items():
                reduced_pad_indices.append(indices[0])
            for i in range(len(pads)):
                if i not in connected_indices:
                    reduced_pad_indices.append(i)

            if len(reduced_pad_indices) < 2:
                return routes

            pads_for_routing = [pads[i] for i in reduced_pad_indices]
        else:
            pads_for_routing = pads

        # MST-based routing with negotiated mode
        if len(pads_for_routing) > 2:
            pad_objs = [self.pads[p] for p in pads_for_routing]
            n = len(pad_objs)

            # Prim's algorithm for MST
            connected: set[int] = {0}
            unconnected = set(range(1, n))
            mst_edges: list[tuple[int, int]] = []

            while unconnected:
                best_dist = float("inf")
                best_edge: tuple[int, int] | None = None

                for i in connected:
                    for j in unconnected:
                        dist = abs(pad_objs[i].x - pad_objs[j].x) + abs(
                            pad_objs[i].y - pad_objs[j].y
                        )
                        if dist < best_dist:
                            best_dist = dist
                            best_edge = (i, j)

                if best_edge:
                    i, j = best_edge
                    mst_edges.append((i, j))
                    connected.add(j)
                    unconnected.remove(j)

            # Route MST edges
            mst_edges.sort(
                key=lambda e: abs(pad_objs[e[0]].x - pad_objs[e[1]].x)
                + abs(pad_objs[e[0]].y - pad_objs[e[1]].y)
            )

            for i, j in mst_edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]
                route = self.router.route(
                    source_pad,
                    target_pad,
                    negotiated_mode=True,
                    present_cost_factor=present_cost_factor,
                )
                if route:
                    self.grid.mark_route(route)
                    routes.append(route)
        else:
            # 2-pin net
            first_pad = self.pads[pads_for_routing[0]]
            target_pad = self.pads[pads_for_routing[1]]
            route = self.router.route(
                first_pad,
                target_pad,
                negotiated_mode=True,
                present_cost_factor=present_cost_factor,
            )
            if route:
                self.grid.mark_route(route)
                routes.append(route)

        return routes

    # =========================================================================
    # MONTE CARLO MULTI-START ROUTING
    # =========================================================================

    def _reset_for_new_trial(self):
        """Reset the router to initial state (after pads but before routes).

        This recreates the grid and router, preserving pad/net information,
        allowing fresh routing attempts with different orderings.
        """
        # Get grid dimensions from current grid
        width = self.grid.width
        height = self.grid.height
        origin_x = self.grid.origin_x
        origin_y = self.grid.origin_y

        # Recreate grid and router
        self.grid = RoutingGrid(width, height, self.rules, origin_x, origin_y)
        self.router = Router(self.grid, self.rules, self.net_class_map)

        # Recreate zone manager with new grid
        self.zone_manager = ZoneManager(self.grid, self.rules)

        # Re-add all pads as obstacles
        for pad in self.pads.values():
            self.grid.add_pad(pad)

        # Clear routes
        self.routes = []

    def _shuffle_within_tiers(self, net_order: list[int]) -> list[int]:
        """Shuffle nets but keep priority ordering.

        Nets within the same priority tier are shuffled randomly,
        but the tier ordering is preserved.
        """
        # Group by priority tier
        tiers: dict[int, list[int]] = {}
        for net in net_order:
            priority, _ = self._get_net_priority(net)
            if priority not in tiers:
                tiers[priority] = []
            tiers[priority].append(net)

        # Shuffle within each tier and reassemble
        result: list[int] = []
        for priority in sorted(tiers.keys()):
            tier_nets = tiers[priority].copy()
            random.shuffle(tier_nets)
            result.extend(tier_nets)

        return result

    def _evaluate_solution(self, routes: list[Route]) -> float:
        """Score a routing solution (higher = better).

        Scoring prioritizes:
        1. Completion rate (primary - weighted heavily)
        2. Lower via count (secondary)
        3. Shorter total length (tertiary)
        """
        if not routes:
            return 0.0

        total_nets = len([n for n in self.nets if n != 0])
        routed_nets = len({r.net for r in routes})
        completion_rate = routed_nets / total_nets if total_nets > 0 else 0

        total_vias = sum(len(r.vias) for r in routes)
        total_length = sum(
            math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for r in routes for s in r.segments
        )

        # Completion rate is most important (1000x weight)
        # Penalize vias and length slightly
        return completion_rate * 1000 - total_vias * 0.1 - total_length * 0.01

    def route_all_monte_carlo(
        self,
        num_trials: int = 10,
        use_negotiated: bool = False,
        seed: int | None = None,
        verbose: bool = True,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route using Monte Carlo multi-start with randomized net orderings.

        Tries multiple random net orderings within priority tiers,
        keeping the best result. This helps escape local minima
        caused by unfortunate routing order.

        Args:
            num_trials: Number of random orderings to try
            use_negotiated: Use negotiated congestion per trial (if available)
            seed: Random seed for reproducibility (None = random)
            verbose: Print progress information
            progress_callback: Optional callback for progress reporting.
                Signature: (progress: float, message: str, cancelable: bool) -> bool
                Returns False to cancel, True to continue.

        Returns:
            Best routes found across all trials
        """
        if seed is not None:
            random.seed(seed)

        if verbose:
            print("\n=== Monte Carlo Multi-Start Routing ===")
            print(f"  Trials: {num_trials}")
            print(f"  Negotiated: {use_negotiated}")

        # Get base priority order
        base_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        base_order = [n for n in base_order if n != 0]

        best_routes: list[Route] | None = None
        best_score = float("-inf")
        best_trial = -1

        for trial in range(num_trials):
            # Report progress for this trial
            if progress_callback is not None:
                progress = trial / num_trials
                if not progress_callback(progress, f"Trial {trial + 1}/{num_trials}", True):
                    break  # Cancelled

            # Reset grid state for this trial
            self._reset_for_new_trial()

            # Determine net order for this trial
            if trial == 0:
                # First trial: use standard priority order
                net_order = base_order.copy()
            else:
                # Subsequent trials: shuffle within priority tiers
                net_order = self._shuffle_within_tiers(base_order)

            # Route with this ordering
            if use_negotiated and hasattr(self, "route_all_negotiated"):
                # Use negotiated congestion if available
                routes = self.route_all_negotiated()
            else:
                # Use standard single-pass routing
                routes = self.route_all(net_order)

            # Score this solution
            score = self._evaluate_solution(routes)
            routed = len({r.net for r in routes})
            total = len(base_order)

            if verbose:
                status = "NEW BEST" if score > best_score else ""
                print(
                    f"  Trial {trial + 1}: {routed}/{total} nets, "
                    f"{sum(len(r.vias) for r in routes)} vias, "
                    f"score={score:.2f} {status}"
                )

            if score > best_score:
                best_score = score
                best_routes = routes.copy()
                best_trial = trial

        if verbose:
            print(f"\n  Best: Trial {best_trial + 1} (score={best_score:.2f})")

        # Restore best solution
        self.routes = best_routes if best_routes else []

        # Final progress report
        if progress_callback is not None:
            routed = len({r.net for r in self.routes}) if self.routes else 0
            progress_callback(
                1.0, f"Best result: trial {best_trial + 1}, {routed}/{len(base_order)} nets", False
            )

        return self.routes

    def route_all_advanced(
        self,
        monte_carlo_trials: int = 0,
        use_negotiated: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Unified entry point for advanced routing strategies.

        Args:
            monte_carlo_trials: Number of random orderings to try (0 = single pass)
            use_negotiated: Use negotiated congestion routing
            progress_callback: Optional callback for progress reporting.

        Returns:
            Best routes found
        """
        if monte_carlo_trials > 0:
            return self.route_all_monte_carlo(
                num_trials=monte_carlo_trials,
                use_negotiated=use_negotiated,
                progress_callback=progress_callback,
            )
        elif use_negotiated and hasattr(self, "route_all_negotiated"):
            return self.route_all_negotiated(progress_callback=progress_callback)
        else:
            return self.route_all(progress_callback=progress_callback)

    def to_sexp(self) -> str:
        """Generate KiCad S-expressions for all routes."""
        parts = []
        for route in self.routes:
            parts.append(route.to_sexp())
        return "\n\t".join(parts)

    def get_statistics(self) -> dict:
        """Get routing statistics including congestion metrics."""
        total_segments = sum(len(r.segments) for r in self.routes)
        total_vias = sum(len(r.vias) for r in self.routes)
        total_length = sum(
            math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2)
            for r in self.routes
            for s in r.segments
        )

        # Get congestion stats
        congestion_stats = self.grid.get_congestion_map()

        return {
            "routes": len(self.routes),
            "segments": total_segments,
            "vias": total_vias,
            "total_length_mm": total_length,
            "nets_routed": len({r.net for r in self.routes}),
            "max_congestion": congestion_stats["max_congestion"],
            "avg_congestion": congestion_stats["avg_congestion"],
            "congested_regions": congestion_stats["congested_regions"],
        }

    # =========================================================================
    # BUS ROUTING SUPPORT
    # =========================================================================

    def detect_buses(self, min_bus_width: int = 2) -> list[BusGroup]:
        """Detect bus signals from net names.

        Args:
            min_bus_width: Minimum number of signals to form a bus

        Returns:
            List of detected BusGroup objects
        """
        signals = detect_bus_signals(self.net_names, min_bus_width)
        return group_buses(signals, min_bus_width)

    def get_bus_analysis(self) -> dict:
        """Get a summary of detected buses in the design.

        Returns:
            Dictionary with bus analysis information
        """
        return analyze_buses(self.net_names)

    def route_bus_group(
        self,
        bus_group: BusGroup,
        mode: BusRoutingMode = BusRoutingMode.PARALLEL,
        spacing: float | None = None,
    ) -> list[Route]:
        """Route all signals in a bus group together.

        Routes bus signals maintaining parallel alignment and consistent spacing.
        Signals are routed in bit order (LSB to MSB).

        Args:
            bus_group: BusGroup containing the signals to route
            mode: Routing mode (PARALLEL, STACKED, BUNDLED)
            spacing: Spacing between signals (None = auto)

        Returns:
            List of routes for all bus signals
        """
        if not bus_group.signals:
            return []

        # Calculate spacing if not provided
        if spacing is None:
            spacing = self.rules.trace_width + self.rules.trace_clearance

        routes: list[Route] = []
        print(f"\n  Routing bus {bus_group} ({mode.value} mode, spacing={spacing}mm)")

        # Get net IDs in bit order
        net_ids = bus_group.get_net_ids()

        if mode == BusRoutingMode.PARALLEL:
            # Route signals in order, trying to maintain parallel alignment
            routes = self._route_bus_parallel(net_ids, bus_group.name, spacing)
        elif mode == BusRoutingMode.STACKED:
            # Alternate layers for dense routing
            routes = self._route_bus_stacked(net_ids, bus_group.name)
        else:  # BUNDLED
            # Route each signal individually, closest packing
            for net_id in net_ids:
                net_routes = self.route_net(net_id)
                routes.extend(net_routes)

        return routes

    def _route_bus_parallel(self, net_ids: list[int], bus_name: str, spacing: float) -> list[Route]:
        """Route bus signals in parallel with consistent spacing.

        Args:
            net_ids: List of net IDs in bit order
            bus_name: Name of the bus for logging
            spacing: Spacing between adjacent signals

        Returns:
            List of routes for all bus signals
        """
        routes: list[Route] = []

        # Route the first (LSB) signal normally - it sets the path
        if not net_ids:
            return routes

        first_routes = self.route_net(net_ids[0])
        routes.extend(first_routes)

        if not first_routes:
            print(f"    Warning: Could not route first bus signal {bus_name}[0]")
            # Try to route remaining signals individually
            for i, net_id in enumerate(net_ids[1:], 1):
                net_routes = self.route_net(net_id)
                routes.extend(net_routes)
            return routes

        # For remaining signals, try to route parallel to the first
        # This is a simplified parallel routing that routes each signal
        # individually but prioritizes paths that stay close to previous routes
        for i, net_id in enumerate(net_ids[1:], 1):
            print(f"    Signal [{i}] (net {net_id})...")
            net_routes = self.route_net(net_id)
            routes.extend(net_routes)
            if net_routes:
                print(
                    f"      Routed: {len(net_routes)} routes, "
                    f"{sum(len(r.segments) for r in net_routes)} segments"
                )
            else:
                print(f"      Warning: Could not route {bus_name}[{i}]")

        return routes

    def _route_bus_stacked(self, net_ids: list[int], bus_name: str) -> list[Route]:
        """Route bus signals on alternating layers.

        Args:
            net_ids: List of net IDs in bit order
            bus_name: Name of the bus for logging

        Returns:
            List of routes for all bus signals
        """
        routes: list[Route] = []

        # For stacked mode, we route each signal normally
        # The router will naturally use different layers as congestion increases
        for i, net_id in enumerate(net_ids):
            print(f"    Signal [{i}] (net {net_id})...")
            net_routes = self.route_net(net_id)
            routes.extend(net_routes)

        return routes

    def route_all_with_buses(
        self,
        bus_config: BusRoutingConfig | None = None,
        net_order: list[int] | None = None,
    ) -> list[Route]:
        """Route all nets with bus-aware routing.

        When bus routing is enabled, detected bus signals are routed together
        as groups before routing other nets.

        Args:
            bus_config: Configuration for bus routing (None = bus routing disabled)
            net_order: Optional order for non-bus nets

        Returns:
            List of all routes
        """
        if bus_config is None or not bus_config.enabled:
            # Fall back to standard routing
            return self.route_all(net_order)

        print("\n=== Bus-Aware Routing ===")

        # Detect buses
        bus_groups = self.detect_buses(bus_config.min_bus_width)
        bus_net_ids: set[int] = set()

        if bus_groups:
            print(f"  Detected {len(bus_groups)} bus groups:")
            for group in bus_groups:
                print(f"    - {group}: {group.width} bits")
                bus_net_ids.update(group.get_net_ids())
        else:
            print("  No bus signals detected")
            return self.route_all(net_order)

        # Calculate spacing
        spacing = bus_config.get_spacing(self.rules.trace_width, self.rules.trace_clearance)

        # Route bus groups first
        print("\n--- Routing bus signals ---")
        all_routes: list[Route] = []

        for group in bus_groups:
            bus_routes = self.route_bus_group(group, bus_config.mode, spacing)
            all_routes.extend(bus_routes)

        # Route remaining non-bus nets
        non_bus_nets = [n for n in self.nets if n not in bus_net_ids and n != 0]
        if non_bus_nets:
            print(f"\n--- Routing {len(non_bus_nets)} non-bus nets ---")
            if net_order:
                # Filter net_order to only include non-bus nets
                non_bus_order = [n for n in net_order if n in non_bus_nets]
            else:
                non_bus_order = sorted(non_bus_nets, key=lambda n: self._get_net_priority(n))

            for net in non_bus_order:
                routes = self.route_net(net)
                all_routes.extend(routes)
                if routes:
                    print(
                        f"  Net {net}: {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments"
                    )

        print("\n=== Bus-Aware Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Bus nets: {len(bus_net_ids)}")
        print(f"  Other nets: {len(non_bus_nets)}")

        return all_routes

    # =========================================================================
    # DIFFERENTIAL PAIR ROUTING
    # =========================================================================

    def detect_differential_pairs(self) -> list[DifferentialPair]:
        """Detect differential pairs from net names.

        Returns:
            List of detected DifferentialPair objects
        """
        return detect_differential_pairs(self.net_names)

    def analyze_differential_pairs(self) -> dict[str, any]:
        """Analyze net names for differential pairs.

        Returns:
            Dictionary with differential pair analysis information
        """
        return analyze_differential_pairs(self.net_names)

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair together.

        Routes both P and N signals, attempting to maintain consistent spacing
        and track length for matching.

        Args:
            pair: DifferentialPair to route
            spacing: Override spacing between traces (None = use pair rules)

        Returns:
            Tuple of (routes, warning) where warning is set if length mismatch
        """
        if pair.rules is None:
            return [], None

        # Use pair-specific spacing if not overridden
        if spacing is None:
            spacing = pair.rules.spacing

        routes: list[Route] = []
        print(f"\n  Routing differential pair {pair}")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        # Get net IDs
        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        # Route the positive signal first
        print(f"    Routing {pair.positive.net_name} (P)...")
        p_routes = self.route_net(p_net_id)
        routes.extend(p_routes)

        # Calculate length of P route
        p_length = self._calculate_route_length(p_routes)
        pair.routed_length_p = p_length
        print(f"      Length: {p_length:.3f}mm")

        # Route the negative signal
        print(f"    Routing {pair.negative.net_name} (N)...")
        n_routes = self.route_net(n_net_id)
        routes.extend(n_routes)

        # Calculate length of N route
        n_length = self._calculate_route_length(n_routes)
        pair.routed_length_n = n_length
        print(f"      Length: {n_length:.3f}mm")

        # Check length matching
        delta = pair.length_delta
        warning = None
        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def _calculate_route_length(self, routes: list[Route]) -> float:
        """Calculate total length of all segments in routes.

        Args:
            routes: List of Route objects

        Returns:
            Total length in mm
        """
        total_length = 0.0
        for route in routes:
            for seg in route.segments:
                # Calculate segment length
                dx = seg.x2 - seg.x1
                dy = seg.y2 - seg.y1
                total_length += math.sqrt(dx * dx + dy * dy)
        return total_length

    def route_all_with_diffpairs(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        net_order: list[int] | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning]]:
        """Route all nets with differential pair-aware routing.

        When differential pair routing is enabled, detected pairs are routed
        together before routing other nets.

        Args:
            diffpair_config: Configuration for differential pair routing
                (None = differential pair routing disabled)
            net_order: Optional order for non-differential nets

        Returns:
            Tuple of (routes, warnings) where warnings is a list of length
            mismatch warnings for pairs that exceeded tolerance
        """
        if diffpair_config is None or not diffpair_config.enabled:
            # Fall back to standard routing
            return self.route_all(net_order), []

        print("\n=== Differential Pair Routing ===")

        # Detect differential pairs
        diff_pairs = self.detect_differential_pairs()
        diff_net_ids: set[int] = set()

        if diff_pairs:
            print(f"  Detected {len(diff_pairs)} differential pairs:")
            for pair in diff_pairs:
                print(f"    - {pair}: {pair.pair_type.value}")
                p_id, n_id = pair.get_net_ids()
                diff_net_ids.add(p_id)
                diff_net_ids.add(n_id)
        else:
            print("  No differential pairs detected")
            return self.route_all(net_order), []

        # Apply config overrides to pair rules if specified
        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        # Route differential pairs first
        print("\n--- Routing differential pairs ---")
        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []

        for pair in diff_pairs:
            pair_routes, warning = self.route_differential_pair(pair, diffpair_config.spacing)
            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        # Route remaining non-differential nets
        non_diff_nets = [n for n in self.nets if n not in diff_net_ids and n != 0]
        if non_diff_nets:
            print(f"\n--- Routing {len(non_diff_nets)} non-differential nets ---")
            if net_order:
                # Filter net_order to only include non-diff nets
                non_diff_order = [n for n in net_order if n in non_diff_nets]
            else:
                non_diff_order = sorted(non_diff_nets, key=lambda n: self._get_net_priority(n))

            for net in non_diff_order:
                routes = self.route_net(net)
                all_routes.extend(routes)
                if routes:
                    print(
                        f"  Net {net}: {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments"
                    )

        print("\n=== Differential Pair Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Differential pair nets: {len(diff_net_ids)}")
        print(f"  Other nets: {len(non_diff_nets)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings


# =============================================================================
# ADAPTIVE LAYER AUTOROUTER
# =============================================================================


@dataclass
class RoutingResult:
    """Result of a routing attempt with convergence metrics."""

    routes: list[Route]
    layer_count: int
    layer_stack: LayerStack
    nets_requested: int
    nets_routed: int
    overflow: int
    converged: bool
    iterations_used: int
    statistics: dict

    @property
    def success_rate(self) -> float:
        """Fraction of nets successfully routed."""
        if self.nets_requested == 0:
            return 1.0
        return self.nets_routed / self.nets_requested

    def __str__(self) -> str:
        status = "CONVERGED" if self.converged else "NOT CONVERGED"
        return (
            f"RoutingResult({self.layer_count}L, {status}, "
            f"{self.nets_routed}/{self.nets_requested} nets, "
            f"overflow={self.overflow})"
        )


class AdaptiveAutorouter:
    """Autorouter that automatically increases layer count if routing fails.

    Tries routing with 2 layers first, then 4, then 6 if needed.
    This provides automatic complexity discovery - simpler boards stay cheap
    while complex boards get more routing resources.

    Example:
        adaptive = AdaptiveAutorouter(
            width=65, height=56,
            components=components,
            net_map=net_map,
            skip_nets=['GND', 'VCC'],
        )
        result = adaptive.route()
        print(f"Routed with {result.layer_count} layers")
        sexp = adaptive.to_sexp()
    """

    # Layer stack progression: 2 → 4 → 6
    LAYER_STACKS = [
        LayerStack.two_layer(),
        LayerStack.four_layer_sig_gnd_pwr_sig(),
        LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
    ]

    def __init__(
        self,
        width: float,
        height: float,
        components: list[dict],
        net_map: dict[str, int],
        rules: DesignRules | None = None,
        origin_x: float = 0,
        origin_y: float = 0,
        skip_nets: list[str] | None = None,
        max_layers: int = 6,
        verbose: bool = True,
    ):
        """Initialize adaptive autorouter.

        Args:
            width, height: Board dimensions in mm
            components: List of component dicts (ref, x, y, rotation, pads)
            net_map: Net name to number mapping
            rules: Design rules (optional)
            origin_x, origin_y: Board origin
            skip_nets: Nets to skip (e.g., power planes)
            max_layers: Maximum layers to try (2, 4, or 6)
            verbose: Print progress
        """
        self.width = width
        self.height = height
        self.components = components
        self.net_map = net_map.copy()
        self.rules = rules or DesignRules()
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.skip_nets = skip_nets or []
        self.max_layers = max_layers
        self.verbose = verbose

        # Result after routing
        self.result: RoutingResult | None = None
        self._autorouter: Autorouter | None = None

    def _create_autorouter(self, layer_stack: LayerStack) -> Autorouter:
        """Create an Autorouter instance with the given layer stack."""
        # Create grid with layer stack
        grid = RoutingGrid(
            self.width,
            self.height,
            self.rules,
            self.origin_x,
            self.origin_y,
            layer_stack=layer_stack,
        )

        # Create autorouter with custom grid
        autorouter = Autorouter.__new__(Autorouter)
        autorouter.rules = self.rules
        autorouter.net_class_map = DEFAULT_NET_CLASS_MAP
        autorouter.grid = grid
        autorouter.router = Router(grid, self.rules, autorouter.net_class_map)
        autorouter.pads = {}
        autorouter.nets = {}
        autorouter.net_names = {}
        autorouter.routes = []

        # Add components
        for comp in self.components:
            self._add_component_to_router(autorouter, comp)

        return autorouter

    def _add_component_to_router(self, router: Autorouter, comp: dict):
        """Add a component to the router with proper coordinate transformation."""
        ref = comp["ref"]
        cx, cy = comp["x"], comp["y"]
        rotation = comp.get("rotation", 0)

        # Transform pad positions
        rot_rad = math.radians(-rotation)  # KiCad uses clockwise
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        pads: list[dict] = []
        for pad in comp.get("pads", []):
            # Rotate pad position around component center
            px, py = pad["x"], pad["y"]
            rx = px * cos_r - py * sin_r
            ry = px * sin_r + py * cos_r

            net_name = pad.get("net", "")
            if net_name in self.skip_nets:
                continue

            net_num = self.net_map.get(net_name, 0)
            if net_num == 0 and net_name:
                net_num = len(self.net_map) + 1
                self.net_map[net_name] = net_num

            is_pth = pad.get("through_hole", False)
            pads.append(
                {
                    "number": pad["number"],
                    "x": cx + rx,
                    "y": cy + ry,
                    "width": pad.get("width", 0.5),
                    "height": pad.get("height", 0.5),
                    "net": net_num,
                    "net_name": net_name,
                    "layer": Layer.F_CU,
                    "through_hole": is_pth,
                    "drill": pad.get("drill", 1.0 if is_pth else 0.0),
                }
            )

        if pads:
            router.add_component(ref, pads)

    def _check_convergence(self, router: Autorouter, overflow: int) -> bool:
        """Check if routing has converged.

        Convergence criteria:
        1. All nets routed (nets_routed == nets_requested)
        2. No overflow (no resource conflicts)
        """
        nets_requested = len([n for n in router.nets if n != 0])
        nets_routed = len({r.net for r in router.routes if r.net != 0})

        return nets_routed >= nets_requested and overflow == 0

    def route(self, method: str = "negotiated", max_iterations: int = 10) -> RoutingResult:
        """Route the board, increasing layers as needed.

        Args:
            method: 'simple' or 'negotiated'
            max_iterations: Max iterations for negotiated routing

        Returns:
            RoutingResult with convergence information
        """
        # Determine which layer stacks to try
        stacks_to_try = [s for s in self.LAYER_STACKS if s.num_layers <= self.max_layers]

        for stack in stacks_to_try:
            if self.verbose:
                print(f"\n{'=' * 60}")
                print(f"TRYING {stack.num_layers}-LAYER ROUTING ({stack.name})")
                print(f"{'=' * 60}")

            # Create fresh autorouter with this layer stack
            router = self._create_autorouter(stack)

            # Count nets to route
            nets_requested = len([n for n in router.nets if n != 0])

            if self.verbose:
                print(f"  Nets to route: {nets_requested}")
                print(f"  Routable layers: {stack.get_routable_indices()}")

            # Attempt routing
            if method == "negotiated":
                routes = router.route_all_negotiated(max_iterations=max_iterations)
                overflow = router.grid.get_total_overflow()
                iterations = max_iterations  # TODO: track actual iterations used
            else:
                routes = router.route_all()
                overflow = 0  # Simple routing doesn't track overflow
                iterations = 1

            # Check convergence
            nets_routed = len({r.net for r in routes if r.net != 0})
            converged = self._check_convergence(router, overflow)

            # Build result
            self.result = RoutingResult(
                routes=routes,
                layer_count=stack.num_layers,
                layer_stack=stack,
                nets_requested=nets_requested,
                nets_routed=nets_routed,
                overflow=overflow,
                converged=converged,
                iterations_used=iterations,
                statistics=router.get_statistics(),
            )
            self._autorouter = router

            if self.verbose:
                print(f"\n  Result: {self.result}")

            if converged:
                if self.verbose:
                    print(f"\n✓ Routing CONVERGED with {stack.num_layers} layers!")
                return self.result

            if self.verbose:
                print(f"\n✗ {stack.num_layers}-layer routing did not converge")
                if stack.num_layers < self.max_layers:
                    print("  → Trying more layers...")

        # Return best result (even if not converged)
        if self.verbose:
            print(f"\n{'=' * 60}")
            print("ADAPTIVE ROUTING COMPLETE")
            print(f"{'=' * 60}")
            if self.result:
                print(f"  Final: {self.result}")
                if not self.result.converged:
                    print(
                        f"  Warning: Routing did not fully converge even with "
                        f"{self.result.layer_count} layers"
                    )

        # Should always have a result at this point
        assert self.result is not None
        return self.result

    def to_sexp(self) -> str:
        """Generate KiCad S-expression for the routes."""
        if self._autorouter is None:
            raise ValueError("No routing result. Call route() first.")
        return self._autorouter.to_sexp()

    def get_routes(self) -> list[Route]:
        """Get the list of routes."""
        if self.result is None:
            raise ValueError("No routing result. Call route() first.")
        return self.result.routes

    @property
    def layer_count(self) -> int:
        """Get the number of layers used."""
        if self.result is None:
            return 0
        return self.result.layer_count
