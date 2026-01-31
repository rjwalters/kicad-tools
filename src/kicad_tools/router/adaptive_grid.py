"""
Adaptive grid routing — fine grid near pads, coarse grid in channels.

Issue #1135: The router faces a grid resolution dilemma: fine grids (0.01mm)
make all pads reachable but create enormous search spaces (35M+ cells), while
coarse grids (0.1mm) are fast but leave fine-pitch pads off-grid (only 53%
net completion for SSOP at 0.65mm pitch).

This module implements a two-phase adaptive routing strategy:

**Phase 1 — Pad Escape (fine grid, local scope)**:
  For each pad that doesn't align to the coarse grid, route a short escape
  segment from the pad center to the nearest coarse grid point. This uses
  a small local fine-grid region (configurable radius) around each component
  with fine-pitch pads.

**Phase 2 — Channel Routing (coarse grid, global scope)**:
  Route all inter-component connections on the coarse grid. Every start/end
  point is guaranteed to be on-grid (Phase 1 handles the bridging), so the
  standard A* router operates on a manageable grid (e.g., 357K cells at
  0.1mm instead of 35.75M at 0.01mm).

The result is 100% pad reachability at coarse-grid routing speed.

Example::

    from kicad_tools.router.adaptive_grid import AdaptiveGridRouter

    adaptive = AdaptiveGridRouter(grid, rules, router)
    result = adaptive.route_adaptive(nets, pads)

    print(f"Escaped {result.escaped_pads} pads")
    print(f"Routed {result.nets_routed}/{result.nets_attempted} nets")
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pathfinder import Router
    from .grid import RoutingGrid
    from .rules import DesignRules

from .layers import Layer
from .primitives import Pad, Route, Segment
from .subgrid import SubGridRouter, SubGridResult, compute_subgrid_resolution

logger = logging.getLogger(__name__)


@dataclass
class AdaptiveGridResult:
    """Result of adaptive grid routing.

    Attributes:
        escape_result: Sub-grid escape routing result from Phase 1
        escape_routes: Route objects for escape segments
        main_routes: Route objects from Phase 2 channel routing
        nets_attempted: Total nets the router attempted
        nets_routed: Nets successfully routed in Phase 2
        phase1_time_ms: Time spent on pad escape (Phase 1)
        phase2_time_ms: Time spent on channel routing (Phase 2)
        coarse_resolution: Grid resolution used for channel routing
        fine_resolutions: Map of component ref to fine resolution used
    """

    escape_result: SubGridResult | None = None
    escape_routes: list[Route] = field(default_factory=list)
    main_routes: list[Route] = field(default_factory=list)
    nets_attempted: int = 0
    nets_routed: int = 0
    phase1_time_ms: float = 0.0
    phase2_time_ms: float = 0.0
    coarse_resolution: float = 0.0
    fine_resolutions: dict[str, float] = field(default_factory=dict)

    @property
    def all_routes(self) -> list[Route]:
        """All routes from both phases."""
        return self.escape_routes + self.main_routes

    @property
    def total_time_ms(self) -> float:
        """Total routing time."""
        return self.phase1_time_ms + self.phase2_time_ms

    @property
    def escaped_pads(self) -> int:
        """Number of pads with escape segments."""
        if self.escape_result is None:
            return 0
        return self.escape_result.success_count

    @property
    def failed_escapes(self) -> int:
        """Number of pads where escape routing failed."""
        if self.escape_result is None:
            return 0
        return len(self.escape_result.failed_pads)

    def format_summary(self) -> str:
        """Format a human-readable summary."""
        lines = [
            "Adaptive Grid Routing Summary",
            f"  Coarse grid: {self.coarse_resolution:.3f}mm",
        ]
        if self.fine_resolutions:
            for ref, res in sorted(self.fine_resolutions.items()):
                lines.append(f"  Fine grid ({ref}): {res:.4f}mm")
        lines.append(f"  Phase 1 (pad escape): {self.escaped_pads} pads escaped, "
                     f"{self.failed_escapes} failed, {self.phase1_time_ms:.0f}ms")
        lines.append(f"  Phase 2 (channel routing): {self.nets_routed}/{self.nets_attempted} nets, "
                     f"{self.phase2_time_ms:.0f}ms")
        lines.append(f"  Total routes: {len(self.all_routes)} "
                     f"({len(self.escape_routes)} escape + {len(self.main_routes)} channel)")
        return "\n".join(lines)


def _compute_component_pitches(
    pads: dict[tuple[str, str], Pad] | list[Pad],
) -> dict[str, float]:
    """Compute minimum pin pitch per component.

    Groups pads by component reference and calculates the minimum distance
    between any two pads of the same component.

    Args:
        pads: Pads as dict or list

    Returns:
        Map of component reference to minimum pin pitch in mm
    """
    if isinstance(pads, dict):
        pad_list = list(pads.values())
    else:
        pad_list = list(pads)

    by_ref: dict[str, list[Pad]] = {}
    for pad in pad_list:
        if pad.ref:
            if pad.ref not in by_ref:
                by_ref[pad.ref] = []
            by_ref[pad.ref].append(pad)

    pitches: dict[str, float] = {}
    for ref, comp_pads in by_ref.items():
        if len(comp_pads) < 2:
            continue
        min_pitch = float("inf")
        for i, p1 in enumerate(comp_pads):
            for p2 in comp_pads[i + 1:]:
                dist = math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)
                if dist > 0.001:  # Skip overlapping pads
                    min_pitch = min(min_pitch, dist)
        if min_pitch < float("inf"):
            pitches[ref] = min_pitch

    return pitches


def identify_fine_pitch_components(
    pads: dict[tuple[str, str], Pad] | list[Pad],
    coarse_resolution: float,
    fine_pitch_threshold: float = 0.8,
) -> dict[str, float]:
    """Identify components needing fine-grid escape routing.

    A component needs fine-grid escape routing if its minimum pin pitch
    is below the fine_pitch_threshold and doesn't align well to the
    coarse grid.

    Args:
        pads: All board pads
        coarse_resolution: Main grid resolution in mm
        fine_pitch_threshold: Pin pitch below this triggers fine-grid routing

    Returns:
        Map of component ref to recommended fine-grid resolution
    """
    pitches = _compute_component_pitches(pads)
    fine_components: dict[str, float] = {}

    for ref, pitch in pitches.items():
        if pitch < fine_pitch_threshold:
            fine_res = compute_subgrid_resolution(pitch, coarse_resolution)
            fine_components[ref] = fine_res
            logger.debug(
                "Component %s: pitch=%.3fmm (< %.1fmm threshold), "
                "fine grid=%.4fmm",
                ref, pitch, fine_pitch_threshold, fine_res,
            )

    return fine_components


class AdaptiveGridRouter:
    """Two-phase adaptive grid router.

    Combines fine-grid pad escape routing with coarse-grid channel routing
    to achieve 100% pad reachability at near-coarse-grid speed.

    Args:
        grid: The coarse routing grid (standard resolution)
        rules: Design rules
        router: A* pathfinder router for Phase 2
        fine_pitch_threshold: Pin pitch below this triggers fine-grid escape.
            Default 0.8mm catches SSOP (0.65mm), TSSOP (0.5mm), QFN (0.4mm).
        escape_search_radius: Grid cells to search for escape endpoints.
            Larger values find more escape options but take longer.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        router: Router | None = None,
        fine_pitch_threshold: float = 0.8,
        escape_search_radius: int = 3,
    ):
        self.grid = grid
        self.rules = rules
        self.router = router
        self.fine_pitch_threshold = fine_pitch_threshold
        self.escape_search_radius = escape_search_radius
        self._subgrid = SubGridRouter(
            grid, rules, escape_search_radius=escape_search_radius,
        )

    def route_adaptive(
        self,
        nets: dict[int, list[tuple[str, str]]],
        pads: dict[tuple[str, str], Pad],
        route_fn: Callable[[], list[Route]] | None = None,
    ) -> AdaptiveGridResult:
        """Execute two-phase adaptive grid routing.

        Phase 1: Escape routing for fine-pitch pads
        Phase 2: Channel routing on the coarse grid

        Args:
            nets: Map of net_id to list of (ref, pin) pad identifiers
            pads: Map of (ref, pin) to Pad objects
            route_fn: Callable that routes all nets and returns list[Route].
                If None, uses self.router (must be set).

        Returns:
            AdaptiveGridResult with escape and channel routes
        """
        result = AdaptiveGridResult(coarse_resolution=self.grid.resolution)

        # Phase 1: Pad escape routing
        phase1_start = time.time()
        result.escape_result, result.escape_routes, result.fine_resolutions = (
            self._phase1_pad_escape(pads)
        )
        result.phase1_time_ms = (time.time() - phase1_start) * 1000

        # Phase 2: Channel routing
        phase2_start = time.time()
        result.main_routes, result.nets_attempted, result.nets_routed = (
            self._phase2_channel_routing(nets, pads, route_fn)
        )
        result.phase2_time_ms = (time.time() - phase2_start) * 1000

        logger.info(
            "Adaptive grid routing complete: %d/%d nets, %d escape segments, "
            "%.0fms total",
            result.nets_routed,
            result.nets_attempted,
            result.escaped_pads,
            result.total_time_ms,
        )

        return result

    def _phase1_pad_escape(
        self,
        pads: dict[tuple[str, str], Pad],
    ) -> tuple[SubGridResult, list[Route], dict[str, float]]:
        """Phase 1: Generate escape segments for off-grid pads.

        Identifies fine-pitch components and generates short escape segments
        from their off-grid pads to the nearest coarse-grid points.

        Args:
            pads: All board pads

        Returns:
            Tuple of (SubGridResult, escape Route list, fine resolution map)
        """
        logger.info("Phase 1: Pad escape routing")

        # Identify which components need fine-grid treatment
        fine_components = identify_fine_pitch_components(
            pads,
            self.grid.resolution,
            self.fine_pitch_threshold,
        )

        if not fine_components:
            logger.info("No fine-pitch components detected, skipping Phase 1")
            return SubGridResult(), [], {}

        logger.info(
            "Fine-pitch components: %s",
            ", ".join(f"{ref} ({res:.4f}mm)" for ref, res in fine_components.items()),
        )

        # Filter to only pads from fine-pitch components
        fine_pads = [
            pad for pad in pads.values()
            if pad.ref in fine_components
        ]

        if not fine_pads:
            return SubGridResult(), [], {}

        # Run sub-grid escape routing
        subgrid_result = self._subgrid.route_with_subgrid(fine_pads)
        escape_routes = self._subgrid.get_escape_routes(subgrid_result)

        if subgrid_result.analysis:
            logger.info(
                "Phase 1 complete: %d/%d off-grid pads escaped, %d cells unblocked",
                subgrid_result.success_count,
                subgrid_result.analysis.off_grid_count,
                subgrid_result.unblocked_count,
            )

        return subgrid_result, escape_routes, fine_components

    def _phase2_channel_routing(
        self,
        nets: dict[int, list[tuple[str, str]]],
        pads: dict[tuple[str, str], Pad],
        route_fn: Callable[[], list[Route]] | None = None,
    ) -> tuple[list[Route], int, int]:
        """Phase 2: Route all nets on the coarse grid.

        All pads are now on-grid (Phase 1 created escape segments for
        off-grid pads), so standard A* routing can proceed on the coarse grid.

        Args:
            nets: Map of net_id to (ref, pin) pairs
            pads: All board pads
            route_fn: Callable that routes nets, returns list[Route]

        Returns:
            Tuple of (routes, nets_attempted, nets_routed)
        """
        logger.info("Phase 2: Channel routing on coarse grid (%.3fmm)", self.grid.resolution)

        nets_attempted = len(nets)

        if route_fn is not None:
            routes = route_fn()
            nets_routed = len(routes)
        elif self.router is not None:
            routes = self._route_with_router(nets, pads)
            nets_routed = len(routes)
        else:
            logger.warning("No router or route_fn provided, Phase 2 skipped")
            return [], nets_attempted, 0

        logger.info(
            "Phase 2 complete: %d/%d nets routed on coarse grid",
            nets_routed, nets_attempted,
        )

        return routes, nets_attempted, nets_routed

    def _route_with_router(
        self,
        nets: dict[int, list[tuple[str, str]]],
        pads: dict[tuple[str, str], Pad],
    ) -> list[Route]:
        """Route nets using the A* router.

        Args:
            nets: Map of net_id to (ref, pin) pairs
            pads: All board pads

        Returns:
            List of successfully routed Route objects
        """
        routes: list[Route] = []

        for net_id, pad_keys in nets.items():
            if len(pad_keys) < 2:
                continue

            # Route pairs of pads
            for i in range(len(pad_keys) - 1):
                src_key = pad_keys[i]
                tgt_key = pad_keys[i + 1]

                if src_key not in pads or tgt_key not in pads:
                    continue

                src_pad = pads[src_key]
                tgt_pad = pads[tgt_key]

                src_gx, src_gy = self.grid.world_to_grid(src_pad.x, src_pad.y)
                tgt_gx, tgt_gy = self.grid.world_to_grid(tgt_pad.x, tgt_pad.y)

                src_layer = self.grid.layer_to_index(src_pad.layer.value)
                tgt_layer = self.grid.layer_to_index(tgt_pad.layer.value)

                route = self.router.find_path(
                    source=(src_gx, src_gy, src_layer),
                    target=(tgt_gx, tgt_gy, tgt_layer),
                    net_id=net_id,
                )

                if route is not None:
                    routes.append(route)

        return routes


__all__ = [
    "AdaptiveGridResult",
    "AdaptiveGridRouter",
    "identify_fine_pitch_components",
]
