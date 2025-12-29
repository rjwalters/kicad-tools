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

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .pathfinder import Router
from .primitives import Obstacle, Pad, Route, Segment
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting


class Autorouter:
    """High-level autorouter for complete PCBs with net class awareness."""

    def __init__(
        self,
        width: float,
        height: float,
        origin_x: float = 0,
        origin_y: float = 0,
        rules: Optional[DesignRules] = None,
        net_class_map: Optional[Dict[str, NetClassRouting]] = None,
        layer_stack: Optional[LayerStack] = None,
    ):
        self.rules = rules or DesignRules()
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.layer_stack = layer_stack
        self.grid = RoutingGrid(
            width, height, self.rules, origin_x, origin_y, layer_stack=layer_stack
        )
        self.router = Router(self.grid, self.rules, self.net_class_map)

        self.pads: Dict[Tuple[str, str], Pad] = {}  # (ref, pin) -> Pad
        self.nets: Dict[int, List[Tuple[str, str]]] = {}  # net -> [(ref, pin), ...]
        self.net_names: Dict[int, str] = {}  # net_id -> net_name
        self.routes: List[Route] = []

    def add_component(self, ref: str, pads: List[dict]):
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

    def _create_intra_ic_routes(
        self, net: int, pads: List[Tuple[str, str]]
    ) -> Tuple[List[Route], Set[int]]:
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
        routes: List[Route] = []
        connected_indices: Set[int] = set()

        # Group pads by component reference
        by_ref: Dict[str, List[int]] = {}
        for i, (ref, pin) in enumerate(pads):
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
            sorted_pairs = sorted(zip(indices, pad_objs), key=lambda p: (p[1].x, p[1].y))

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

    def route_net(self, net: int, use_mst: bool = True) -> List[Route]:
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

        routes: List[Route] = []

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
            ref_to_indices: Dict[str, List[int]] = {}
            for i in connected_indices:
                ref = pads[i][0]
                if ref not in ref_to_indices:
                    ref_to_indices[ref] = []
                ref_to_indices[ref].append(i)

            # Create reduced pads list: one representative per connected group + unconnected pads
            reduced_pad_indices: List[int] = []
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
            connected: Set[int] = {0}  # Start with first pad
            unconnected = set(range(1, n))
            mst_edges: List[Tuple[int, int]] = []

            while unconnected:
                # Find shortest edge from connected to unconnected
                best_dist = float("inf")
                best_edge: Optional[Tuple[int, int]] = None

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

    def _get_net_priority(self, net_id: int) -> Tuple[int, int]:
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

    def route_all(self, net_order: Optional[List[int]] = None) -> List[Route]:
        """Route all nets in priority order.

        Args:
            net_order: Optional list specifying routing order.
                       If None, uses net class priority then pad count.
        """
        if net_order is None:
            # Sort by: (1) net class priority, (2) pad count
            # Higher priority nets (power, clock) route first to get best paths
            net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))

        all_routes: List[Route] = []
        for net in net_order:
            if net == 0:
                continue  # Skip "no net"
            routes = self.route_net(net)
            all_routes.extend(routes)
            if routes:
                print(
                    f"  Net {net}: {len(routes)} routes, "
                    f"{sum(len(r.segments) for r in routes)} segments, "
                    f"{sum(len(r.vias) for r in routes)} vias"
                )

        return all_routes

    def route_all_negotiated(
        self,
        max_iterations: int = 10,
        initial_present_factor: float = 0.5,
        present_factor_increment: float = 0.5,
        history_increment: float = 1.0,
    ) -> List[Route]:
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
        net_routes: Dict[int, List[Route]] = {}

        # Initial routing pass with sharing allowed
        print("\n--- Iteration 0: Initial routing with sharing ---")
        present_factor = initial_present_factor

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
            return list(self.routes)

        # Iterative negotiation
        for iteration in range(1, max_iterations + 1):
            print(f"\n--- Iteration {iteration}: Rip-up and reroute ---")
            present_factor += present_factor_increment

            # Update history costs for currently overused cells
            self.grid.update_history_costs(history_increment)

            # Find nets with routes through overused cells
            overused_set = set((x, y, layer) for x, y, layer, _ in overused)
            nets_to_reroute: List[int] = []

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

        return list(self.routes)

    def _route_net_negotiated(self, net: int, present_cost_factor: float) -> List[Route]:
        """Route a single net in negotiated mode.

        Similar to route_net but uses negotiated routing with sharing allowed.
        """
        if net not in self.nets:
            return []

        pads = self.nets[net]
        if len(pads) < 2:
            return []

        routes: List[Route] = []

        # Handle intra-IC connections first (these don't use negotiated mode)
        intra_routes, connected_indices = self._create_intra_ic_routes(net, pads)
        for route in intra_routes:
            self.grid.mark_route(route)
            routes.append(route)

        # Build reduced pad list
        if connected_indices:
            ref_to_indices: Dict[str, List[int]] = {}
            for i in connected_indices:
                ref = pads[i][0]
                if ref not in ref_to_indices:
                    ref_to_indices[ref] = []
                ref_to_indices[ref].append(i)

            reduced_pad_indices: List[int] = []
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
            connected: Set[int] = {0}
            unconnected = set(range(1, n))
            mst_edges: List[Tuple[int, int]] = []

            while unconnected:
                best_dist = float("inf")
                best_edge: Optional[Tuple[int, int]] = None

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

        # Re-add all pads as obstacles
        for pad in self.pads.values():
            self.grid.add_pad(pad)

        # Clear routes
        self.routes = []

    def _shuffle_within_tiers(self, net_order: List[int]) -> List[int]:
        """Shuffle nets but keep priority ordering.

        Nets within the same priority tier are shuffled randomly,
        but the tier ordering is preserved.
        """
        # Group by priority tier
        tiers: Dict[int, List[int]] = {}
        for net in net_order:
            priority, _ = self._get_net_priority(net)
            if priority not in tiers:
                tiers[priority] = []
            tiers[priority].append(net)

        # Shuffle within each tier and reassemble
        result: List[int] = []
        for priority in sorted(tiers.keys()):
            tier_nets = tiers[priority].copy()
            random.shuffle(tier_nets)
            result.extend(tier_nets)

        return result

    def _evaluate_solution(self, routes: List[Route]) -> float:
        """Score a routing solution (higher = better).

        Scoring prioritizes:
        1. Completion rate (primary - weighted heavily)
        2. Lower via count (secondary)
        3. Shorter total length (tertiary)
        """
        if not routes:
            return 0.0

        total_nets = len([n for n in self.nets.keys() if n != 0])
        routed_nets = len(set(r.net for r in routes))
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
        seed: Optional[int] = None,
        verbose: bool = True,
    ) -> List[Route]:
        """Route using Monte Carlo multi-start with randomized net orderings.

        Tries multiple random net orderings within priority tiers,
        keeping the best result. This helps escape local minima
        caused by unfortunate routing order.

        Args:
            num_trials: Number of random orderings to try
            use_negotiated: Use negotiated congestion per trial (if available)
            seed: Random seed for reproducibility (None = random)
            verbose: Print progress information

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

        best_routes: Optional[List[Route]] = None
        best_score = float("-inf")
        best_trial = -1

        for trial in range(num_trials):
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
            routed = len(set(r.net for r in routes))
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
        return self.routes

    def route_all_advanced(
        self, monte_carlo_trials: int = 0, use_negotiated: bool = False
    ) -> List[Route]:
        """Unified entry point for advanced routing strategies.

        Args:
            monte_carlo_trials: Number of random orderings to try (0 = single pass)
            use_negotiated: Use negotiated congestion routing

        Returns:
            Best routes found
        """
        if monte_carlo_trials > 0:
            return self.route_all_monte_carlo(
                num_trials=monte_carlo_trials, use_negotiated=use_negotiated
            )
        elif use_negotiated and hasattr(self, "route_all_negotiated"):
            return self.route_all_negotiated()
        else:
            return self.route_all()

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
            "nets_routed": len(set(r.net for r in self.routes)),
            "max_congestion": congestion_stats["max_congestion"],
            "avg_congestion": congestion_stats["avg_congestion"],
            "congested_regions": congestion_stats["congested_regions"],
        }


# =============================================================================
# ADAPTIVE LAYER AUTOROUTER
# =============================================================================


@dataclass
class RoutingResult:
    """Result of a routing attempt with convergence metrics."""

    routes: List[Route]
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
        components: List[dict],
        net_map: Dict[str, int],
        rules: Optional[DesignRules] = None,
        origin_x: float = 0,
        origin_y: float = 0,
        skip_nets: Optional[List[str]] = None,
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
        self.result: Optional[RoutingResult] = None
        self._autorouter: Optional[Autorouter] = None

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

        pads: List[dict] = []
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
        nets_requested = len([n for n in router.nets.keys() if n != 0])
        nets_routed = len(set(r.net for r in router.routes if r.net != 0))

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
            nets_requested = len([n for n in router.nets.keys() if n != 0])

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
            nets_routed = len(set(r.net for r in routes if r.net != 0))
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

    def get_routes(self) -> List[Route]:
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
