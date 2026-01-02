"""Adaptive layer autorouter.

This module provides an autorouter that automatically increases layer count
if routing fails, trying 2 → 4 → 6 layers until convergence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter

from .grid import RoutingGrid
from .layers import Layer, LayerStack
from .pathfinder import Router
from .primitives import Route
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules


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
        # Import here to avoid circular dependency
        from .core import Autorouter

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
        autorouter.layer_stack = layer_stack
        autorouter.grid = grid
        autorouter.router = Router(grid, self.rules, autorouter.net_class_map)
        autorouter.pads = {}
        autorouter.nets = {}
        autorouter.net_names = {}
        autorouter.routes = []

        # Initialize zone manager
        from .zones import ZoneManager

        autorouter.zone_manager = ZoneManager(grid, self.rules)

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
