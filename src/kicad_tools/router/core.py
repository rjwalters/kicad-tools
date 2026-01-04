"""High-level autorouter API with Autorouter, AdaptiveAutorouter, and RoutingResult."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.physics import Stackup, TransmissionLine
    from kicad_tools.progress import ProgressCallback

from .adaptive import AdaptiveAutorouter, RoutingResult
from .algorithms import MonteCarloRouter, MSTRouter, NegotiatedRouter
from .bus import BusGroup, BusRoutingConfig, BusRoutingMode
from .bus_routing import BusRouter
from .diffpair import DifferentialPair, DifferentialPairConfig, LengthMismatchWarning
from .diffpair_routing import DiffPairRouter
from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .path import create_intra_ic_routes, reduce_pads_after_intra_ic
from .pathfinder import Router
from .primitives import Obstacle, Pad, Route
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting
from .zones import ZoneManager

# Re-export for backward compatibility
__all__ = [
    "Autorouter",
    "AdaptiveAutorouter",
    "RoutingResult",
]


class Autorouter:
    """High-level autorouter for complete PCBs with net class awareness.

    Supports impedance-controlled routing when a stackup is provided.
    The physics module calculates appropriate trace widths for target
    impedances on each layer.
    """

    def __init__(
        self,
        width: float,
        height: float,
        origin_x: float = 0,
        origin_y: float = 0,
        rules: DesignRules | None = None,
        net_class_map: dict[str, NetClassRouting] | None = None,
        layer_stack: LayerStack | None = None,
        stackup: Stackup | None = None,
        physics_enabled: bool = True,
    ):
        """Initialize the autorouter.

        Args:
            width: Board width in mm
            height: Board height in mm
            origin_x: X origin offset
            origin_y: Y origin offset
            rules: Design rules for routing
            net_class_map: Net class to routing config mapping
            layer_stack: Layer stack for routing
            stackup: PCB stackup for physics calculations (optional)
            physics_enabled: Enable physics-based calculations (default True)
        """
        self.rules = rules or DesignRules()
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.layer_stack = layer_stack
        self.grid = RoutingGrid(
            width, height, self.rules, origin_x, origin_y, layer_stack=layer_stack
        )
        self.router = Router(self.grid, self.rules, self.net_class_map)
        self.zone_manager = ZoneManager(self.grid, self.rules)

        self.pads: dict[tuple[str, str], Pad] = {}
        self.nets: dict[int, list[tuple[str, str]]] = {}
        self.net_names: dict[int, str] = {}
        self.routes: list[Route] = []

        # Physics integration
        self._stackup = stackup
        self._physics_enabled = physics_enabled
        self._transmission_line: TransmissionLine | None = None
        self._init_physics()

        # Lazy-initialized routers
        self._bus_router: BusRouter | None = None
        self._diffpair_router: DiffPairRouter | None = None

    def _init_physics(self) -> None:
        """Initialize physics module if available and enabled."""
        if not self._physics_enabled or self._stackup is None:
            return

        try:
            from kicad_tools.physics import TransmissionLine

            self._transmission_line = TransmissionLine(self._stackup)
        except ImportError:
            # Physics module not available
            self._transmission_line = None
        except Exception:
            # Stackup or other initialization error
            self._transmission_line = None

    @property
    def physics_available(self) -> bool:
        """Check if physics calculations are available."""
        return self._transmission_line is not None

    def get_width_for_impedance(
        self,
        z0_target: float,
        layer: str | Layer,
    ) -> float | None:
        """Calculate trace width for target impedance on a specific layer.

        Uses the physics module to determine the trace width needed
        for the target characteristic impedance.

        Args:
            z0_target: Target impedance in ohms (e.g., 50.0)
            layer: Layer name or Layer enum (e.g., "F.Cu" or Layer.F_CU)

        Returns:
            Trace width in mm, or None if physics not available
        """
        if not self.physics_available:
            return None

        # Convert Layer enum to string if needed
        layer_name = layer.value if isinstance(layer, Layer) else layer

        try:
            return self._transmission_line.width_for_impedance(z0_target, layer_name)
        except (ValueError, AttributeError):
            return None

    def get_impedance_layer_widths(
        self,
        z0_target: float,
        layers: list[str] | None = None,
    ) -> dict[str, float]:
        """Calculate trace widths for target impedance across multiple layers.

        This is useful for impedance-controlled routing where trace widths
        vary by layer due to different dielectric heights and properties.

        Args:
            z0_target: Target impedance in ohms
            layers: List of layer names to calculate, defaults to all copper layers

        Returns:
            Dictionary mapping layer names to trace widths in mm
        """
        if not self.physics_available:
            return {}

        if layers is None:
            layers = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"]

        layer_widths: dict[str, float] = {}
        for layer in layers:
            try:
                width = self._transmission_line.width_for_impedance(z0_target, layer)
                layer_widths[layer] = width
            except (ValueError, AttributeError):
                continue

        return layer_widths

    def add_component(self, ref: str, pads: list[dict]):
        """Add a component's pads."""
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

            if pad.net > 0:
                if pad.net not in self.nets:
                    self.nets[pad.net] = []
                self.nets[pad.net].append(key)
                if pad.net_name:
                    self.net_names[pad.net] = pad.net_name

            self.grid.add_pad(pad)

    def add_obstacle(
        self, x: float, y: float, width: float, height: float, layer: Layer = Layer.F_CU
    ):
        """Add an obstacle (keepout area, mounting hole, etc.)."""
        obs = Obstacle(x, y, width, height, layer)
        self.grid.add_obstacle(obs)

    def add_zones(self, zones: list) -> None:
        """Add zones (copper pours) to the router."""
        pad_list = list(self.pads.values())
        filled = self.zone_manager.fill_all_zones(zones, pad_list, apply_to_grid=True)
        zone_count = len(filled)
        total_cells = sum(len(z.filled_cells) for z in filled)
        print(f"  Zones: {zone_count} zones, {total_cells} cells filled")

    def clear_zones(self) -> None:
        """Remove all zone markings from the grid."""
        self.zone_manager.clear_all_zones()

    def get_zone_statistics(self) -> dict:
        """Get statistics about filled zones."""
        return self.zone_manager.get_zone_statistics()

    def _create_intra_ic_routes(
        self, net: int, pads: list[tuple[str, str]]
    ) -> tuple[list[Route], set[int]]:
        """Create direct routes for same-IC pins on the same net."""
        return create_intra_ic_routes(net, pads, self.pads, self.rules)

    def route_net(
        self,
        net: int,
        use_mst: bool = True,
        target_impedance: float | None = None,
    ) -> list[Route]:
        """Route all connections for a net.

        Args:
            net: Net ID to route
            use_mst: Use minimum spanning tree routing for multi-point nets
            target_impedance: Target characteristic impedance in ohms (optional).
                When specified and physics module is available, calculates
                appropriate trace widths per layer to achieve this impedance.

        Returns:
            List of Route objects for this net
        """
        if net not in self.nets:
            return []

        pads = self.nets[net]
        if len(pads) < 2:
            return []

        routes: list[Route] = []

        # Calculate layer-specific widths for impedance control if requested
        layer_widths: dict[str, float] | None = None
        if target_impedance and self.physics_available:
            layer_widths = self.get_impedance_layer_widths(target_impedance)
            if layer_widths:
                net_name = self.net_names.get(net, f"Net {net}")
                print(f"  Impedance control: {net_name} @ {target_impedance}Ω")
                for layer, width in layer_widths.items():
                    print(f"    {layer}: {width * 1000:.1f}mil ({width:.3f}mm)")

        # Handle intra-IC connections first
        intra_routes, connected_indices = self._create_intra_ic_routes(net, pads)
        for route in intra_routes:
            self.grid.mark_route(route)
            routes.append(route)
            self.routes.append(route)

        # Build reduced pad list for inter-IC routing
        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        pad_objs = [self.pads[p] for p in pads_for_routing]
        mst_router = MSTRouter(self.grid, self.router, self.rules, self.net_class_map)

        def mark_route(route: Route):
            self.grid.mark_route(route)
            self.routes.append(route)

        if use_mst and len(pad_objs) > 2:
            new_routes = mst_router.route_net(pad_objs, mark_route)
        else:
            new_routes = mst_router.route_net_star(pad_objs, mark_route)

        routes.extend(new_routes)
        return routes

    def _get_net_priority(self, net_id: int) -> tuple[int, int]:
        """Get routing priority for a net (lower = higher priority)."""
        net_name = self.net_names.get(net_id, "")
        net_class = self.net_class_map.get(net_name)
        priority = net_class.priority if net_class else 10
        pad_count = len(self.nets.get(net_id, []))
        return (priority, pad_count)

    def route_all(
        self,
        net_order: list[int] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route all nets in priority order."""
        if net_order is None:
            net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))

        nets_to_route = [n for n in net_order if n != 0]
        total_nets = len(nets_to_route)
        all_routes: list[Route] = []

        for i, net in enumerate(nets_to_route):
            if progress_callback is not None:
                progress = i / total_nets if total_nets > 0 else 0.0
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self.route_net(net)
            all_routes.extend(routes)
            if routes:
                print(
                    f"  Net {net}: {len(routes)} routes, "
                    f"{sum(len(r.segments) for r in routes)} segments, "
                    f"{sum(len(r.vias) for r in routes)} vias"
                )

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
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets using PathFinder-style negotiated congestion.

        Args:
            max_iterations: Maximum number of rip-up and reroute iterations
            initial_present_factor: Initial congestion penalty factor
            present_factor_increment: Factor increase per iteration
            history_increment: History cost increment per iteration
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds. If reached, returns best partial result.

        Returns:
            List of routes (may be partial if timeout reached)
        """
        import time

        start_time = time.time()

        print("\n=== Negotiated Congestion Routing ===")
        print(f"  Max iterations: {max_iterations}")
        print(f"  Present factor: {initial_present_factor} + {present_factor_increment}/iter")
        if timeout:
            print(f"  Timeout: {timeout}s")

        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
        total_nets = len(net_order)

        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = initial_present_factor
        timed_out = False

        def check_timeout() -> bool:
            """Check if timeout has been reached."""
            if timeout is None:
                return False
            elapsed = time.time() - start_time
            return elapsed >= timeout

        def elapsed_str() -> str:
            """Get formatted elapsed time."""
            elapsed = time.time() - start_time
            return f"{elapsed:.1f}s"

        print("\n--- Iteration 0: Initial routing with sharing ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Initial routing pass", True):
                return list(self.routes)

        for i, net in enumerate(net_order):
            if check_timeout():
                print(f"  ⚠ Timeout reached at net {i}/{total_nets} ({elapsed_str()})")
                timed_out = True
                break

            # Progress output every 5 nets or for the last net
            if i % 5 == 0 or i == total_nets - 1:
                print(f"  Routing net {i + 1}/{total_nets}... ({elapsed_str()})")

            routes = self._route_net_negotiated(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        overused = self.grid.find_overused_cells()
        print(
            f"  Routed {len(net_routes)}/{total_nets} nets, overflow: {overflow} ({elapsed_str()})"
        )

        if timed_out:
            print("  ⚠ Returning partial result due to timeout")
        elif overflow == 0:
            print("  No conflicts - routing complete!")
            if progress_callback is not None:
                progress_callback(1.0, "Routing complete - no conflicts", False)
            return list(self.routes)

        # Skip iteration loop if already timed out
        if not timed_out:
            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    print(f"\n  ⚠ Timeout reached at iteration {iteration} ({elapsed_str()})")
                    timed_out = True
                    break

                if progress_callback is not None:
                    progress = iteration / (max_iterations + 1)
                    if not progress_callback(
                        progress,
                        f"Iteration {iteration}/{max_iterations}: rip-up and reroute",
                        True,
                    ):
                        break

                print(f"\n--- Iteration {iteration}: Rip-up and reroute ---")
                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)
                print(f"  Ripping up {len(nets_to_reroute)} nets with conflicts ({elapsed_str()})")

                neg_router.rip_up_nets(nets_to_reroute, net_routes, self.routes)

                rerouted_count = 0
                for i, net in enumerate(nets_to_reroute):
                    if check_timeout():
                        print(
                            f"  ⚠ Timeout during reroute at net {i}/{len(nets_to_reroute)} ({elapsed_str()})"
                        )
                        timed_out = True
                        break

                    routes = self._route_net_negotiated(net, present_factor)
                    if routes:
                        net_routes[net] = routes
                        rerouted_count += 1
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            self.routes.append(route)

                if timed_out:
                    break

                overflow = self.grid.get_total_overflow()
                overused = self.grid.find_overused_cells()
                print(
                    f"  Rerouted {rerouted_count}/{len(nets_to_reroute)} nets, overflow: {overflow} ({elapsed_str()})"
                )

                if overflow == 0:
                    print(f"  Convergence achieved at iteration {iteration}!")
                    break

        successful_nets = sum(1 for routes in net_routes.values() if routes)
        total_elapsed = time.time() - start_time
        print("\n=== Negotiated Routing Complete ===")
        print(f"  Total nets: {total_nets}")
        print(f"  Successful: {successful_nets}")
        print(f"  Final overflow: {overflow}")
        print(f"  Total time: {total_elapsed:.1f}s")
        if timed_out:
            print("  ⚠ Stopped due to timeout - returning best partial result")

        if progress_callback is not None:
            status = (
                "converged"
                if overflow == 0
                else ("timeout" if timed_out else f"overflow={overflow}")
            )
            progress_callback(
                1.0, f"Routing complete: {successful_nets}/{total_nets} nets ({status})", False
            )

        return list(self.routes)

    def _route_net_negotiated(self, net: int, present_cost_factor: float) -> list[Route]:
        """Route a single net in negotiated mode."""
        if net not in self.nets:
            return []

        pads = self.nets[net]
        if len(pads) < 2:
            return []

        routes: list[Route] = []
        intra_routes, connected_indices = self._create_intra_ic_routes(net, pads)
        for route in intra_routes:
            self.grid.mark_route(route)
            routes.append(route)

        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        pad_objs = [self.pads[p] for p in pads_for_routing]
        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)

        def mark_route(route: Route):
            self.grid.mark_route(route)

        new_routes = neg_router.route_net_negotiated(pad_objs, present_cost_factor, mark_route)
        routes.extend(new_routes)
        return routes

    def _reset_for_new_trial(self):
        """Reset the router to initial state for a new trial."""
        width, height = self.grid.width, self.grid.height
        origin_x, origin_y = self.grid.origin_x, self.grid.origin_y

        self.grid = RoutingGrid(width, height, self.rules, origin_x, origin_y)
        self.router = Router(self.grid, self.rules, self.net_class_map)
        self.zone_manager = ZoneManager(self.grid, self.rules)

        for pad in self.pads.values():
            self.grid.add_pad(pad)
        self.routes = []

    def _shuffle_within_tiers(self, net_order: list[int]) -> list[int]:
        """Shuffle nets but keep priority ordering."""
        mc_router = MonteCarloRouter(len([n for n in self.nets if n != 0]))
        return mc_router.shuffle_within_tiers(net_order, self._get_net_priority)

    def _evaluate_solution(self, routes: list[Route]) -> float:
        """Score a routing solution (higher = better)."""
        mc_router = MonteCarloRouter(len([n for n in self.nets if n != 0]))
        return mc_router.evaluate_solution(routes)

    def route_all_monte_carlo(
        self,
        num_trials: int = 10,
        use_negotiated: bool = False,
        seed: int | None = None,
        verbose: bool = True,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route using Monte Carlo multi-start with randomized net orderings."""
        if seed is not None:
            random.seed(seed)

        if verbose:
            print("\n=== Monte Carlo Multi-Start Routing ===")
            print(f"  Trials: {num_trials}, Negotiated: {use_negotiated}")

        base_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        base_order = [n for n in base_order if n != 0]

        best_routes: list[Route] | None = None
        best_score, best_trial = float("-inf"), -1

        for trial in range(num_trials):
            if progress_callback is not None:
                if not progress_callback(
                    trial / num_trials, f"Trial {trial + 1}/{num_trials}", True
                ):
                    break

            self._reset_for_new_trial()
            net_order = base_order.copy() if trial == 0 else self._shuffle_within_tiers(base_order)
            routes = self.route_all_negotiated() if use_negotiated else self.route_all(net_order)
            score = self._evaluate_solution(routes)

            if verbose:
                status = "NEW BEST" if score > best_score else ""
                print(
                    f"  Trial {trial + 1}: {len({r.net for r in routes})}/{len(base_order)} nets, "
                    f"{sum(len(r.vias) for r in routes)} vias, score={score:.2f} {status}"
                )

            if score > best_score:
                best_score, best_routes, best_trial = score, routes.copy(), trial

        if verbose:
            print(f"\n  Best: Trial {best_trial + 1} (score={best_score:.2f})")

        self.routes = best_routes if best_routes else []
        if progress_callback is not None:
            routed = len({r.net for r in self.routes}) if self.routes else 0
            progress_callback(
                1.0, f"Best: trial {best_trial + 1}, {routed}/{len(base_order)} nets", False
            )

        return self.routes

    def route_all_advanced(
        self,
        monte_carlo_trials: int = 0,
        use_negotiated: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Unified entry point for advanced routing strategies."""
        if monte_carlo_trials > 0:
            return self.route_all_monte_carlo(
                monte_carlo_trials, use_negotiated, progress_callback=progress_callback
            )
        elif use_negotiated:
            return self.route_all_negotiated(progress_callback=progress_callback)
        return self.route_all(progress_callback=progress_callback)

    def to_sexp(self) -> str:
        """Generate KiCad S-expressions for all routes."""
        return "\n\t".join(route.to_sexp() for route in self.routes)

    def get_statistics(self) -> dict:
        """Get routing statistics including congestion metrics."""
        total_length = sum(
            math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2)
            for r in self.routes
            for s in r.segments
        )
        congestion_stats = self.grid.get_congestion_map()

        return {
            "routes": len(self.routes),
            "segments": sum(len(r.segments) for r in self.routes),
            "vias": sum(len(r.vias) for r in self.routes),
            "total_length_mm": total_length,
            "nets_routed": len({r.net for r in self.routes}),
            "max_congestion": congestion_stats["max_congestion"],
            "avg_congestion": congestion_stats["avg_congestion"],
            "congested_regions": congestion_stats["congested_regions"],
        }

    @property
    def _bus(self) -> BusRouter:
        """Lazy-initialize bus router."""
        if self._bus_router is None:
            self._bus_router = BusRouter(self)
        return self._bus_router

    def detect_buses(self, min_bus_width: int = 2) -> list[BusGroup]:
        """Detect bus signals from net names."""
        return self._bus.detect_buses(min_bus_width)

    def get_bus_analysis(self) -> dict:
        """Get a summary of detected buses in the design."""
        return self._bus.get_bus_analysis()

    def route_bus_group(
        self,
        bus_group: BusGroup,
        mode: BusRoutingMode = BusRoutingMode.PARALLEL,
        spacing: float | None = None,
    ) -> list[Route]:
        """Route all signals in a bus group together."""
        return self._bus.route_bus_group(bus_group, mode, spacing)

    def route_all_with_buses(
        self,
        bus_config: BusRoutingConfig | None = None,
        net_order: list[int] | None = None,
    ) -> list[Route]:
        """Route all nets with bus-aware routing."""
        return self._bus.route_all_with_buses(bus_config, net_order)

    @property
    def _diffpair(self) -> DiffPairRouter:
        """Lazy-initialize differential pair router."""
        if self._diffpair_router is None:
            self._diffpair_router = DiffPairRouter(self)
        return self._diffpair_router

    def detect_differential_pairs(self) -> list[DifferentialPair]:
        """Detect differential pairs from net names."""
        return self._diffpair.detect_differential_pairs()

    def analyze_differential_pairs(self) -> dict[str, any]:
        """Analyze net names for differential pairs."""
        return self._diffpair.analyze_differential_pairs()

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair together."""
        return self._diffpair.route_differential_pair(pair, spacing)

    def route_all_with_diffpairs(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        net_order: list[int] | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning]]:
        """Route all nets with differential pair-aware routing."""
        return self._diffpair.route_all_with_diffpairs(diffpair_config, net_order)
