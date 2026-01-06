"""High-level autorouter API with Autorouter, AdaptiveAutorouter, and RoutingResult."""

from __future__ import annotations

import math
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.physics import Stackup, TransmissionLine
    from kicad_tools.progress import ProgressCallback

from .adaptive import AdaptiveAutorouter, RoutingResult
from .algorithms import MonteCarloRouter, MSTRouter, NegotiatedRouter
from .bus import BusGroup, BusRoutingConfig, BusRoutingMode
from .bus_routing import BusRouter
from .cpp_backend import CppGrid, CppPathfinder, create_hybrid_router, get_backend_info
from .diffpair import DifferentialPair, DifferentialPairConfig, LengthMismatchWarning
from .diffpair_routing import DiffPairRouter
from .failure_analysis import CongestionMap, FailureAnalysis, RootCauseAnalyzer
from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .path import create_intra_ic_routes, reduce_pads_after_intra_ic
from .primitives import Obstacle, Pad, Route
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting
from .zones import ZoneManager

# Re-export for backward compatibility
__all__ = [
    "Autorouter",
    "AdaptiveAutorouter",
    "RoutingResult",
]


def _run_monte_carlo_trial(config: dict) -> tuple[list, float, int]:
    """Run a single Monte Carlo trial in a worker process.

    This is a module-level function to be picklable for ProcessPoolExecutor.
    Creates its own Autorouter instance with the provided configuration.

    Args:
        config: Dictionary containing:
            - trial_num: Trial number
            - seed: Random seed for this trial
            - base_order: Base net ordering
            - use_negotiated: Whether to use negotiated routing
            - width, height, origin_x, origin_y: Grid dimensions
            - rules_dict: Serialized design rules
            - net_class_map: Net class configuration
            - pads_data: List of pad dictionaries
            - nets: Net to pad mapping
            - net_names: Net ID to name mapping

    Returns:
        Tuple of (routes, score, trial_num)
    """
    # Import inside worker to avoid issues with multiprocessing
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.rules import DesignRules

    trial_num = config["trial_num"]
    seed = config["seed"]
    base_order = config["base_order"]
    use_negotiated = config["use_negotiated"]

    # Set random seed for reproducibility
    random.seed(seed)

    # Recreate design rules
    rules_dict = config.get("rules_dict", {})
    rules = DesignRules(**rules_dict) if rules_dict else DesignRules()

    # Create new Autorouter instance
    router = Autorouter(
        width=config["width"],
        height=config["height"],
        origin_x=config["origin_x"],
        origin_y=config["origin_y"],
        rules=rules,
        net_class_map=config.get("net_class_map"),
        physics_enabled=False,  # Skip physics in workers for simplicity
    )

    # Add pads from serialized data
    for pad_data in config["pads_data"]:
        ref = pad_data["ref"]
        pad_info = {
            "number": pad_data["number"],
            "x": pad_data["x"],
            "y": pad_data["y"],
            "width": pad_data["width"],
            "height": pad_data["height"],
            "net": pad_data["net"],
            "net_name": pad_data["net_name"],
            "layer": Layer(pad_data["layer"])
            if isinstance(pad_data["layer"], str)
            else pad_data["layer"],
            "through_hole": pad_data.get("through_hole", False),
            "drill": pad_data.get("drill", 0.0),
        }
        # Add directly to avoid component grouping overhead
        from kicad_tools.router.primitives import Pad

        pad = Pad(
            x=pad_info["x"],
            y=pad_info["y"],
            width=pad_info["width"],
            height=pad_info["height"],
            net=pad_info["net"],
            net_name=pad_info["net_name"],
            layer=pad_info["layer"],
            ref=ref,
            through_hole=pad_info["through_hole"],
            drill=pad_info["drill"],
        )
        key = (ref, str(pad_info["number"]))
        router.pads[key] = pad
        router.grid.add_pad(pad)

    # Restore nets and net_names
    router.nets = {int(k): v for k, v in config["nets"].items()}
    router.net_names = {int(k): v for k, v in config["net_names"].items()}

    # Shuffle net order (first trial uses base order)
    if trial_num == 0:
        net_order = base_order.copy()
    else:
        net_order = router._shuffle_within_tiers(base_order)

    # Run routing
    if use_negotiated:
        routes = router.route_all_negotiated()
    else:
        routes = router.route_all(net_order)

    score = router._evaluate_solution(routes)

    return routes, score, trial_num


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
        force_python: bool = False,
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
            force_python: If True, force use of Python backend even if C++ is
                available. Default False (use C++ when available for 10-100x speedup).
        """
        self.rules = rules or DesignRules()
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.layer_stack = layer_stack
        self._force_python = force_python
        self.grid = RoutingGrid(
            width, height, self.rules, origin_x, origin_y, layer_stack=layer_stack
        )
        self.router = create_hybrid_router(self.grid, self.rules, force_python=force_python)
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
    def _cpp_grid(self) -> CppGrid | None:
        """Get the C++ grid if using C++ backend, None otherwise.

        The C++ grid must be kept in sync with the Python grid to prevent
        routing through previously placed traces (issue #590).
        """
        if isinstance(self.router, CppPathfinder):
            return self.router._grid
        return None

    def _mark_route_on_cpp_grid(self, route: Route) -> None:
        """Mark a route on the C++ grid to keep it in sync with Python grid.

        This prevents the C++ pathfinder from routing through clearance zones
        of previously routed traces. Without this sync, the C++ grid becomes
        stale after the first route, causing DRC violations (issue #590).
        """
        cpp_grid = self._cpp_grid
        if cpp_grid is None:
            return

        # Calculate clearance in grid cells (same logic as Python grid)
        total_clearance = self.rules.trace_width / 2 + self.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Mark all segments on C++ grid
        for seg in route.segments:
            gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
            gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
            layer_idx = self.grid.layer_to_index(seg.layer.value)
            cpp_grid.mark_segment(gx1, gy1, gx2, gy2, layer_idx, seg.net, clearance_cells)

        # Mark all vias on C++ grid
        for via in route.vias:
            gx, gy = self.grid.world_to_grid(via.x, via.y)
            # Via radius includes via_clearance + trace half-width (same as Python)
            radius_cells = int(
                (via.diameter / 2 + self.rules.via_clearance + self.rules.trace_width / 2)
                / self.grid.resolution
            )
            cpp_grid.mark_via(gx, gy, via.net, radius_cells)

    def _mark_route(self, route: Route) -> None:
        """Mark a route on both Python and C++ grids.

        This is the unified method that should be used instead of calling
        self.grid.mark_route() directly, to ensure grid synchronization.
        """
        self.grid.mark_route(route)
        self._mark_route_on_cpp_grid(route)

    @property
    def physics_available(self) -> bool:
        """Check if physics calculations are available."""
        return self._transmission_line is not None

    @property
    def backend_info(self) -> dict:
        """Get information about the active router backend.

        Returns:
            Dictionary with backend info:
                - backend: "cpp" or "python"
                - version: Backend version string
                - available: True if C++ backend is available
                - active: "cpp" or "python" (what's actually being used)

        Example:
            >>> router = Autorouter(100, 100)
            >>> print(router.backend_info)
            {'backend': 'cpp', 'version': '1.0.0', 'available': True, 'active': 'cpp'}
        """
        info = get_backend_info()
        # Determine what's actually active based on router type
        from .cpp_backend import CppPathfinder

        active = "cpp" if isinstance(self.router, CppPathfinder) else "python"
        info["active"] = active
        return info

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
            self._mark_route(route)
            routes.append(route)
            self.routes.append(route)

        # Build reduced pad list for inter-IC routing
        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        pad_objs = [self.pads[p] for p in pads_for_routing]
        mst_router = MSTRouter(self.grid, self.router, self.rules, self.net_class_map)

        def mark_route(route: Route):
            self._mark_route(route)
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

            # Progress output for every net with percentage
            net_name = self.net_names.get(net, f"Net {net}")
            pct = (i / total_nets * 100) if total_nets > 0 else 0
            print(
                f"  [{pct:5.1f}%] Routing net {i + 1}/{total_nets}: {net_name}... ({elapsed_str()})"
            )

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
            self._mark_route(route)
            routes.append(route)

        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        pad_objs = [self.pads[p] for p in pads_for_routing]
        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)

        def mark_route(route: Route):
            self._mark_route(route)

        new_routes = neg_router.route_net_negotiated(pad_objs, present_cost_factor, mark_route)
        routes.extend(new_routes)
        return routes

    def _reset_for_new_trial(self):
        """Reset the router to initial state for a new trial."""
        width, height = self.grid.width, self.grid.height
        origin_x, origin_y = self.grid.origin_x, self.grid.origin_y

        self.grid = RoutingGrid(width, height, self.rules, origin_x, origin_y)
        self.router = create_hybrid_router(self.grid, self.rules, force_python=self._force_python)
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

    def _serialize_for_parallel(self) -> dict:
        """Serialize router state for parallel worker processes.

        Returns:
            Dictionary with all state needed to recreate an Autorouter.
        """
        from dataclasses import asdict

        # Serialize pads data
        pads_data = []
        for (ref, num), pad in self.pads.items():
            pads_data.append(
                {
                    "ref": ref,
                    "number": num,
                    "x": pad.x,
                    "y": pad.y,
                    "width": pad.width,
                    "height": pad.height,
                    "net": pad.net,
                    "net_name": pad.net_name,
                    "layer": pad.layer.value if hasattr(pad.layer, "value") else str(pad.layer),
                    "through_hole": pad.through_hole,
                    "drill": pad.drill,
                }
            )

        # Serialize rules (handle nested dataclasses)
        try:
            rules_dict = asdict(self.rules)
            # Convert Layer enums to strings
            if "preferred_layer" in rules_dict:
                rules_dict["preferred_layer"] = self.rules.preferred_layer.value
            if "alternate_layer" in rules_dict:
                rules_dict["alternate_layer"] = self.rules.alternate_layer.value
        except Exception:
            rules_dict = {}

        return {
            "width": self.grid.width,
            "height": self.grid.height,
            "origin_x": self.grid.origin_x,
            "origin_y": self.grid.origin_y,
            "rules_dict": rules_dict,
            "net_class_map": self.net_class_map,
            "pads_data": pads_data,
            "nets": {str(k): v for k, v in self.nets.items()},
            "net_names": {str(k): v for k, v in self.net_names.items()},
        }

    def route_all_monte_carlo(
        self,
        num_trials: int = 10,
        use_negotiated: bool = False,
        seed: int | None = None,
        verbose: bool = True,
        progress_callback: ProgressCallback | None = None,
        num_workers: int | None = None,
    ) -> list[Route]:
        """Route using Monte Carlo multi-start with randomized net orderings.

        Args:
            num_trials: Number of routing trials to run
            use_negotiated: Whether to use negotiated congestion routing
            seed: Random seed for reproducibility
            verbose: Whether to print progress information
            progress_callback: Optional callback for progress updates
            num_workers: Number of parallel workers. None or 0 for auto-detection
                based on CPU count. 1 for sequential execution.

        Returns:
            List of routes from the best trial
        """
        base_seed = seed if seed is not None else random.randint(0, 2**31 - 1)
        random.seed(base_seed)

        # Determine number of workers
        if num_workers is None or num_workers <= 0:
            num_workers = min(num_trials, os.cpu_count() or 4)
        num_workers = min(num_workers, num_trials)

        if verbose:
            print("\n=== Monte Carlo Multi-Start Routing ===")
            print(f"  Trials: {num_trials}, Negotiated: {use_negotiated}")
            if num_workers > 1:
                print(f"  Parallel workers: {num_workers}")

        base_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        base_order = [n for n in base_order if n != 0]

        best_routes: list[Route] | None = None
        best_score, best_trial = float("-inf"), -1

        # Use parallel execution if num_workers > 1
        if num_workers > 1:
            try:
                best_routes, best_score, best_trial = self._run_parallel_monte_carlo(
                    num_trials=num_trials,
                    use_negotiated=use_negotiated,
                    base_seed=base_seed,
                    base_order=base_order,
                    num_workers=num_workers,
                    verbose=verbose,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                if verbose:
                    print(f"  ⚠ Parallel execution failed: {e}")
                    print("  Falling back to sequential execution...")
                # Fall back to sequential execution
                num_workers = 1

        # Sequential execution (num_workers == 1 or fallback)
        if num_workers == 1:
            for trial in range(num_trials):
                if progress_callback is not None:
                    if not progress_callback(
                        trial / num_trials, f"Trial {trial + 1}/{num_trials}", True
                    ):
                        break

                random.seed(base_seed + trial)
                self._reset_for_new_trial()
                net_order = (
                    base_order.copy() if trial == 0 else self._shuffle_within_tiers(base_order)
                )
                routes = (
                    self.route_all_negotiated() if use_negotiated else self.route_all(net_order)
                )
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

    def _run_parallel_monte_carlo(
        self,
        num_trials: int,
        use_negotiated: bool,
        base_seed: int,
        base_order: list[int],
        num_workers: int,
        verbose: bool,
        progress_callback: ProgressCallback | None,
    ) -> tuple[list[Route] | None, float, int]:
        """Run Monte Carlo trials in parallel using ProcessPoolExecutor.

        Args:
            num_trials: Total number of trials to run
            use_negotiated: Whether to use negotiated routing
            base_seed: Base random seed
            base_order: Base net ordering
            num_workers: Number of parallel workers
            verbose: Whether to print progress
            progress_callback: Optional progress callback

        Returns:
            Tuple of (best_routes, best_score, best_trial)
        """
        # Serialize current state for workers
        base_config = self._serialize_for_parallel()

        # Create configs for each trial
        trial_configs = []
        for trial in range(num_trials):
            config = base_config.copy()
            config.update(
                {
                    "trial_num": trial,
                    "seed": base_seed + trial,
                    "base_order": base_order,
                    "use_negotiated": use_negotiated,
                }
            )
            trial_configs.append(config)

        best_routes: list[Route] | None = None
        best_score = float("-inf")
        best_trial = -1
        completed = 0

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            # Submit all tasks
            futures = {
                executor.submit(_run_monte_carlo_trial, config): config["trial_num"]
                for config in trial_configs
            }

            # Process results as they complete
            for future in as_completed(futures):
                trial_num = futures[future]
                try:
                    routes, score, _ = future.result()
                    completed += 1

                    if progress_callback is not None:
                        if not progress_callback(
                            completed / num_trials,
                            f"Completed {completed}/{num_trials} trials",
                            True,
                        ):
                            # Cancel remaining futures
                            for f in futures:
                                f.cancel()
                            break

                    is_new_best = score > best_score
                    if is_new_best:
                        best_score = score
                        best_routes = routes
                        best_trial = trial_num

                    if verbose:
                        status = "NEW BEST" if is_new_best else ""
                        net_count = len({r.net for r in routes}) if routes else 0
                        via_count = sum(len(r.vias) for r in routes) if routes else 0
                        print(
                            f"  Trial {trial_num + 1}: {net_count}/{len(base_order)} nets, "
                            f"{via_count} vias, score={score:.2f} {status}"
                        )

                except Exception as e:
                    if verbose:
                        print(f"  Trial {trial_num + 1}: FAILED - {e}")
                    completed += 1

        return best_routes, best_score, best_trial

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

    # =========================================================================
    # Failure Analysis API
    # =========================================================================

    def analyze_routing_failure(
        self,
        net: int | str,
        start_pad: tuple[str, str] | None = None,
        end_pad: tuple[str, str] | None = None,
    ) -> FailureAnalysis | None:
        """Analyze why routing failed for a net.

        Provides detailed root cause analysis including congestion score,
        blocking elements, and actionable suggestions.

        Args:
            net: Net ID or net name to analyze
            start_pad: Optional (ref, pin) tuple for start pad
            end_pad: Optional (ref, pin) tuple for end pad

        Returns:
            FailureAnalysis with root cause and suggestions, or None if net not found

        Example::

            # After routing fails
            failed_nets = router.get_failed_nets()
            for net in failed_nets:
                analysis = router.analyze_routing_failure(net)
                print(f"Net {net}: {analysis.root_cause.value}")
                for suggestion in analysis.suggestions:
                    print(f"  - {suggestion}")
        """
        # Resolve net ID
        net_id = net if isinstance(net, int) else self._resolve_net_id(net)
        if net_id is None or net_id not in self.nets:
            return None

        pads = self.nets[net_id]
        if len(pads) < 2:
            return None

        # Determine which pads to analyze
        if start_pad and end_pad:
            pad1 = self.pads.get(start_pad)
            pad2 = self.pads.get(end_pad)
        else:
            # Use first two pads
            pad1 = self.pads.get(pads[0])
            pad2 = self.pads.get(pads[1])

        if not pad1 or not pad2:
            return None

        # Create analyzer and analyze
        analyzer = RootCauseAnalyzer()
        net_name = self.net_names.get(net_id, f"Net_{net_id}")

        return analyzer.analyze_routing_failure(
            grid=self.grid,
            start=(pad1.x, pad1.y),
            end=(pad2.x, pad2.y),
            net=net_name,
            layer=self.grid.layer_to_index(pad1.layer.value),
        )

    def _resolve_net_id(self, net_name: str) -> int | None:
        """Resolve a net name to its ID."""
        for net_id, name in self.net_names.items():
            if name == net_name:
                return net_id
        return None

    def get_failed_nets(self) -> list[int]:
        """Get list of nets that failed to route.

        Returns:
            List of net IDs that were not successfully routed
        """
        routed_nets = {r.net for r in self.routes}
        all_nets = {n for n in self.nets.keys() if n != 0}
        return list(all_nets - routed_nets)

    def get_congestion_map(self) -> CongestionMap:
        """Get a congestion heatmap for the current board state.

        Returns:
            CongestionMap that can be queried for congestion scores
            and hotspots

        Example::

            cmap = router.get_congestion_map()
            hotspots = cmap.find_congestion_hotspots(threshold=0.7)
            for hotspot in hotspots:
                print(f"Hotspot at ({hotspot.center[0]:.1f}, {hotspot.center[1]:.1f})")
        """
        return CongestionMap(self.grid)

    def analyze_all_failures(self) -> dict[int, FailureAnalysis]:
        """Analyze all failed nets and return failure analyses.

        Returns:
            Dictionary mapping net ID to FailureAnalysis

        Example::

            failures = router.analyze_all_failures()
            for net_id, analysis in failures.items():
                net_name = router.net_names.get(net_id, f"Net_{net_id}")
                print(f"{net_name}: {analysis.root_cause.value} ({analysis.confidence:.0%})")
        """
        failed_nets = self.get_failed_nets()
        analyses: dict[int, FailureAnalysis] = {}

        for net_id in failed_nets:
            analysis = self.analyze_routing_failure(net_id)
            if analysis:
                analyses[net_id] = analysis

        return analyses
