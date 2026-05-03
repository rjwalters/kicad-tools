"""High-level autorouter API with Autorouter, AdaptiveAutorouter, and RoutingResult."""

from __future__ import annotations

import logging
import math
import os
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_tools.explain.decisions import DecisionStore
    from kicad_tools.pcb.blocks.base import PCBBlock
    from kicad_tools.physics import Stackup, TransmissionLine
    from kicad_tools.progress import ProgressCallback

    from .io import FineZone
    from .pathfinder import Router

from kicad_tools.cli.progress import flush_print

logger = logging.getLogger(__name__)

from .adaptive import AdaptiveAutorouter, RoutingResult
from .algorithms import (
    HierarchicalRouter,
    MonteCarloRouter,
    MSTRouter,
    NegotiatedRouter,
    TwoPhaseRouter,
    calculate_congestion_tuned_params,
    calculate_history_increment,
    calculate_present_cost,
    detect_oscillation,
    should_terminate_early,
)
from .bus import BusGroup, BusRoutingConfig, BusRoutingMode
from .cache import (
    RoutingCache,
    SubProblemSignature,
    normalize_routes_to_origin,
    transform_routes,
)
from .bus_routing import BusRouter
from .cpp_backend import CppGrid, CppPathfinder, create_hybrid_router, get_backend_info
from .diffpair import DifferentialPair, DifferentialPairConfig, LengthMismatchWarning
from .diffpair_routing import DiffPairRouter
from .escape import EscapeRouter, PackageInfo, is_dense_package
from .adaptive_grid import AdaptiveGridResult, AdaptiveGridRouter
from .subgrid import SubGridResult, SubGridRouter
from .failure_analysis import (
    CongestionMap,
    FailureAnalysis,
    FailureCause,
    RootCauseAnalyzer,
)
from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .net_class import NetClass, classify_from_name
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
    SIMPLE_NET_THRESHOLD_MM,
    DesignRules,
    LengthConstraint,
    NetClassRouting,
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
from .congestion_estimator import CongestionEstimator
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


