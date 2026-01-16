"""High-level autorouter API with Autorouter, AdaptiveAutorouter, and RoutingResult."""

from __future__ import annotations

import math
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_tools.explain.decisions import DecisionStore
    from kicad_tools.physics import Stackup, TransmissionLine
    from kicad_tools.progress import ProgressCallback

from .adaptive import AdaptiveAutorouter, RoutingResult
from .algorithms import (
    MonteCarloRouter,
    MSTRouter,
    NegotiatedRouter,
    calculate_history_increment,
    calculate_present_cost,
    detect_oscillation,
    should_terminate_early,
)
from .bus import BusGroup, BusRoutingConfig, BusRoutingMode
from .bus_routing import BusRouter
from .cpp_backend import CppGrid, CppPathfinder, create_hybrid_router, get_backend_info
from .diffpair import DifferentialPair, DifferentialPairConfig, LengthMismatchWarning
from .diffpair_routing import DiffPairRouter
from .escape import EscapeRouter, PackageInfo, is_dense_package
from .failure_analysis import CongestionMap, FailureAnalysis, RootCauseAnalyzer
from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .length import LengthTracker, LengthViolation
from .parallel import ParallelRouter, find_independent_groups
from .path import create_intra_ic_routes, reduce_pads_after_intra_ic
from .placement_feedback import PlacementFeedbackLoop, PlacementFeedbackResult
from .primitives import Obstacle, Pad, Route
from .rules import (
    DEFAULT_NET_CLASS_MAP,
    DesignRules,
    LengthConstraint,
    NetClassRouting,
    assign_layer_preferences,
)
from .sparse import Corridor, SparseRouter, Waypoint
from .tuning import (
    COST_PROFILES,
    CostProfile,
    analyze_board,
    create_adaptive_router,
    quick_tune,
    tune_parameters,
)
from .zones import ZoneManager


@dataclass
class RoutingFailure:
    """Records a failed routing attempt with diagnostic information.

    Attributes:
        net: Net ID
        net_name: Human-readable net name
        source_pad: Source pad (ref, pin) tuple
        target_pad: Target pad (ref, pin) tuple
        blocking_nets: Set of net IDs that blocked this route
        blocking_components: Components blocking the path (e.g., "U1", "R4")
        reason: Human-readable failure reason
    """

    net: int
    net_name: str
    source_pad: tuple[str, str]
    target_pad: tuple[str, str]
    blocking_nets: set[int] = field(default_factory=set)
    blocking_components: list[str] = field(default_factory=list)
    reason: str = "No path found"


