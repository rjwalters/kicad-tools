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

    from .pathfinder import Router

from kicad_tools.cli.progress import flush_print

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
from .subgrid import SubGridResult, SubGridRouter
from .failure_analysis import (
    CongestionMap,
    FailureAnalysis,
    FailureCause,
    RootCauseAnalyzer,
)
from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .length import LengthTracker, LengthViolation
from .output import format_failed_nets_summary
from .parallel import (
    ParallelRouter,
    RegionBasedNegotiatedRouter,
    find_independent_groups,
)
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
from .global_router import CorridorAssignment, GlobalRouter, GlobalRoutingResult
from .region_graph import RegionGraph
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
        source_coords: Source pad coordinates (x, y) in mm
        target_coords: Target pad coordinates (x, y) in mm
        blocking_nets: Set of net IDs that blocked this route
        blocking_components: Components blocking the path (e.g., "U1", "R4")
        reason: Human-readable failure reason
        failure_cause: Categorized failure cause from FailureCause enum
        analysis: Detailed failure analysis from RootCauseAnalyzer (optional)
    """

    net: int
    net_name: str
    source_pad: tuple[str, str]
    target_pad: tuple[str, str]
    source_coords: tuple[float, float] | None = None
    target_coords: tuple[float, float] | None = None
    blocking_nets: set[int] = field(default_factory=set)
    blocking_components: list[str] = field(default_factory=list)
    reason: str = "No path found"
    failure_cause: FailureCause = FailureCause.UNKNOWN
    analysis: FailureAnalysis | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "net": self.net,
            "net_name": self.net_name,
            "source_pad": {"ref": self.source_pad[0], "pin": self.source_pad[1]},
            "target_pad": {"ref": self.target_pad[0], "pin": self.target_pad[1]},
            "source_coords": list(self.source_coords) if self.source_coords else None,
            "target_coords": list(self.target_coords) if self.target_coords else None,
            "blocking_nets": list(self.blocking_nets),
            "blocking_components": self.blocking_components,
            "reason": self.reason,
            "failure_cause": self.failure_cause.value,
        }
        if self.analysis:
            result["analysis"] = self.analysis.to_dict()
        return result


@dataclass
class MSTEdgeInfo:
    """Information about an MST edge for interleaved net ordering.

    Used to track individual edges of N-port nets so they can be
    interleaved with 2-port nets based on edge length.

    Attributes:
        net_id: The net this edge belongs to
        edge_index: Index of this edge in the MST (0 = shortest)
        source_idx: Index of source pad in pad list
        target_idx: Index of target pad in pad list
        distance: Manhattan distance of this edge in mm
        is_first: Whether this is the first (shortest) edge of the net
    """

    net_id: int
    edge_index: int
    source_idx: int
    target_idx: int
    distance: float
    is_first: bool = False


# Re-export for backward compatibility
__all__ = [
    "Autorouter",
    "AdaptiveAutorouter",
    "Corridor",
    "MSTEdgeInfo",
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

        # Initialize grid and routers using shared helper
        # Issue #972: Helper includes adaptive grid resolution for large boards
        self.grid, self.router, self.zone_manager = self._create_grid_and_routers(
            width, height, origin_x, origin_y
        )

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
        self._subgrid_router: SubGridRouter | None = None

        # Length constraint tracking (Issue #630)
        self._length_tracker: LengthTracker = LengthTracker()

        # Routing failure tracking (Issue #688)
        self.routing_failures: list[RoutingFailure] = []

        # Decision recording (Issue #829)
        self.record_decisions = record_decisions
        self._decision_store: DecisionStore | None = None
        if record_decisions:
            from kicad_tools.explain.decisions import DecisionStore

            self._decision_store = DecisionStore()

        # Constraint-aware net ordering (Issue #1020)
        # Cache for component pitches, computed lazily on first access
        self._component_pitches: dict[str, float] | None = None

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

    def _create_grid_and_routers(
        self,
        width: float,
        height: float,
        origin_x: float,
        origin_y: float,
    ) -> tuple[RoutingGrid, CppPathfinder | Router, ZoneManager]:
        """Create routing grid and associated routers.

        This helper centralizes the common pattern of creating a RoutingGrid,
        hybrid router, and ZoneManager. Used by both __init__ and _reset_for_new_trial.

        Issue #972: Automatically uses adaptive grid resolution for large boards
        to prevent excessive memory usage and improve routing performance.
        Threshold: 500k cells per layer (matches create_adaptive default).

        Args:
            width: Board width in mm
            height: Board height in mm
            origin_x: X origin offset
            origin_y: Y origin offset

        Returns:
            Tuple of (RoutingGrid, Router, ZoneManager)
        """
        # Issue #972: Use adaptive resolution for large boards
        num_layers = (self.layer_stack or LayerStack.two_layer()).num_layers
        estimated_cells = (
            (width / self.rules.grid_resolution)
            * (height / self.rules.grid_resolution)
            * num_layers
        )
        adaptive_threshold = 500_000

        if estimated_cells > adaptive_threshold:
            # Use adaptive resolution for better performance on large boards
            grid = RoutingGrid.create_adaptive(
                width, height, self.rules, origin_x, origin_y, layer_stack=self.layer_stack
            )
        else:
            grid = RoutingGrid(
                width, height, self.rules, origin_x, origin_y, layer_stack=self.layer_stack
            )
        router = create_hybrid_router(grid, self.rules, force_python=self._force_python)
        zone_manager = ZoneManager(grid, self.rules)
        return grid, router, zone_manager

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

    def _get_unrouted_pads(self, exclude_net: int | None = None) -> list[Pad]:
        """Get list of pads that haven't been routed yet.

        Issue #1019: Used for via impact scoring to determine which pads
        might be blocked by a via placement.

        Args:
            exclude_net: Net ID to exclude from the list (the net being routed)

        Returns:
            List of Pad objects that haven't been connected yet.
        """
        # Collect all nets that have been fully routed
        routed_nets: set[int] = set()
        for route in self.routes:
            routed_nets.add(route.net)

        # Collect pads from nets that still need routing
        unrouted_pads = []
        for net_id, pad_keys in self.nets.items():
            if net_id == 0:  # Skip unconnected pads
                continue
            if net_id == exclude_net:  # Skip the net we're currently routing
                continue
            if net_id in routed_nets:  # Skip already routed nets
                continue
            if len(pad_keys) < 2:  # Skip single-pad nets
                continue

            for pad_key in pad_keys:
                if pad_key in self.pads:
                    unrouted_pads.append(self.pads[pad_key])

        return unrouted_pads

    def _update_router_unrouted_pads(self, current_net: int) -> None:
        """Update the router's unrouted pad information for via impact scoring.

        Issue #1019: Called before routing each net to provide the router
        with information about which pads haven't been routed yet.

        Args:
            current_net: The net ID being routed (excluded from unrouted list)
        """
        # Only update if via impact scoring is enabled
        if self.rules.via_impact_weight <= 0 and self.rules.via_exclusion_from_fine_pitch <= 0:
            return

        unrouted_pads = self._get_unrouted_pads(exclude_net=current_net)

        # Update the Router (Python backend) if available
        if hasattr(self.router, "set_unrouted_pads"):
            self.router.set_unrouted_pads(unrouted_pads)

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

        # Issue #1019: Update router with unrouted pad info for via impact scoring
        self._update_router_unrouted_pads(net)

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
            """Record a routing failure with detailed diagnostics."""
            # Get pad coordinates
            source_coords = (source_pad.x, source_pad.y)
            target_coords = (target_pad.x, target_pad.y)

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

            # Run root cause analysis for detailed diagnostics
            analyzer = RootCauseAnalyzer()
            net_name = self.net_names.get(net, f"Net_{net}")
            analysis = analyzer.analyze_routing_failure(
                grid=self.grid,
                start=source_coords,
                end=target_coords,
                net=net_name,
            )
            failure_cause = analysis.root_cause

            # Check for pad accessibility issues (pad not on grid)
            # Use tight threshold (resolution/10) to detect even slightly off-grid pads
            # that can cause routing failures when snapped to blocked grid points.
            # Previously resolution/2 missed cases where pads were "slightly" off-grid
            # but still caused BLOCKED_BY_COMPONENT errors after snapping.
            src_gx, src_gy = self.grid.world_to_grid(source_pad.x, source_pad.y)
            tgt_gx, tgt_gy = self.grid.world_to_grid(target_pad.x, target_pad.y)
            src_world = self.grid.grid_to_world(src_gx, src_gy)
            tgt_world = self.grid.grid_to_world(tgt_gx, tgt_gy)

            src_dist = (
                (source_pad.x - src_world[0]) ** 2 + (source_pad.y - src_world[1]) ** 2
            ) ** 0.5
            tgt_dist = (
                (target_pad.x - tgt_world[0]) ** 2 + (target_pad.y - tgt_world[1]) ** 2
            ) ** 0.5
            # Use tighter threshold to catch pads that are "slightly" off-grid
            grid_threshold = self.grid.resolution / 10

            # Collect all off-grid pads to report them together
            off_grid_pads: list[str] = []
            if src_dist > grid_threshold:
                off_grid_pads.append(f"{source_pad.ref}.{source_pad.pin} off by {src_dist:.3f}mm")
            if tgt_dist > grid_threshold:
                off_grid_pads.append(f"{target_pad.ref}.{target_pad.pin} off by {tgt_dist:.3f}mm")

            if off_grid_pads:
                failure_cause = FailureCause.PIN_ACCESS

                # Analyze which nets' clearance zones are blocking pad access
                pad_blockers = []
                src_layer = self.grid.layer_to_index(source_pad.layer.value)
                tgt_layer = self.grid.layer_to_index(target_pad.layer.value)

                if src_dist > grid_threshold:
                    src_blockers = analyzer.analyze_pad_access_blockers(
                        grid=self.grid,
                        pad_x=source_pad.x,
                        pad_y=source_pad.y,
                        pad_ref=f"{source_pad.ref}.{source_pad.pin}",
                        pad_net=net,
                        layer=src_layer,
                        net_names=self.net_names,
                    )
                    pad_blockers.extend(src_blockers)

                if tgt_dist > grid_threshold:
                    tgt_blockers = analyzer.analyze_pad_access_blockers(
                        grid=self.grid,
                        pad_x=target_pad.x,
                        pad_y=target_pad.y,
                        pad_ref=f"{target_pad.ref}.{target_pad.pin}",
                        pad_net=net,
                        layer=tgt_layer,
                        net_names=self.net_names,
                    )
                    pad_blockers.extend(tgt_blockers)

                # Store blockers in analysis for detailed reporting
                analysis.pad_access_blockers = pad_blockers

                # Build detailed reason with blocking net information
                if pad_blockers:
                    # Group by pad and format
                    blocker_details = []
                    for blocker in pad_blockers[:3]:  # Show top 3 blockers
                        blocker_details.append(
                            f"{blocker.pad_ref}: blocked by {blocker.blocking_net_name} "
                            f"({blocker.blocking_type} at {blocker.distance:.2f}mm)"
                        )
                    reason = f"PADS_OFF_GRID: {', '.join(off_grid_pads)}"
                    if blocker_details:
                        reason += f" | Clearance blocked by: {'; '.join(blocker_details)}"

                    # Add suggestion for minimum clearance
                    min_clearance = min(b.suggested_clearance for b in pad_blockers)
                    if min_clearance < self.grid.rules.trace_clearance:
                        analysis.suggestions.insert(
                            0,
                            f"Reduce clearance to {min_clearance:.2f}mm to allow pad access",
                        )
                else:
                    reason = f"PADS_OFF_GRID: {', '.join(off_grid_pads)}"
            elif failure_cause == FailureCause.BLOCKED_PATH:
                if blocking_components:
                    reason = (
                        f"BLOCKED_BY_COMPONENT: Path blocked by {', '.join(blocking_components)}"
                    )
                else:
                    reason = "BLOCKED_BY_COMPONENT: Path blocked by component keepout"
            elif failure_cause == FailureCause.CONGESTION:
                reason = f"CONGESTION: Routing channel saturated (score: {analysis.congestion_score:.0%})"
            elif failure_cause == FailureCause.CLEARANCE:
                reason = (
                    f"CLEARANCE_VIOLATION: Cannot meet clearance requirements "
                    f"(margin: {analysis.clearance_margin:.2f}mm)"
                )
            elif failure_cause == FailureCause.KEEPOUT:
                reason = "KEEPOUT: Path crosses keepout zone"
            elif failure_cause == FailureCause.LAYER_CONFLICT:
                reason = "LAYER_CONSTRAINT: Requires layer not available"
            else:
                # Fallback to basic blocking info
                if blocking_nets:
                    if len(blocking_nets) == 1:
                        reason = f"Blocked by {blocking_components[0] if blocking_components else 'unknown'}"
                    else:
                        reason = f"Blocked by {len(blocking_nets)} nets"
                else:
                    reason = "No path found (congestion or obstacles)"

            failure = RoutingFailure(
                net=net,
                net_name=net_name,
                source_pad=(source_pad.ref, source_pad.pin),
                target_pad=(target_pad.ref, target_pad.pin),
                source_coords=source_coords,
                target_coords=target_coords,
                blocking_nets=blocking_nets,
                blocking_components=blocking_components,
                reason=reason,
                failure_cause=failure_cause,
                analysis=analysis,
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

    def get_decision_store(self) -> DecisionStore | None:
        """Get the decision store for this autorouter.

        Returns:
            DecisionStore if record_decisions was enabled, None otherwise.
        """
        return self._decision_store

    def save_decisions(self, path: Path) -> bool:
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

    def _get_net_bounding_box_diagonal(self, net_id: int) -> float:
        """Calculate the bounding box diagonal distance for a net's pads.

        This provides a fast approximation of net complexity/length.
        Shorter nets (smaller bounding boxes) are simpler to route and
        should generally be routed first to leave more routing freedom
        for longer, more complex nets.

        Args:
            net_id: The net ID to calculate distance for.

        Returns:
            Bounding box diagonal in mm, or 0.0 if net has fewer than 2 pads.
        """
        pad_keys = self.nets.get(net_id, [])
        if len(pad_keys) < 2:
            return 0.0

        # Get all pad coordinates
        coords = []
        for key in pad_keys:
            pad = self.pads.get(key)
            if pad:
                coords.append((pad.x, pad.y))

        if len(coords) < 2:
            return 0.0

        # Calculate bounding box
        min_x = min(c[0] for c in coords)
        max_x = max(c[0] for c in coords)
        min_y = min(c[1] for c in coords)
        max_y = max(c[1] for c in coords)

        # Return diagonal distance
        return math.sqrt((max_x - min_x) ** 2 + (max_y - min_y) ** 2)

    @property
    def component_pitches(self) -> dict[str, float]:
        """Get component pin pitches for constraint-aware net ordering.

        Issue #1020: Computed lazily on first access and cached.
        Used for identifying fine-pitch components that need priority routing.

        Returns:
            Dict mapping component reference to minimum pin pitch in mm.
        """
        if self._component_pitches is None:
            self._component_pitches = self.grid.compute_component_pitches()
        return self._component_pitches

    def _calculate_constraint_score(self, net_id: int) -> float:
        """Calculate a constraint score for a net based on routing difficulty.

        Issue #1020: Nets connecting to fine-pitch components or with many pads
        are more constrained and should be routed first. Higher score = more constrained.

        The score is computed as:
        - Fine-pitch component connections: 10.0 / pitch for each pad on a fine-pitch IC
        - Pad count penalty: 0.5 per pad (more pads = more routing constraints)

        Args:
            net_id: The net ID to calculate constraint score for.

        Returns:
            Constraint score (higher = more constrained, should route first).
        """
        if not self.rules.constraint_ordering_enabled:
            return 0.0

        score = 0.0
        pad_keys = self.nets.get(net_id, [])

        fine_pitch_weight = self.rules.constraint_fine_pitch_weight
        pad_count_weight = self.rules.constraint_pad_count_weight
        fine_pitch_threshold = self.rules.fine_pitch_threshold

        # Fine-pitch component connections
        pitches = self.component_pitches
        for pad_key in pad_keys:
            ref = pad_key[0]  # Component reference
            if ref in pitches:
                pitch = pitches[ref]
                if pitch < fine_pitch_threshold:
                    # Smaller pitch = higher score (more constrained)
                    score += fine_pitch_weight / pitch

        # More pads = more routing constraints
        score += len(pad_keys) * pad_count_weight

        return score

    def _get_net_priority(self, net_id: int) -> tuple[int, float, int, float]:
        """Get routing priority for a net (lower = higher priority).

        Returns a 4-tuple used for sorting:
        1. Net class priority (1-10, where 1 = highest priority like POWER)
        2. Negative constraint score (higher constraint = route first, so negate)
        3. Pad count (fewer pads = higher priority, simpler nets first)
        4. Bounding box diagonal (shorter nets first, leaves room for longer nets)

        Issue #1020: Adds constraint-aware ordering. Nets connecting to fine-pitch
        ICs are routed before unconstrained nets within the same priority class.
        This improves routing success by giving highly-constrained nets first access
        to limited routing resources (escape channels, narrow clearances).

        This ordering strategy routes critical signal classes first (power, clock),
        then within each class routes highly-constrained nets (fine-pitch IC connections)
        before simpler/shorter nets, giving constrained nets the best routing freedom.
        """
        net_name = self.net_names.get(net_id, "")
        net_class = self.net_class_map.get(net_name)
        priority = net_class.priority if net_class else 10
        pad_count = len(self.nets.get(net_id, []))
        distance = self._get_net_bounding_box_diagonal(net_id)

        # Issue #1020: Constraint score (negated so higher constraint = lower tuple value)
        constraint_score = self._calculate_constraint_score(net_id)

        return (priority, -constraint_score, pad_count, distance)

    def _compute_mst_edges(self, net_id: int) -> list[MSTEdgeInfo]:
        """Compute MST edges for a net and return them sorted by distance.

        Pre-computes the minimum spanning tree for N-port nets so that
        edge distances can be used for interleaved net ordering.

        Args:
            net_id: The net ID to compute MST for.

        Returns:
            List of MSTEdgeInfo objects sorted by distance (shortest first).
            Empty list for 2-port nets (they have exactly one edge).
        """
        pad_keys = self.nets.get(net_id, [])
        if len(pad_keys) <= 2:
            return []

        # Get pad objects
        pad_objs = [self.pads[key] for key in pad_keys if key in self.pads]
        if len(pad_objs) <= 2:
            return []

        # Build MST using Prim's algorithm (same as MSTRouter.build_mst)
        n = len(pad_objs)
        connected: set[int] = {0}
        unconnected = set(range(1, n))
        mst_edges: list[tuple[int, int, float]] = []

        while unconnected:
            best_dist = float("inf")
            best_edge: tuple[int, int] | None = None

            for i in connected:
                for j in unconnected:
                    # Manhattan distance
                    dist = abs(pad_objs[i].x - pad_objs[j].x) + abs(pad_objs[i].y - pad_objs[j].y)
                    if dist < best_dist:
                        best_dist = dist
                        best_edge = (i, j)

            if best_edge:
                i, j = best_edge
                mst_edges.append((i, j, best_dist))
                connected.add(j)
                unconnected.remove(j)

        # Sort by distance and convert to MSTEdgeInfo
        mst_edges.sort(key=lambda e: e[2])
        return [
            MSTEdgeInfo(
                net_id=net_id,
                edge_index=idx,
                source_idx=edge[0],
                target_idx=edge[1],
                distance=edge[2],
                is_first=(idx == 0),
            )
            for idx, edge in enumerate(mst_edges)
        ]

    def _get_shortest_mst_edge_distance(self, net_id: int) -> float:
        """Get the distance of the shortest MST edge for an N-port net.

        For interleaved ordering, we treat the shortest MST edge as a
        "virtual 2-port net" and use its distance for comparison with
        actual 2-port nets.

        Args:
            net_id: The net ID to get shortest edge for.

        Returns:
            Distance of shortest MST edge in mm, or 0.0 if not an N-port net.
        """
        edges = self._compute_mst_edges(net_id)
        if edges:
            return edges[0].distance
        return 0.0

    def _get_interleaved_net_order(
        self, use_interleaving: bool = True
    ) -> tuple[list[int], dict[int, list[MSTEdgeInfo]]]:
        """Get net ordering with 2-port and N-port nets interleaved by distance.

        Creates an ordering where the shortest edge of N-port nets is treated
        as a "virtual 2-port net" and interleaved with actual 2-port nets.
        This allows short segments of N-port nets to be routed early when
        they have fewer obstacles.

        Args:
            use_interleaving: If True, use interleaved ordering. If False,
                fall back to standard priority ordering (pad_count based).

        Returns:
            Tuple of:
            - List of net IDs in routing order
            - Dict mapping net_id to list of MSTEdgeInfo for N-port nets
              (used for two-phase routing)
        """
        # Group nets by class priority first
        priority_groups: dict[int, list[int]] = {}
        for net_id in self.nets.keys():
            if net_id == 0:  # Skip unconnected nets
                continue
            net_name = self.net_names.get(net_id, "")
            net_class = self.net_class_map.get(net_name)
            priority = net_class.priority if net_class else 10
            if priority not in priority_groups:
                priority_groups[priority] = []
            priority_groups[priority].append(net_id)

        if not use_interleaving:
            # Fall back to standard ordering
            ordered_nets = []
            for priority in sorted(priority_groups.keys()):
                nets = priority_groups[priority]
                nets.sort(key=lambda n: self._get_net_priority(n))
                ordered_nets.extend(nets)
            return ordered_nets, {}

        # Pre-compute MST edges for all N-port nets
        mst_cache: dict[int, list[MSTEdgeInfo]] = {}
        for net_id in self.nets.keys():
            if net_id == 0:
                continue
            pad_count = len(self.nets.get(net_id, []))
            if pad_count > 2:
                edges = self._compute_mst_edges(net_id)
                if edges:
                    mst_cache[net_id] = edges

        # Build interleaved ordering within each priority group
        ordered_nets = []

        for priority in sorted(priority_groups.keys()):
            nets_in_group = priority_groups[priority]

            # Separate 2-port and N-port nets
            two_port_nets: list[tuple[float, int]] = []  # (distance, net_id)
            nport_first_edges: list[tuple[float, int]] = []  # (distance, net_id)

            for net_id in nets_in_group:
                pad_count = len(self.nets.get(net_id, []))
                if pad_count == 2:
                    # 2-port net: use direct distance
                    distance = self._get_net_bounding_box_diagonal(net_id)
                    two_port_nets.append((distance, net_id))
                elif net_id in mst_cache:
                    # N-port net: use shortest MST edge distance
                    shortest_edge = mst_cache[net_id][0].distance
                    nport_first_edges.append((shortest_edge, net_id))

            # Combine and sort by distance
            combined: list[tuple[float, int]] = two_port_nets + nport_first_edges
            combined.sort(key=lambda x: x[0])

            # Add to ordered list
            for _, net_id in combined:
                ordered_nets.append(net_id)

        return ordered_nets, mst_cache

    def route_all(
        self,
        net_order: list[int] | None = None,
        progress_callback: ProgressCallback | None = None,
        parallel: bool = False,
        max_workers: int = 4,
        interleaved: bool = False,
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
            interleaved: If True, use interleaved ordering for N-port nets.
                The shortest MST edge of each N-port net is treated as a
                "virtual 2-port net" and interleaved with actual 2-port nets
                sorted by distance. This gives short segments the best chance
                of routing before longer routes consume grid space.

        Returns:
            List of Route objects for all nets
        """
        if interleaved:
            return self.route_all_interleaved(progress_callback=progress_callback)

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
                flush_print(
                    f"  Net {net}: {len(routes)} routes, "
                    f"{sum(len(r.segments) for r in routes)} segments, "
                    f"{sum(len(r.vias) for r in routes)} vias"
                )

        if progress_callback is not None:
            routed_count = len({r.net for r in all_routes})
            progress_callback(1.0, f"Routed {routed_count}/{total_nets} nets", False)

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        return all_routes

    def route_all_interleaved(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route all nets using interleaved ordering for N-port nets.

        This routing strategy treats the shortest MST edge of N-port nets
        as a "virtual 2-port net" and interleaves it with actual 2-port nets
        sorted by distance. This gives short segments of N-port nets the
        best chance of routing successfully before longer routes consume
        grid space.

        The algorithm:
        1. Pre-compute MST for all N-port nets
        2. Extract shortest edge from each N-port net's MST
        3. Create combined pool: 2-port nets + shortest N-port edges
        4. Sort by edge length and route in that order
        5. After each N-port's first edge routes, continue with remaining edges

        Args:
            progress_callback: Optional callback for progress updates

        Returns:
            List of Route objects for all nets

        Example:
            >>> router = Autorouter(100, 100)
            >>> # ... add components ...
            >>> routes = router.route_all_interleaved()
            >>> # Net B's 3mm edge routes before Net A's 5mm edge
        """
        print("\n=== Interleaved Net Routing ===")

        # Get interleaved ordering and MST cache
        net_order, mst_cache = self._get_interleaved_net_order(use_interleaving=True)
        nets_to_route = [n for n in net_order if n != 0]
        total_nets = len(nets_to_route)

        print(f"  Total nets: {total_nets}")
        print(f"  N-port nets with cached MST: {len(mst_cache)}")

        all_routes: list[Route] = []
        nport_routed_edges: dict[int, int] = {}  # net_id -> number of edges routed

        for i, net in enumerate(nets_to_route):
            if progress_callback is not None:
                progress = i / total_nets if total_nets > 0 else 0.0
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            pad_count = len(self.nets.get(net, []))

            if pad_count == 2 or net not in mst_cache:
                # 2-port net or N-port without MST: route normally
                routes = self.route_net(net)
                all_routes.extend(routes)
                if routes:
                    flush_print(
                        f"  Net {net}: {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments, "
                        f"{sum(len(r.vias) for r in routes)} vias"
                    )
            else:
                # N-port net: route using cached MST edges in order
                mst_edges = mst_cache[net]
                routes = self._route_net_with_mst_edges(net, mst_edges)
                all_routes.extend(routes)
                nport_routed_edges[net] = len(mst_edges)
                if routes:
                    flush_print(
                        f"  Net {net} (N-port): {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments, "
                        f"{sum(len(r.vias) for r in routes)} vias"
                    )

        if progress_callback is not None:
            routed_count = len({r.net for r in all_routes})
            progress_callback(1.0, f"Routed {routed_count}/{total_nets} nets", False)

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        return all_routes

    def _route_net_with_mst_edges(self, net: int, mst_edges: list[MSTEdgeInfo]) -> list[Route]:
        """Route an N-port net using pre-computed MST edges.

        Routes the MST edges in order (shortest first), using the cached
        edge information to avoid recomputing the MST.

        Args:
            net: Net ID to route
            mst_edges: Pre-computed MST edges sorted by distance

        Returns:
            List of Route objects for this net
        """
        if net not in self.nets:
            return []

        pads = self.nets[net]
        if len(pads) < 2:
            return []

        routes: list[Route] = []

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

        # Route MST edges in order (shortest first)
        # The mst_edges contain indices into the original pad list
        # We need to map them to pads_for_routing if intra-IC reduced the list
        if len(pads_for_routing) == len(pads):
            # No intra-IC reduction, use edges directly
            for edge in mst_edges:
                if edge.source_idx < len(pad_objs) and edge.target_idx < len(pad_objs):
                    source_pad = pad_objs[edge.source_idx]
                    target_pad = pad_objs[edge.target_idx]
                    route = self.router.route(source_pad, target_pad)

                    if route:
                        self._mark_route(route)
                        self.routes.append(route)
                        routes.append(route)
                    else:
                        self._record_routing_failure(net, source_pad, target_pad)
        else:
            # Intra-IC reduced the pad list, rebuild MST for reduced set
            mst_router = MSTRouter(self.grid, self.router, self.rules, self.net_class_map)

            def mark_route(route: Route):
                self._mark_route(route)
                self.routes.append(route)

            def record_failure(source_pad: Pad, target_pad: Pad):
                self._record_routing_failure(net, source_pad, target_pad)

            mst_routes = mst_router.route_net(pad_objs, mark_route, record_failure)
            routes.extend(mst_routes)

        return routes

    def _record_routing_failure(self, net: int, source_pad: Pad, target_pad: Pad):
        """Record a routing failure with diagnostic information.

        Helper method to record failures when routing with pre-computed MST edges.

        Args:
            net: Net ID
            source_pad: Source pad object
            target_pad: Target pad object
        """
        source_coords = (source_pad.x, source_pad.y)
        target_coords = (target_pad.x, target_pad.y)

        # Find blocking nets
        blocking_nets = self.router.find_blocking_nets(source_pad, target_pad)

        # Map blocking nets to component references
        blocking_components = []
        for blocking_net in blocking_nets:
            for (ref, _pin), pad in self.pads.items():
                if pad.net == blocking_net and ref not in blocking_components:
                    blocking_components.append(ref)
                    break

        # Run root cause analysis
        analyzer = RootCauseAnalyzer()
        net_name = self.net_names.get(net, f"Net_{net}")
        analysis = analyzer.analyze_routing_failure(
            grid=self.grid,
            start=source_coords,
            goal=target_coords,
            net_id=net,
            net_name=net_name,
        )

        failure = RoutingFailure(
            net=net,
            net_name=net_name,
            source_pad=(source_pad.ref, source_pad.pin),
            target_pad=(target_pad.ref, target_pad.pin),
            source_coords=source_coords,
            target_coords=target_coords,
            blocking_nets=set(blocking_nets),
            blocking_components=blocking_components,
            reason=analysis.primary_cause if analysis else "No path found",
            failure_cause=analysis.failure_cause if analysis else FailureCause.UNKNOWN,
            analysis=analysis,
        )
        self.routing_failures.append(failure)

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
        region_parallel: bool = False,
        partition_rows: int = 2,
        partition_cols: int = 2,
        max_parallel_workers: int = 4,
        batch_routing: bool = False,
        hierarchical: bool = False,
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
            region_parallel: If True, use region-based parallelism (Issue #965).
                Partitions the grid into regions and routes non-adjacent regions
                in parallel during each iteration, providing 2-3x speedup.
            partition_rows: Number of region rows for partitioning (default 2)
            partition_cols: Number of region columns for partitioning (default 2)
            max_parallel_workers: Maximum parallel workers per region group (default 4)
            batch_routing: If True, use GPU-accelerated batch routing (Issue #1092).
                Routes multiple independent nets simultaneously using GPU compute.
                Best results with 4+ independent nets and Metal/CUDA GPU.
            hierarchical: If True, use hierarchical coarse-to-fine routing (Issue #1092).
                First performs global routing on a coarse grid, then refines with
                the fine grid near pads and congestion points.

        Returns:
            List of routes (may be partial if timeout reached)
        """
        import time

        start_time = time.time()

        # If hierarchical mode is requested, delegate to two-phase routing
        if hierarchical:
            return self.route_all_two_phase(
                use_negotiated=True,
                corridor_width_factor=2.0,
                corridor_penalty=5.0,
                progress_callback=progress_callback,
                timeout=timeout,
            )

        flush_print("\n=== Negotiated Congestion Routing ===")
        flush_print(f"  Max iterations: {max_iterations}")
        if adaptive:
            print("  Mode: Adaptive (Issue #633)")
            print(f"  Present factor: {initial_present_factor} (adaptive)")
            print(f"  History increment: {history_increment} (adaptive)")
        else:
            print(f"  Present factor: {initial_present_factor} + {present_factor_increment}/iter")
        if use_targeted_ripup:
            print(f"  Targeted rip-up: enabled (max {max_ripups_per_net} ripups/net)")
        if region_parallel:
            print(
                f"  Region parallel: enabled ({partition_rows}x{partition_cols} regions, "
                f"{max_parallel_workers} workers)"
            )
        if batch_routing:
            print("  Batch routing: enabled (GPU-accelerated)")
        if timeout:
            print(f"  Timeout: {timeout}s")

        # Track overflow history for adaptive mode (Issue #633)
        overflow_history: list[int] = []
        escape_strategy_index = 0

        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
        total_nets = len(net_order)

        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)

        # Initialize region-based parallel router if enabled (Issue #965)
        region_router: RegionBasedNegotiatedRouter | None = None
        if region_parallel:
            # Enable thread safety on grid for parallel operations
            if not self.grid.thread_safe:
                # Recreate grid with thread safety enabled
                from .grid import RoutingGrid

                old_grid = self.grid
                self.grid = RoutingGrid(
                    width=old_grid.width,
                    height=old_grid.height,
                    rules=old_grid.rules,
                    origin_x=old_grid.origin_x,
                    origin_y=old_grid.origin_y,
                    layer_stack=old_grid.layer_stack,
                    expanded_obstacles=old_grid.expanded_obstacles,
                    resolution_override=old_grid.resolution,
                    thread_safe=True,
                )
                # Copy blocked cells and obstacles from old grid
                self.grid._blocked = old_grid._blocked.copy()
                self.grid._net = old_grid._net.copy()
                self.grid._is_obstacle = old_grid._is_obstacle.copy()
                self.grid._is_zone = old_grid._is_zone.copy()
                self.grid._pad_blocked = old_grid._pad_blocked.copy()
                self.grid._original_net = old_grid._original_net.copy()
                self.grid._pads = old_grid._pads.copy()
                # Update router to use new grid
                self.router.grid = self.grid
                neg_router = NegotiatedRouter(
                    self.grid, self.router, self.rules, self.net_class_map
                )

            region_router = RegionBasedNegotiatedRouter(
                router=self,
                partition_rows=partition_rows,
                partition_cols=partition_cols,
                max_workers=max_parallel_workers,
            )
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

        flush_print("\n--- Iteration 0: Initial routing with sharing ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Initial routing pass", True):
                return list(self.routes)

        # Use region-based parallelism for initial pass if enabled (Issue #965)
        if region_router is not None:

            def route_fn_init(net: int, pf: float) -> list[Route]:
                return self._route_net_negotiated(net, pf)

            def mark_fn_init(route: Route) -> None:
                self.grid.mark_route_usage(route)
                self.routes.append(route)

            result = region_router.route_iteration_parallel(
                nets_to_route=net_order,
                present_factor=present_factor,
                route_fn=route_fn_init,
                mark_route_fn=mark_fn_init,
            )

            # Update net_routes with results
            for net in result.successful_nets:
                net_routes[net] = [r for r in result.routes if r.net == net]

        else:
            # Sequential routing (original behavior)
            for i, net in enumerate(net_order):
                if check_timeout():
                    print(f"  ⚠ Timeout reached at net {i}/{total_nets} ({elapsed_str()})")
                    timed_out = True
                    break

                # Progress output for every net with percentage
                net_name = self.net_names.get(net, f"Net {net}")
                pct = (i / total_nets * 100) if total_nets > 0 else 0
                flush_print(
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
        flush_print(
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

                flush_print(f"\n--- Iteration {iteration}: Rip-up and reroute ---")

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
                    flush_print(
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
                    flush_print(
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
                    flush_print(
                        f"  Ripping up {len(nets_to_reroute)} nets with conflicts ({elapsed_str()})"
                    )

                    neg_router.rip_up_nets(nets_to_reroute, net_routes, self.routes)

                    # Use region-based parallelism if enabled (Issue #965)
                    if region_router is not None and len(nets_to_reroute) > 1:
                        # Route using region-based parallelism
                        def route_fn(net: int, pf: float) -> list[Route]:
                            return self._route_net_negotiated(net, pf)

                        def mark_fn(route: Route) -> None:
                            self.grid.mark_route_usage(route)
                            self.routes.append(route)

                        result = region_router.route_iteration_parallel(
                            nets_to_route=nets_to_reroute,
                            present_factor=present_factor,
                            route_fn=route_fn,
                            mark_route_fn=mark_fn,
                        )

                        # Update net_routes with results
                        for net in result.successful_nets:
                            # Find the routes for this net from result
                            net_routes[net] = [r for r in result.routes if r.net == net]

                        rerouted_count = len(result.successful_nets)
                    else:
                        # Sequential routing (original behavior)
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
                    flush_print(
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

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

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

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

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

    # =========================================================================
    # HIERARCHICAL ROUTING (Issue #1095 - Phase A)
    # =========================================================================

    def route_all_hierarchical(
        self,
        num_cols: int = 10,
        num_rows: int = 10,
        corridor_width_factor: float = 2.0,
        use_negotiated: bool = True,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets using hierarchical global-to-detailed flow.

        This strategy uses a RegionGraph to plan coarse routing corridors
        for each net before performing detailed routing. The flow is:

        1. Build a RegionGraph partitioning the board into regions
        2. Use GlobalRouter to assign each net a corridor (sequence of regions)
        3. Convert corridors to grid-level preferences
        4. Run detailed routing (standard or negotiated) with corridor guidance
        5. Fallback: nets that fail global routing are routed without corridors

        This approach provides better resource allocation than direct routing
        because nets are guided into non-overlapping channels, reducing
        contention during detailed routing.

        Args:
            num_cols: Number of region columns for the RegionGraph (default: 10)
            num_rows: Number of region rows for the RegionGraph (default: 10)
            corridor_width_factor: Corridor width as multiple of clearance
                (default: 2.0). The actual corridor half-width is
                corridor_width_factor * trace_clearance.
            use_negotiated: Use negotiated congestion routing in detailed
                phase (default: True)
            progress_callback: Optional callback for progress updates.
                Signature: callback(progress: float, message: str, active: bool) -> bool
            timeout: Optional timeout in seconds for the entire operation

        Returns:
            List of Route objects (may be partial if timeout reached)
        """
        import time

        start_time = time.time()

        flush_print("\n=== Hierarchical Routing (Global + Detailed) ===")

        # Get nets to route in priority order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
        total_nets = len(net_order)

        if total_nets == 0:
            flush_print("  No nets to route")
            return []

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        # =================================================================
        # Phase 1: Build RegionGraph and run GlobalRouter
        # =================================================================
        flush_print("\n--- Phase 1: Global Routing via RegionGraph ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Phase 1: Building region graph", True):
                return list(self.routes)

        corridor_width = corridor_width_factor * self.rules.trace_clearance

        # Build region graph
        region_graph = RegionGraph(
            board_width=self.grid.width,
            board_height=self.grid.height,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_cols=num_cols,
            num_rows=num_rows,
        )

        # Register obstacles (pads reduce region capacity)
        all_pads = list(self.pads.values())
        region_graph.register_obstacles(all_pads)

        rg_stats = region_graph.get_statistics()
        flush_print(
            f"  Region graph: {rg_stats['num_regions']} regions "
            f"({rg_stats['num_rows']}x{rg_stats['num_cols']}), "
            f"{rg_stats['num_edges']} edges, "
            f"{rg_stats['regions_with_obstacles']} regions with obstacles"
        )

        # Run global router
        global_router = GlobalRouter(
            region_graph=region_graph,
            corridor_width=corridor_width,
            default_layer=0,
        )

        if progress_callback is not None:
            if not progress_callback(0.05, "Phase 1: Assigning corridors", True):
                return list(self.routes)

        global_result = global_router.route_all(
            nets=self.nets,
            pad_dict=self.pads,
            net_order=net_order,
        )

        flush_print(
            f"  Global routing: {len(global_result.assignments)}/{total_nets} "
            f"nets assigned corridors ({elapsed_str()})"
        )
        if global_result.failed_nets:
            flush_print(
                f"  Warning: {len(global_result.failed_nets)} nets failed "
                f"global routing (will attempt without corridors)"
            )

        # =================================================================
        # Phase 2: Detailed Routing with Corridor Guidance
        # =================================================================
        flush_print("\n--- Phase 2: Detailed Routing with Corridors ---")
        if progress_callback is not None:
            if not progress_callback(0.2, "Phase 2: Detailed routing", True):
                return list(self.routes)

        # Set corridor preferences on the grid for nets with assignments
        corridor_penalty = 5.0
        for net, assignment in global_result.assignments.items():
            self.grid.set_corridor_preference(
                assignment.corridor, net, corridor_penalty
            )

        # Route all nets (corridor-assigned nets get guidance, others route freely)
        if use_negotiated:
            detailed_routes = self._route_hierarchical_detailed_negotiated(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )
        else:
            detailed_routes = self._route_hierarchical_detailed_standard(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )

        # Clear corridor preferences
        self.grid.clear_all_corridor_preferences()

        # Summary
        successful_nets = len({r.net for r in detailed_routes})
        total_elapsed = time.time() - start_time
        flush_print("\n=== Hierarchical Routing Complete ===")
        flush_print(f"  Total nets: {total_nets}")
        flush_print(
            f"  Global routing: {len(global_result.assignments)} corridors assigned"
        )
        flush_print(f"  Detailed routing: {successful_nets} nets routed")
        flush_print(f"  Total time: {total_elapsed:.1f}s")

        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        if progress_callback is not None:
            progress_callback(
                1.0,
                f"Complete: {successful_nets}/{total_nets} nets in {total_elapsed:.1f}s",
                False,
            )

        return detailed_routes

    def _route_hierarchical_detailed_negotiated(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed phase of hierarchical routing using negotiated congestion.

        Uses the same negotiated routing infrastructure as two-phase routing
        but with corridor preferences set by the hierarchical global router.
        """
        import time

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        total_nets = len(net_order)

        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = 0.5

        # Initial routing pass
        for i, net in enumerate(net_order):
            if check_timeout():
                flush_print(
                    f"  Timeout during detailed routing at net {i}/{total_nets}"
                )
                break

            if progress_callback is not None:
                progress = 0.2 + 0.6 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self._route_net_with_corridor(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        flush_print(
            f"  Initial pass: {len(net_routes)}/{total_nets} nets, overflow: {overflow}"
        )

        # Rip-up and reroute if overflow remains
        if overflow > 0:
            max_iterations = 10
            history_increment = 1.0
            present_factor_increment = 0.5

            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    flush_print(f"  Timeout at iteration {iteration}")
                    break

                if progress_callback is not None:
                    progress = 0.8 + 0.15 * (iteration / max_iterations)
                    if not progress_callback(
                        progress, f"Iteration {iteration}/{max_iterations}", True
                    ):
                        break

                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                overused = self.grid.find_overused_cells()
                nets_to_reroute = neg_router.find_nets_through_overused_cells(
                    net_routes, overused
                )
                flush_print(
                    f"  Iteration {iteration}: ripping up {len(nets_to_reroute)} nets"
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

                new_overflow = self.grid.get_total_overflow()
                if new_overflow == 0:
                    flush_print(f"  Overflow resolved at iteration {iteration}")
                    break
                overflow = new_overflow

        # Collect all routes
        all_routes: list[Route] = []
        for routes in net_routes.values():
            all_routes.extend(routes)

        return all_routes

    def _route_hierarchical_detailed_standard(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed phase of hierarchical routing using standard sequential routing.

        Routes each net sequentially with corridor guidance. This is simpler
        than negotiated routing and works well when the global routing provides
        good corridor assignments.
        """
        import time

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        total_nets = len(net_order)
        all_routes: list[Route] = []

        for i, net in enumerate(net_order):
            if check_timeout():
                flush_print(f"  Timeout at net {i}/{total_nets}")
                break

            if progress_callback is not None:
                progress = 0.2 + 0.7 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self.route_net(net)
            all_routes.extend(routes)

        return all_routes

    def _reset_for_new_trial(self):
        """Reset the router to initial state for a new trial."""
        width, height = self.grid.width, self.grid.height
        origin_x, origin_y = self.grid.origin_x, self.grid.origin_y

        # Recreate grid and routers using shared helper
        # Issue #972: Helper includes adaptive grid resolution for large boards
        self.grid, self.router, self.zone_manager = self._create_grid_and_routers(
            width, height, origin_x, origin_y
        )

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
        use_hierarchical: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Unified entry point for advanced routing strategies.

        Args:
            monte_carlo_trials: Number of Monte Carlo trials (0 = disabled)
            use_negotiated: Use negotiated congestion routing
            use_two_phase: Use two-phase global+detailed routing
            use_hierarchical: Use hierarchical global-to-detailed routing
                (Issue #1095). This builds a RegionGraph for coarse-grid
                corridor assignment before detailed routing.
            progress_callback: Optional callback for progress updates

        Returns:
            List of routes

        Note:
            Priority order: monte_carlo > hierarchical > two_phase > negotiated > standard
        """
        if monte_carlo_trials > 0:
            return self.route_all_monte_carlo(
                monte_carlo_trials, use_negotiated, progress_callback=progress_callback
            )
        elif use_hierarchical:
            return self.route_all_hierarchical(
                use_negotiated=use_negotiated, progress_callback=progress_callback
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

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        return all_routes

    # =========================================================================
    # SUB-GRID ROUTING FOR FINE-PITCH COMPONENTS (Issue #1109)
    # =========================================================================

    @property
    def _subgrid(self) -> SubGridRouter:
        """Lazy-initialize sub-grid router."""
        if self._subgrid_router is None:
            self._subgrid_router = SubGridRouter(self.grid, self.rules)
        return self._subgrid_router

    def prepare_subgrid_escapes(self) -> SubGridResult:
        """Prepare sub-grid escape segments for fine-pitch components.

        Analyzes all pads and generates escape segments for those that
        don't align with the main routing grid. This should be called
        before main routing to ensure the router can reach off-grid pads.

        Returns:
            SubGridResult with escape segments and statistics

        Example::

            # Prepare escapes before routing
            subgrid_result = router.prepare_subgrid_escapes()
            print(subgrid_result.format_summary())

            # Then route normally
            routes = router.route_all()
        """
        pad_list = list(self.pads.values())
        return self._subgrid.route_with_subgrid(pad_list)

    def route_with_subgrid(
        self,
        use_negotiated: bool = True,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route with automatic sub-grid escape routing for fine-pitch components.

        First generates sub-grid escape segments for any detected off-grid pads
        (fine-pitch ICs with 0.5-0.65mm pitch), then routes remaining connections
        using the standard algorithm. This is the recommended approach for boards
        with TSSOP, SSOP, QFN, or other fine-pitch components.

        Issue #1109: Sub-grid routing enables routing to pads that fall between
        main grid points without requiring a global fine grid (which would be
        computationally intractable for large boards).

        Args:
            use_negotiated: Use negotiated congestion routing (default True)
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of all routes (sub-grid escapes + regular routing)

        Example::

            # Route with automatic sub-grid handling
            routes = router.route_with_subgrid()

            # Check statistics
            stats = router.get_statistics()
            print(f"Routed {stats['nets_routed']} nets")
        """
        print("\n=== Routing with Sub-Grid Escape (Fine-Pitch Support) ===")

        # Phase 1: Sub-grid escape routing for off-grid pads
        print("\n--- Phase 1: Sub-Grid Escape Routing ---")
        subgrid_result = self.prepare_subgrid_escapes()

        if subgrid_result.analysis and subgrid_result.analysis.has_off_grid_pads:
            print(f"  Off-grid pads: {subgrid_result.analysis.off_grid_count}")
            print(f"  Escape segments: {subgrid_result.success_count}")
            print(f"  Grid cells unblocked: {subgrid_result.unblocked_count}")

            if subgrid_result.failed_pads:
                print(
                    f"  Failed escapes: {len(subgrid_result.failed_pads)} "
                    f"(components: {', '.join(sorted({p.ref for p in subgrid_result.failed_pads}))})"
                )

            # Collect escape routes
            escape_routes = self._subgrid.get_escape_routes(subgrid_result)
            for route in escape_routes:
                self.routes.append(route)
        else:
            print("  No off-grid pads detected, sub-grid routing not needed")
            escape_routes = []

        # Phase 2: Main routing
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
        print("\n=== Routing with Sub-Grid Complete ===")
        if subgrid_result.analysis:
            print(f"  Fine-pitch pads escaped: {subgrid_result.success_count}")
        print(f"  Total nets routed: {stats['nets_routed']}")
        print(f"  Total segments: {stats['segments']}")
        print(f"  Total vias: {stats['vias']}")

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        return all_routes

    def get_subgrid_statistics(self) -> dict:
        """Get statistics about sub-grid routing.

        Returns:
            Dictionary with sub-grid routing stats:
            - off_grid_pads: Number of off-grid pads detected
            - escaped_pads: Number of pads with escape segments
            - failed_pads: Number of pads where escape failed
            - unblocked_cells: Grid cells unblocked for routing
        """
        if self._subgrid_router is None:
            return {
                "off_grid_pads": 0,
                "escaped_pads": 0,
                "failed_pads": 0,
                "unblocked_cells": 0,
            }

        pad_list = list(self.pads.values())
        analysis = self._subgrid.analyze_pads(pad_list)
        return {
            "off_grid_pads": analysis.off_grid_count,
            "on_grid_pads": len(analysis.on_grid_pads),
            "total_pads": analysis.total_pads,
            "off_grid_percentage": analysis.off_grid_percentage,
        }

    # =========================================================================
    # PROGRESSIVE CLEARANCE RELAXATION
    # =========================================================================

    def route_with_progressive_clearance(
        self,
        min_clearance: float | None = None,
        num_relaxation_levels: int = 3,
        max_iterations: int = 15,
        timeout: float | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[list[Route], dict[int, float]]:
        """Route all nets with progressive clearance relaxation for failed nets.

        This method first routes all nets with standard clearance, then identifies
        nets that failed due to clearance violations and retries them with
        progressively relaxed clearance settings.

        Unlike --adaptive-rules which globally relaxes all rules and reroutes
        everything, this method only relaxes clearance for specific failed nets,
        preserving the original clearance for successfully routed nets.

        Args:
            min_clearance: Minimum clearance floor (default: 50% of original clearance)
            num_relaxation_levels: Number of relaxation steps to try (default: 3)
            max_iterations: Max iterations for negotiated routing (default: 15)
            timeout: Optional timeout in seconds
            progress_callback: Optional callback for progress updates

        Returns:
            Tuple of:
            - List of all routes (successfully routed nets)
            - Dict mapping net IDs to the clearance level used (for reporting)

        Example:
            >>> router = Autorouter(...)
            >>> routes, relaxed_nets = router.route_with_progressive_clearance(
            ...     min_clearance=0.08
            ... )
            >>> print(f"Nets needing relaxed clearance: {len(relaxed_nets)}")
        """
        import time

        from .cpp_backend import create_hybrid_router
        from .failure_analysis import FailureCause

        start_time = time.time()
        original_clearance = self.rules.trace_clearance

        # Calculate minimum clearance if not specified
        if min_clearance is None:
            min_clearance = original_clearance * 0.5

        # Ensure min_clearance doesn't exceed original
        min_clearance = min(min_clearance, original_clearance)

        print("\n=== Progressive Clearance Relaxation ===")
        print(f"  Original clearance: {original_clearance:.3f}mm")
        print(f"  Minimum clearance: {min_clearance:.3f}mm")
        print(f"  Relaxation levels: {num_relaxation_levels}")

        # Generate relaxation levels (linear interpolation)
        relaxation_levels = [
            original_clearance - i * (original_clearance - min_clearance) / num_relaxation_levels
            for i in range(num_relaxation_levels + 1)
        ]

        # Track which nets needed clearance relaxation
        nets_relaxed: dict[int, float] = {}

        # Pass 1: Route with standard clearance
        print(f"\n--- Pass 1: Standard clearance ({original_clearance:.3f}mm) ---")
        self.route_all_negotiated(
            max_iterations=max_iterations,
            timeout=timeout,
            progress_callback=progress_callback,
            use_targeted_ripup=True,
            adaptive=True,
        )

        # Get initial statistics
        initial_stats = self.get_statistics()
        nets_routed_initial = initial_stats["nets_routed"]

        # Identify failed nets due to clearance issues
        clearance_failed_nets: list[int] = []
        for failure in self.routing_failures:
            if failure.failure_cause == FailureCause.CLEARANCE:
                clearance_failed_nets.append(failure.net)

        if not clearance_failed_nets:
            print(f"\n  All {nets_routed_initial} nets routed successfully!")
            print("  No clearance relaxation needed.")
            return list(self.routes), nets_relaxed

        print(f"\n  {len(clearance_failed_nets)} net(s) failed due to clearance constraints")

        # Progressive relaxation passes
        for level_idx, relaxed_clearance in enumerate(relaxation_levels[1:], start=2):
            if not clearance_failed_nets:
                break

            # Check timeout
            if timeout and (time.time() - start_time) >= timeout:
                print(f"\n  Timeout reached during relaxation pass {level_idx}")
                break

            print(f"\n--- Pass {level_idx}: Relaxed clearance ({relaxed_clearance:.3f}mm) ---")
            print(f"  Retrying {len(clearance_failed_nets)} failed net(s)")

            # Create relaxed design rules
            relaxed_rules = DesignRules(
                grid_resolution=self.rules.grid_resolution,
                trace_width=self.rules.trace_width,
                trace_clearance=relaxed_clearance,
                via_drill=self.rules.via_drill,
                via_diameter=self.rules.via_diameter,
                via_clearance=relaxed_clearance,  # Also relax via clearance
            )

            # Create a relaxed router for these nets
            relaxed_router = create_hybrid_router(
                self.grid, relaxed_rules, force_python=self._force_python
            )

            # Try to route each failed net with relaxed clearance
            newly_routed: list[int] = []
            for net in clearance_failed_nets:
                if net not in self.nets:
                    continue

                pads = self.nets[net]
                if len(pads) < 2:
                    continue

                # Get pad objects
                pad_objs = [self.pads[p] for p in pads]

                # Create negotiated router with relaxed rules
                neg_router = NegotiatedRouter(
                    self.grid, relaxed_router, relaxed_rules, self.net_class_map
                )

                def mark_route(route: Route) -> None:
                    self._mark_route(route)

                # Try to route the net
                new_routes = neg_router.route_net_negotiated(
                    pad_objs, present_cost_factor=1.0, mark_route_callback=mark_route
                )

                if new_routes:
                    # Success! Record the relaxed clearance used
                    nets_relaxed[net] = relaxed_clearance
                    newly_routed.append(net)
                    self.routes.extend(new_routes)

                    # Remove from routing failures
                    self.routing_failures = [f for f in self.routing_failures if f.net != net]

                    net_name = self.net_names.get(net, f"Net {net}")
                    print(f"    ✓ {net_name} routed with {relaxed_clearance:.3f}mm clearance")

            # Update list of failed nets
            clearance_failed_nets = [n for n in clearance_failed_nets if n not in newly_routed]

            if newly_routed:
                print(f"  Routed {len(newly_routed)} net(s) at this level")

        # Final summary
        final_stats = self.get_statistics()
        total_routed = final_stats["nets_routed"]
        total_nets = len([n for n in self.nets if n > 0 and len(self.nets[n]) >= 2])

        print("\n=== Progressive Clearance Complete ===")
        print(f"  Nets routed: {total_routed}/{total_nets}")
        print(f"  Nets with standard clearance: {total_routed - len(nets_relaxed)}")
        print(f"  Nets with relaxed clearance: {len(nets_relaxed)}")

        if nets_relaxed:
            print("\n  Relaxed clearance details:")
            for net, clearance in sorted(nets_relaxed.items()):
                net_name = self.net_names.get(net, f"Net {net}")
                reduction = (1 - clearance / original_clearance) * 100
                print(f"    {net_name}: {clearance:.3f}mm ({reduction:.0f}% reduction)")

        if clearance_failed_nets:
            print(f"\n  ⚠ {len(clearance_failed_nets)} net(s) still failed after max relaxation")

        return list(self.routes), nets_relaxed

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