def _format_pad_ref(pad: "Pad") -> str:
    """Return a human-readable identifier for *pad*.

    Issue #2329: Steiner-tree branch points have empty ``ref`` and ``pin``
    fields which previously produced the cryptic ``.`` string in failure
    diagnostics.  This helper falls back to coordinate-based identification
    when the component reference is missing.
    """
    if pad.ref and pad.pin:
        return f"{pad.ref}.{pad.pin}"
    if pad.ref:
        return pad.ref
    # Steiner point or other synthetic pad — identify by position
    return f"steiner@({pad.x:.3f},{pad.y:.3f})"


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
        # Pre-existing routes loaded as obstacles for DRC/merge but NOT
        # emitted by to_sexp() or subject to rip-up/reroute.
        self.existing_routes: list[Route] = []

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

        # Issue #2330: Waypoint injection replaces the sub-grid escape pre-pass.
        # When True, the pathfinder injects off-grid pad positions directly into
        # the A* search graph, eliminating the need for escape segments.
        # The sub-grid pre-pass is retained as a fallback when this is False.
        self.use_waypoint_injection: bool = True

        # Fine zones for multi-resolution escape routing (Issue #1828)
        # Set externally (e.g., from CLI's MultiResolutionGridPlan) before
        # routing so the SubGridRouter uses fine grid resolution for pads
        # within dense IC packages.
        self.fine_zones: list[FineZone] = []

        # Pour nets without zones that should be routed as signals (Issue #1841)
        # Set externally (e.g., from CLI's _auto_skip_pour_nets) after loading.
        # Nets in this set have is_pour_net=True in their net class but lack a
        # copper zone, so they must be routed as signal traces instead of skipped.
        self._pour_nets_without_zones: set[str] = set()

        # Board bounding box for OOB filtering in cleanup_artifacts().
        # Defaults to grid origin/dimensions but can be overridden with the
        # actual board edge cuts bbox when available (Issue #2039).
        self._board_bbox: tuple[float, float, float, float] | None = None

        # Copper-to-board-edge clearance for escape routing (Issue #2136).
        # When set, the EscapeRouter clamps escape points to stay within
        # the edge clearance zone.
        self._edge_clearance: float | None = None

        # Shapely-based board geometry for accurate non-rectangular edge
        # clearance (Issue #2340).  Set by load_pcb_for_routing() when
        # Shapely is available.
        self._board_geometry: Any | None = None

        # Length constraint tracking (Issue #630)
        self._length_tracker: LengthTracker = LengthTracker()

        # Sub-problem pattern cache (Issue #2336)
        # When set, enables cross-board reuse of routing solutions for
        # recurring pad geometries (e.g. bypass caps, pull-up networks).
        self._sub_problem_cache: RoutingCache | None = None
        self._sub_problem_hits: int = 0
        self._sub_problem_misses: int = 0
        self._sub_problem_collisions: int = 0

        # Issue #2334: Stochastic cost perturbation to escape local minima.
        # When stagnation is detected, random noise is added to per-net
        # priority scores in _get_net_priority() to break symmetry and
        # explore alternative routing orders.
        self._perturbation_magnitude: float = 0.0
        self._perturbation_rng: random.Random = random.Random(42)

        # Routing failure tracking (Issue #688)
        self.routing_failures: list[RoutingFailure] = []

        # Power-net stall abort tracking (Issue #2388).
        # Set to True by the negotiated routing loop when it bails out
        # because all stalled nets are power/pour nets and overflow has
        # plateaued.  Populated with the names of the stalled power nets
        # so the CLI can surface actionable suggestions (e.g. enable
        # --power-nets zones, escalate layers) instead of spinning until
        # timeout.
        self.power_stall_abort: bool = False
        self.power_stall_nets: list[str] = []

        # Issue #2401: Escape endpoint pad overrides.
        # After escape routing, pads that have successful escape routes are
        # replaced by virtual pads at the escape endpoint coordinates.  The
        # main routing pipeline (RSMT + A*) then routes between escape
        # endpoints instead of original pad centers, avoiding conflicts with
        # escape stub segments.
        # Maps (ref, pin) -> virtual Pad at escape endpoint.
        self._escape_pad_overrides: dict[tuple[str, str], Pad] = {}
        # Fine-grid routing count (updated by route_all_multi_resolution)
        self.fine_grid_nets_count: int = 0

        # Decision recording (Issue #829)
        self.record_decisions = record_decisions
        self._decision_store: DecisionStore | None = None
        if record_decisions:
            from kicad_tools.explain.decisions import DecisionStore

            self._decision_store = DecisionStore()

        # Constraint-aware net ordering (Issue #1020)
        # Cache for component pitches, computed lazily on first access
        self._component_pitches: dict[str, float] | None = None

        # Issue #2432: Matrix-conflicting net IDs (charlieplex topology).
        # Populated by _detect_and_apply_matrix_preferences() before the
        # routing loop so _calculate_constraint_score() can boost priority.
        self._matrix_conflict_nets: set[int] = set()

        # Pre-route congestion estimator (Issue #2278)
        # Computed lazily before net ordering; provides RUDY-based
        # congestion scores used as a 6th tiebreaker in _get_net_priority().
        self._congestion_estimator: CongestionEstimator | None = None

        # Registered PCB blocks for protected-zone routing (Issue #1586)
        self.registered_blocks: dict[str, "PCBBlock"] = {}

        # Block-internal connectivity: net_name -> list of (pad_key_set, trace_data)
        # Each entry is a group of pad keys connected by block-internal traces.
        # Populated by register_block() when blocks have internal traces.
        # (Issue #1587)
        self._block_internal_connections: dict[str, list[dict]] = {}

    def enable_sub_problem_cache(
        self,
        cache: RoutingCache | None = None,
    ) -> None:
        """Enable sub-problem pattern caching for recurring pad geometries.

        Issue #2336: When enabled, the router computes a position/rotation-
        invariant signature for each net's pad configuration before routing.
        If a matching solution exists in the cache, it is transformed to the
        current location and validated against the grid. On cache miss, the
        freshly routed solution is stored for future reuse.

        Args:
            cache: RoutingCache instance to use. If None, creates a default
                   cache using the standard cache directory.
        """
        if cache is None:
            cache = RoutingCache()
        self._sub_problem_cache = cache
        logger.info("Sub-problem pattern cache enabled")

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
        # C++ backend handles much larger grids efficiently
        if not self._force_python:
            from .cpp_backend import is_cpp_available

            if is_cpp_available():
                adaptive_threshold = 50_000_000

        if estimated_cells > adaptive_threshold:
            # Use adaptive resolution for better performance on large boards
            grid = RoutingGrid.create_adaptive(
                width, height, self.rules, origin_x, origin_y, layer_stack=self.layer_stack
            )
        else:
            grid = RoutingGrid(
                width, height, self.rules, origin_x, origin_y, layer_stack=self.layer_stack
            )
        router = create_hybrid_router(
            grid, self.rules, force_python=self._force_python, net_class_map=self.net_class_map
        )
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

        # Issue #1674: Compute clearance per-segment using seg.width
        # instead of the global rules.trace_width, matching the Python
        # grid's mark_route() behaviour for per-net-class trace widths.
        for seg in route.segments:
            total_clearance = seg.width / 2 + self.rules.trace_clearance
            clearance_cells = int(total_clearance / self.grid.resolution) + 1
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

        Issue #1250: Also feeds committed segments to the pathfinder for
        crossing-aware cost computation in subsequent A* searches.

        Issue #2275: Refreshes the pathfinder's cached layer fill ratios
        so subsequent A* searches see updated utilization.
        """
        self.grid.mark_route(route)
        self._mark_route_on_cpp_grid(route)

        # Issue #1250: Feed committed segments to pathfinder for crossing detection
        if hasattr(self.router, "add_routed_segments"):
            self.router.add_routed_segments(route.segments)

        # Issue #2275: Update layer fill ratios for utilization-aware routing
        if hasattr(self.router, "update_layer_fill_ratios"):
            self.router.update_layer_fill_ratios()

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

        # Include Python fallback statistics when using C++ backend
        if isinstance(self.router, CppPathfinder):
            info["fallback_stats"] = self.router.fallback_stats

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
        """Add a component's pads.

        Computes the component's minimum pin pitch from pad positions and
        passes it to the grid so fine-pitch pads get reduced clearance
        envelopes (Issue #1778).
        """
        # Pre-compute minimum pin pitch for this component from pad positions
        # so the grid can apply reduced clearance for fine-pitch packages.
        pin_pitch = self._compute_component_pitch(pads)

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

            self.grid.add_pad(pad, pin_pitch=pin_pitch)

    @staticmethod
    def _compute_component_pitch(pads: list[dict]) -> float | None:
        """Compute minimum pin pitch from a component's pad list.

        Args:
            pads: List of pad info dicts with 'x' and 'y' keys.

        Returns:
            Minimum center-to-center distance between adjacent pads in mm,
            or None if fewer than 2 pads.
        """
        if len(pads) < 2:
            return None

        import math

        min_pitch = float("inf")
        positions = [(p["x"], p["y"]) for p in pads]
        for i, (x1, y1) in enumerate(positions):
            for x2, y2 in positions[i + 1:]:
                dist = math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)
                if dist > 0.01:  # Ignore overlapping pads
                    min_pitch = min(min_pitch, dist)

        return min_pitch if min_pitch != float("inf") else None

    def add_obstacle(
        self, x: float, y: float, width: float, height: float, layer: Layer = Layer.F_CU
    ):
        """Add an obstacle (keepout area, mounting hole, etc.)."""
        obs = Obstacle(x, y, width, height, layer)
        self.grid.add_obstacle(obs)

    def register_block(self, block: "PCBBlock") -> None:
        """Register a PCBBlock as a protected zone on the routing grid.

        This marks the block's bounding box as blocked for external routing,
        while keeping port positions available as routing endpoints. Block
        ports are registered as router pads so the pathfinder can route to them.

        The obstacle is registered BEFORE port pads to ensure correct grid
        state: add_obstacle blocks the bounding box, then add_pad for each
        port punches through the blocked region for the port's net.

        Args:
            block: A placed PCBBlock with components and ports.

        Raises:
            ValueError: If the block has not been placed yet.
        """
        from kicad_tools.pcb.blocks.base import PCBBlock  # noqa: F811

        if not block.placed:
            raise ValueError(
                f"Block '{block.block_id}' must be placed before registering with the router"
            )

        self.registered_blocks[block.block_id] = block

        # Step 1: Compute absolute bounding box and block it on all routable layers
        bbox = block.bounding_box
        # Translate bounding box to absolute coordinates
        abs_min_x = bbox.min_x + block.origin.x
        abs_min_y = bbox.min_y + block.origin.y
        abs_max_x = bbox.max_x + block.origin.x
        abs_max_y = bbox.max_y + block.origin.y

        # Block the bounding box on all routable layers
        routable_layers = [
            self.grid.index_to_layer(idx) for idx in self.grid.get_routable_indices()
        ]
        for layer_val in routable_layers:
            router_layer = Layer(layer_val)
            center_x = (abs_min_x + abs_max_x) / 2
            center_y = (abs_min_y + abs_max_y) / 2
            width = abs_max_x - abs_min_x
            height = abs_max_y - abs_min_y
            obs = Obstacle(center_x, center_y, width, height, router_layer)
            self.grid.add_obstacle(obs)

        # Step 2: Register port positions as router pads so they remain
        # valid routing endpoints despite the blocked bounding box.
        for port_name, port_obj in block.ports.items():
            abs_pos = block.port(port_name)

            # Map the PCB layer to a router CopperLayer
            from kicad_tools.core.types import CopperLayer

            try:
                router_layer = CopperLayer.from_kicad_name(port_obj.layer.value)
            except (ValueError, AttributeError):
                # Default to F_CU if layer mapping fails
                router_layer = Layer.F_CU

            # Create a router Pad for this port
            # Use a synthetic ref/pin derived from block_id and port name
            port_pad = Pad(
                x=abs_pos.x,
                y=abs_pos.y,
                width=0.5,  # Default port pad size
                height=0.5,
                net=0,  # Net assigned later when connecting
                net_name="",
                layer=router_layer,
                ref=f"_block_{block.block_id}",
                pin=port_name,
            )
            key = (port_pad.ref, port_pad.pin)
            self.pads[key] = port_pad
            self.grid.add_pad(port_pad)

        # Step 3: Index block-internal traces for auto-routing skip (Issue #1587)
        self._index_block_internal_traces(block)

    def _index_block_internal_traces(self, block: "PCBBlock") -> None:
        """Build internal-connectivity map from a block's internal traces.

        For each internal trace, match its start/end positions to router pads
        by proximity (0.01 mm epsilon). Record the connected pad keys so that
        ``_create_block_internal_routes`` can skip pathfinding for those pairs.

        Args:
            block: A placed PCBBlock whose internal traces should be indexed.
        """
        EPSILON = 0.01  # mm tolerance for pad-to-trace endpoint matching

        placed_traces = block.get_placed_traces()
        internal_traces = [t for t in placed_traces if t.get("internal", False)]
        if not internal_traces:
            return

        # Build reverse map: net_name -> net_id
        name_to_id: dict[str, int] = {}
        for net_id, name in self.net_names.items():
            name_to_id[name] = net_id

        for trace_data in internal_traces:
            net_name = trace_data.get("net")
            if not net_name:
                continue

            if net_name not in name_to_id:
                flush_print(
                    f"  Warning: block '{block.block_id}' internal trace references "
                    f"unknown net '{net_name}', skipping"
                )
                continue

            start = trace_data["start"]
            end = trace_data["end"]

            # Find pads near the trace endpoints
            start_keys = self._find_pads_near(start[0], start[1], EPSILON)
            end_keys = self._find_pads_near(end[0], end[1], EPSILON)

            if not start_keys or not end_keys:
                continue

            # Record the connection: all start pads are connected to all end pads
            connected_keys = start_keys | end_keys

            if net_name not in self._block_internal_connections:
                self._block_internal_connections[net_name] = []

            self._block_internal_connections[net_name].append(
                {
                    "pad_keys": connected_keys,
                    "trace": trace_data,
                    "block_id": block.block_id,
                }
            )

    def _find_pads_near(
        self, x: float, y: float, epsilon: float
    ) -> set[tuple[str, str]]:
        """Find all router pad keys within epsilon distance of (x, y).

        Args:
            x: X coordinate in mm.
            y: Y coordinate in mm.
            epsilon: Distance tolerance in mm.

        Returns:
            Set of (ref, pin) pad keys near the given position.
        """
        result: set[tuple[str, str]] = set()
        eps_sq = epsilon * epsilon
        for key, pad in self.pads.items():
            dx = pad.x - x
            dy = pad.y - y
            if dx * dx + dy * dy <= eps_sq:
                result.add(key)
        return result

    def _create_block_internal_routes(
        self, net: int, pads: list[tuple[str, str]]
    ) -> tuple[list[Route], set[int]]:
        """Create Route objects for block-internal traces on this net.

        Follows the same pattern as ``_create_intra_ic_routes``: returns
        pre-built Route objects and the set of pad indices that were
        connected internally so they can be removed from the MST pool.

        Args:
            net: Net ID being routed.
            pads: List of (ref, pin) pad keys for this net.

        Returns:
            Tuple of (routes created, set of pad indices connected internally).
        """
        net_name = self.net_names.get(net, "")
        if not net_name or net_name not in self._block_internal_connections:
            return [], set()

        routes: list[Route] = []
        connected_indices: set[int] = set()

        # Build a lookup from pad key to index in the pads list
        key_to_indices: dict[tuple[str, str], list[int]] = {}
        for i, key in enumerate(pads):
            if key not in key_to_indices:
                key_to_indices[key] = []
            key_to_indices[key].append(i)

        for conn in self._block_internal_connections[net_name]:
            trace_data = conn["trace"]
            block_id = conn["block_id"]
            pad_keys = conn["pad_keys"]

            # Find which pads from this net's pad list are in the connected set
            matched_indices: set[int] = set()
            for pk in pad_keys:
                if pk in key_to_indices:
                    for idx in key_to_indices[pk]:
                        matched_indices.add(idx)

            if len(matched_indices) < 2:
                continue

            # Create a Route from the block trace data (no pathfinding needed)
            start = trace_data["start"]
            end = trace_data["end"]
            layer_name = trace_data.get("layer", "F.Cu")

            from kicad_tools.core.types import CopperLayer

            try:
                router_layer = CopperLayer.from_kicad_name(layer_name)
            except (ValueError, AttributeError):
                router_layer = Layer.F_CU

            from .primitives import Segment

            route = Route(net=net, net_name=net_name)
            seg = Segment(
                x1=start[0],
                y1=start[1],
                x2=end[0],
                y2=end[1],
                width=trace_data.get("width", self.rules.trace_width),
                layer=router_layer,
                net=net,
                net_name=net_name,
            )
            route.segments.append(seg)
            routes.append(route)
            connected_indices |= matched_indices

            flush_print(
                f"  Block-internal route: {block_id} net={net_name} "
                f"({len(matched_indices)} pads skipped)"
            )

        return routes, connected_indices

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
        return create_intra_ic_routes(net, pads, self.pads, self.rules, self.net_class_map)

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
        _subgrid_retry: bool = False,
    ) -> list[Route]:
        """Route all connections for a net.

        Args:
            net: Net ID to route
            use_mst: Use minimum spanning tree routing for multi-point nets
            target_impedance: Target characteristic impedance in ohms (optional).
                When specified and physics module is available, calculates
                appropriate trace widths per layer to achieve this impedance.
            _subgrid_retry: Internal flag to prevent recursive retry loops.
                Do not set this parameter directly.

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

        # Handle block-internal connections (Issue #1587)
        block_routes, block_connected = self._create_block_internal_routes(net, pads)
        for route in block_routes:
            self._mark_route(route)
            routes.append(route)
            self.routes.append(route)
        connected_indices |= block_connected

        # Build reduced pad list for inter-IC routing
        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        # Issue #2401: Substitute escaped pads with virtual pads at escape
        # endpoints so RSMT + A* route between escape endpoints, not original
        # pad centers.
        pad_objs = [
            self._escape_pad_overrides.get(p, self.pads[p])
            for p in pads_for_routing
        ]

        # Issue #2336: Try sub-problem pattern cache before A* search.
        # Compute a position/rotation-invariant signature and check for a
        # cached solution that can be transformed to the current location.
        sub_sig = None
        if self._sub_problem_cache is not None:
            cached_routes = self._try_sub_problem_cache(net, pad_objs)
            if cached_routes is not None:
                for route in cached_routes:
                    self._mark_route(route)
                    routes.append(route)
                    self.routes.append(route)
                # Record decision if enabled
                if self.record_decisions and cached_routes:
                    self._record_routing_decision(net, cached_routes)
                return routes
            # Signature was computed; we'll store the solution after routing
            sub_sig = SubProblemSignature.compute(pad_objs, self.rules)

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
                off_grid_pads.append(f"{_format_pad_ref(source_pad)} off by {src_dist:.3f}mm")
            if tgt_dist > grid_threshold:
                off_grid_pads.append(f"{_format_pad_ref(target_pad)} off by {tgt_dist:.3f}mm")

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
                        pad_ref=_format_pad_ref(source_pad),
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
                        pad_ref=_format_pad_ref(target_pad),
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
                source_pad=(
                    source_pad.ref or f"steiner@({source_pad.x:.3f},{source_pad.y:.3f})",
                    source_pad.pin or "",
                ),
                target_pad=(
                    target_pad.ref or f"steiner@({target_pad.x:.3f},{target_pad.y:.3f})",
                    target_pad.pin or "",
                ),
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
            # Issue #2329: Disable Steiner tree decomposition for nets with
            # off-grid pads.  RSMT Steiner points inherit off-grid coordinates
            # (the median of terminal positions), creating virtual pads that
            # the sub-grid prepass doesn't know about and the A* pathfinder
            # cannot reach.  Plain MST connects real pads directly, avoiding
            # the off-grid Steiner point failure mode.
            has_off_grid = self._net_has_off_grid_pads(net)
            new_routes = mst_router.route_net(
                pad_objs, mark_route, record_failure,
                use_steiner=not has_off_grid,
            )
        else:
            new_routes = mst_router.route_net_star(pad_objs, mark_route, record_failure)

        routes.extend(new_routes)

        # Issue #1603: Retry with sub-grid escape on PIN_ACCESS failure
        # Issue #2330: Skip sub-grid retry when waypoint injection is active
        if not _subgrid_retry and not new_routes and not self.use_waypoint_injection:
            # Check if this net has a PIN_ACCESS failure
            has_pin_access_failure = any(
                f.net == net and f.failure_cause == FailureCause.PIN_ACCESS
                for f in self.routing_failures
            )
            if has_pin_access_failure:
                retry_routes = self._retry_net_with_subgrid(net)
                if retry_routes:
                    routes.extend(retry_routes)

        # Issue #2336: Store freshly routed solution in sub-problem cache
        if sub_sig is not None and new_routes and self._sub_problem_cache is not None:
            self._store_sub_problem(sub_sig, new_routes)

        # Issue #2438: Clear DRC avoidance costs accumulated during this net's
        # routing so they don't pollute subsequent nets.
        if hasattr(self.router, "clear_avoidance_costs"):
            self.router.clear_avoidance_costs()

        # Record routing decision if enabled
        if self.record_decisions and routes:
            self._record_routing_decision(net, routes)

        return routes

    def _try_sub_problem_cache(
        self,
        net: int,
        pad_objs: list[Pad],
    ) -> list[Route] | None:
        """Attempt to reuse a cached sub-problem solution.

        Issue #2336: Computes a position/rotation-invariant signature from
        the pad geometry, looks up the cache, transforms a hit to the
        current position, and validates against the current grid state.

        Args:
            net: Net ID being routed.
            pad_objs: Pad objects for this net.

        Returns:
            Transformed routes if cache hit and validation passes, else None.
        """
        assert self._sub_problem_cache is not None

        sig = SubProblemSignature.compute(pad_objs, self.rules)
        cached = self._sub_problem_cache.get_sub_problem(sig)

        if cached is None:
            self._sub_problem_misses += 1
            return None

        # Deserialize and transform to current position
        raw_routes = self._sub_problem_cache.deserialize_routes(cached.route_data)
        net_name = self.net_names.get(net, f"Net_{net}")
        transformed = transform_routes(
            raw_routes,
            dx=sig.centroid_x,
            dy=sig.centroid_y,
            angle=sig.rotation_angle,
            target_net=net,
            target_net_name=net_name,
        )

        # Validate: ensure no segment overlaps an occupied grid cell
        # belonging to a different net
        if not self._validate_transformed_routes(transformed, net):
            self._sub_problem_collisions += 1
            logger.debug(
                "Sub-problem cache hit rejected (collision): net %d sig %s",
                net,
                sig.signature_hash[:12],
            )
            return None

        self._sub_problem_hits += 1
        logger.debug(
            "Sub-problem cache hit accepted: net %d sig %s (%d segs, %d vias)",
            net,
            sig.signature_hash[:12],
            sum(len(r.segments) for r in transformed),
            sum(len(r.vias) for r in transformed),
        )
        return transformed

    def _validate_transformed_routes(
        self,
        routes: list[Route],
        net: int,
    ) -> bool:
        """Check that transformed routes do not collide with existing routes.

        Walks every segment endpoint and via location, converting to grid
        coordinates and checking that the cell is not blocked by another net.

        Args:
            routes: Transformed routes to validate.
            net: Net ID (same-net cells are allowed).

        Returns:
            True if all cells are available or belong to this net.
        """
        from .layers import Layer

        for route in routes:
            for seg in route.segments:
                for wx, wy in [(seg.x1, seg.y1), (seg.x2, seg.y2)]:
                    gx, gy = self.grid.world_to_grid(wx, wy)
                    if self.grid.is_blocked(gx, gy, seg.layer, net):
                        return False
            for via in route.vias:
                gx, gy = self.grid.world_to_grid(via.x, via.y)
                for layer in [via.layers[0], via.layers[1]]:
                    if self.grid.is_blocked(gx, gy, layer, net):
                        return False
        return True

    def _store_sub_problem(
        self,
        sig: SubProblemSignature,
        routes: list[Route],
    ) -> None:
        """Store a freshly routed solution in the sub-problem cache.

        Args:
            sig: Sub-problem signature for the pad configuration.
            routes: Successfully routed solution in world coordinates.
        """
        assert self._sub_problem_cache is not None

        normalized = normalize_routes_to_origin(
            routes,
            sig.centroid_x,
            sig.centroid_y,
            sig.rotation_angle,
        )
        self._sub_problem_cache.put_sub_problem(sig, normalized)

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

    def _activate_perturbation(self, stagnation_count: int) -> None:
        """Activate stochastic cost perturbation (Issue #2334).

        Scales perturbation magnitude with stagnation duration so the
        router explores more aggressively the longer it remains stuck.

        Args:
            stagnation_count: Number of consecutive iterations with no
                improvement in overflow.
        """
        self._perturbation_magnitude = 0.1 * stagnation_count
        # Re-seed the RNG each activation so different stagnation
        # episodes explore different orderings.
        self._perturbation_rng = random.Random(stagnation_count * 7 + 13)

    def _reset_perturbation(self) -> None:
        """Reset perturbation to zero (Issue #2334).

        Called when a new best overflow is achieved, indicating the
        router has escaped the local minimum.
        """
        self._perturbation_magnitude = 0.0

    def reset_attempt_state(self) -> None:
        """Reset per-attempt mutable state to pristine defaults (Issue #2396).

        Called by the layer-escalation orchestrator at the start of each
        attempt, immediately after ``load_pcb_for_routing()`` creates a
        fresh ``Autorouter``.  Today this is a no-op because the
        orchestrator already creates a new instance per attempt, but this
        method documents the contract: *between escalation attempts, no
        router state from the prior attempt influences the next.*

        Clearing these fields defensively prevents silent regression if
        future refactors reuse an ``Autorouter`` across attempts.
        """
        self.power_stall_abort = False
        self.power_stall_nets = []
        self.routing_failures = []
        self._perturbation_magnitude = 0.0
        self._congestion_estimator = None

    def _calculate_constraint_score(self, net_id: int) -> float:
        """Calculate a constraint score for a net based on routing difficulty.

        Issue #1020: Nets connecting to fine-pitch components or with many pads
        are more constrained and should be routed first. Higher score = more constrained.

        Issue #2329: Nets with off-grid pads (pads that don't align to the
        routing grid) receive a priority boost so they route before on-grid
        nets.  Off-grid pads require sub-grid escape routes and have fewer
        viable paths, making them more constrained.

        The score is computed as:
        - Fine-pitch component connections: 10.0 / pitch for each pad on a fine-pitch IC
        - Pad count penalty: 0.5 per pad (more pads = more routing constraints)
        - Off-grid pad boost: fine_pitch_weight if any pad is off-grid

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

        # Issue #2329: Off-grid pad boost — nets with pads that don't align to
        # the routing grid are more constrained because they need sub-grid escape
        # routes and have fewer viable paths.  Route them first so they get
        # first pick of grid resources before on-grid nets consume corridors.
        grid_threshold = self.grid.resolution / 10
        for pad_key in pad_keys:
            pad = self.pads.get(pad_key)
            if pad is None:
                continue
            gx, gy = self.grid.world_to_grid(pad.x, pad.y)
            snap_x, snap_y = self.grid.grid_to_world(gx, gy)
            offset = max(abs(pad.x - snap_x), abs(pad.y - snap_y))
            if offset > grid_threshold:
                # Significant boost: off-grid pads are heavily constrained.
                # Use fine_pitch_weight as the base magnitude so the boost is
                # comparable to fine-pitch scoring and large enough to move
                # the net ahead of unconstrained same-tier nets.
                score += fine_pitch_weight
                break  # One off-grid pad is enough to boost the whole net

        # Issue #2432: Matrix-conflicting nets (charlieplex topology) are
        # heavily constrained because they share pads with other nets in
        # the same conflict group.  Boost their priority so they route
        # before non-matrix nets, giving the layer preference assignment
        # maximum effect.
        if net_id in self._matrix_conflict_nets:
            score += fine_pitch_weight * 2

        return score

    def _net_has_off_grid_pads(self, net_id: int) -> bool:
        """Return True if any pad in *net_id* is off the routing grid.

        Issue #2329: Used by :meth:`_get_net_priority` to promote nets with
        off-grid pads to complexity tier 0 so they route before unconstrained
        nets and get first pick of grid resources.
        """
        grid_threshold = self.grid.resolution / 10
        pad_keys = self.nets.get(net_id, [])
        for pad_key in pad_keys:
            pad = self.pads.get(pad_key)
            if pad is None:
                continue
            gx, gy = self.grid.world_to_grid(pad.x, pad.y)
            snap_x, snap_y = self.grid.grid_to_world(gx, gy)
            offset = max(abs(pad.x - snap_x), abs(pad.y - snap_y))
            if offset > grid_threshold:
                return True
        return False

    def _is_pour_net(self, net_id: int) -> bool:
        """Check if a net is a pour net (e.g. GND, VCC) that should be skipped.

        Pour nets are intended to be connected via copper pours (zone fills)
        rather than individual traces.  They are identified by the
        ``is_pour_net`` flag on their :class:`NetClassRouting` entry.

        Issue #1841: Pour nets that lack a copper zone in the PCB are stored
        in :attr:`_pour_nets_without_zones` and must be routed as signals.
        This method returns False for such nets even though their net class
        has ``is_pour_net=True``.

        Args:
            net_id: The net ID to check.

        Returns:
            True if the net's class has ``is_pour_net=True`` and the net
            is not in ``_pour_nets_without_zones``, False otherwise.
        """
        net_name = self.net_names.get(net_id, "")
        if net_name in self._pour_nets_without_zones:
            return False
        net_class = self.net_class_map.get(net_name)
        return bool(net_class and net_class.is_pour_net)

    def _is_power_net_by_class(self, net_id: int) -> bool:
        """Check if a net is classified as power or ground by its name.

        Issue #2388: Used by the negotiated-loop early-abort heuristic to
        identify when stalled nets are all power/pour nets that the router
        cannot resolve as signal traces.  Such nets typically need either
        copper zones (``--power-nets``) or dedicated planes
        (``--auto-layers``).

        Unlike :meth:`_is_pour_net`, this also returns True for power/ground
        nets that lack zones (i.e. nets in ``_pour_nets_without_zones``),
        because those are precisely the nets the early-abort heuristic
        targets.

        Args:
            net_id: The net ID to check.

        Returns:
            True if the net name pattern matches POWER or GROUND
            classification, False otherwise.
        """
        net_name = self.net_names.get(net_id, "")
        if not net_name:
            return False
        net_class = classify_from_name(net_name)
        return net_class in (NetClass.POWER, NetClass.GROUND)

    def _ensure_congestion_estimator(self) -> CongestionEstimator:
        """Build the pre-route congestion estimator if not already computed.

        Issue #2278: The estimator uses RUDY to distribute each net's HPWL
        across a coarse tile grid, producing per-tile demand values.  The
        per-net congestion score (average demand in bbox) is used as a
        tiebreaker in :meth:`_get_net_priority`.

        Returns:
            The cached or newly built ``CongestionEstimator``.
        """
        if self._congestion_estimator is not None:
            return self._congestion_estimator

        # Collect pour-net IDs for exclusion
        pour_ids: set[int] = set()
        for net_id in self.nets:
            if self._is_pour_net(net_id):
                pour_ids.add(net_id)

        self._congestion_estimator = CongestionEstimator.from_nets(
            nets=self.nets,
            pads=self.pads,
            board_origin_x=self.grid.origin_x,
            board_origin_y=self.grid.origin_y,
            board_width=self.grid.width,
            board_height=self.grid.height,
            pour_net_ids=pour_ids,
        )
        return self._congestion_estimator

    def _filter_pour_nets(self, net_order: list[int]) -> list[int]:
        """Remove pour nets from a net ordering and log a warning.

        Pour nets (GND, VCC, etc.) should be connected via zone fills, not
        individual traces.  This helper filters them out of the routing order
        and emits a single warning listing the skipped net names.

        Args:
            net_order: List of net IDs in routing order.

        Returns:
            Filtered list with pour nets removed.
        """
        pour_nets = [n for n in net_order if self._is_pour_net(n)]
        if not pour_nets:
            return net_order

        pour_names = [self.net_names.get(n, f"Net {n}") for n in pour_nets]
        flush_print(
            f"  Skipping {len(pour_nets)} pour net(s) (use zone fill instead): {pour_names}"
        )
        return [n for n in net_order if not self._is_pour_net(n)]

    # ------------------------------------------------------------------
    # Matrix-conflict detection and layer preference assignment (Issue #2432)
    # ------------------------------------------------------------------

    def _detect_matrix_conflicts(
        self, net_ids: list[int], threshold: int = 2
    ) -> list[set[int]]:
        """Detect groups of nets sharing multiple components (matrix topology).

        In a charlieplex LED matrix, nets like NODE_A through NODE_D each
        connect to many of the same LEDs.  Two nets are "matrix-conflicting"
        when they share more than *threshold* components (by reference
        designator, e.g. "D1").  This method returns groups of mutually
        conflicting nets that require layer separation to avoid circular
        blocking during negotiated rip-up.

        Issue #2432: Charlieplex NODE nets stall because they share
        interleaved LED pads and cannot be ordered sequentially.

        Args:
            net_ids: Net IDs to analyse (typically the routing order,
                already filtered of pour nets).
            threshold: Minimum number of shared components for two nets
                to be considered conflicting (default 2).

        Returns:
            List of sets, each set containing net IDs that are mutually
            conflicting.  Non-conflicting nets are not included.
        """
        # Build per-net component sets (references only, ignore pin number)
        net_components: dict[int, set[str]] = {}
        for net_id in net_ids:
            pad_keys = self.nets.get(net_id, [])
            refs: set[str] = set()
            for ref, _pin in pad_keys:
                refs.add(ref)
            if refs:
                net_components[net_id] = refs

        # Build adjacency: two nets are conflicting if they share > threshold refs
        from collections import defaultdict

        adjacency: dict[int, set[int]] = defaultdict(set)
        net_list = list(net_components.keys())
        for i in range(len(net_list)):
            for j in range(i + 1, len(net_list)):
                a, b = net_list[i], net_list[j]
                shared = len(net_components[a] & net_components[b])
                if shared >= threshold:
                    adjacency[a].add(b)
                    adjacency[b].add(a)

        if not adjacency:
            return []

        # Connected-components via BFS to find groups of conflicting nets
        visited: set[int] = set()
        groups: list[set[int]] = []
        for net_id in adjacency:
            if net_id in visited:
                continue
            group: set[int] = set()
            queue = [net_id]
            while queue:
                current = queue.pop()
                if current in visited:
                    continue
                visited.add(current)
                group.add(current)
                for neighbor in adjacency[current]:
                    if neighbor not in visited:
                        queue.append(neighbor)
            if len(group) >= 2:
                groups.append(group)

        return groups

    def _assign_matrix_layer_preferences(
        self, conflict_groups: list[set[int]]
    ) -> dict[int, list[int]]:
        """Assign alternating preferred layers for matrix-conflicting nets.

        For each group of conflicting nets, alternates between front and
        back copper layers so that adjacent charlieplex nets are steered
        to different layers, breaking the circular blocking pattern.

        On single-layer boards (only one routable layer), no preferences
        are assigned so routing falls back to the normal ordering.

        Issue #2432.

        Args:
            conflict_groups: Groups of mutually conflicting net IDs,
                as returned by :meth:`_detect_matrix_conflicts`.

        Returns:
            Dict mapping net_id -> list of preferred layer indices.
            Only nets that received a preference are included.
        """
        # Determine available routing layers
        num_layers = self.grid.num_layers
        if num_layers < 2:
            # Single-layer board: layer separation is impossible
            return {}

        # Use the first and last layer indices (outer layers) for alternation
        layer_a = 0
        layer_b = num_layers - 1

        preferences: dict[int, list[int]] = {}
        for group in conflict_groups:
            sorted_nets = sorted(group)  # Deterministic ordering
            for idx, net_id in enumerate(sorted_nets):
                if idx % 2 == 0:
                    preferences[net_id] = [layer_a]
                else:
                    preferences[net_id] = [layer_b]

        return preferences

    def _inject_matrix_layer_preferences(
        self, net_layer_prefs: dict[int, list[int]]
    ) -> None:
        """Inject per-net layer preferences into net_class_map overrides.

        Creates per-net NetClassRouting entries with ``preferred_layers``
        set, so the pathfinder's ``_get_layer_preference_cost()`` biases
        A* toward the assigned layer.  If the net already has a net class
        entry, a copy is created with the layer preference added.

        Issue #2432.

        Args:
            net_layer_prefs: Dict mapping net_id -> preferred layer indices,
                as returned by :meth:`_assign_matrix_layer_preferences`.
        """
        from dataclasses import replace

        for net_id, layers in net_layer_prefs.items():
            net_name = self.net_names.get(net_id, "")
            if not net_name:
                continue
            existing = self.net_class_map.get(net_name)
            if existing is not None:
                # Copy existing net class and add layer preference
                override = replace(existing, preferred_layers=layers)
            else:
                # Create a new net class entry with default values + layer pref
                override = NetClassRouting(
                    name=f"matrix_{net_name}",
                    preferred_layers=layers,
                )
            self.net_class_map[net_name] = override

        if net_layer_prefs:
            names = [
                self.net_names.get(n, f"Net {n}") for n in net_layer_prefs
            ]
            flush_print(
                f"  Matrix conflict: assigned layer preferences for {len(names)} net(s): {names}"
            )

    # Cache of net IDs detected as matrix-conflicting (Issue #2432).
    # Populated by _detect_and_apply_matrix_preferences() so that
    # _calculate_constraint_score() can boost priority for these nets.
    _matrix_conflict_nets: set[int]

    def _detect_and_apply_matrix_preferences(
        self, net_order: list[int]
    ) -> None:
        """Detect matrix-conflicting nets and inject layer preferences.

        Convenience method that chains :meth:`_detect_matrix_conflicts`,
        :meth:`_assign_matrix_layer_preferences`, and
        :meth:`_inject_matrix_layer_preferences`.

        Also populates :attr:`_matrix_conflict_nets` so that
        :meth:`_calculate_constraint_score` can boost priority for
        matrix nets.

        Issue #2432.

        Args:
            net_order: Filtered net ordering (pour nets removed).
        """
        groups = self._detect_matrix_conflicts(net_order)
        if not groups:
            self._matrix_conflict_nets = set()
            return

        prefs = self._assign_matrix_layer_preferences(groups)
        self._inject_matrix_layer_preferences(prefs)

        # Cache the set of matrix-conflicting net IDs for priority boost
        all_conflict_nets: set[int] = set()
        for group in groups:
            all_conflict_nets |= group
        self._matrix_conflict_nets = all_conflict_nets

    def _get_net_priority(self, net_id: int) -> tuple[int, int, float, int, float, float]:
        """Get routing priority for a net (lower = higher priority).

        Returns a 6-tuple used for sorting:
        1. Net class priority (1-10 for signal nets, 99 for pour nets)
        2. Complexity tier (0 = simple 2-pin short net, 1 = complex/multi-pin)
        3. Negative constraint score (higher constraint = route first, so negate)
        4. Pad count (fewer pads = higher priority, simpler nets first)
        5. Bounding box diagonal (shorter nets first, leaves room for longer nets)
        6. Negative congestion score (higher congestion = route first, so negate)

        Pour nets (``is_pour_net=True``) are assigned priority 99 so they sort
        to the very end.  They are additionally filtered out before the routing
        loop by :meth:`_filter_pour_nets`, but the high priority value acts as
        a safety net for callers that bypass the filter.

        Issue #1020: Adds constraint-aware ordering. Nets connecting to fine-pitch
        ICs are routed before unconstrained nets within the same priority class
        and complexity tier.

        Issue #1295: Adds complexity-tier ordering and pour-net deprioritisation.
        Within each priority class, simple 2-pin short nets are routed before
        complex multi-pin or long-span nets.  Within each tier, more constrained
        (fine-pitch) nets still route first.

        Issue #2278: Adds pre-route RUDY congestion score as a 6th tiebreaker.
        Nets in congested regions are routed first (higher congestion = lower
        sort value) to reserve resources while the grid is uncrowded.

        Issue #2329: Off-grid pads (pads that don't snap to the routing grid)
        now boost the constraint score, ensuring nets with such pads route
        before unconstrained nets.  This prevents on-grid nets from consuming
        corridors that off-grid pads need for their escape routes.

        The resulting ordering for signal nets is:
        - Clock/diff-pair (class priority 2) > Digital (4) > Debug (5) > Default (10)
        - Within each class: simple 2-pin short > complex multi-pin/long-span
        - Within each tier: fine-pitch / off-grid constrained > unconstrained
        - Within same constraint: congested > uncongested
        """
        net_name = self.net_names.get(net_id, "")
        net_class = self.net_class_map.get(net_name)

        # Pour nets get pushed to the very back of the ordering.
        if net_class and net_class.is_pour_net:
            return (99, 0, 0.0, 0, 0.0, 0.0)

        priority = net_class.priority if net_class else 10
        pad_count = len(self.nets.get(net_id, []))
        distance = self._get_net_bounding_box_diagonal(net_id)

        # Issue #1020: Constraint score (negated so higher constraint = lower tuple value)
        constraint_score = self._calculate_constraint_score(net_id)

        # Issue #1295: Complexity tier — simple 2-pin short nets (tier 0) before
        # multi-pin or long-span nets (tier 1).
        # Issue #2329: Nets with off-grid pads are promoted to tier 0 regardless
        # of pad count, because off-grid pads are heavily constrained and need
        # first pick of grid resources before on-grid nets block their corridors.
        complexity_tier = 0 if (pad_count == 2 and distance < SIMPLE_NET_THRESHOLD_MM) else 1
        if complexity_tier == 1 and self._net_has_off_grid_pads(net_id):
            complexity_tier = 0

        # Issue #2278: Pre-route RUDY congestion score (negated so higher = route first)
        estimator = self._ensure_congestion_estimator()
        congestion_score = estimator.get_net_congestion_score(net_id)

        # Issue #2334: When stochastic perturbation is active, add random
        # noise to the congestion score to break symmetry and explore
        # alternative routing orders.  The noise is applied to the 6th
        # tuple element (congestion score) which serves as the final
        # tiebreaker, so it only changes the ordering of nets that are
        # otherwise equivalent in priority, complexity, constraint, and
        # pad count.
        if self._perturbation_magnitude > 0:
            noise = self._perturbation_rng.gauss(0, self._perturbation_magnitude)
            congestion_score += noise

        return (priority, complexity_tier, -constraint_score, pad_count, distance, -congestion_score)

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

        if net_order is None:
            net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))

        # Issue #1295: Filter out pour nets (GND, VCC, etc.) — they should be
        # connected via zone fills, not routed as individual traces.
        net_order = self._filter_pour_nets(net_order)

        # Issue #2432: Detect charlieplex/matrix topology and assign
        # alternating layer preferences to break circular blocking.
        self._detect_and_apply_matrix_preferences(net_order)
        # Re-sort after matrix priority boost
        net_order = sorted(net_order, key=lambda n: self._get_net_priority(n))

        if parallel:
            return self.route_all_parallel(
                net_order=net_order,
                progress_callback=progress_callback,
                max_workers=max_workers,
            )

        # Issue #1603: Sub-grid escape pre-pass for off-grid pads
        escape_routes = self._run_subgrid_prepass()

        nets_to_route = [n for n in net_order if n != 0]
        total_nets = len(nets_to_route)
        all_routes: list[Route] = list(escape_routes)

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

        # Issue #1603: Sub-grid escape pre-pass for off-grid pads
        escape_routes = self._run_subgrid_prepass()

        # Get interleaved ordering and MST cache
        net_order, mst_cache = self._get_interleaved_net_order(use_interleaving=True)
        # Issue #1295: Filter out pour nets before routing
        net_order = self._filter_pour_nets(net_order)
        nets_to_route = [n for n in net_order if n != 0]
        total_nets = len(nets_to_route)

        print(f"  Total nets: {total_nets}")
        print(f"  N-port nets with cached MST: {len(mst_cache)}")

        all_routes: list[Route] = list(escape_routes)
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

        # Handle block-internal connections (Issue #1587)
        block_routes, block_connected = self._create_block_internal_routes(net, pads)
        for route in block_routes:
            self._mark_route(route)
            routes.append(route)
            self.routes.append(route)
        connected_indices |= block_connected

        # Build reduced pad list for inter-IC routing
        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        # Issue #2401: Substitute escaped pads with virtual pads at escape
        # endpoints.
        pad_objs = [
            self._escape_pad_overrides.get(p, self.pads[p])
            for p in pads_for_routing
        ]

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
            end=target_coords,
            net=net_name,
        )

        failure = RoutingFailure(
            net=net,
            net_name=net_name,
            source_pad=(
                source_pad.ref or f"steiner@({source_pad.x:.3f},{source_pad.y:.3f})",
                source_pad.pin or "",
            ),
            target_pad=(
                target_pad.ref or f"steiner@({target_pad.x:.3f},{target_pad.y:.3f})",
                target_pad.pin or "",
            ),
            source_coords=source_coords,
            target_coords=target_coords,
            blocking_nets=set(blocking_nets),
            blocking_components=blocking_components,
            reason=analysis.root_cause.value if analysis else "No path found",
            failure_cause=analysis.root_cause if analysis else FailureCause.UNKNOWN,
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

        # Issue #1603: Sub-grid escape pre-pass for off-grid pads
        escape_routes = self._run_subgrid_prepass()

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

        return list(escape_routes) + result.routes

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

        flush_print("  Board characteristics:")
        flush_print(f"    Pads: {characteristics.total_pads}")
        flush_print(f"    Nets: {characteristics.total_nets}")
        flush_print(f"    Pin density: {characteristics.pin_density:.4f} pads/mm²")
        flush_print(f"    Avg net span: {characteristics.avg_net_span:.1f}mm")

        if method == "adaptive":
            # Use adaptive routing with dynamic cost adjustment
            flush_print(f"  Method: Adaptive (max {max_iterations} iterations)")
            adaptive_router = create_adaptive_router(
                self,
                max_iterations=max_iterations,
            )
            routes = adaptive_router()
        elif method == "quick":
            # Fast heuristic tuning
            params = quick_tune(characteristics)
            flush_print("  Method: Quick heuristic tuning")
            flush_print("  Tuned parameters:")
            flush_print(f"    Via cost: {params.via:.1f}")
            flush_print(f"    Turn cost: {params.turn:.1f}")
            flush_print(f"    Congestion cost: {params.congestion:.1f}")

            self.rules = params.apply_to_rules(self.rules)
            routes = self.route_all(progress_callback=progress_callback)
        else:
            # Full optimization
            flush_print(f"  Method: {method} optimization (max {max_iterations} iterations)")
            result = tune_parameters(
                self,
                max_iterations=max_iterations,
                method=method,
            )

            flush_print(f"  Tuning completed in {result.tuning_time_ms:.0f}ms")
            flush_print("  Best parameters:")
            flush_print(f"    Via cost: {result.params.via:.1f}")
            flush_print(f"    Turn cost: {result.params.turn:.1f}")
            flush_print(f"    Congestion cost: {result.params.congestion:.1f}")

            if result.quality:
                flush_print(f"  Quality score: {result.quality.score:.1f}")
                flush_print(f"    Completion: {result.quality.completion_rate * 100:.1f}%")
                flush_print(f"    Total vias: {result.quality.total_vias}")

            self.rules = result.params.apply_to_rules(self.rules)
            routes = self.route_all(progress_callback=progress_callback)

        flush_print("\n=== Tuned Routing Complete ===")
        flush_print(f"  Routes: {len(routes)}")
        flush_print(f"  Segments: {sum(len(r.segments) for r in routes)}")
        flush_print(f"  Vias: {sum(len(r.vias) for r in routes)}")

        return routes

    def route_all_negotiated(
        self,
        max_iterations: int = 10,
        initial_present_factor: float = 0.5,
        present_factor_increment: float = 0.5,
        history_increment: float = 1.0,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
        per_net_timeout: float | None = None,
        use_targeted_ripup: bool = False,
        max_ripups_per_net: int = 3,
        adaptive: bool = True,
        region_parallel: bool = False,
        partition_rows: int = 2,
        partition_cols: int = 2,
        max_parallel_workers: int = 4,
        batch_routing: bool = False,
        hierarchical: bool = False,
        neighborhood_stall_threshold: int = 2,
        neighborhood_max_attempts: int = 3,
        neighborhood_initial_radius: float = 1.0,
        neighborhood_escalation_factor: float = 2.0,
        ema_smoothing: bool = False,
        ema_alpha: float = 0.6,
        exponential_cost: bool = False,
        pres_fac_mult: float = 1.3,
        pres_fac_cap: float = 50.0,
        congestion_auto_tune: bool = False,
        hotset_only: bool = False,
        perturbation: bool = True,
    ) -> list[Route]:
        """Route all nets using PathFinder-style negotiated congestion.

        Args:
            max_iterations: Maximum number of rip-up and reroute iterations
            initial_present_factor: Initial congestion penalty factor
            present_factor_increment: Factor increase per iteration (used when adaptive=False)
            history_increment: Base history cost increment per iteration
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds. If reached, returns best partial result.
            per_net_timeout: Wall-clock timeout in seconds for each per-net A* search
                (Issue #1605). Prevents individual nets from monopolizing the router
                on dense grids. Set to None to disable. Default: None (disabled).
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
            neighborhood_stall_threshold: Number of consecutive stall iterations
                (0 overflow, unrouted nets, no progress) before activating
                neighborhood rip-up (Issue #2274). Default: 2.
            neighborhood_max_attempts: Maximum rip-up attempts per neighborhood
                rip-up activation (Issue #2274). Default: 3.
            neighborhood_initial_radius: Initial bounding-box expansion factor
                for neighborhood rip-up (Issue #2274). Default: 1.0.
            neighborhood_escalation_factor: Multiplier applied to radius on
                each consecutive stall (Issue #2274). Default: 2.0.
            ema_smoothing: If True, use EMA-smoothed per-cell present cost
                instead of raw ``factor * usage_count``.  Prevents bang-bang
                oscillation by smoothing cost transitions across iterations
                (Issue #2333). Default: False.
            ema_alpha: Weight of the new value in the EMA update
                (Issue #2333).  Default: 0.6 (60% new, 40% previous).
            exponential_cost: If True, use exponential present cost
                escalation (OrthoRoute-style) instead of the default
                linear ramp.  More aggressively forces nets away from
                congested areas in later iterations (Issue #2333).
                Default: False.
            pres_fac_mult: Multiplicative factor per iteration in
                exponential cost mode (Issue #2333). Default: 1.3.
            pres_fac_cap: Maximum present cost factor in exponential
                mode (Issue #2333). Default: 50.0.
            congestion_auto_tune: If True, dynamically adjust
                ``pres_fac_mult`` and ``history_increment`` based on the
                actual congestion ratio each iteration (Issue #2333).
                Default: False.
            hotset_only: If True, each rip-up iteration only reroutes
                nets identified by ``find_nets_through_overused_cells``,
                skipping neighborhood rip-up and full-reorder fallbacks.
                Produces faster, more predictable iterations at the cost
                of potentially slower convergence (Issue #2333).
                Default: False.
            perturbation: If True (default), enable stochastic cost perturbation
                when oscillation is detected (Issue #2334). Adds random noise to
                per-net priority scores to break symmetry and escape local minima.

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
                progress_callback=progress_callback,
                timeout=timeout,
            )

        # Issue #1603: Sub-grid escape pre-pass for off-grid pads
        escape_routes = self._run_subgrid_prepass()

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
        if ema_smoothing:
            print(f"  EMA smoothing: enabled (alpha={ema_alpha})")
        if exponential_cost:
            print(f"  Exponential cost: enabled (mult={pres_fac_mult}, cap={pres_fac_cap})")
        if congestion_auto_tune:
            print("  Congestion auto-tune: enabled")
        if hotset_only:
            print("  Hotset-only reroute: enabled")
        if timeout:
            print(f"  Timeout: {timeout}s")
        if per_net_timeout:
            print(f"  Per-net timeout: {per_net_timeout}s")

        # Track overflow history for adaptive mode (Issue #633)
        overflow_history: list[int] = []
        escape_strategy_index = 0

        # Issue #2334: Stochastic perturbation state tracking.
        # perturbation_stagnation_count tracks how many iterations the
        # overflow has not improved, used to scale perturbation magnitude.
        perturbation_stagnation_count = 0
        perturbation_best_overflow: int | None = None
        # Ensure perturbation is reset at the start of each routing call
        self._reset_perturbation()

        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        # Issue #1295: Filter out pour nets before negotiated routing
        net_order = self._filter_pour_nets(net_order)
        net_order = [n for n in net_order if n != 0]

        # Issue #2432: Detect charlieplex/matrix topology and assign
        # alternating layer preferences to break circular blocking.
        self._detect_and_apply_matrix_preferences(net_order)
        # Re-sort after matrix priority boost
        net_order = sorted(net_order, key=lambda n: self._get_net_priority(n))

        total_nets = len(net_order)

        neg_router = NegotiatedRouter(
            self.grid, self.router, self.rules, self.net_class_map,
            congestion_estimator=self._ensure_congestion_estimator(),
        )

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
                    self.grid, self.router, self.rules, self.net_class_map,
                    congestion_estimator=self._ensure_congestion_estimator(),
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
        # Issue #2295: Per-net rip-up stall tracking.  For each net that
        # passes through overused cells, count consecutive rip-up iterations
        # where the net's overflow contribution did not improve.  Nets that
        # stall for ``max_net_stall_iterations`` consecutive rip-ups are
        # excluded from future rip-up sets to avoid wasting time on
        # high-pad-count nets (e.g., 15-16 pad decoupling nets) that cannot
        # resolve their overflow.
        max_net_stall_iterations = 3
        net_ripup_stall: dict[int, int] = {}  # net_id -> consecutive stall count
        net_prev_overflow: dict[int, int] = {}  # net_id -> overflow last time it was ripped up
        stalled_nets: set[int] = set()  # nets excluded from rip-up
        # Issue #2274: Track consecutive stalls for neighborhood rip-up escalation
        neighborhood_stall_count = 0
        prev_routed_count = 0
        full_reorder_used_this_iter = False
        for net in net_order:
            if net in self.nets:
                pads_for_routing = self.nets[net]
                if len(pads_for_routing) >= 2:
                    # Issue #2401: Substitute escaped pads with virtual pads
                    # at escape endpoints.
                    pads_by_net[net] = [
                        self._escape_pad_overrides.get(p, self.pads[p])
                        for p in pads_for_routing
                    ]

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
                return self._route_net_negotiated(net, pf, per_net_timeout=per_net_timeout)

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

                routes = self._route_net_negotiated(
                    net, present_factor, per_net_timeout=per_net_timeout
                )
                if routes:
                    net_routes[net] = routes
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        self.routes.append(route)
                    # Issue #2275: Update layer fill ratios after each net
                    if hasattr(self.router, "update_layer_fill_ratios"):
                        self.router.update_layer_fill_ratios()

        overflow = self.grid.get_total_overflow()
        overused = self.grid.find_overused_cells()
        overflow_history.append(overflow)  # Track for adaptive mode
        perturbation_best_overflow = overflow  # Issue #2334: baseline for perturbation
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

        # Issue #1605: Collect nets that are structurally unroutable (off-grid pads)
        # These nets cannot be resolved by rip-up iterations and must be excluded
        # from the recovery loop to avoid futile full-rip-up fallbacks.
        off_grid_nets: set[int] = {
            f.net for f in self.routing_failures if f.reason.startswith("PADS_OFF_GRID")
        }
        if off_grid_nets:
            off_grid_names = [self.net_names.get(n, f"Net {n}") for n in off_grid_nets]
            print(
                f"  Excluding {len(off_grid_nets)} structurally unroutable net(s) "
                f"from rip-up: {', '.join(off_grid_names)}"
            )

        # Skip iteration loop if already timed out
        if not timed_out:
            for iteration in range(1, max_iterations + 1):
                full_reorder_used_this_iter = False
                if check_timeout():
                    print(f"\n  ⚠ Timeout reached at iteration {iteration} ({elapsed_str()})")
                    timed_out = True
                    break

                # Adaptive early termination check (Issue #633)
                # Issue #2334: When perturbation is enabled, activate it
                # instead of terminating early. Only terminate if
                # perturbation has already been active for 3+ iterations
                # without finding a new best.
                unrouted = total_nets - len(net_routes)
                if adaptive and should_terminate_early(overflow_history, iteration, unrouted_count=unrouted):
                    if perturbation and perturbation_stagnation_count < 3:
                        # Activate perturbation instead of terminating
                        perturbation_stagnation_count += 1
                        self._activate_perturbation(perturbation_stagnation_count)
                        flush_print(
                            f"  Perturbation activated (magnitude={self._perturbation_magnitude:.2f}, "
                            f"stagnation={perturbation_stagnation_count}) ({elapsed_str()})"
                        )
                    else:
                        print(f"\n  ⚠ Early termination: no progress detected ({elapsed_str()})")
                        print(f"    Overflow history: {overflow_history[-5:]}")
                        self._reset_perturbation()
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

                    # Issue #2333: Congestion-ratio-based auto-tuning
                    iter_pres_fac_mult = pres_fac_mult
                    iter_history_increment = history_increment
                    if congestion_auto_tune:
                        iter_pres_fac_mult, iter_history_increment = (
                            calculate_congestion_tuned_params(
                                overflow_ratio, pres_fac_mult, history_increment
                            )
                        )

                    # Adaptive history increment based on convergence progress
                    adaptive_history = calculate_history_increment(
                        iteration, overflow_history, iter_history_increment
                    )
                    # Adaptive present cost based on iteration and congestion
                    present_factor = calculate_present_cost(
                        iteration, max_iterations, overflow_ratio, initial_present_factor,
                        exponential=exponential_cost,
                        pres_fac_mult=iter_pres_fac_mult,
                        pres_fac_cap=pres_fac_cap,
                    )
                    print(
                        f"  Adaptive params: history={adaptive_history:.2f}, present={present_factor:.2f}"
                    )
                    self.grid.update_history_costs(adaptive_history)

                    # Issue #2333: EMA smoothing of per-cell present cost
                    if ema_smoothing:
                        self.grid.update_present_cost_ema(present_factor, alpha=ema_alpha)
                else:
                    present_factor += present_factor_increment
                    self.grid.update_history_costs(history_increment)

                    # Issue #2333: EMA smoothing in non-adaptive mode
                    if ema_smoothing:
                        self.grid.update_present_cost_ema(present_factor, alpha=ema_alpha)

                # Issue #2334: Re-sort net order when perturbation is active
                # so the rip-up iterations explore different orderings.
                if self._perturbation_magnitude > 0:
                    net_order = sorted(
                        net_order, key=lambda n: self._get_net_priority(n)
                    )

                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)

                # Issue #858: Also include nets that completely failed to route
                # (not in net_routes) - these need recovery via targeted rip-up
                # Issue #1605: Exclude structurally unroutable nets (PADS_OFF_GRID)
                failed_nets_to_recover = [
                    n
                    for n in net_order
                    if n not in net_routes and n in pads_by_net and n not in off_grid_nets
                ]
                if failed_nets_to_recover:
                    # Add failed nets to reroute list if not already present
                    for failed_net in failed_nets_to_recover:
                        if failed_net not in nets_to_reroute:
                            nets_to_reroute.append(failed_net)
                    print(f"  Including {len(failed_nets_to_recover)} failed net(s) in recovery")

                # Issue #2295: Per-net rip-up stall filtering.
                # Track each net's overflow contribution across rip-up
                # iterations.  If a net has been ripped up N consecutive times
                # without its overflow improving, exclude it from future
                # rip-up sets.  This prevents high-pad-count decoupling nets
                # (e.g., C11-2 with 16 pads) from consuming ~200s per
                # iteration on fruitless A* searches.
                #
                # Issue #2396: Nets that completely failed to route (not in
                # net_routes) should not accumulate stall counts -- they
                # have no overflow to improve.  Only nets that ARE routed
                # but pass through overused cells should be stall-tracked.
                current_overflow = self.grid.get_total_overflow()
                failed_net_ids = set(failed_nets_to_recover)
                for net in nets_to_reroute:
                    if net in failed_net_ids:
                        # Issue #2396: Skip stall tracking for unrouted nets.
                        # They failed entirely and need fresh attempts, not
                        # stall-based exclusion.
                        continue
                    prev = net_prev_overflow.get(net)
                    if prev is not None:
                        if current_overflow >= prev:
                            # No improvement since last rip-up of this net
                            net_ripup_stall[net] = net_ripup_stall.get(net, 0) + 1
                        else:
                            # Overflow improved -- reset stall counter
                            net_ripup_stall[net] = 0
                    net_prev_overflow[net] = current_overflow

                # Filter out stalled nets
                newly_stalled = [
                    n for n in nets_to_reroute
                    if net_ripup_stall.get(n, 0) >= max_net_stall_iterations
                    and n not in stalled_nets
                ]
                if newly_stalled:
                    stalled_nets.update(newly_stalled)
                    stalled_names = [
                        self.net_names.get(n, f"Net_{n}") for n in newly_stalled
                    ]
                    flush_print(
                        f"  Excluding {len(newly_stalled)} stalled net(s) from rip-up: "
                        f"{', '.join(stalled_names)}"
                    )

                if stalled_nets:
                    nets_to_reroute = [
                        n for n in nets_to_reroute if n not in stalled_nets
                    ]

                # Issue #2396: When overflow is 0 but nets remain unrouted,
                # the rip-up loop has no overflow signal to act on.  Force
                # a fresh A* attempt for each unrouted net with an elevated
                # present_factor to encourage exploration of alternative
                # paths.  This directly addresses the 4L 3/8 plateau
                # observed in board 04.
                if (
                    current_overflow == 0
                    and failed_net_ids
                    and not timed_out
                ):
                    recovery_factor = max(present_factor * 2.0, initial_present_factor * 4.0)
                    recovered = 0
                    for fn in list(failed_net_ids):
                        if fn in stalled_nets or fn in net_routes:
                            continue
                        routes = self._route_net_negotiated(
                            fn, recovery_factor, per_net_timeout=per_net_timeout
                        )
                        if routes:
                            net_routes[fn] = routes
                            recovered += 1
                            for route in routes:
                                self.grid.mark_route_usage(route)
                                self.routes.append(route)
                    if recovered > 0:
                        flush_print(
                            f"  Zero-overflow recovery: routed {recovered}/{len(failed_net_ids)} "
                            f"previously-failed net(s) with elevated cost"
                        )
                        # Recompute overflow after recovery
                        current_overflow = self.grid.get_total_overflow()
                        overused = self.grid.find_overused_cells()

                # If overflow improves globally, give stalled nets another
                # chance -- their blockage may have cleared.
                if (
                    stalled_nets
                    and len(overflow_history) >= 2
                    and overflow_history[-1] < overflow_history[-2]
                ):
                    flush_print(
                        f"  Re-enabling {len(stalled_nets)} stalled net(s) after overflow improvement"
                    )
                    stalled_nets.clear()
                    net_ripup_stall.clear()

                # Issue #2413: Early termination when no nets remain to rip up.
                # All conflicting nets have been excluded by the stall detector,
                # so further iterations cannot make progress.
                if not nets_to_reroute:
                    flush_print(
                        f"  No nets to rip up, terminating at iteration "
                        f"{iteration}/{max_iterations} ({elapsed_str()})"
                    )
                    break

                # Issue #2388: Early-abort heuristic for power-net stalls.
                # If every currently stalled net is a power/pour net, AND
                # overflow has been flat for at least 2 iterations, the
                # negotiated loop cannot make further progress (the residual
                # congestion comes entirely from power nets the router has
                # already given up rerouting).  Bail out with a clear
                # diagnostic so the CLI can surface actionable suggestions
                # (e.g. --power-nets, --auto-layers) instead of spinning
                # until --timeout.
                if (
                    stalled_nets
                    and len(overflow_history) >= 2
                    and overflow_history[-1] == overflow_history[-2]
                    and all(
                        self._is_pour_net(n) or self._is_power_net_by_class(n)
                        for n in stalled_nets
                    )
                ):
                    stall_names = sorted(
                        self.net_names.get(n, f"Net_{n}") for n in stalled_nets
                    )
                    self.power_stall_abort = True
                    self.power_stall_nets = stall_names
                    flush_print(
                        f"\n  Power-net stall detected: {len(stalled_nets)} stalled "
                        f"net(s) are all power/pour nets and overflow has plateaued "
                        f"at {overflow_history[-1]}."
                    )
                    flush_print(
                        f"  Stalled nets: {', '.join(stall_names)}"
                    )
                    flush_print(
                        "  Aborting iteration loop to avoid spin-wait. "
                        "Try --auto-layers (dedicated planes) or "
                        "--power-nets (copper zones) to resolve this."
                    )
                    break

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

                        net_name = self.net_names.get(failed_net, f"Net_{failed_net}")
                        flush_print(
                            f"    Re-routing net {i + 1}/{len(nets_to_reroute)}: {net_name}... ({elapsed_str()})"
                        )

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
                                per_net_timeout=per_net_timeout,
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
                            if routed_nets and iteration <= 2 and not full_reorder_used_this_iter and not hotset_only:  # Try in first few iterations
                                print(
                                    f"    No direct blockers found - trying full reorder for net {failed_net}"
                                )
                                # Rip up all routed nets
                                neg_router.rip_up_nets(routed_nets, net_routes, self.routes)

                                # Route the failed net first (it now has priority)
                                routes = self._route_net_negotiated(
                                    failed_net,
                                    present_factor,
                                    per_net_timeout=per_net_timeout,
                                )
                                if routes:
                                    net_routes[failed_net] = routes
                                    for route in routes:
                                        self.grid.mark_route_usage(route)
                                        self.routes.append(route)
                                    targeted_ripup_count += 1
                                    # Issue #2275: Update layer fill ratios
                                    if hasattr(self.router, "update_layer_fill_ratios"):
                                        self.router.update_layer_fill_ratios()

                                # Always re-route the other nets (even if failed net didn't route)
                                for other_net in routed_nets:
                                    other_routes = self._route_net_negotiated(
                                        other_net,
                                        present_factor,
                                        per_net_timeout=per_net_timeout,
                                    )
                                    if other_routes:
                                        net_routes[other_net] = other_routes
                                        for route in other_routes:
                                            self.grid.mark_route_usage(route)
                                            self.routes.append(route)
                                        # Issue #2275: Update layer fill ratios
                                        if hasattr(self.router, "update_layer_fill_ratios"):
                                            self.router.update_layer_fill_ratios()
                                full_reorder_used_this_iter = True
                            else:
                                # Fallback: try regular reroute
                                routes = self._route_net_negotiated(
                                    failed_net,
                                    present_factor,
                                    per_net_timeout=per_net_timeout,
                                )
                                if routes:
                                    net_routes[failed_net] = routes
                                    targeted_ripup_count += 1
                                    for route in routes:
                                        self.grid.mark_route_usage(route)
                                        self.routes.append(route)
                                    # Issue #2275: Update layer fill ratios
                                    if hasattr(self.router, "update_layer_fill_ratios"):
                                        self.router.update_layer_fill_ratios()

                    if timed_out:
                        break

                    overflow = self.grid.get_total_overflow()
                    overused = self.grid.find_overused_cells()
                    # Track overflow for both branches (Issue #633)
                    overflow_history.append(overflow)

                    # Issue #2334: Reset perturbation when overflow improves
                    if perturbation and perturbation_best_overflow is not None:
                        if overflow < perturbation_best_overflow:
                            if self._perturbation_magnitude > 0:
                                flush_print(
                                    f"  Perturbation reset: overflow improved "
                                    f"{perturbation_best_overflow} → {overflow} ({elapsed_str()})"
                                )
                            perturbation_stagnation_count = 0
                            self._reset_perturbation()
                            perturbation_best_overflow = overflow
                        # Update best even when perturbation is inactive
                        elif overflow == perturbation_best_overflow:
                            pass  # No change
                    if perturbation_best_overflow is None or overflow < perturbation_best_overflow:
                        perturbation_best_overflow = overflow

                    flush_print(
                        f"  Targeted rip-up resolved {targeted_ripup_count}/{len(nets_to_reroute)} nets, "
                        f"overflow: {overflow} ({elapsed_str()})"
                    )

                    # Check for convergence in targeted mode
                    # Issue #858: Also check that all nets were routed
                    if overflow == 0 and len(net_routes) == total_nets:
                        self._reset_perturbation()
                        print(f"  Convergence achieved at iteration {iteration}!")
                        break

                    # Issue #2274: Neighborhood rip-up when stalled with 0
                    # overflow but unrouted nets remain.
                    still_unrouted_targeted = [
                        n for n in net_order
                        if (n not in net_routes or not net_routes.get(n))
                        and n in pads_by_net
                        and n not in off_grid_nets
                    ]
                    if overflow == 0 and still_unrouted_targeted and not timed_out and not hotset_only:
                        # Track stall progression
                        current_routed = len(net_routes)
                        if current_routed <= prev_routed_count:
                            neighborhood_stall_count += 1
                        else:
                            neighborhood_stall_count = 0
                        prev_routed_count = current_routed

                        if neighborhood_stall_count >= neighborhood_stall_threshold:
                            flush_print(
                                f"  Neighborhood rip-up: {len(still_unrouted_targeted)} "
                                f"net(s) stuck, stall #{neighborhood_stall_count} "
                                f"(radius escalation) ({elapsed_str()})"
                            )

                            def _mark_route_neighborhood(route: Route) -> None:
                                self._mark_route(route)

                            improved, new_count = neg_router.neighborhood_ripup(
                                failed_nets=still_unrouted_targeted,
                                net_routes=net_routes,
                                routes_list=self.routes,
                                pads_by_net=pads_by_net,
                                present_cost_factor=present_factor,
                                mark_route_callback=_mark_route_neighborhood,
                                stall_count=neighborhood_stall_count - neighborhood_stall_threshold,
                                per_net_timeout=per_net_timeout,
                                max_attempts=neighborhood_max_attempts,
                                initial_radius_factor=neighborhood_initial_radius,
                                escalation_factor=neighborhood_escalation_factor,
                                ripup_history=ripup_history,
                            )

                            overflow = self.grid.get_total_overflow()
                            overused = self.grid.find_overused_cells()
                            overflow_history[-1] = overflow

                            if improved:
                                flush_print(
                                    f"    Neighborhood rip-up routed {new_count - current_routed} "
                                    f"new net(s), total: {new_count}/{total_nets} ({elapsed_str()})"
                                )
                                neighborhood_stall_count = 0
                                prev_routed_count = new_count
                            else:
                                flush_print(
                                    f"    Neighborhood rip-up did not improve "
                                    f"({new_count}/{total_nets} nets) ({elapsed_str()})"
                                )

                            if overflow == 0 and len(net_routes) == total_nets:
                                print(f"  Convergence achieved at iteration {iteration}!")
                                break

                    # Adaptive oscillation detection for targeted mode (Issue #633)
                    # Guard: skip escape strategies when overflow is already 0 (#2262)
                    if adaptive and overflow > 0 and detect_oscillation(overflow_history):
                        # Issue #2334: Activate perturbation on oscillation to
                        # perturb net ordering for subsequent iterations.
                        if perturbation:
                            perturbation_stagnation_count += 1
                            self._activate_perturbation(perturbation_stagnation_count)
                            flush_print(
                                f"  Perturbation activated (magnitude={self._perturbation_magnitude:.2f}, "
                                f"stagnation={perturbation_stagnation_count}) ({elapsed_str()})"
                            )

                        print(f"  ⚠ Oscillation detected: {overflow_history[-4:]}")
                        print(f"    Attempting escape strategies starting from {escape_strategy_index + 1}...")

                        def mark_route_targeted(route: Route) -> None:
                            self._mark_route(route)

                        # Issue #2415: Compute escape budget from remaining time
                        escape_budget = None
                        if timeout is not None:
                            remaining = timeout - (time.time() - start_time)
                            escape_budget = min(60.0, remaining * 0.25)
                            if escape_budget <= 0:
                                escape_budget = 0.0

                        success, new_overflow, tried = neg_router.escape_local_minimum(
                            overflow_history=overflow_history,
                            net_routes=net_routes,
                            routes_list=self.routes,
                            pads_by_net=pads_by_net,
                            net_order=net_order,
                            present_cost_factor=present_factor,
                            mark_route_callback=mark_route_targeted,
                            strategy_index=escape_strategy_index,
                            per_net_timeout=per_net_timeout,
                            escape_budget=escape_budget,
                        )
                        escape_strategy_index += tried

                        if success:
                            print(f"    Escape successful! Overflow: {overflow} → {new_overflow}")
                            overflow = new_overflow
                            overflow_history[-1] = new_overflow
                            overused = self.grid.find_overused_cells()
                        else:
                            print(f"    All {tried} escape strategies exhausted without improvement")

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
                            return self._route_net_negotiated(
                                net, pf, per_net_timeout=per_net_timeout
                            )

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

                            net_name = self.net_names.get(net, f"Net_{net}")
                            flush_print(
                                f"    Re-routing net {i + 1}/{len(nets_to_reroute)}: {net_name}... ({elapsed_str()})"
                            )

                            routes = self._route_net_negotiated(
                                net, present_factor, per_net_timeout=per_net_timeout
                            )
                            if routes:
                                net_routes[net] = routes
                                rerouted_count += 1
                                for route in routes:
                                    self.grid.mark_route_usage(route)
                                    self.routes.append(route)
                                # Issue #2275: Update layer fill ratios
                                if hasattr(self.router, "update_layer_fill_ratios"):
                                    self.router.update_layer_fill_ratios()

                        if timed_out:
                            break

                    overflow = self.grid.get_total_overflow()
                    overused = self.grid.find_overused_cells()
                    flush_print(
                        f"  Rerouted {rerouted_count}/{len(nets_to_reroute)} nets, overflow: {overflow} ({elapsed_str()})"
                    )
                    flush_print(
                        f"  Progress: {len(net_routes)}/{total_nets} nets routed total"
                    )

                    # Issue #2265: When overflow is 0 but nets remain unrouted,
                    # the standard rip-up path only re-attempts failed nets without
                    # clearing the routed nets that block them. Fall back to
                    # targeted rip-up to identify and displace blockers.
                    # Issue #2333: Skip fallbacks in hotset-only mode.
                    still_failed = [
                        n for n in net_order
                        if n not in net_routes and n in pads_by_net and n not in off_grid_nets
                    ]
                    if overflow == 0 and still_failed and not timed_out and not hotset_only:
                        flush_print(
                            f"  Stall detected: {len(still_failed)} net(s) unrouted with 0 overflow"
                            f" - engaging targeted rip-up fallback ({elapsed_str()})"
                        )
                        targeted_fallback_count = 0
                        for failed_net in still_failed:
                            if check_timeout():
                                timed_out = True
                                break
                            pads_for_net = pads_by_net.get(failed_net, [])
                            if len(pads_for_net) < 2:
                                continue
                            blocking_nets: set[int] = set()
                            for j in range(len(pads_for_net) - 1):
                                blockers = neg_router.find_blocking_nets_for_connection(
                                    pads_for_net[j], pads_for_net[j + 1]
                                )
                                blocking_nets.update(blockers)
                            if blocking_nets:
                                def _mark_route_fallback(route: Route) -> None:
                                    self._mark_route(route)

                                success = neg_router.targeted_ripup(
                                    failed_net=failed_net,
                                    blocking_nets=blocking_nets,
                                    net_routes=net_routes,
                                    routes_list=self.routes,
                                    pads_by_net=pads_by_net,
                                    present_cost_factor=present_factor,
                                    mark_route_callback=_mark_route_fallback,
                                    ripup_history=ripup_history,
                                    max_ripups_per_net=max_ripups_per_net,
                                    per_net_timeout=per_net_timeout,
                                )
                                if success:
                                    targeted_fallback_count += 1
                        overflow = self.grid.get_total_overflow()
                        overused = self.grid.find_overused_cells()
                        flush_print(
                            f"  Targeted fallback resolved {targeted_fallback_count}/{len(still_failed)} nets ({elapsed_str()})"
                        )

                    # Issue #2297: Neighborhood rip-up for standard path when
                    # targeted fallback was insufficient and nets remain stalled.
                    # Issue #2333: Skip in hotset-only mode.
                    still_unrouted_std = [
                        n for n in net_order
                        if (n not in net_routes or not net_routes.get(n))
                        and n in pads_by_net
                        and n not in off_grid_nets
                    ]
                    if overflow == 0 and still_unrouted_std and not timed_out and not hotset_only:
                        # Track stall progression
                        current_routed = len(net_routes)
                        if current_routed <= prev_routed_count:
                            neighborhood_stall_count += 1
                        else:
                            neighborhood_stall_count = 0
                        prev_routed_count = current_routed

                        if neighborhood_stall_count >= neighborhood_stall_threshold:
                            flush_print(
                                f"  Neighborhood rip-up: {len(still_unrouted_std)} "
                                f"net(s) stuck, stall #{neighborhood_stall_count} "
                                f"(radius escalation) ({elapsed_str()})"
                            )

                            def _mark_route_neighborhood_std(route: Route) -> None:
                                self._mark_route(route)

                            improved, new_count = neg_router.neighborhood_ripup(
                                failed_nets=still_unrouted_std,
                                net_routes=net_routes,
                                routes_list=self.routes,
                                pads_by_net=pads_by_net,
                                present_cost_factor=present_factor,
                                mark_route_callback=_mark_route_neighborhood_std,
                                stall_count=neighborhood_stall_count - neighborhood_stall_threshold,
                                per_net_timeout=per_net_timeout,
                                max_attempts=neighborhood_max_attempts,
                                initial_radius_factor=neighborhood_initial_radius,
                                escalation_factor=neighborhood_escalation_factor,
                                ripup_history=ripup_history,
                            )

                            overflow = self.grid.get_total_overflow()
                            overused = self.grid.find_overused_cells()

                            if improved:
                                flush_print(
                                    f"    Neighborhood rip-up routed {new_count - current_routed} "
                                    f"new net(s), total: {new_count}/{total_nets} ({elapsed_str()})"
                                )
                                neighborhood_stall_count = 0
                                prev_routed_count = new_count
                            else:
                                flush_print(
                                    f"    Neighborhood rip-up did not improve "
                                    f"({new_count}/{total_nets} nets) ({elapsed_str()})"
                                )

                            if overflow == 0 and len(net_routes) == total_nets:
                                print(f"  Convergence achieved at iteration {iteration}!")
                                break

                # Track overflow history for adaptive mode (Issue #633)
                overflow_history.append(overflow)

                # Issue #2334: Reset perturbation when overflow improves
                if perturbation and perturbation_best_overflow is not None:
                    if overflow < perturbation_best_overflow:
                        if self._perturbation_magnitude > 0:
                            flush_print(
                                f"  Perturbation reset: overflow improved "
                                f"{perturbation_best_overflow} → {overflow} ({elapsed_str()})"
                            )
                        perturbation_stagnation_count = 0
                        self._reset_perturbation()
                        perturbation_best_overflow = overflow
                if perturbation_best_overflow is None or overflow < perturbation_best_overflow:
                    perturbation_best_overflow = overflow

                # Issue #858: Also check that all nets were routed
                if overflow == 0 and len(net_routes) == total_nets:
                    self._reset_perturbation()
                    print(f"  Convergence achieved at iteration {iteration}!")
                    break

                # Adaptive oscillation detection and escape (Issue #633)
                # Guard: skip escape strategies when overflow is already 0 (#2262)
                if adaptive and overflow > 0 and detect_oscillation(overflow_history):
                    # Issue #2334: Activate perturbation on oscillation
                    if perturbation:
                        perturbation_stagnation_count += 1
                        self._activate_perturbation(perturbation_stagnation_count)
                        flush_print(
                            f"  Perturbation activated (magnitude={self._perturbation_magnitude:.2f}, "
                            f"stagnation={perturbation_stagnation_count}) ({elapsed_str()})"
                        )

                    print(f"  ⚠ Oscillation detected: {overflow_history[-4:]}")
                    print(f"    Attempting escape strategies starting from {escape_strategy_index + 1}...")

                    def mark_route(route: Route) -> None:
                        self._mark_route(route)

                    # Issue #2415: Compute escape budget from remaining time
                    escape_budget = None
                    if timeout is not None:
                        remaining = timeout - (time.time() - start_time)
                        escape_budget = min(60.0, remaining * 0.25)
                        if escape_budget <= 0:
                            escape_budget = 0.0

                    success, new_overflow, tried = neg_router.escape_local_minimum(
                        overflow_history=overflow_history,
                        net_routes=net_routes,
                        routes_list=self.routes,
                        pads_by_net=pads_by_net,
                        net_order=net_order,
                        present_cost_factor=present_factor,
                        mark_route_callback=mark_route,
                        strategy_index=escape_strategy_index,
                        per_net_timeout=per_net_timeout,
                        escape_budget=escape_budget,
                    )
                    escape_strategy_index += tried

                    if success:
                        print(f"    Escape successful! Overflow: {overflow} → {new_overflow}")
                        overflow = new_overflow
                        overflow_history[-1] = new_overflow  # Update last entry
                        overused = self.grid.find_overused_cells()
                    else:
                        print(f"    All {tried} escape strategies exhausted without improvement")
                        # Continue to next iteration with different parameters

        # Issue #2334: Always reset perturbation at end of routing
        self._reset_perturbation()

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

        # Issue #1666: Post-route seg-seg clearance correction pass.
        # After the negotiated loop converges, validate all routes for
        # segment-to-segment clearance violations that slipped through
        # grid-level checks due to quantization.  For each violation,
        # rip up both offending nets and reroute them with the current
        # (tightened) blocking radius so they settle into violation-free
        # positions.
        if not timed_out and successful_nets > 0:
            corrected = self._post_route_clearance_correction(
                net_routes=net_routes,
                pads_by_net=pads_by_net,
                present_factor=present_factor,
                per_net_timeout=per_net_timeout,
            )
            if corrected > 0:
                total_elapsed = time.time() - start_time
                flush_print(
                    f"\n  Post-route clearance correction rerouted {corrected} net(s) "
                    f"({total_elapsed:.1f}s)"
                )

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

    def _route_net_negotiated(
        self,
        net: int,
        present_cost_factor: float,
        per_net_timeout: float | None = None,
    ) -> list[Route]:
        """Route a single net in negotiated mode.

        Args:
            net: Net ID to route
            present_cost_factor: Congestion cost factor
            per_net_timeout: Optional wall-clock timeout in seconds for each
                A* search within this net (Issue #1605)
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

        # Handle block-internal connections (Issue #1587)
        block_routes, block_connected = self._create_block_internal_routes(net, pads)
        for route in block_routes:
            self._mark_route(route)
            routes.append(route)
        connected_indices |= block_connected

        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        # Issue #2401: Substitute escaped pads with virtual pads at escape
        # endpoints so RSMT + A* route between escape endpoints, not original
        # pad centers.
        pad_objs = [
            self._escape_pad_overrides.get(p, self.pads[p])
            for p in pads_for_routing
        ]
        neg_router = NegotiatedRouter(
            self.grid, self.router, self.rules, self.net_class_map,
            congestion_estimator=self._ensure_congestion_estimator(),
        )

        def mark_route(route: Route):
            self._mark_route(route)

        new_routes = neg_router.route_net_negotiated(
            pad_objs,
            present_cost_factor,
            mark_route,
            per_net_timeout=per_net_timeout,
        )
        routes.extend(new_routes)
        return routes

    def _post_route_clearance_correction(
        self,
        net_routes: dict[int, list[Route]],
        pads_by_net: dict[int, list[Pad]] | None = None,
        present_factor: float = 0.5,
        per_net_timeout: float | None = None,
        max_correction_passes: int = 3,
    ) -> int:
        """Rip-up and reroute nets involved in seg-seg clearance violations.

        Issue #1666: After the negotiated loop converges, grid quantization
        can leave segment-to-segment distances slightly below the required
        clearance.  This method detects those violations using
        ``validate_routes()`` from ``io.py`` and selectively rip-up /
        reroute the offending nets so they settle into positions that
        satisfy the world-coordinate clearance constraint.

        Issue #1783: After each net is rerouted, re-validate it against all
        already-placed routes to catch violations introduced by sequential
        rerouting.  This prevents Net B's new position from creating a new
        violation with Net A that goes undetected.

        Args:
            net_routes: Mapping of net ID to its current routes.
            pads_by_net: (Unused, kept for backward compatibility.)
            present_factor: Congestion cost factor for rerouting.
            per_net_timeout: Optional per-net A* timeout.
            max_correction_passes: Maximum correction iterations (default 3).

        Returns:
            Total number of nets that were rerouted across all passes.
        """
        from .io import validate_routes

        total_corrected = 0

        for pass_idx in range(max_correction_passes):
            violations = validate_routes(self)

            # Only consider segment-to-segment violations
            seg_violations = [v for v in violations if v.obstacle_type == "segment"]
            if not seg_violations:
                break

            # Collect unique nets involved in violations
            violating_nets: set[int] = set()
            for v in seg_violations:
                violating_nets.add(v.net)
                violating_nets.add(v.obstacle_net)

            # Issue #1798: Categorise violations by layer to identify
            # inner-layer congestion that needs stronger repulsion.
            inner_layer_violations = [
                v for v in seg_violations
                if getattr(v, "layer", None) is not None
                and v.layer not in (Layer.F_CU, Layer.B_CU)
            ]

            # Collect the distinct layer names for the log message.
            violation_layers: set[str] = set()
            for v in seg_violations:
                v_layer = getattr(v, "layer", None)
                if v_layer is not None:
                    violation_layers.add(v_layer.kicad_name)

            layer_info = ", ".join(sorted(violation_layers)) if violation_layers else "unknown"
            flush_print(
                f"\n--- Post-route clearance pass {pass_idx + 1}: "
                f"{len(seg_violations)} seg-seg violation(s) across "
                f"{len(violating_nets)} net(s) "
                f"[layers: {layer_info}] ---"
            )

            # Nets with inner-layer violations need a higher congestion
            # penalty so the rerouter pushes traces further apart on the
            # typically more congested inner layers.
            inner_violating_nets: set[int] = set()
            for v in inner_layer_violations:
                inner_violating_nets.add(v.net)
                inner_violating_nets.add(v.obstacle_net)

            neg_router = NegotiatedRouter(
                self.grid, self.router, self.rules, self.net_class_map,
                congestion_estimator=self._ensure_congestion_estimator(),
            )

            # Rip up all violating nets
            nets_to_reroute = [n for n in violating_nets if n in net_routes]
            neg_router.rip_up_nets(nets_to_reroute, net_routes, self.routes)

            # Reroute them one at a time, re-validating after each net to
            # catch violations introduced by sequential placement (Issue #1783).
            rerouted_count = 0
            for net_idx, net in enumerate(nets_to_reroute):
                # Issue #1798: Use a higher present_factor for nets that had
                # inner-layer violations, encouraging wider spacing.
                net_pf = present_factor * 2.0 if net in inner_violating_nets else present_factor
                routes = self._route_net_negotiated(
                    net, net_pf, per_net_timeout=per_net_timeout
                )
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        # Issue #1694: Use mark_route() (not mark_route_usage())
                        # so rerouted nets block the correct width-aware envelope
                        # on the grid, preventing subsequent reroutes from landing
                        # too close to wider traces.
                        self.grid.mark_route(route)
                        self.routes.append(route)

                    # Issue #1783: After placing this net, check if it violates
                    # clearance against already-placed routes.  If so, rip it up
                    # and reroute once more with updated grid state.
                    if net_idx < len(nets_to_reroute) - 1:
                        post_violations = validate_routes(self)
                        net_seg_violations = [
                            v
                            for v in post_violations
                            if v.obstacle_type == "segment"
                            and (v.net == net or v.obstacle_net == net)
                        ]
                        if net_seg_violations:
                            # Rip up just this net and try again.
                            # Issue #1798: Use a stronger factor for
                            # inner-layer nets where congestion is tighter.
                            retry_pf = (
                                present_factor * 3.0
                                if net in inner_violating_nets
                                else present_factor * 1.5
                            )
                            neg_router.rip_up_nets([net], net_routes, self.routes)
                            retry_routes = self._route_net_negotiated(
                                net, retry_pf, per_net_timeout=per_net_timeout
                            )
                            if retry_routes:
                                net_routes[net] = retry_routes
                                for route in retry_routes:
                                    self.grid.mark_route(route)
                                    self.routes.append(route)

            total_corrected += rerouted_count
            flush_print(
                f"  Rerouted {rerouted_count}/{len(nets_to_reroute)} net(s)"
            )

            if rerouted_count == 0:
                # No progress -- stop trying
                break

        return total_corrected

    # =========================================================================
    # TWO-PHASE ROUTING (GLOBAL + DETAILED)
    # =========================================================================

    def _create_two_phase_router(self) -> TwoPhaseRouter:
        """Create a TwoPhaseRouter with access to Autorouter state."""
        return TwoPhaseRouter(
            grid=self.grid,
            router=self.router,
            rules=self.rules,
            net_class_map=self.net_class_map,
            nets=self.nets,
            net_names=self.net_names,
            pads=self.pads,
            routes=self.routes,
            routing_failures=self.routing_failures,
            get_net_priority=self._get_net_priority,
            route_net=self.route_net,
            route_net_with_corridor=self._route_net_with_corridor,
            mark_route=self._mark_route,
            pour_nets_without_zones=self._pour_nets_without_zones,
        )

    def route_all_two_phase(
        self,
        use_negotiated: bool = True,
        corridor_width_factor: float = 2.0,
        corridor_penalty: float | None = None,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
        per_net_timeout: float | None = None,
        initial_routes: list[Route] | None = None,
        max_iterations: int = 20,
    ) -> list[Route]:
        """Route all nets using two-phase global+detailed routing.

        Phase 1 (Global): Use SparseRouter to find coarse paths and reserve
        corridors for each net.

        Phase 2 (Detailed): Use grid-based routing with corridor guidance.

        Args:
            use_negotiated: Use negotiated congestion routing in detailed phase
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            corridor_penalty: Cost penalty for routing outside corridor.
                Defaults to ``self.rules.cost_corridor_deviation`` when *None*.
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds
            per_net_timeout: Optional wall-clock timeout per A* search
            initial_routes: Pre-existing routes (e.g. escape routes) to seed
                into the negotiated router's tracking dict.  These routes
                participate in rip-up/reroute so they are not permanently
                reserved on the grid (Issue #2294).
            max_iterations: Maximum rip-up-and-reroute iterations for the
                Phase 2 detailed negotiated routing loop (default: 20).

        Returns:
            List of routes (may be partial if timeout reached or some nets fail)
        """
        tp_router = self._create_two_phase_router()
        return tp_router.route_all(
            use_negotiated=use_negotiated,
            corridor_width_factor=corridor_width_factor,
            corridor_penalty=corridor_penalty,
            progress_callback=progress_callback,
            timeout=timeout,
            per_net_timeout=per_net_timeout,
            initial_routes=initial_routes,
            max_iterations=max_iterations,
        )

    def _route_net_with_corridor(
        self, net: int, present_cost_factor: float, per_net_timeout: float | None = None,
    ) -> list[Route]:
        """Route a single net with corridor-aware costs.

        This is similar to _route_net_negotiated but the pathfinder will
        use corridor costs from the grid when available.

        Args:
            net: Net number to route
            present_cost_factor: Multiplier for present sharing cost
            per_net_timeout: Optional wall-clock timeout per A* search
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

        # Handle block-internal connections (Issue #1587)
        block_routes, block_connected = self._create_block_internal_routes(net, pads)
        for route in block_routes:
            self._mark_route(route)
            routes.append(route)
        connected_indices |= block_connected

        pads_for_routing = reduce_pads_after_intra_ic(pads, connected_indices)
        if len(pads_for_routing) < 2:
            return routes

        # Issue #2401: Substitute escaped pads with virtual pads at escape
        # endpoints so RSMT + A* route between escape endpoints, not original
        # pad centers.
        pad_objs = [
            self._escape_pad_overrides.get(p, self.pads[p])
            for p in pads_for_routing
        ]
        neg_router = NegotiatedRouter(
            self.grid, self.router, self.rules, self.net_class_map,
            congestion_estimator=self._ensure_congestion_estimator(),
        )

        def mark_route(route: Route):
            self._mark_route(route)

        def record_failure(source_pad: Pad, target_pad: Pad):
            self._record_routing_failure(net, source_pad, target_pad)

        # Route with corridor-aware costs (negotiated router will pick up corridor costs)
        new_routes = neg_router.route_net_negotiated(
            pad_objs, present_cost_factor, mark_route,
            per_net_timeout=per_net_timeout,
            failure_callback=record_failure,
        )
        routes.extend(new_routes)
        return routes

    # =========================================================================
    # HIERARCHICAL ROUTING (Issue #1095 - Phase A)
    # =========================================================================

    def _create_hierarchical_router(self) -> HierarchicalRouter:
        """Create a HierarchicalRouter with access to Autorouter state."""
        return HierarchicalRouter(
            grid=self.grid,
            router=self.router,
            rules=self.rules,
            net_class_map=self.net_class_map,
            nets=self.nets,
            net_names=self.net_names,
            pads=self.pads,
            routes=self.routes,
            routing_failures=self.routing_failures,
            get_net_priority=self._get_net_priority,
            route_net=self.route_net,
            route_net_with_corridor=self._route_net_with_corridor,
            mark_route=self._mark_route,
            pour_nets_without_zones=self._pour_nets_without_zones,
        )

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
        for each net before performing detailed routing.

        Args:
            num_cols: Number of region columns for the RegionGraph (default: 10)
            num_rows: Number of region rows for the RegionGraph (default: 10)
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            use_negotiated: Use negotiated congestion routing in detailed phase
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of Route objects (may be partial if timeout reached)
        """
        h_router = self._create_hierarchical_router()
        return h_router.route_all(
            num_cols=num_cols,
            num_rows=num_rows,
            corridor_width_factor=corridor_width_factor,
            use_negotiated=use_negotiated,
            progress_callback=progress_callback,
            timeout=timeout,
        )

    def _reset_for_new_trial(self):
        """Reset the router to initial state for a new trial."""
        width, height = self.grid.width, self.grid.height
        origin_x, origin_y = self.grid.origin_x, self.grid.origin_y

        # Recreate grid and routers using shared helper
        # Issue #972: Helper includes adaptive grid resolution for large boards
        self.grid, self.router, self.zone_manager = self._create_grid_and_routers(
            width, height, origin_x, origin_y
        )

        # Issue #1778: Pass component pitch so fine-pitch pads get reduced clearance
        pitches = self.component_pitches
        for pad in self.pads.values():
            self.grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))
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
        from .algorithms.monte_carlo import run_monte_carlo

        return run_monte_carlo(
            autorouter=self,
            num_trials=num_trials,
            use_negotiated=use_negotiated,
            seed=seed,
            verbose=verbose,
            progress_callback=progress_callback,
            num_workers=num_workers,
        )

    def route_all_evolutionary(
        self,
        pop_size: int = 20,
        generations: int = 10,
        seed: int | None = None,
        verbose: bool = True,
        progress_callback: ProgressCallback | None = None,
        num_workers: int | None = None,
    ) -> list[Route]:
        """Route using evolutionary optimization with GA-style operators.

        Maintains a population of routing chromosomes encoding net ordering
        and per-net cost parameters, and evolves them over multiple
        generations using tournament selection, order crossover, and mutation.

        Args:
            pop_size: Population size per generation.
            generations: Number of evolutionary generations.
            seed: Random seed for reproducibility.
            verbose: Whether to print progress information.
            progress_callback: Optional callback for progress updates.
            num_workers: Number of parallel workers. None or 0 for auto-detection
                based on CPU count. 1 for sequential execution.

        Returns:
            List of routes from the best chromosome found.
        """
        from .algorithms.evolutionary import run_evolutionary

        return run_evolutionary(
            autorouter=self,
            pop_size=pop_size,
            generations=generations,
            seed=seed,
            verbose=verbose,
            progress_callback=progress_callback,
            num_workers=num_workers,
        )

    def route_all_block_aware(
        self,
        blocks: list[PCBBlock] | None = None,
        block_margin: float = 1.0,
        use_negotiated: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> list[Route]:
        """Route all nets using per-block detail routing with sub-Pathfinders.

        This strategy routes in three phases:
        - Phase A: For each registered block, create a BlockRouter with a
          confined sub-grid and route block-internal nets independently.
        - Phase B: Mark block-occupied regions on the main grid, then route
          inter-block and non-block nets via the standard routing pipeline.
        - Phase C: Combine block-internal and global routes.

        When no blocks are defined (or *blocks* is empty), falls back to
        ``route_all()`` for identical behavior on non-block designs.

        Args:
            blocks: Optional list of PCBBlocks to route. If None, uses
                ``self.registered_blocks``.
            block_margin: Extra margin around block bounding boxes for the
                sub-grid (mm). Default: 1.0.
            use_negotiated: Use negotiated congestion routing for inter-block
                nets. Default: False.
            progress_callback: Optional callback for progress updates.

        Returns:
            List of Route objects (block-internal + inter-block).
        """
        from .block_router import BlockRouter, BlockRoutingResult
        from .global_router import GlobalRouter
        from .region_graph import RegionGraph

        # Determine which blocks to route
        if blocks is not None:
            block_list = list(blocks)
        else:
            block_list = list(self.registered_blocks.values())

        # Fallback to flat routing when no blocks defined
        if not block_list:
            if use_negotiated:
                return self.route_all_negotiated(progress_callback=progress_callback)
            return self.route_all(progress_callback=progress_callback)

        flush_print("\n=== Block-Aware Routing ===")
        flush_print(f"  Blocks: {len(block_list)}")

        all_routes: list[Route] = []

        # Create a RegionGraph for inter-block routing guidance
        region_graph = RegionGraph(
            board_width=self.grid.width,
            board_height=self.grid.height,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_cols=10,
            num_rows=10,
        )
        region_graph.register_obstacles(list(self.pads.values()))

        # Phase A: Route each block's internal nets via BlockRouter
        block_results: list[BlockRoutingResult] = []
        globally_connected: set[tuple[str, str]] = set()
        all_inter_block_nets: set[int] = set()

        for i, block in enumerate(block_list):
            if progress_callback is not None:
                progress = i / (len(block_list) + 1)
                if not progress_callback(
                    progress,
                    f"Block routing: {block.block_id}",
                    True,
                ):
                    break

            flush_print(f"  Phase A: routing block '{block.block_id}'...")

            block_router = BlockRouter(
                block=block,
                rules=self.rules,
                net_class_map=self.net_class_map,
                layer_stack=self.layer_stack,
                margin=block_margin,
                force_python=self._force_python,
            )

            # Feed pads from main router into block router
            block_router.add_pads_from_autorouter(
                self.pads, self.nets, self.net_names
            )

            result = block_router.route_block()
            block_results.append(result)

            # Mark block-internal routes on the main grid so inter-block
            # routing respects them as obstacles.
            for route in result.routes:
                self._mark_route(route)
                self.routes.append(route)
            all_routes.extend(result.routes)
            globally_connected |= result.connected_pad_keys
            all_inter_block_nets |= result.inter_block_nets

            # Register block occupancy on the RegionGraph so Phase B
            # global routing avoids block interiors.
            min_x, min_y, max_x, max_y = block_router.bounds
            region_graph.register_block_occupancy(
                min_x, min_y, max_x, max_y,
                trace_count=len(result.routes),
            )

            flush_print(
                f"    Routed {len(result.routed_nets)} nets, "
                f"{len(result.routes)} routes, "
                f"{len(result.failed_nets)} failed, "
                f"{len(result.inter_block_nets)} inter-block"
            )

        # Phase B: Route inter-block and remaining nets on the main grid
        flush_print("  Phase B: routing inter-block / global nets...")

        # Build net ordering, excluding nets fully handled by block routing
        fully_routed_nets: set[int] = set()
        for br in block_results:
            for net_id in br.routed_nets:
                # A net is fully routed if ALL its pads were connected by block routing
                pad_keys = self.nets.get(net_id, [])
                if pad_keys and all(k in globally_connected for k in pad_keys):
                    fully_routed_nets.add(net_id)

        net_order = sorted(
            self.nets.keys(), key=lambda n: self._get_net_priority(n)
        )
        net_order = self._filter_pour_nets(net_order)
        nets_to_route = [
            n for n in net_order if n != 0 and n not in fully_routed_nets
        ]

        flush_print(
            f"    Skipping {len(fully_routed_nets)} fully block-routed nets"
        )
        flush_print(f"    Routing {len(nets_to_route)} remaining nets")
        if all_inter_block_nets:
            flush_print(
                f"    Inter-block nets (corridor routing): "
                f"{len(all_inter_block_nets)}"
            )

        # Issue #1654: Wire RegionGraph corridor costs into inter-block routing.
        # Use GlobalRouter to assign corridors for inter-block nets so that
        # detailed routing prefers paths through low-utilization regions
        # (corridors between blocks) rather than block interiors.
        corridor_width = 2.0 * self.rules.trace_clearance
        global_router = GlobalRouter(
            region_graph=region_graph,
            corridor_width=corridor_width,
            default_layer=0,
        )
        corridor_penalty = self.rules.cost_corridor_deviation
        corridor_assigned_nets: set[int] = set()

        inter_block_to_route = all_inter_block_nets & set(nets_to_route)
        for net in inter_block_to_route:
            pad_keys = self.nets.get(net, [])
            pad_positions = []
            for pk in pad_keys:
                pad_obj = self.pads.get(pk)
                if pad_obj is not None:
                    pad_positions.append((pad_obj.x, pad_obj.y))
            assignment = global_router.route_net(net, pad_positions)
            if assignment is not None:
                self.grid.set_corridor_preference(
                    assignment.corridor, net, corridor_penalty
                )
                corridor_assigned_nets.add(net)

        if corridor_assigned_nets:
            flush_print(
                f"    Corridor assignments: {len(corridor_assigned_nets)}/"
                f"{len(inter_block_to_route)} inter-block nets"
            )

        # Issue #1603: Sub-grid escape pre-pass for off-grid pads
        escape_routes = self._run_subgrid_prepass()
        all_routes.extend(escape_routes)

        total_remaining = len(nets_to_route)
        for i, net in enumerate(nets_to_route):
            if progress_callback is not None:
                progress = (len(block_list) + i) / (
                    len(block_list) + total_remaining
                )
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(
                    progress, f"Routing {net_name}", True
                ):
                    break

            if net in all_inter_block_nets:
                # Inter-block nets use corridor-aware routing so they
                # prefer paths through block ports rather than block
                # interiors.  The RegionGraph congestion from
                # register_block_occupancy() guides corridors away
                # from occupied block regions.
                routes = self._route_net_with_corridor(net, present_cost_factor=1.0)
            else:
                routes = self.route_net(net)
            all_routes.extend(routes)
            if routes:
                flush_print(
                    f"  Net {net}: {len(routes)} routes, "
                    f"{sum(len(r.segments) for r in routes)} segments"
                )

        # Clear corridor preferences after Phase B routing (Issue #1654)
        if corridor_assigned_nets:
            self.grid.clear_all_corridor_preferences()

        if progress_callback is not None:
            routed_count = len({r.net for r in all_routes})
            total = len([n for n in self.nets if n != 0])
            progress_callback(1.0, f"Routed {routed_count}/{total} nets", False)

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        return all_routes

    def _build_net_routes_map(self) -> dict[int, list[Route]]:
        """Build a net_id -> routes mapping from ``self.routes``.

        Used by ``_post_route_clearance_correction`` when the calling
        strategy does not maintain its own ``net_routes`` dictionary.
        """
        net_routes: dict[int, list[Route]] = {}
        for route in self.routes:
            net_routes.setdefault(route.net, []).append(route)
        return net_routes

    def route_all_advanced(
        self,
        monte_carlo_trials: int = 0,
        use_negotiated: bool = False,
        use_two_phase: bool = False,
        use_hierarchical: bool = False,
        use_multi_resolution: bool = False,
        use_block_aware: bool = False,
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
            use_multi_resolution: Use multi-resolution routing with fine-grid
                fallback for nets that fail on the coarse grid (Issue #1251).
            use_block_aware: Use per-block detail routing with sub-Pathfinders
                (Issue #1589). Routes each registered block's internal nets
                independently before global inter-block routing.
            progress_callback: Optional callback for progress updates

        Returns:
            List of routes

        Note:
            Priority order: block_aware > monte_carlo > multi_resolution >
            hierarchical > two_phase > negotiated > standard
        """
        if use_block_aware and self.registered_blocks:
            result = self.route_all_block_aware(
                use_negotiated=use_negotiated, progress_callback=progress_callback
            )
        elif monte_carlo_trials > 0:
            result = self.route_all_monte_carlo(
                monte_carlo_trials, use_negotiated, progress_callback=progress_callback
            )
        elif use_multi_resolution:
            result = self.route_all_multi_resolution(
                use_negotiated=use_negotiated,
                progress_callback=progress_callback,
            )
        elif use_hierarchical:
            result = self.route_all_hierarchical(
                use_negotiated=use_negotiated, progress_callback=progress_callback
            )
        elif use_two_phase:
            result = self.route_all_two_phase(
                use_negotiated=use_negotiated, progress_callback=progress_callback
            )
        elif use_negotiated:
            # Negotiated strategy already runs clearance correction internally;
            # skip the unified pass below to avoid double-correction.
            return self.route_all_negotiated(progress_callback=progress_callback)
        else:
            result = self.route_all(progress_callback=progress_callback)

        # Issue #1783: Run post-route clearance correction for ALL strategies.
        # Previously this only ran inside route_all_negotiated, leaving
        # monte-carlo, two-phase, hierarchical, block-aware, and plain
        # routing without seg-seg clearance verification.
        if result:
            import time

            start = time.time()
            net_routes = self._build_net_routes_map()
            corrected = self._post_route_clearance_correction(
                net_routes=net_routes,
                present_factor=0.5,
            )
            if corrected > 0:
                elapsed = time.time() - start
                flush_print(
                    f"\n  Post-route clearance correction rerouted {corrected} net(s) "
                    f"({elapsed:.1f}s)"
                )

        return result

    @staticmethod
    def _count_net_components(
        routes: list[Route],
        tolerance: float = 0.01,
    ) -> dict[int, int]:
        """Count connected components per net using union-find.

        Returns a mapping of net_id -> number of connected components
        formed by segment endpoints and via positions.
        """
        from .observability import _UnionFind, _pt

        routes_by_net: dict[int, list[Route]] = {}
        for r in routes:
            if r.net != 0:
                routes_by_net.setdefault(r.net, []).append(r)

        result: dict[int, int] = {}
        for net_id, net_routes in routes_by_net.items():
            uf = _UnionFind()
            all_points: set[tuple[float, float]] = set()

            for route in net_routes:
                for seg in route.segments:
                    p1 = _pt(seg.x1, seg.y1, tolerance)
                    p2 = _pt(seg.x2, seg.y2, tolerance)
                    uf.union(p1, p2)
                    all_points.add(p1)
                    all_points.add(p2)
                for via in route.vias:
                    vp = _pt(via.x, via.y, tolerance)
                    uf._ensure(vp)
                    all_points.add(vp)
                    # Union via with coincident segment endpoints
                    for seg in route.segments:
                        for sp in [
                            _pt(seg.x1, seg.y1, tolerance),
                            _pt(seg.x2, seg.y2, tolerance),
                        ]:
                            if sp == vp:
                                uf.union(vp, sp)

            roots = {uf.find(p) for p in all_points} if all_points else set()
            result[net_id] = len(roots)

        return result

    def cleanup_artifacts(
        self,
        oob_margin: float = 0.5,
    ) -> dict[str, int]:
        """Remove net-0 orphan traces and out-of-bounds segments/vias.

        This post-route cleanup pass removes artifacts left by intermediate
        routing strategies (escape routing, sub-grid pre-pass) that can
        produce segments with net=0 or endpoints outside the board outline.

        The cleanup is **connectivity-aware**: after removing artifacts it
        verifies that per-net connectivity (number of connected components)
        has not been degraded.  Any segments or vias whose removal would
        fragment a net are restored automatically.

        Args:
            oob_margin: Margin in mm beyond the board bounding box within
                which segments are still considered valid.  Default 0.5mm.

        Returns:
            Dictionary with cleanup statistics:
            - ``net0_routes_removed``: Routes with net==0 removed entirely.
            - ``net0_segments_removed``: Individual net-0 segments stripped
              from otherwise valid routes.
            - ``net0_vias_removed``: Individual net-0 vias stripped from
              otherwise valid routes.
            - ``oob_segments_removed``: Segments with both endpoints outside
              the board bounding box (plus margin).
            - ``oob_vias_removed``: Vias with center outside the board
              bounding box (plus margin).
            - ``segments_restored``: Segments restored to preserve
              connectivity.
            - ``vias_restored``: Vias restored to preserve connectivity.
        """
        stats: dict[str, int] = {
            "net0_routes_removed": 0,
            "net0_segments_removed": 0,
            "net0_vias_removed": 0,
            "oob_segments_removed": 0,
            "oob_vias_removed": 0,
            "segments_restored": 0,
            "vias_restored": 0,
        }

        # -- Snapshot pre-cleanup connectivity --
        pre_components = self._count_net_components(self.routes)

        # -- Step 1: Handle routes with net == 0 --
        # A Route may have net=0 while its child segments/vias carry
        # the correct net (Issue #2039).  Propagate the child net to
        # the Route instead of discarding valid routed traces.
        kept_routes: list[Route] = []
        for route in self.routes:
            if route.net == 0:
                child_nets = {s.net for s in route.segments if s.net != 0}
                child_nets |= {v.net for v in route.vias if v.net != 0}
                if child_nets:
                    # Adopt a valid child net -- all children typically
                    # share the same net so pick any.
                    route.net = child_nets.pop()
                    kept_routes.append(route)
                else:
                    stats["net0_routes_removed"] += 1
            else:
                kept_routes.append(route)
        self.routes = kept_routes

        # -- Step 2: Strip individual net-0 segments/vias inside valid routes --
        # Track removed items per route so we can restore them if needed.
        removed_net0_segs: dict[int, list[Segment]] = {}  # route index -> segs
        removed_net0_vias: dict[int, list[Via]] = {}
        for idx, route in enumerate(self.routes):
            orig_segs = route.segments[:]
            orig_vias = route.vias[:]
            route.segments = [s for s in route.segments if s.net != 0]
            route.vias = [v for v in route.vias if v.net != 0]
            dropped_segs = [s for s in orig_segs if s.net == 0]
            dropped_vias = [v for v in orig_vias if v.net == 0]
            if dropped_segs:
                removed_net0_segs[idx] = dropped_segs
            if dropped_vias:
                removed_net0_vias[idx] = dropped_vias
            stats["net0_segments_removed"] += len(dropped_segs)
            stats["net0_vias_removed"] += len(dropped_vias)

        # -- Step 3: Remove out-of-bounds segments and vias --
        # Use the board edge cuts bbox when available (Issue #2039).
        # Falls back to the grid origin/dimensions which may differ
        # from the physical board outline on non-rectangular boards or
        # when adaptive grid resolution is in effect.
        if self._board_bbox is not None:
            bb_min_x, bb_min_y, bb_max_x, bb_max_y = self._board_bbox
        else:
            bb_min_x = self.grid.origin_x
            bb_min_y = self.grid.origin_y
            bb_max_x = self.grid.origin_x + self.grid.width
            bb_max_y = self.grid.origin_y + self.grid.height
        min_x = bb_min_x - oob_margin
        min_y = bb_min_y - oob_margin
        max_x = bb_max_x + oob_margin
        max_y = bb_max_y + oob_margin

        removed_oob_segs: dict[int, list[Segment]] = {}
        removed_oob_vias: dict[int, list[Via]] = {}
        for idx, route in enumerate(self.routes):
            orig_seg_count = len(route.segments)
            orig_via_count = len(route.vias)

            # Keep segment if at least one endpoint is inside bounds
            # (bridges the board edge -- preserve to avoid breaking
            # legitimate near-edge traces).
            kept_segs = []
            oob_segs = []
            for seg in route.segments:
                p1_inside = min_x <= seg.x1 <= max_x and min_y <= seg.y1 <= max_y
                p2_inside = min_x <= seg.x2 <= max_x and min_y <= seg.y2 <= max_y
                if p1_inside or p2_inside:
                    kept_segs.append(seg)
                else:
                    oob_segs.append(seg)
            route.segments = kept_segs

            # Remove vias with center outside bounds
            kept_vias = []
            oob_vias_list = []
            for v in route.vias:
                if min_x <= v.x <= max_x and min_y <= v.y <= max_y:
                    kept_vias.append(v)
                else:
                    oob_vias_list.append(v)
            route.vias = kept_vias

            if oob_segs:
                removed_oob_segs[idx] = oob_segs
            if oob_vias_list:
                removed_oob_vias[idx] = oob_vias_list

            stats["oob_segments_removed"] += len(oob_segs)
            stats["oob_vias_removed"] += len(oob_vias_list)

        # -- Step 4: Connectivity-aware restoration --
        # Check whether cleanup fragmented any net.  If a net now has
        # more connected components than before, restore the removed
        # segments/vias for every route belonging to that net.
        post_components = self._count_net_components(self.routes)
        degraded_nets: set[int] = set()
        for net_id, pre_count in pre_components.items():
            post_count = post_components.get(net_id, 0)
            if post_count > pre_count:
                degraded_nets.add(net_id)

        if degraded_nets:
            for idx, route in enumerate(self.routes):
                if route.net not in degraded_nets:
                    continue
                # Restore net-0 segments removed in step 2
                if idx in removed_net0_segs:
                    restored = removed_net0_segs[idx]
                    # Adopt the route's net so they are no longer net-0
                    for seg in restored:
                        seg.net = route.net
                    route.segments.extend(restored)
                    stats["net0_segments_removed"] -= len(restored)
                    stats["segments_restored"] += len(restored)
                if idx in removed_net0_vias:
                    restored = removed_net0_vias[idx]
                    for via in restored:
                        via.net = route.net
                    route.vias.extend(restored)
                    stats["net0_vias_removed"] -= len(restored)
                    stats["vias_restored"] += len(restored)
                # Restore OOB segments removed in step 3
                if idx in removed_oob_segs:
                    restored = removed_oob_segs[idx]
                    route.segments.extend(restored)
                    stats["oob_segments_removed"] -= len(restored)
                    stats["segments_restored"] += len(restored)
                if idx in removed_oob_vias:
                    restored = removed_oob_vias[idx]
                    route.vias.extend(restored)
                    stats["oob_vias_removed"] -= len(restored)
                    stats["vias_restored"] += len(restored)

            if degraded_nets:
                flush_print(
                    f"  Cleanup: restored segments/vias for {len(degraded_nets)} "
                    f"net(s) to preserve connectivity"
                )

        # Store stats for retrieval by output module
        self._cleanup_stats = stats
        return stats

    def to_sexp(self, *, skip_cleanup: bool = False) -> str:
        """Generate KiCad S-expressions for all routes.

        Automatically runs ``cleanup_artifacts()`` before emitting to
        remove net-0 orphans and out-of-bounds segments, unless
        *skip_cleanup* is True.
        """
        if not skip_cleanup:
            self.cleanup_artifacts()
        return "\n\t".join(route.to_sexp() for route in self.routes)

    def get_statistics(self, nets_to_route_ids: set[int] | None = None) -> dict:
        """Get routing statistics including congestion metrics.

        Args:
            nets_to_route_ids: Optional set of net IDs that were targeted
                for routing (multi-pad signal nets).  When provided,
                ``nets_routed`` only counts nets in this set.

        When pad information is available (``self.pads`` and ``self.nets``),
        connectivity validation is performed so that ``nets_routed``
        reflects actual pad-to-pad connectivity rather than mere segment
        existence.
        """
        from .observability import compute_routing_statistics

        # Build net_pads mapping when pad data is available
        net_pads: dict[int, list] | None = None
        if self.pads and self.nets:
            net_pads = {}
            for net_id, pad_keys in self.nets.items():
                pad_list = [self.pads[k] for k in pad_keys if k in self.pads]
                if pad_list:
                    net_pads[net_id] = pad_list

        return compute_routing_statistics(
            routes=self.routes,
            grid=self.grid,
            layer_stats=self.get_layer_usage_statistics(),
            nets_to_route_ids=nets_to_route_ids,
            net_pads=net_pads,
        )

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
        from .observability import compute_layer_usage_statistics

        return compute_layer_usage_statistics(
            routes=self.routes,
            grid=self.grid,
            layer_stack=self.layer_stack,
        )

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
        from .length_tuning import apply_length_tuning as _apply

        self._update_length_tracker()
        return _apply(
            routes=self.routes,
            length_tracker=self._length_tracker,
            grid=self.grid,
            net_names=self.net_names,
            verbose=verbose,
        )

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
            self._escape_router = EscapeRouter(
                self.grid, self.rules, net_class_map=self.net_class_map,
                edge_clearance=self._edge_clearance,
                board_bounds=self._board_bbox,
            )
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

            # Issue #2401: Build virtual pads at escape endpoints so the
            # main routing pipeline routes between escape endpoints instead
            # of original pad centers.  Also mark escape nets as protected
            # so their stub segments are not ripped up.
            for escape in escapes:
                pad = escape.pad
                pad_key = (pad.ref, pad.pin)
                if pad_key in self.pads:
                    ep_x, ep_y = escape.escape_point
                    virtual_pad = Pad(
                        x=ep_x,
                        y=ep_y,
                        width=pad.width,
                        height=pad.height,
                        net=pad.net,
                        net_name=pad.net_name,
                        layer=escape.escape_layer,
                        ref=pad.ref,
                        pin=pad.pin,
                        through_hole=pad.through_hole,
                        drill=pad.drill,
                    )
                    self._escape_pad_overrides[pad_key] = virtual_pad

            print(
                f"  Escape routes: {package.ref} ({package.package_type.name})"
                f" - {len(escapes)} pins escaped"
            )

        if self._escape_pad_overrides:
            print(
                f"  Escape endpoint overrides: {len(self._escape_pad_overrides)} pads "
                f"remapped to escape endpoints"
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

        # Issue #2294: Sub-grid escape pre-pass for off-grid pads.
        # This must run before dense-package escape routing so that
        # off-grid pads (e.g. J2 connector pads that don't snap to the
        # routing grid) get escape segments that bridge them to the
        # nearest on-grid cell.  Without this, off-grid pads are
        # classified as PADS_OFF_GRID and excluded from rip-up recovery.
        subgrid_escapes = self._run_subgrid_prepass()

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
            # Issue #2401: Escape routes are now permanent infrastructure.
            # The main routing pipeline routes between escape endpoints
            # (virtual pads), not original pad centers, so escape stubs
            # naturally connect without blocking.  Do NOT pass escape
            # routes as initial_routes -- they must be preserved on the
            # grid as fixed segments.
            main_routes = self.route_all_two_phase(
                use_negotiated=True,
                corridor_width_factor=2.0,
                progress_callback=progress_callback,
                timeout=timeout,
            )
        else:
            main_routes = self.route_all(
                progress_callback=progress_callback,
            )

        # Combine results -- escape routes that survived rip-up are
        # already in main_routes (via self.routes), so only add
        # sub-grid escapes which are infrastructure, not net routes.
        all_routes = subgrid_escapes + main_routes

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
        """Lazy-initialize sub-grid router.

        Passes any configured fine zones (Issue #1828) so that escape
        routing uses fine-grid resolution for pads within dense IC packages
        instead of the coarse global grid.
        """
        if self._subgrid_router is None:
            self._subgrid_router = SubGridRouter(
                self.grid,
                self.rules,
                fine_zones=self.fine_zones if self.fine_zones else None,
            )
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

    def _run_subgrid_prepass(self) -> list[Route]:
        """Run sub-grid escape pre-pass for off-grid pads.

        Analyzes all pads, generates escape segments for any that fall
        between main grid points, and unblocks grid cells at escape
        endpoints. This is a no-op for boards with no off-grid pads.

        Issue #2330: When waypoint injection is enabled, the pathfinder
        handles off-grid pads directly via injected waypoint nodes in the
        A* search graph.  The sub-grid pre-pass is skipped entirely.

        Returns:
            List of escape Route objects (empty if no off-grid pads)

        Issue #1603: Wire sub-grid routing into default route_all pipeline.
        """
        # Issue #2330: Skip sub-grid pre-pass when waypoint injection is active
        if self.use_waypoint_injection:
            return []

        subgrid_result = self.prepare_subgrid_escapes()

        if not (subgrid_result.analysis and subgrid_result.analysis.has_off_grid_pads):
            return []

        flush_print(
            f"  Sub-grid pre-pass: {subgrid_result.success_count} escape segments "
            f"for {subgrid_result.analysis.off_grid_count} off-grid pads, "
            f"{subgrid_result.unblocked_count} cells unblocked"
        )

        if subgrid_result.failed_pads:
            failed_refs = sorted({p.ref for p in subgrid_result.failed_pads})
            flush_print(
                f"  Sub-grid pre-pass: {len(subgrid_result.failed_pads)} "
                f"pads could not escape (components: {', '.join(failed_refs)})"
            )

        escape_routes = self._subgrid.get_escape_routes(subgrid_result)
        for route in escape_routes:
            self._mark_route(route)
            self.routes.append(route)

        return escape_routes

    def _retry_net_with_subgrid(self, net: int) -> list[Route]:
        """Retry routing a net after applying sub-grid escapes for its pads.

        Called when route_net() fails with PIN_ACCESS for a specific net.
        Generates escape segments only for the failing net's pads, then
        retries routing.

        Args:
            net: Net ID to retry

        Returns:
            List of Route objects if retry succeeds, empty list otherwise

        Issue #1603: Sub-grid retry on PIN_ACCESS failure.
        """
        if net not in self.nets:
            return []

        # Get only this net's pads
        net_pad_keys = self.nets[net]
        net_pads = [self.pads[key] for key in net_pad_keys if key in self.pads]

        if len(net_pads) < 2:
            return []

        # Run sub-grid escape for just this net's pads
        subgrid = self._subgrid
        analysis = subgrid.analyze_pads(net_pads)

        if not analysis.has_off_grid_pads:
            return []

        result = subgrid.generate_escape_segments(analysis)
        subgrid.apply_escape_segments(result)

        if result.success_count == 0:
            return []

        net_name = self.net_names.get(net, f"Net {net}")
        flush_print(
            f"  Sub-grid retry for {net_name}: "
            f"{result.success_count} escapes, "
            f"{result.unblocked_count} cells unblocked"
        )

        # Collect escape routes
        escape_routes = subgrid.get_escape_routes(result)
        for route in escape_routes:
            self._mark_route(route)
            self.routes.append(route)

        # Remove the failure records for this net before retrying
        self.routing_failures = [f for f in self.routing_failures if f.net != net]

        # Retry routing
        retry_routes = self.route_net(net, _subgrid_retry=True)
        return escape_routes + retry_routes

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

    def route_with_adaptive_grid(
        self,
        use_negotiated: bool = True,
        fine_pitch_threshold: float = 0.8,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route with adaptive grid: fine grid near pads, coarse grid in channels.

        Two-phase routing strategy (Issue #1135):
        - Phase 1: Detect fine-pitch components and generate escape segments
          from off-grid pads to the nearest coarse-grid points
        - Phase 2: Route all nets on the coarse grid where every endpoint
          is guaranteed on-grid

        This achieves 100% pad reachability at coarse-grid routing speed.

        Args:
            use_negotiated: Use negotiated congestion routing for Phase 2
            fine_pitch_threshold: Pin pitch below this triggers fine-grid escape
            progress_callback: Optional progress callback
            timeout: Optional timeout in seconds

        Returns:
            List of all routes (escape segments + channel routes)

        Example::

            routes = router.route_with_adaptive_grid()
            stats = router.get_statistics()
        """
        from kicad_tools.cli.progress import flush_print

        flush_print("\n=== Adaptive Grid Routing (Fine Grid + Coarse Grid) ===")

        adaptive = AdaptiveGridRouter(
            grid=self.grid,
            rules=self.rules,
            router=self.router,
            fine_pitch_threshold=fine_pitch_threshold,
        )

        def _route_phase2() -> list[Route]:
            if use_negotiated:
                return self.route_all_negotiated(
                    progress_callback=progress_callback,
                    timeout=timeout,
                )
            else:
                return self.route_all(
                    progress_callback=progress_callback,
                )

        result = adaptive.route_adaptive(
            nets=self.nets,
            pads=self.pads,
            route_fn=_route_phase2,
        )

        # Add escape routes to our route list
        for route in result.escape_routes:
            self.routes.append(route)

        # Summary
        flush_print(f"\n{result.format_summary()}")

        stats = self.get_statistics()
        flush_print(f"\n=== Adaptive Grid Routing Complete ===")
        flush_print(f"  Total nets routed: {stats['nets_routed']}")
        flush_print(f"  Total segments: {stats['segments']}")
        flush_print(f"  Total vias: {stats['vias']}")

        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                flush_print(failure_summary)

        return result.all_routes

    # =========================================================================
    # MULTI-RESOLUTION ROUTING (Issue #1251)
    # =========================================================================

    def route_all_multi_resolution(
        self,
        fine_resolution_factor: float = 0.5,
        pin_order_trials: list[str] | None = None,
        use_negotiated: bool = True,
        max_iterations: int = 10,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets with multi-resolution fallback for failed nets.

        First routes all nets on the current (coarse) grid. Nets that fail
        are retried on a finer grid (2x resolution by default) scoped to the
        failed nets' bounding boxes to avoid memory explosion.

        Args:
            fine_resolution_factor: Cell size multiplier for the fine grid
                (0.5 = half cell size = 2x resolution). Default 0.5.
            pin_order_trials: List of pin orderings to try on the fine grid.
                Options: "default", "reversed", "shuffled".
                Default is ["default"].
            use_negotiated: Use negotiated congestion routing for the
                initial coarse pass. Default True.
            max_iterations: Max iterations for negotiated routing. Default 10.
            progress_callback: Optional callback for progress updates.
            timeout: Optional timeout in seconds for the entire operation.

        Returns:
            List of Route objects (coarse + fine grid results merged).

        Example:
            >>> router = Autorouter(100, 100)
            >>> # ... add components ...
            >>> routes = router.route_all_multi_resolution(
            ...     fine_resolution_factor=0.5,
            ...     pin_order_trials=["default", "reversed"],
            ... )
        """
        import time

        start_time = time.time()

        if pin_order_trials is None:
            pin_order_trials = ["default"]

        flush_print("\n=== Multi-Resolution Routing ===")
        flush_print(f"  Fine grid factor: {fine_resolution_factor}")
        flush_print(f"  Pin order trials: {pin_order_trials}")

        # --- Pass 1: Route on the current (coarse) grid ---
        flush_print("\n--- Pass 1: Coarse grid routing ---")
        if use_negotiated:
            self.route_all_negotiated(
                max_iterations=max_iterations,
                timeout=timeout,
                progress_callback=progress_callback,
                adaptive=True,
            )
        else:
            self.route_all(progress_callback=progress_callback)

        coarse_stats = self.get_statistics()
        coarse_routed = coarse_stats["nets_routed"]
        total_nets = len([n for n in self.nets if n > 0 and len(self.nets[n]) >= 2])
        flush_print(f"  Coarse pass: {coarse_routed}/{total_nets} nets routed")

        # Collect failed nets
        failed_net_ids = [f.net for f in self.routing_failures]

        if not failed_net_ids:
            flush_print("  All nets routed on coarse grid -- no fine-grid pass needed")
            return list(self.routes)

        flush_print(f"  {len(failed_net_ids)} net(s) failed on coarse grid")

        # Check timeout before fine-grid pass
        if timeout and (time.time() - start_time) >= timeout:
            flush_print("  Timeout reached -- skipping fine-grid pass")
            return list(self.routes)

        # --- Pass 2: Fine-grid retry for failed nets ---
        flush_print(f"\n--- Pass 2: Fine-grid retry ({len(failed_net_ids)} nets) ---")

        fine_resolution = self.grid.resolution * fine_resolution_factor
        flush_print(f"  Coarse resolution: {self.grid.resolution:.4f}mm")
        flush_print(f"  Fine resolution: {fine_resolution:.4f}mm")

        # Compute bounding box around all failed nets' pads (with padding)
        padding_mm = max(
            self.rules.trace_clearance * 4,
            self.grid.resolution * 10,
        )

        all_failed_pads: list[Pad] = []
        for net_id in failed_net_ids:
            if net_id not in self.nets:
                continue
            for pad_key in self.nets[net_id]:
                if pad_key in self.pads:
                    all_failed_pads.append(self.pads[pad_key])

        if not all_failed_pads:
            flush_print("  No pads found for failed nets -- skipping fine-grid pass")
            return list(self.routes)

        bbox_min_x = min(p.x for p in all_failed_pads) - padding_mm
        bbox_max_x = max(p.x for p in all_failed_pads) + padding_mm
        bbox_min_y = min(p.y for p in all_failed_pads) - padding_mm
        bbox_max_y = max(p.y for p in all_failed_pads) + padding_mm

        # Clamp to board bounds
        bbox_min_x = max(bbox_min_x, self.grid.origin_x)
        bbox_min_y = max(bbox_min_y, self.grid.origin_y)
        bbox_max_x = min(bbox_max_x, self.grid.origin_x + self.grid.width)
        bbox_max_y = min(bbox_max_y, self.grid.origin_y + self.grid.height)

        fine_width = bbox_max_x - bbox_min_x
        fine_height = bbox_max_y - bbox_min_y

        # Safety check: estimate fine-grid cell count
        num_layers = self.grid.num_layers
        estimated_fine_cells = (
            (fine_width / fine_resolution) * (fine_height / fine_resolution) * num_layers
        )
        max_fine_cells = 16_000_000  # 16M cell safety limit

        if estimated_fine_cells > max_fine_cells:
            flush_print(
                f"  WARNING: Fine grid would have {estimated_fine_cells:.0f} cells "
                f"(limit {max_fine_cells}). Increasing resolution to fit."
            )
            # Scale resolution up to fit within the cell limit
            scale = (estimated_fine_cells / max_fine_cells) ** 0.5
            fine_resolution = fine_resolution * scale
            flush_print(f"  Adjusted fine resolution: {fine_resolution:.4f}mm")

        flush_print(
            f"  Fine grid region: {fine_width:.1f}x{fine_height:.1f}mm "
            f"at ({bbox_min_x:.1f}, {bbox_min_y:.1f})"
        )

        # Create fine grid scoped to bounding box
        fine_rules = DesignRules(
            grid_resolution=fine_resolution,
            trace_width=self.rules.trace_width,
            trace_clearance=self.rules.trace_clearance,
            via_drill=self.rules.via_drill,
            via_diameter=self.rules.via_diameter,
            via_clearance=self.rules.via_clearance,
        )

        fine_grid = RoutingGrid(
            width=fine_width,
            height=fine_height,
            rules=fine_rules,
            origin_x=bbox_min_x,
            origin_y=bbox_min_y,
            layer_stack=self.grid.layer_stack,
            resolution_override=fine_resolution,
        )

        # Mark already-routed traces as obstacles on fine grid
        for route in self.routes:
            fine_grid.mark_route(route)

        # Add pads for the failed nets to the fine grid
        # Issue #1778: Pass component pitch so fine-pitch pads get reduced clearance
        pitches = self.component_pitches
        for pad in all_failed_pads:
            fine_grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))

        # Also add pads from other nets that are in the bounding box region,
        # so the fine grid knows about obstacles from other nets' pads
        for (ref, pin), pad in self.pads.items():
            if pad.net not in failed_net_ids:
                if bbox_min_x <= pad.x <= bbox_max_x and bbox_min_y <= pad.y <= bbox_max_y:
                    fine_grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))

        # Create a fine-grid router
        fine_router = create_hybrid_router(
            fine_grid, fine_rules, force_python=self._force_python, net_class_map=self.net_class_map
        )

        fine_grid_nets_count = 0
        fine_grid_deadline: float | None = None
        if timeout:
            remaining_timeout = timeout - (time.time() - start_time)
            if remaining_timeout <= 0:
                flush_print("  Timeout reached before fine-grid routing")
                return list(self.routes)
            fine_grid_deadline = time.time() + remaining_timeout

        # Try each failed net with pin order trials
        for net_id in failed_net_ids:
            if fine_grid_deadline is not None and time.time() >= fine_grid_deadline:
                flush_print("  Timeout reached during fine-grid pass")
                break

            if net_id not in self.nets:
                continue

            pads_keys = self.nets[net_id]
            if len(pads_keys) < 2:
                continue

            # Issue #2401: Substitute escaped pads with virtual pads at
            # escape endpoints.
            pad_objs = [
                self._escape_pad_overrides.get(p, self.pads[p])
                for p in pads_keys if p in self.pads
            ]
            if len(pad_objs) < 2:
                continue

            net_name = self.net_names.get(net_id, f"Net {net_id}")
            best_routes: list[Route] = []

            import numpy as _np

            mst_router = MSTRouter(fine_grid, fine_router, fine_rules, self.net_class_map)

            for trial in pin_order_trials:
                trial_pads = list(pad_objs)

                if trial == "reversed":
                    trial_pads = list(reversed(trial_pads))
                elif trial == "shuffled":
                    trial_pads = list(trial_pads)
                    random.shuffle(trial_pads)
                # "default" keeps original order

                # Snapshot fine grid state before this trial so we can roll
                # back if the trial fails, preventing route marks from one
                # ordering from polluting subsequent orderings.
                blocked_snapshot = _np.copy(fine_grid._blocked)
                net_snapshot = _np.copy(fine_grid._net)
                routes_snapshot = list(fine_grid.routes)

                trial_routes: list[Route] = []

                def mark_fine_route(route: Route, _tl: list = trial_routes) -> None:
                    fine_grid.mark_route(route)
                    _tl.append(route)

                mst_router.route_net(
                    trial_pads,
                    mark_route_callback=mark_fine_route,
                )

                if trial_routes and (not best_routes or len(trial_routes) > len(best_routes)):
                    best_routes = list(trial_routes)
                    if len(trial_routes) >= len(pads_keys) - 1:
                        # Fully routed, no need to try more orderings
                        break
                    # Partial success: roll back and try next ordering
                    fine_grid._blocked = blocked_snapshot
                    fine_grid._net = net_snapshot
                    fine_grid.routes = routes_snapshot
                else:
                    # Trial produced no improvement: roll back
                    fine_grid._blocked = blocked_snapshot
                    fine_grid._net = net_snapshot
                    fine_grid.routes = routes_snapshot

            if best_routes:
                fine_grid_nets_count += 1
                # Re-apply winning routes to the fine grid so subsequent nets
                # see them as obstacles, then mark on the main grid too.
                for route in best_routes:
                    if route not in fine_grid.routes:
                        fine_grid.mark_route(route)
                    self._mark_route(route)
                    self.routes.append(route)

                # Remove from routing failures
                self.routing_failures = [f for f in self.routing_failures if f.net != net_id]

                flush_print(f"    {net_name}: routed on fine grid ({len(best_routes)} routes)")
            else:
                flush_print(f"    {net_name}: still failed on fine grid")

        # Persist fine-grid count so callers can read it (e.g. for RoutingMetrics)
        self.fine_grid_nets_count = fine_grid_nets_count

        # --- Summary ---
        final_stats = self.get_statistics()
        final_routed = final_stats["nets_routed"]

        flush_print("\n=== Multi-Resolution Complete ===")
        flush_print(f"  Nets routed: {final_routed}/{total_nets}")
        flush_print(f"  Coarse grid: {coarse_routed} nets")
        flush_print(f"  Fine grid fallback: {fine_grid_nets_count} nets")

        if self.routing_failures:
            flush_print(f"  Still failed: {len(self.routing_failures)} net(s)")

        return list(self.routes)

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
                self.grid,
                relaxed_rules,
                force_python=self._force_python,
                net_class_map=self.net_class_map,
            )

            # Try to route each failed net with relaxed clearance
            newly_routed: list[int] = []
            for net in clearance_failed_nets:
                if net not in self.nets:
                    continue

                pads = self.nets[net]
                if len(pads) < 2:
                    continue

                # Issue #2401: Substitute escaped pads with virtual pads at
                # escape endpoints.
                pad_objs = [
                    self._escape_pad_overrides.get(p, self.pads[p])
                    for p in pads
                ]

                # Create negotiated router with relaxed rules
                neg_router = NegotiatedRouter(
                    self.grid, relaxed_router, relaxed_rules, self.net_class_map,
                    congestion_estimator=self._ensure_congestion_estimator(),
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