# Re-export for backward compatibility
__all__ = [
    "Autorouter",
    "AdaptiveAutorouter",
    "Corridor",
    "RoutingFailure",
    "RoutingResult",
    "SparseRouter",
    "Waypoint",
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
        # Convert layer data to Layer enum
        layer_data = pad_data["layer"]
        if isinstance(layer_data, int):
            # Serialized as Layer enum value (integer)
            pad_layer = Layer(layer_data)
        elif isinstance(layer_data, str):
            # Serialized as KiCad layer name (string)
            try:
                pad_layer = Layer.from_kicad_name(layer_data)
            except ValueError:
                pad_layer = Layer.F_CU  # Default for unknown layers
        elif isinstance(layer_data, Layer):
            # Already a Layer enum
            pad_layer = layer_data
        else:
            pad_layer = Layer.F_CU  # Default fallback

        pad_info = {
            "number": pad_data["number"],
            "x": pad_data["x"],
            "y": pad_data["y"],
            "width": pad_data["width"],
            "height": pad_data["height"],
            "net": pad_data["net"],
            "net_name": pad_data["net_name"],
            "layer": pad_layer,
            "through_hole": pad_data.get("through_hole", False),
            "drill": pad_data.get("drill", 0.0),
        }
        # Add directly to avoid component grouping overhead
        from kicad_tools.router.primitives import Pad

        pin = str(pad_info["number"])
        pad = Pad(
            x=pad_info["x"],
            y=pad_info["y"],
            width=pad_info["width"],
            height=pad_info["height"],
            net=pad_info["net"],
            net_name=pad_info["net_name"],
            layer=pad_info["layer"],
            ref=ref,
            pin=pin,
            through_hole=pad_info["through_hole"],
            drill=pad_info["drill"],
        )
        key = (ref, pin)
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
        record_decisions: bool = False,
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
            record_decisions: If True, record routing decisions for later querying.
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
        self._escape_router: EscapeRouter | None = None

        # Length constraint tracking (Issue #630)
        self._length_tracker: LengthTracker = LengthTracker()

        # Routing failure tracking (Issue #688)
        self.routing_failures: list[RoutingFailure] = []

        # Decision recording (Issue #829)
        self.record_decisions = record_decisions
        self._decision_store: "DecisionStore | None" = None
        if record_decisions:
            from kicad_tools.explain.decisions import DecisionStore

            self._decision_store = DecisionStore()

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
            pin = str(pad_info["number"])
            pad = Pad(
                x=pad_info["x"],
                y=pad_info["y"],
                width=pad_info.get("width", 0.5),
                height=pad_info.get("height", 0.5),
                net=pad_info.get("net", 0),
                net_name=pad_info.get("net_name", ""),
                layer=pad_info.get("layer", Layer.F_CU),
                ref=ref,
                pin=pin,
                through_hole=pad_info.get("through_hole", False),
                drill=pad_info.get("drill", 0.0),
            )
            key = (ref, pin)
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

        def record_failure(source_pad: Pad, target_pad: Pad):
            """Record a routing failure for diagnostics."""
            # Find blocking nets using the router's analysis
            blocking_nets = self.router.find_blocking_nets(source_pad, target_pad)

            # Map blocking nets to component references
            blocking_components = []
            for blocking_net in blocking_nets:
                # Find pads on this blocking net
                for (ref, _pin), pad in self.pads.items():
                    if pad.net == blocking_net and ref not in blocking_components:
                        blocking_components.append(ref)
                        break  # One component per net is enough

            # Determine failure reason
            if blocking_nets:
                if len(blocking_nets) == 1:
                    reason = (
                        f"Blocked by {blocking_components[0] if blocking_components else 'unknown'}"
                    )
                else:
                    reason = f"Blocked by {len(blocking_nets)} nets"
            else:
                reason = "No path found (congestion or obstacles)"

            failure = RoutingFailure(
                net=net,
                net_name=self.net_names.get(net, f"Net_{net}"),
                source_pad=(source_pad.ref, source_pad.pin),
                target_pad=(target_pad.ref, target_pad.pin),
                blocking_nets=blocking_nets,
                blocking_components=blocking_components,
                reason=reason,
            )
            self.routing_failures.append(failure)

        if use_mst and len(pad_objs) > 2:
            new_routes = mst_router.route_net(pad_objs, mark_route, record_failure)
        else:
            new_routes = mst_router.route_net_star(pad_objs, mark_route, record_failure)

        routes.extend(new_routes)

        # Record routing decision if enabled
        if self.record_decisions and routes:
            self._record_routing_decision(net, routes)

        return routes

    def _record_routing_decision(self, net: int, routes: list[Route]) -> None:
        """Record a routing decision for a net."""
        if not self._decision_store:
            return

        from kicad_tools.explain.decisions import Decision

        net_name = self.net_names.get(net, f"Net_{net}")

        # Calculate total route metrics
        total_length = sum(
            sum(
                ((s.end_x - s.start_x) ** 2 + (s.end_y - s.start_y) ** 2) ** 0.5
                for s in route.segments
            )
            for route in routes
        )
        total_vias = sum(len(route.vias) for route in routes)
        total_segments = sum(len(route.segments) for route in routes)

        # Determine rationale based on net class
        net_class = self.net_class_map.get(net_name)
        rationale_parts = []

        if net_class:
            if net_class.priority <= 1:
                rationale_parts.append(f"High-priority net ({net_class.__class__.__name__})")
            rationale_parts.append(f"Routed with {net_class.min_trace_width}mm min trace width")
            if net_class.clearance:
                rationale_parts.append(f"{net_class.clearance}mm clearance")

        if total_vias > 0:
            rationale_parts.append(f"Used {total_vias} via(s) for layer transitions")

        if not rationale_parts:
            rationale_parts.append("Standard routing using MST algorithm")

        rationale = "; ".join(rationale_parts)

        # Get pads involved
        pads_for_net = self.nets.get(net, [])
        components = list({ref for ref, _pin in pads_for_net})

        decision = Decision.create(
            action="route",
            components=components,
            nets=[net_name],
            rationale=rationale,
            decided_by="autorouter",
            metrics={
                "total_length_mm": round(total_length, 3),
                "via_count": total_vias,
                "segment_count": total_segments,
                "route_count": len(routes),
            },
        )

        self._decision_store.record(decision)

    def get_decision_store(self) -> "DecisionStore | None":
        """Get the decision store for this autorouter.

        Returns:
            DecisionStore if record_decisions was enabled, None otherwise.
        """
        return self._decision_store

    def save_decisions(self, path: "Path") -> bool:
        """Save routing decisions to a file.

        Args:
            path: Path to save the decisions JSON file.

        Returns:
            True if saved successfully, False if no decisions recorded.
        """
        if not self._decision_store:
            return False

        self._decision_store.save(path)
        return True

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
        parallel: bool = False,
        max_workers: int = 4,
    ) -> list[Route]:
        """Route all nets in priority order.

        Args:
            net_order: Optional explicit net ordering (by priority)
            progress_callback: Optional callback for progress updates
            parallel: If True, route independent nets in parallel using
                bounding box analysis to find non-overlapping net groups.
                Can provide 3-4x speedup for boards with many independent nets.
            max_workers: Maximum number of parallel workers (default: 4).
                Only used when parallel=True.

        Returns:
            List of Route objects for all nets
        """
        if parallel:
            return self.route_all_parallel(
                net_order=net_order,
                progress_callback=progress_callback,
                max_workers=max_workers,
            )

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

    def route_all_parallel(
        self,
        net_order: list[int] | None = None,
        progress_callback: ProgressCallback | None = None,
        max_workers: int = 4,
    ) -> list[Route]:
        """Route all nets using parallel execution where possible.

        Groups nets by bounding box independence and routes independent
        nets concurrently using ThreadPoolExecutor. This can provide
        3-4x speedup for boards with many independent nets.

        Args:
            net_order: Optional explicit net ordering (by priority)
            progress_callback: Optional callback for progress updates
            max_workers: Maximum number of parallel workers

        Returns:
            List of Route objects for all nets

        Example:
            >>> router = Autorouter(100, 100)
            >>> # ... add components ...
            >>> routes = router.route_all_parallel(max_workers=4)
            >>> print(f"Routed {len(routes)} routes")
        """
        print("\n=== Parallel Net Routing ===")
        print(f"  Max workers: {max_workers}")

        # Find independent groups
        clearance = self.rules.trace_clearance * 2
        groups = find_independent_groups(self.nets, self.pads, clearance)

        print(f"  Found {len(groups)} parallel groups")
        for i, group in enumerate(groups):
            print(f"    Group {i + 1}: {len(group.nets)} nets")

        # Create parallel router and execute
        parallel_router = ParallelRouter(self, max_workers=max_workers)
        result = parallel_router.route_parallel(
            net_order=net_order,
            progress_callback=progress_callback,
        )

        # Report results
        print("\n=== Parallel Routing Complete ===")
        print(f"  Successful nets: {len(result.successful_nets)}")
        print(f"  Failed nets: {len(result.failed_nets)}")
        print(f"  Conflicts resolved: {result.conflicts_resolved}")
        print(f"  Total time: {result.total_time_ms:.0f}ms")

        return result.routes

    def route_all_tuned(
        self,
        method: str = "quick",
        max_iterations: int = 10,
        profile: CostProfile | str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route all nets with auto-tuned cost parameters.

        Automatically adjusts routing cost parameters (via cost, turn cost,
        congestion cost) based on board characteristics for better results.

        Args:
            method: Tuning method to use:
                - "quick": Fast heuristic-based tuning (default)
                - "nelder-mead": Gradient-free optimization
                - "powell": Powell's optimization method
                - "adaptive": Adjust costs during routing
            max_iterations: Maximum optimization iterations (for non-quick methods)
            profile: Optional cost profile to use instead of auto-tuning:
                - CostProfile enum value
                - String: "sparse", "standard", "dense", "minimize_vias",
                  "minimize_length", "high_speed"
            progress_callback: Optional callback for progress updates

        Returns:
            List of Route objects for all nets

        Example:
            >>> router = Autorouter(100, 100)
            >>> # ... add components ...
            >>> # Auto-tune parameters
            >>> routes = router.route_all_tuned(method="quick")
            >>> # Or use a preset profile
            >>> routes = router.route_all_tuned(profile="dense")
        """
        print("\n=== Auto-Tuned Routing ===")

        # Handle preset profiles
        if profile is not None:
            if isinstance(profile, str):
                profile = CostProfile(profile)
            params = COST_PROFILES[profile]
            print(f"  Using profile: {profile.value}")
            print(f"    Via cost: {params.via}")
            print(f"    Turn cost: {params.turn}")
            print(f"    Congestion cost: {params.congestion}")

            # Apply parameters
            self.rules = params.apply_to_rules(self.rules)
            return self.route_all(progress_callback=progress_callback)

        # Analyze board characteristics
        characteristics = analyze_board(
            nets=self.nets,
            pads=self.pads,
            board_width=self.grid.width,
            board_height=self.grid.height,
            layer_count=self.grid.num_layers,
        )

        print("  Board characteristics:")
        print(f"    Pads: {characteristics.total_pads}")
        print(f"    Nets: {characteristics.total_nets}")
        print(f"    Pin density: {characteristics.pin_density:.4f} pads/mm²")
        print(f"    Avg net span: {characteristics.avg_net_span:.1f}mm")

        if method == "adaptive":
            # Use adaptive routing with dynamic cost adjustment
            print(f"  Method: Adaptive (max {max_iterations} iterations)")
            adaptive_router = create_adaptive_router(
                self,
                max_iterations=max_iterations,
            )
            routes = adaptive_router()
        elif method == "quick":
            # Fast heuristic tuning
            params = quick_tune(characteristics)
            print("  Method: Quick heuristic tuning")
            print("  Tuned parameters:")
            print(f"    Via cost: {params.via:.1f}")
            print(f"    Turn cost: {params.turn:.1f}")
            print(f"    Congestion cost: {params.congestion:.1f}")

            self.rules = params.apply_to_rules(self.rules)
            routes = self.route_all(progress_callback=progress_callback)
        else:
            # Full optimization
            print(f"  Method: {method} optimization (max {max_iterations} iterations)")
            result = tune_parameters(
                self,
                max_iterations=max_iterations,
                method=method,
            )

            print(f"  Tuning completed in {result.tuning_time_ms:.0f}ms")
            print("  Best parameters:")
            print(f"    Via cost: {result.params.via:.1f}")
            print(f"    Turn cost: {result.params.turn:.1f}")
            print(f"    Congestion cost: {result.params.congestion:.1f}")

            if result.quality:
                print(f"  Quality score: {result.quality.score:.1f}")
                print(f"    Completion: {result.quality.completion_rate * 100:.1f}%")
                print(f"    Total vias: {result.quality.total_vias}")

            self.rules = result.params.apply_to_rules(self.rules)
            routes = self.route_all(progress_callback=progress_callback)

        print("\n=== Tuned Routing Complete ===")
        print(f"  Routes: {len(routes)}")
        print(f"  Segments: {sum(len(r.segments) for r in routes)}")
        print(f"  Vias: {sum(len(r.vias) for r in routes)}")

        return routes

    def route_all_negotiated(
        self,
        max_iterations: int = 10,
        initial_present_factor: float = 0.5,
        present_factor_increment: float = 0.5,
        history_increment: float = 1.0,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
        use_targeted_ripup: bool = False,
        max_ripups_per_net: int = 3,
        adaptive: bool = True,
    ) -> list[Route]:
        """Route all nets using PathFinder-style negotiated congestion.

        Args:
            max_iterations: Maximum number of rip-up and reroute iterations
            initial_present_factor: Initial congestion penalty factor
            present_factor_increment: Factor increase per iteration (used when adaptive=False)
            history_increment: Base history cost increment per iteration
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds. If reached, returns best partial result.
            use_targeted_ripup: If True, use targeted rip-up of blocking nets instead
                of ripping up all nets through overused cells. This can improve
                convergence by only displacing nets that actually block the failed
                net's path.
            max_ripups_per_net: Maximum times a single net can be ripped up during
                targeted rip-up (prevents infinite loops). Only used when
                use_targeted_ripup=True.
            adaptive: If True (default), use adaptive parameter tuning (Issue #633):
                - History increment adjusts based on convergence progress
                - Present cost adapts to congestion level
                - Oscillation detection triggers escape strategies
                - Early termination when no progress is being made
                This improves convergence for difficult routing scenarios.

        Returns:
            List of routes (may be partial if timeout reached)
        """
        import time

        start_time = time.time()

        print("\n=== Negotiated Congestion Routing ===")
        print(f"  Max iterations: {max_iterations}")
        if adaptive:
            print("  Mode: Adaptive (Issue #633)")
            print(f"  Present factor: {initial_present_factor} (adaptive)")
            print(f"  History increment: {history_increment} (adaptive)")
        else:
            print(f"  Present factor: {initial_present_factor} + {present_factor_increment}/iter")
        if use_targeted_ripup:
            print(f"  Targeted rip-up: enabled (max {max_ripups_per_net} ripups/net)")
        if timeout:
            print(f"  Timeout: {timeout}s")

        # Track overflow history for adaptive mode (Issue #633)
        overflow_history: list[int] = []
        escape_strategy_index = 0

        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
        total_nets = len(net_order)

        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = initial_present_factor
        timed_out = False

        # Build pads_by_net mapping for escape strategies and targeted rip-up
        # (Issue #762: escape strategies need this even when use_targeted_ripup=False)
        pads_by_net: dict[int, list[Pad]] = {}
        ripup_history: dict[int, int] = {}
        for net in net_order:
            if net in self.nets:
                pads_for_routing = self.nets[net]
                if len(pads_for_routing) >= 2:
                    pads_by_net[net] = [self.pads[p] for p in pads_for_routing]

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
        overflow_history.append(overflow)  # Track for adaptive mode
        print(
            f"  Routed {len(net_routes)}/{total_nets} nets, overflow: {overflow} ({elapsed_str()})"
        )

        if timed_out:
            print("  ⚠ Returning partial result due to timeout")
        elif overflow == 0 and len(net_routes) == total_nets:
            # Only declare complete if ALL nets were routed AND no conflicts
            print("  No conflicts - routing complete!")
            if progress_callback is not None:
                progress_callback(1.0, "Routing complete - no conflicts", False)
            return list(self.routes)
        elif overflow == 0 and len(net_routes) < total_nets:
            # Some nets failed to route but no overflow - need rip-up
            failed_count = total_nets - len(net_routes)
            print(f"  ⚠ {failed_count} net(s) failed to route - attempting recovery")

        # Skip iteration loop if already timed out
        if not timed_out:
            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    print(f"\n  ⚠ Timeout reached at iteration {iteration} ({elapsed_str()})")
                    timed_out = True
                    break

                # Adaptive early termination check (Issue #633)
                if adaptive and should_terminate_early(overflow_history, iteration):
                    print(f"\n  ⚠ Early termination: no progress detected ({elapsed_str()})")
                    print(f"    Overflow history: {overflow_history[-5:]}")
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

                # Calculate adaptive parameters (Issue #633)
                if adaptive:
                    # Calculate congestion ratio for adaptive present cost
                    total_cells = self.grid.cols * self.grid.rows * self.grid.num_layers
                    overflow_ratio = overflow / max(total_cells, 1)

                    # Adaptive history increment based on convergence progress
                    adaptive_history = calculate_history_increment(
                        iteration, overflow_history, history_increment
                    )
                    # Adaptive present cost based on iteration and congestion
                    present_factor = calculate_present_cost(
                        iteration, max_iterations, overflow_ratio, initial_present_factor
                    )
                    print(
                        f"  Adaptive params: history={adaptive_history:.2f}, present={present_factor:.2f}"
                    )
                    self.grid.update_history_costs(adaptive_history)
                else:
                    present_factor += present_factor_increment
                    self.grid.update_history_costs(history_increment)

                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)

                # Issue #858: Also include nets that completely failed to route
                # (not in net_routes) - these need recovery via targeted rip-up
                failed_nets_to_recover = [
                    n for n in net_order if n not in net_routes and n in pads_by_net
                ]
                if failed_nets_to_recover:
                    # Add failed nets to reroute list if not already present
                    for failed_net in failed_nets_to_recover:
                        if failed_net not in nets_to_reroute:
                            nets_to_reroute.append(failed_net)
                    print(f"  Including {len(failed_nets_to_recover)} failed net(s) in recovery")

                if use_targeted_ripup:
                    # Targeted rip-up: for each conflicting net, find its specific blockers
                    # and only rip up those instead of all conflicting nets at once
                    print(
                        f"  Using targeted rip-up for {len(nets_to_reroute)} nets with conflicts ({elapsed_str()})"
                    )
                    targeted_ripup_count = 0
                    failed_nets: list[int] = []

                    for i, failed_net in enumerate(nets_to_reroute):
                        if check_timeout():
                            print(
                                f"  ⚠ Timeout during targeted reroute at net {i}/{len(nets_to_reroute)} ({elapsed_str()})"
                            )
                            timed_out = True
                            break

                        # Find blocking nets for this failed net
                        pads = pads_by_net.get(failed_net, [])
                        if len(pads) < 2:
                            continue

                        # Find which nets are blocking by checking pad connections
                        blocking_nets: set[int] = set()
                        for j in range(len(pads) - 1):
                            blockers = neg_router.find_blocking_nets_for_connection(
                                pads[j], pads[j + 1]
                            )
                            blocking_nets.update(blockers)

                        if blocking_nets:
                            # Use targeted rip-up to displace only blocking nets
                            def mark_route(route: Route) -> None:
                                self._mark_route(route)

                            success = neg_router.targeted_ripup(
                                failed_net=failed_net,
                                blocking_nets=blocking_nets,
                                net_routes=net_routes,
                                routes_list=self.routes,
                                pads_by_net=pads_by_net,
                                present_cost_factor=present_factor,
                                mark_route_callback=mark_route,
                                ripup_history=ripup_history,
                                max_ripups_per_net=max_ripups_per_net,
                            )
                            if success:
                                targeted_ripup_count += 1
                            else:
                                failed_nets.append(failed_net)
                        else:
                            # Issue #858: No blocking nets found by direct-line check.
                            # This can happen when blocking traces don't intersect the
                            # direct path but still prevent routing via clearance/congestion.
                            # Try ripping up ALL routed nets and re-routing failed net first.
                            routed_nets = list(net_routes.keys())
                            if routed_nets and iteration <= 2:  # Try in first few iterations
                                print(
                                    f"    No direct blockers found - trying full reorder for net {failed_net}"
                                )
                                # Rip up all routed nets
                                neg_router.rip_up_nets(routed_nets, net_routes, self.routes)

                                # Route the failed net first (it now has priority)
                                routes = self._route_net_negotiated(failed_net, present_factor)
                                if routes:
                                    net_routes[failed_net] = routes
                                    for route in routes:
                                        self.grid.mark_route_usage(route)
                                        self.routes.append(route)
                                    targeted_ripup_count += 1

                                # Always re-route the other nets (even if failed net didn't route)
                                for other_net in routed_nets:
                                    other_routes = self._route_net_negotiated(
                                        other_net, present_factor
                                    )
                                    if other_routes:
                                        net_routes[other_net] = other_routes
                                        for route in other_routes:
                                            self.grid.mark_route_usage(route)
                                            self.routes.append(route)
                            else:
                                # Fallback: try regular reroute
                                routes = self._route_net_negotiated(failed_net, present_factor)
                                if routes:
                                    net_routes[failed_net] = routes
                                    targeted_ripup_count += 1
                                    for route in routes:
                                        self.grid.mark_route_usage(route)
                                        self.routes.append(route)

                    if timed_out:
                        break

                    overflow = self.grid.get_total_overflow()
                    overused = self.grid.find_overused_cells()
                    # Track overflow for both branches (Issue #633)
                    overflow_history.append(overflow)
                    print(
                        f"  Targeted rip-up resolved {targeted_ripup_count}/{len(nets_to_reroute)} nets, "
                        f"overflow: {overflow} ({elapsed_str()})"
                    )

                    # Check for convergence in targeted mode
                    # Issue #858: Also check that all nets were routed
                    if overflow == 0 and len(net_routes) == total_nets:
                        print(f"  Convergence achieved at iteration {iteration}!")
                        break

                    # Adaptive oscillation detection for targeted mode (Issue #633)
                    if adaptive and detect_oscillation(overflow_history):
                        print(f"  ⚠ Oscillation detected: {overflow_history[-4:]}")
                        print(f"    Attempting escape strategy {escape_strategy_index + 1}...")

                        def mark_route_targeted(route: Route) -> None:
                            self._mark_route(route)

                        success, new_overflow = neg_router.escape_local_minimum(
                            overflow_history=overflow_history,
                            net_routes=net_routes,
                            routes_list=self.routes,
                            pads_by_net=pads_by_net,
                            net_order=net_order,
                            present_cost_factor=present_factor,
                            mark_route_callback=mark_route_targeted,
                            strategy_index=escape_strategy_index,
                        )
                        escape_strategy_index += 1

                        if success:
                            print(f"    Escape successful! Overflow: {overflow} → {new_overflow}")
                            overflow = new_overflow
                            overflow_history[-1] = new_overflow
                            overused = self.grid.find_overused_cells()
                        else:
                            print("    Escape attempt did not improve overflow")

                    # Skip common code since targeted ripup handles everything
                    # (convergence and oscillation already checked above)
                    continue

                else:
                    # Full rip-up: rip up all nets through overused cells
                    print(
                        f"  Ripping up {len(nets_to_reroute)} nets with conflicts ({elapsed_str()})"
                    )

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

                # Track overflow history for adaptive mode (Issue #633)
                overflow_history.append(overflow)

                # Issue #858: Also check that all nets were routed
                if overflow == 0 and len(net_routes) == total_nets:
                    print(f"  Convergence achieved at iteration {iteration}!")
                    break

                # Adaptive oscillation detection and escape (Issue #633)
                if adaptive and detect_oscillation(overflow_history):
                    print(f"  ⚠ Oscillation detected: {overflow_history[-4:]}")
                    print(f"    Attempting escape strategy {escape_strategy_index + 1}...")

                    def mark_route(route: Route) -> None:
                        self._mark_route(route)

                    success, new_overflow = neg_router.escape_local_minimum(
                        overflow_history=overflow_history,
                        net_routes=net_routes,
                        routes_list=self.routes,
                        pads_by_net=pads_by_net,
                        net_order=net_order,
                        present_cost_factor=present_factor,
                        mark_route_callback=mark_route,
                        strategy_index=escape_strategy_index,
                    )
                    escape_strategy_index += 1

                    if success:
                        print(f"    Escape successful! Overflow: {overflow} → {new_overflow}")
                        overflow = new_overflow
                        overflow_history[-1] = new_overflow  # Update last entry
                        overused = self.grid.find_overused_cells()
                    else:
                        print("    Escape attempt did not improve overflow")
                        # Continue to next iteration with different parameters

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

    # =========================================================================
    # TWO-PHASE ROUTING (GLOBAL + DETAILED)
    # =========================================================================

    def route_all_two_phase(
        self,
        use_negotiated: bool = True,
        corridor_width_factor: float = 2.0,
        corridor_penalty: float = 5.0,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets using two-phase global+detailed routing.

        Phase 1 (Global): Use SparseRouter to find coarse paths and reserve
        corridors for each net. This establishes routing channels that prevent
        nets from blocking each other.

        Phase 2 (Detailed): Use grid-based routing with corridor guidance.
        Routes prefer to stay within their assigned corridors but can exit
        with a cost penalty.

        This approach provides:
        - Early detection of unroutable nets (global routing fails fast)
        - Better resource allocation (corridors prevent contention)
        - Faster convergence (detailed router has guidance)

        Args:
            use_negotiated: Use negotiated congestion routing in detailed phase
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            corridor_penalty: Cost penalty for routing outside corridor (default: 5.0)
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of routes (may be partial if timeout reached or some nets fail)
        """
        import time

        start_time = time.time()

        print("\n=== Two-Phase Routing (Global + Detailed) ===")

        # Get nets to route in priority order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
        total_nets = len(net_order)

        if total_nets == 0:
            print("  No nets to route")
            return []

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        # =====================================================================
        # Phase 1: Global Routing with SparseRouter
        # =====================================================================
        print("\n--- Phase 1: Global Routing ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Phase 1: Global routing", True):
                return list(self.routes)

        # Create sparse router for global routing
        sparse_router = SparseRouter(
            width=self.grid.width,
            height=self.grid.height,
            rules=self.rules,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_layers=self.grid.num_layers,
        )

        # Add all pads to sparse router
        for pad in self.pads.values():
            sparse_router.add_pad(pad)

        # Build the sparse graph
        sparse_router.build_graph()
        stats = sparse_router.get_statistics()
        print(f"  Sparse graph: {stats['total_waypoints']} waypoints, {stats['total_edges']} edges")

        # Find global paths and reserve corridors
        corridors: dict[int, Corridor] = {}
        global_failures: list[int] = []
        corridor_width = corridor_width_factor * self.rules.trace_clearance

        for i, net in enumerate(net_order):
            if check_timeout():
                print(
                    f"  ⚠ Timeout during global routing at net {i}/{total_nets} ({elapsed_str()})"
                )
                break

            pads = self.nets[net]
            if len(pads) < 2:
                continue

            # For multi-pad nets, find path between first two pads
            # (MST routing will handle the rest in detailed phase)
            pad1 = self.pads[pads[0]]
            pad2 = self.pads[pads[1]]

            waypoints = sparse_router.find_global_path(pad1, pad2)

            if waypoints:
                corridor = sparse_router.reserve_corridor(net, waypoints, corridor_width)
                corridors[net] = corridor
            else:
                global_failures.append(net)
                net_name = self.net_names.get(net, f"Net {net}")
                print(f"  ⚠ Global routing failed for {net_name}")

        print(
            f"  Global routing: {len(corridors)}/{total_nets} nets have corridors ({elapsed_str()})"
        )
        if global_failures:
            print(f"  ⚠ {len(global_failures)} nets failed global routing (will attempt anyway)")

        # =====================================================================
        # Phase 2: Detailed Routing with Corridor Guidance
        # =====================================================================
        print("\n--- Phase 2: Detailed Routing ---")
        if progress_callback is not None:
            if not progress_callback(0.3, "Phase 2: Detailed routing", True):
                return list(self.routes)

        # Set corridor preferences on the grid
        for net, corridor in corridors.items():
            self.grid.set_corridor_preference(corridor, net, corridor_penalty)

        # Route using negotiated or standard routing
        if use_negotiated:
            detailed_routes = self._route_two_phase_detailed_negotiated(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )
        else:
            detailed_routes = self._route_two_phase_detailed_standard(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )

        # Clear corridor preferences (not needed after routing)
        self.grid.clear_all_corridor_preferences()

        # Summary
        successful_nets = len({r.net for r in detailed_routes})
        total_elapsed = time.time() - start_time
        print("\n=== Two-Phase Routing Complete ===")
        print(f"  Total nets: {total_nets}")
        print(f"  Global routing: {len(corridors)} corridors assigned")
        print(f"  Detailed routing: {successful_nets} nets routed")
        print(f"  Total time: {total_elapsed:.1f}s")

        if progress_callback is not None:
            progress_callback(
                1.0,
                f"Complete: {successful_nets}/{total_nets} nets routed in {total_elapsed:.1f}s",
                False,
            )

        return detailed_routes

    def _route_two_phase_detailed_negotiated(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed routing phase using negotiated congestion routing."""
        import time

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        total_nets = len(net_order)

        # Use negotiated routing with corridor guidance
        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = 0.5

        # Initial routing pass
        for i, net in enumerate(net_order):
            if check_timeout():
                print(
                    f"  ⚠ Timeout during detailed routing at net {i}/{total_nets} ({elapsed_str()})"
                )
                break

            net_name = self.net_names.get(net, f"Net {net}")
            pct = (i / total_nets * 100) if total_nets > 0 else 0
            print(f"  [{pct:5.1f}%] Routing {net_name}... ({elapsed_str()})")

            routes = self._route_net_with_corridor(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        print(f"  Initial pass: {len(net_routes)}/{total_nets} nets, overflow: {overflow}")

        # Rip-up and reroute iterations if needed
        if overflow > 0:
            max_iterations = 10
            history_increment = 1.0
            present_factor_increment = 0.5

            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    print(f"  ⚠ Timeout at iteration {iteration} ({elapsed_str()})")
                    break

                if progress_callback is not None:
                    progress = 0.3 + 0.6 * (iteration / max_iterations)
                    if not progress_callback(
                        progress, f"Iteration {iteration}/{max_iterations}", True
                    ):
                        break

                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                overused = self.grid.find_overused_cells()
                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)
                print(
                    f"  Iteration {iteration}: ripping up {len(nets_to_reroute)} nets ({elapsed_str()})"
                )

                neg_router.rip_up_nets(nets_to_reroute, net_routes, self.routes)

                for net in nets_to_reroute:
                    if check_timeout():
                        break
                    routes = self._route_net_with_corridor(net, present_factor)
                    if routes:
                        net_routes[net] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            self.routes.append(route)

                overflow = self.grid.get_total_overflow()
                print(f"  Iteration {iteration} complete: overflow={overflow}")

                if overflow == 0:
                    print(f"  Converged at iteration {iteration}!")
                    break

        return list(self.routes)

    def _route_two_phase_detailed_standard(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed routing phase using standard routing (no negotiation)."""
        import time

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        total_nets = len(net_order)
        all_routes: list[Route] = []

        for i, net in enumerate(net_order):
            if check_timeout():
                print(f"  ⚠ Timeout at net {i}/{total_nets} ({elapsed_str()})")
                break

            if progress_callback is not None:
                progress = 0.3 + 0.7 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self.route_net(net)
            all_routes.extend(routes)

        return all_routes

    def _route_net_with_corridor(self, net: int, present_cost_factor: float) -> list[Route]:
        """Route a single net with corridor-aware costs.

        This is similar to _route_net_negotiated but the pathfinder will
        use corridor costs from the grid when available.
        """
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

        # Route with corridor-aware costs (negotiated router will pick up corridor costs)
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
        use_two_phase: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Unified entry point for advanced routing strategies.

        Args:
            monte_carlo_trials: Number of Monte Carlo trials (0 = disabled)
            use_negotiated: Use negotiated congestion routing
            use_two_phase: Use two-phase global+detailed routing
            progress_callback: Optional callback for progress updates

        Returns:
            List of routes

        Note:
            Priority order: monte_carlo > two_phase > negotiated > standard
        """
        if monte_carlo_trials > 0:
            return self.route_all_monte_carlo(
                monte_carlo_trials, use_negotiated, progress_callback=progress_callback
            )
        elif use_two_phase:
            return self.route_all_two_phase(
                use_negotiated=use_negotiated, progress_callback=progress_callback
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
        layer_stats = self.get_layer_usage_statistics()

        return {
            "routes": len(self.routes),
            "segments": sum(len(r.segments) for r in self.routes),
            "vias": sum(len(r.vias) for r in self.routes),
            "total_length_mm": total_length,
            "nets_routed": len({r.net for r in self.routes}),
            "max_congestion": congestion_stats["max_congestion"],
            "avg_congestion": congestion_stats["avg_congestion"],
            "congested_regions": congestion_stats["congested_regions"],
            "layer_usage": layer_stats,
        }

    def get_layer_usage_statistics(self) -> dict:
        """Get layer utilization statistics from routed segments (Issue #625).

        Returns:
            Dictionary with:
            - per_layer: Dict mapping layer index to usage statistics
            - total_length: Total trace length across all layers
            - most_used_layer: Layer index with highest usage
            - least_used_layer: Layer index with lowest usage (among used layers)
            - balance_ratio: Ratio of min/max usage (1.0 = perfectly balanced)
        """
        # Count segments and length per layer
        layer_stats: dict[int, dict] = {}

        for route in self.routes:
            for seg in route.segments:
                # Get layer index from segment
                layer_idx = (
                    self.grid.layer_to_index(seg.layer.value)
                    if self.layer_stack
                    else seg.layer.value
                )

                if layer_idx not in layer_stats:
                    layer_stats[layer_idx] = {
                        "segments": 0,
                        "length_mm": 0.0,
                        "nets": set(),
                    }

                seg_length = math.sqrt((seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2)
                layer_stats[layer_idx]["segments"] += 1
                layer_stats[layer_idx]["length_mm"] += seg_length
                layer_stats[layer_idx]["nets"].add(route.net)

        # Convert sets to counts for JSON serialization
        for layer_idx, stats in layer_stats.items():
            stats["net_count"] = len(stats["nets"])
            del stats["nets"]

        # Calculate summary statistics
        total_length = sum(s["length_mm"] for s in layer_stats.values())
        lengths = [s["length_mm"] for s in layer_stats.values()] if layer_stats else [0]

        most_used = (
            max(layer_stats.keys(), key=lambda k: layer_stats[k]["length_mm"]) if layer_stats else 0
        )
        least_used = (
            min(layer_stats.keys(), key=lambda k: layer_stats[k]["length_mm"]) if layer_stats else 0
        )

        # Balance ratio: min/max (1.0 = perfectly balanced)
        max_length = max(lengths) if lengths else 0
        min_length = min(lengths) if lengths else 0
        balance_ratio = min_length / max_length if max_length > 0 else 1.0

        return {
            "per_layer": layer_stats,
            "total_length": total_length,
            "most_used_layer": most_used,
            "least_used_layer": least_used,
            "balance_ratio": balance_ratio,
        }

    def enable_auto_layer_preferences(self) -> None:
        """Enable automatic layer preference assignment (Issue #625).

        Analyzes the layer stack and updates the net class map with
        appropriate layer preferences based on signal types:

        - Power/Ground: Inner layers adjacent to planes
        - High-speed/Clock: Layers with reference planes
        - Analog: Outer layers (away from digital noise)
        - Digital: Outer layers (easy access)

        This should be called before routing if you want intelligent
        layer assignment based on signal type.

        Requires a layer_stack to be set on the autorouter.
        """
        if self.layer_stack is None:
            # No layer stack - can't determine layer preferences
            return

        # Update net class map with layer preferences
        self.net_class_map = assign_layer_preferences(
            self.net_class_map,
            self.layer_stack,
        )

        # Also update the router's net class map
        if hasattr(self.router, "net_class_map"):
            self.router.net_class_map = self.net_class_map

    # =========================================================================
    # Length Constraint API (Issue #630)
    # =========================================================================

    def add_length_constraint(self, constraint: LengthConstraint) -> None:
        """Add a length constraint for a net.

        Length constraints are used to enforce timing requirements for
        signals like DDR data buses, differential pairs, and clock
        distribution networks.

        Args:
            constraint: LengthConstraint specifying min/max length or match group

        Example::

            from kicad_tools.router import LengthConstraint

            # Minimum length constraint
            router.add_length_constraint(LengthConstraint(
                net_id=100,
                min_length=50.0,  # mm
            ))

            # Match group for DDR data
            for net_id in [100, 101, 102, 103]:
                router.add_length_constraint(LengthConstraint(
                    net_id=net_id,
                    match_group="DDR_DATA",
                    match_tolerance=0.5,  # mm
                ))
        """
        self._length_tracker.add_constraint(constraint)

    def add_match_group(
        self,
        name: str,
        net_ids: list[int],
        tolerance: float = 0.5,
        min_length: float | None = None,
        max_length: float | None = None,
    ) -> None:
        """Add length constraints for a match group.

        This is a convenience method for creating multiple constraints
        that all belong to the same match group. All nets in the group
        must have similar lengths (within tolerance).

        Args:
            name: Match group name (e.g., "DDR_DATA")
            net_ids: List of net IDs in the group
            tolerance: Length match tolerance in mm (default: 0.5)
            min_length: Minimum length for all nets (optional)
            max_length: Maximum length for all nets (optional)

        Example::

            # DDR data bus - all 8 bits must match
            router.add_match_group(
                "DDR_DATA",
                [100, 101, 102, 103, 104, 105, 106, 107],
                tolerance=0.5,  # 0.5mm tolerance
            )
        """
        from .length import create_match_group

        constraints = create_match_group(
            name=name,
            net_ids=net_ids,
            tolerance=tolerance,
            min_length=min_length,
            max_length=max_length,
        )
        for constraint in constraints:
            self._length_tracker.add_constraint(constraint)

    def get_length_violations(self) -> list[LengthViolation]:
        """Get all length constraint violations.

        Should be called after routing is complete to check if any
        length constraints were violated.

        Returns:
            List of LengthViolation objects describing any violations

        Example::

            # After routing
            violations = router.get_length_violations()
            for v in violations:
                print(f"Violation: {v}")
        """
        # Update tracker with current route lengths
        self._update_length_tracker()
        return self._length_tracker.get_violations()

    def get_length_statistics(self) -> dict:
        """Get statistics about tracked route lengths.

        Returns:
            Dictionary with length statistics including:
            - total_nets: Number of nets with length tracking
            - constrained_nets: Number of nets with constraints
            - match_groups: Number of match groups
            - violations: Number of constraint violations
            - min_length: Minimum route length
            - max_length: Maximum route length
            - avg_length: Average route length
        """
        self._update_length_tracker()
        return self._length_tracker.get_statistics()

    def _update_length_tracker(self) -> None:
        """Update the length tracker with current route lengths."""
        for route in self.routes:
            self._length_tracker.record_route(route.net, route)

    def apply_length_tuning(
        self,
        verbose: bool = True,
    ) -> dict[int, tuple[Route, any]]:
        """Apply serpentine tuning to routes that don't meet length constraints.

        This post-routing pass adds serpentine (meander) patterns to
        routes that are too short or need to match other routes in
        their match group.

        Args:
            verbose: Whether to print progress information

        Returns:
            Dictionary mapping net ID to (tuned_route, result)

        Example::

            # After routing, tune lengths
            results = router.apply_length_tuning()
            for net_id, (new_route, result) in results.items():
                if result.success:
                    print(f"Net {net_id}: added {result.length_added:.3f}mm")
        """
        from .optimizer.serpentine import SerpentineGenerator, tune_match_group

        self._update_length_tracker()
        results: dict[int, tuple[Route, any]] = {}

        # Get violations to determine which nets need tuning
        violations = self._length_tracker.get_violations()

        if not violations and verbose:
            print("No length violations - no tuning needed")
            return results

        if verbose:
            print(f"\n=== Length Tuning ({len(violations)} violations) ===")

        # Build routes by net ID
        routes_by_net: dict[int, Route] = {}
        for route in self.routes:
            routes_by_net[route.net] = route

        # Process match groups
        processed_groups: set[str] = set()
        for group_name, net_ids in self._length_tracker.match_groups.items():
            if group_name in processed_groups:
                continue
            processed_groups.add(group_name)

            # Get tolerance from first constraint
            tolerance = 0.5
            if net_ids and net_ids[0] in self._length_tracker._constraint_map:
                tolerance = self._length_tracker._constraint_map[net_ids[0]].match_tolerance

            if verbose:
                print(f"  Tuning match group '{group_name}' ({len(net_ids)} nets)")

            group_results = tune_match_group(
                routes=routes_by_net,
                group_net_ids=net_ids,
                tolerance=tolerance,
                grid=self.grid,
            )

            # Update routes and collect results
            for net_id, (new_route, result) in group_results.items():
                if result.success and result.length_added > 0:
                    # Replace route in routes list
                    for i, r in enumerate(self.routes):
                        if r.net == net_id:
                            self.routes[i] = new_route
                            break
                    routes_by_net[net_id] = new_route

                    if verbose:
                        print(f"    Net {net_id}: {result.message}")

                results[net_id] = (new_route, result)

        # Process individual min length violations (not in match groups)
        generator = SerpentineGenerator()
        for violation in violations:
            if violation.violation_type.value == "too_short":
                net_id = violation.net_id
                if isinstance(net_id, str):
                    continue  # Match group, already processed

                constraint = self._length_tracker.get_constraint(net_id)
                if constraint and constraint.match_group:
                    continue  # Part of a match group, already processed

                route = routes_by_net.get(net_id)
                if not route:
                    continue

                target = violation.target_length or 0
                new_route, result = generator.add_serpentine(route, target, self.grid)

                if result.success and result.length_added > 0:
                    for i, r in enumerate(self.routes):
                        if r.net == net_id:
                            self.routes[i] = new_route
                            break

                    if verbose:
                        net_name = self.net_names.get(net_id, f"Net {net_id}")
                        print(f"  {net_name}: {result.message}")

                results[net_id] = (new_route, result)

        if verbose:
            print("=== Length Tuning Complete ===\n")

        return results

    @property
    def length_tracker(self) -> LengthTracker:
        """Get the length tracker for manual inspection.

        Returns:
            LengthTracker instance with recorded lengths and constraints
        """
        return self._length_tracker

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

    # =========================================================================
    # Placement-Routing Feedback API
    # =========================================================================

    def route_with_placement_feedback(
        self,
        pcb: any = None,
        max_adjustments: int = 3,
        use_negotiated: bool = True,
        min_confidence: float = 0.5,
        verbose: bool = True,
    ) -> PlacementFeedbackResult:
        """Route with automatic placement adjustment on failures.

        Implements a closed-loop feedback system between routing failures
        and placement optimization. When routing fails, this method:

        1. Analyzes failures to determine root causes
        2. Generates placement strategies to resolve failures
        3. Applies the best safe strategy to adjust placement
        4. Clears routes and retries routing
        5. Repeats until success or max iterations reached

        This enables automatic recovery from placement-induced routing
        failures without manual intervention.

        Args:
            pcb: The PCB object to modify placement on. If None, only
                routing will be attempted (no placement adjustment).
            max_adjustments: Maximum number of placement adjustments to try.
                Each adjustment moves one or more components.
            use_negotiated: Whether to use negotiated congestion routing.
                Negotiated routing is generally more successful but slower.
            min_confidence: Minimum confidence required to apply a strategy.
                Strategies below this threshold are skipped.
            verbose: Whether to print progress information.

        Returns:
            PlacementFeedbackResult with:
            - success: Whether all nets were routed
            - routes: Final list of routes
            - iterations: Number of iterations performed
            - adjustments: List of placement adjustments made
            - failed_nets: Net IDs that remain unrouted
            - total_components_moved: Total components moved

        Example::

            from kicad_tools.router import Autorouter
            from kicad_tools.schema.pcb import PCB

            # Load PCB
            pcb = PCB.from_file("board.kicad_pcb")

            # Create router and add components
            router = Autorouter(100, 100)
            for fp in pcb.footprints:
                pads = [...]  # Extract pad info
                router.add_component(fp.reference, pads)

            # Route with automatic placement feedback
            result = router.route_with_placement_feedback(
                pcb=pcb,
                max_adjustments=3,
            )

            if result.success:
                print(f"Routed successfully!")
                print(f"  Iterations: {result.iterations}")
                print(f"  Components moved: {result.total_components_moved}")
            else:
                print(f"Failed to route {len(result.failed_nets)} nets")
                for adj in result.adjustments:
                    print(f"  Tried: {adj.result.message}")

        Note:
            When pcb is None, this method behaves like route_all() or
            route_all_negotiated() depending on the use_negotiated flag.
            Placement adjustments require a PCB object to modify.

        See Also:
            - route_all_negotiated: For negotiated routing without feedback
            - analyze_routing_failure: For analyzing individual failures
            - get_failed_nets: For getting list of failed nets
        """
        feedback_loop = PlacementFeedbackLoop(
            router=self,
            pcb=pcb,
            verbose=verbose,
        )

        return feedback_loop.run(
            max_adjustments=max_adjustments,
            use_negotiated=use_negotiated,
            min_confidence=min_confidence,
        )

    # =========================================================================
    # Escape Routing API (Dense Packages)
    # =========================================================================

    @property
    def _escape(self) -> EscapeRouter:
        """Lazy-initialize escape router."""
        if self._escape_router is None:
            self._escape_router = EscapeRouter(self.grid, self.rules)
        return self._escape_router

    def detect_dense_packages(self) -> list[PackageInfo]:
        """Detect dense packages that need escape routing.

        Identifies components where pin pitch is too small for traces to
        pass between adjacent pins, or where pin count exceeds 48. Uses
        the design rules (trace width and clearance) to calculate the
        minimum pitch needed for routing.

        For example, a TQFP-32 with 0.8mm pitch may need escape routing
        when using 0.2mm traces with 0.2mm clearance, because the
        required routing space (2 * 0.4mm = 0.8mm) equals the pin pitch.

        Returns:
            List of PackageInfo for dense packages

        Example::

            dense = router.detect_dense_packages()
            for pkg in dense:
                print(f"{pkg.ref}: {pkg.package_type.name} ({pkg.pin_count} pins)")
        """
        dense_packages: list[PackageInfo] = []

        # Group pads by component reference
        component_pads: dict[str, list[Pad]] = {}
        for (ref, _), pad in self.pads.items():
            if ref not in component_pads:
                component_pads[ref] = []
            component_pads[ref].append(pad)

        # Check each component using design rules for dynamic threshold
        for ref, pads in component_pads.items():
            if is_dense_package(
                pads,
                trace_width=self.rules.trace_width,
                clearance=self.rules.trace_clearance,
            ):
                info = self._escape.analyze_package(pads)
                dense_packages.append(info)

        return dense_packages

    def generate_escape_routes(
        self,
        packages: list[PackageInfo] | None = None,
    ) -> list[Route]:
        """Generate escape routes for dense packages.

        Creates escape routes that guide pins outward from dense packages
        without blocking each other. For BGA packages, uses ring-based
        escape with layer alternation. For QFP/QFN, uses alternating
        direction escape.

        Args:
            packages: List of packages to generate escapes for.
                If None, auto-detects dense packages.

        Returns:
            List of Route objects for the escape paths

        Example::

            # Auto-detect and generate escapes
            escape_routes = router.generate_escape_routes()

            # Or specify packages
            packages = router.detect_dense_packages()
            escape_routes = router.generate_escape_routes(packages)
        """
        if packages is None:
            packages = self.detect_dense_packages()

        all_routes: list[Route] = []

        for package in packages:
            escapes = self._escape.generate_escapes(package)
            routes = self._escape.apply_escape_routes(escapes)
            all_routes.extend(routes)

            # Track these routes
            self.routes.extend(routes)

            print(
                f"  Escape routes: {package.ref} ({package.package_type.name})"
                f" - {len(escapes)} pins escaped"
            )

        return all_routes

    def route_with_escape(
        self,
        use_negotiated: bool = True,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route with automatic escape routing for dense packages.

        First generates escape routes for any detected dense packages
        (BGA, QFP, QFN with high pin count or fine pitch), then routes
        remaining connections using the standard algorithm.

        This is the recommended approach for boards with BGAs or
        fine-pitch ICs that struggle with standard routing.

        Args:
            use_negotiated: Use negotiated congestion routing
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of all routes (escapes + regular routing)

        Example::

            # Route with automatic escape handling
            routes = router.route_with_escape()

            # Check statistics
            stats = router.get_statistics()
            print(f"Routed {stats['nets_routed']} nets")
        """
        print("\n=== Routing with Escape Pattern Generation ===")

        # Phase 1: Detect and route dense packages
        dense_packages = self.detect_dense_packages()

        if dense_packages:
            print(f"\n--- Phase 1: Escape Routing ({len(dense_packages)} dense packages) ---")
            for pkg in dense_packages:
                print(f"  {pkg.ref}: {pkg.package_type.name}, {pkg.pin_count} pins")

            escape_routes = self.generate_escape_routes(dense_packages)
            print(f"  Generated {len(escape_routes)} escape route segments")
        else:
            print("\n--- No dense packages detected, skipping escape routing ---")
            escape_routes = []

        # Phase 2: Route remaining connections
        print("\n--- Phase 2: Main Routing ---")

        if use_negotiated:
            main_routes = self.route_all_negotiated(
                progress_callback=progress_callback,
                timeout=timeout,
            )
        else:
            main_routes = self.route_all(
                progress_callback=progress_callback,
            )

        # Combine results
        all_routes = escape_routes + main_routes

        # Summary
        stats = self.get_statistics()
        print("\n=== Routing with Escape Complete ===")
        print(f"  Dense packages escaped: {len(dense_packages)}")
        print(f"  Total nets routed: {stats['nets_routed']}")
        print(f"  Total segments: {stats['segments']}")
        print(f"  Total vias: {stats['vias']}")

        return all_routes

    def get_escape_statistics(self) -> dict:
        """Get statistics about escape routing.

        Returns:
            Dictionary with escape routing stats:
            - dense_packages: Number of detected dense packages
            - total_pins_escaped: Total pins with escape routes
            - escape_segments: Number of escape route segments
            - escape_vias: Number of vias used in escapes
        """
        dense_packages = self.detect_dense_packages()

        total_pins = sum(pkg.pin_count for pkg in dense_packages)

        return {
            "dense_packages": len(dense_packages),
            "total_pins_escaped": total_pins,
            "package_details": [
                {
                    "ref": pkg.ref,
                    "type": pkg.package_type.name,
                    "pin_count": pkg.pin_count,
                    "pitch": pkg.pin_pitch,
                    "is_dense": pkg.is_dense,
                }
                for pkg in dense_packages
            ],
        }
