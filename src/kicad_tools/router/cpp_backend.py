"""
C++ router backend with Python fallback.

This module provides a unified interface to the router that automatically
uses the C++ implementation when available, falling back to pure Python.

The C++ backend provides 10-100x speedup for the core A* loop and grid
operations, making fine-grid routing (0.0635mm) practical for production use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .primitives import Pad, Route
    from .rules import DesignRules, NetClassRouting

# Try to import C++ module
try:
    from . import router_cpp

    _CPP_AVAILABLE = True
except ImportError:
    _CPP_AVAILABLE = False
    router_cpp = None  # type: ignore


def is_cpp_available() -> bool:
    """Check if the C++ router backend is available."""
    return _CPP_AVAILABLE


def get_backend_info() -> dict:
    """Get information about the active backend."""
    if _CPP_AVAILABLE:
        return {
            "backend": "cpp",
            "version": router_cpp.version(),
            "available": True,
        }
    return {
        "backend": "python",
        "version": "pure-python",
        "available": False,
    }


class CppGrid:
    """C++ Grid3D wrapper matching RoutingGrid interface.

    This class wraps the C++ Grid3D implementation, providing the same
    interface as the Python RoutingGrid for drop-in replacement.
    """

    def __init__(
        self,
        cols: int,
        rows: int,
        layers: int,
        resolution: float,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
    ):
        if not _CPP_AVAILABLE:
            raise RuntimeError("C++ router backend not available")
        self._impl = router_cpp.Grid3D(cols, rows, layers, resolution, origin_x, origin_y)
        self.cols = cols
        self.rows = rows
        self.num_layers = layers
        self.resolution = resolution
        self.origin_x = origin_x
        self.origin_y = origin_y

    @classmethod
    def from_routing_grid(cls, grid: RoutingGrid) -> CppGrid:
        """Create a CppGrid from an existing RoutingGrid."""
        cpp_grid = cls(
            cols=grid.cols,
            rows=grid.rows,
            layers=grid.num_layers,
            resolution=grid.resolution,
            origin_x=grid.origin_x,
            origin_y=grid.origin_y,
        )

        # Copy blocked cells from Python grid to C++ grid
        for layer in range(grid.num_layers):
            for y in range(grid.rows):
                for x in range(grid.cols):
                    py_cell = grid.grid[layer][y][x]
                    if py_cell.blocked:
                        cpp_grid._impl.mark_blocked(x, y, layer, py_cell.net, py_cell.is_obstacle)

        return cpp_grid

    def world_to_grid(self, x: float, y: float) -> tuple[int, int]:
        """Convert world coordinates to grid indices."""
        return self._impl.world_to_grid(x, y)

    def grid_to_world(self, gx: int, gy: int) -> tuple[float, float]:
        """Convert grid indices to world coordinates."""
        return self._impl.grid_to_world(gx, gy)

    def is_blocked(self, x: int, y: int, layer: int) -> bool:
        """Check if a cell is blocked."""
        if self._impl.is_valid(x, y, layer):
            return self._impl.at(x, y, layer).blocked
        return True

    def mark_segment(
        self, x1: int, y1: int, x2: int, y2: int, layer: int, net: int, clearance_cells: int
    ) -> None:
        """Mark cells along a segment as blocked."""
        self._impl.mark_segment(x1, y1, x2, y2, layer, net, clearance_cells)

    def mark_via(self, x: int, y: int, net: int, radius_cells: int) -> None:
        """Mark cells around a via as blocked on all layers."""
        self._impl.mark_via(x, y, net, radius_cells)

    def get_congestion(self, x: int, y: int, layer: int) -> float:
        """Get congestion level for a cell."""
        return self._impl.get_congestion(x, y, layer)

    def get_statistics(self) -> dict:
        """Get grid statistics."""
        return {
            "cols": self.cols,
            "rows": self.rows,
            "layers": self.num_layers,
            "total_cells": self._impl.total_cells,
            "blocked_cells": self._impl.count_blocked(),
            "memory_mb": self._impl.memory_mb(),
        }


class CppPathfinder:
    """C++ Pathfinder wrapper.

    This class wraps the C++ Pathfinder implementation for high-performance
    A* routing.
    """

    def __init__(
        self,
        grid: CppGrid,
        rules: DesignRules,
        diagonal_routing: bool = True,
    ):
        if not _CPP_AVAILABLE:
            raise RuntimeError("C++ router backend not available")

        # Convert Python rules to C++ DesignRules
        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        self._impl = router_cpp.Pathfinder(grid._impl, cpp_rules, diagonal_routing)
        self._grid = grid
        self._rules = rules

    def set_routable_layers(self, layers: list[int]) -> None:
        """Set which layers are routable (skip plane layers)."""
        self._impl.set_routable_layers(layers)

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
        start_layers: list[int] | None = None,
        end_layers: list[int] | None = None,
    ) -> Route | None:
        """Route between two pads.

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters (for interface
                compatibility with Python Router; not fully used by C++ backend)
            negotiated_mode: Enable negotiated congestion routing
            present_cost_factor: Multiplier for sharing penalty
            weight: A* weight (1.0 = optimal, >1.0 = faster)
            start_layers: Valid start layers (for PTH pads)
            end_layers: Valid end layers (for PTH pads)

        Returns:
            Route object if successful, None if no path found
        """
        from .layers import Layer
        from .primitives import Route, Segment, Via

        # Get layer indices
        start_layer = self._grid.num_layers // 2  # Default to middle
        end_layer = self._grid.num_layers // 2

        # Try to get actual layer from pad
        if hasattr(start.layer, "value"):
            start_layer = start.layer.value % self._grid.num_layers
        if hasattr(end.layer, "value"):
            end_layer = end.layer.value % self._grid.num_layers

        # Route using C++ implementation
        result = self._impl.route(
            start.x,
            start.y,
            start_layer,
            end.x,
            end.y,
            end_layer,
            start.net,
            start_layers or [],
            end_layers or [],
            negotiated_mode,
            present_cost_factor,
            weight,
        )

        if not result.success:
            return None

        # Convert C++ result to Python Route
        route = Route(net=start.net, net_name=start.net_name)

        for cpp_seg in result.segments:
            seg = Segment(
                x1=cpp_seg.x1,
                y1=cpp_seg.y1,
                x2=cpp_seg.x2,
                y2=cpp_seg.y2,
                width=cpp_seg.width,
                layer=Layer(cpp_seg.layer),
                net=cpp_seg.net,
                net_name=start.net_name,
            )
            route.segments.append(seg)

        for cpp_via in result.vias:
            via = Via(
                x=cpp_via.x,
                y=cpp_via.y,
                drill=cpp_via.drill,
                diameter=cpp_via.diameter,
                layers=(Layer(cpp_via.layer_from), Layer(cpp_via.layer_to)),
                net=cpp_via.net,
                net_name=start.net_name,
            )
            route.vias.append(via)

        # Validate layer transitions and insert any missing vias
        route.validate_layer_transitions(
            via_drill=self._rules.via_drill,
            via_diameter=self._rules.via_diameter,
        )

        return route

    @property
    def iterations(self) -> int:
        """Number of iterations in last route."""
        return self._impl.iterations

    @property
    def nodes_explored(self) -> int:
        """Number of nodes explored in last route."""
        return self._impl.nodes_explored


def create_hybrid_router(
    grid: RoutingGrid,
    rules: DesignRules,
    diagonal_routing: bool = True,
    force_python: bool = False,
):
    """Create a router, preferring C++ backend if available.

    This is the recommended way to create a router for maximum performance.
    It will automatically use the C++ backend when available and fall back
    to the pure Python implementation otherwise.

    Args:
        grid: Routing grid
        rules: Design rules
        diagonal_routing: Enable 45-degree diagonal routing
        force_python: Force use of Python backend (for testing)

    Returns:
        Either CppPathfinder or Python Router instance
    """
    if _CPP_AVAILABLE and not force_python:
        try:
            cpp_grid = CppGrid.from_routing_grid(grid)
            return CppPathfinder(cpp_grid, rules, diagonal_routing)
        except Exception:
            # Fall back to Python if C++ initialization fails
            pass

    # Fall back to Python implementation
    from .pathfinder import Router

    return Router(grid, rules, diagonal_routing=diagonal_routing)
