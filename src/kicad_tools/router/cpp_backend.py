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

# Try to import C++ module with detailed error tracking
_CPP_IMPORT_ERROR: str | None = None
try:
    from . import router_cpp

    _CPP_AVAILABLE = True
except ImportError as e:
    _CPP_AVAILABLE = False
    _CPP_IMPORT_ERROR = str(e)
    router_cpp = None  # type: ignore


def is_cpp_available() -> bool:
    """Check if the C++ router backend is available."""
    return _CPP_AVAILABLE


def get_cpp_unavailable_reason() -> str | None:
    """Get the reason why C++ backend is unavailable.

    Returns:
        Error message if C++ backend failed to load, None if available.
    """
    if _CPP_AVAILABLE:
        return None
    return _CPP_IMPORT_ERROR


def get_backend_info() -> dict:
    """Get information about the active backend.

    Returns a dictionary with:
        - backend: "cpp" or "python"
        - version: version string
        - available: True if C++ backend is available
        - unavailable_reason: Error message if C++ unavailable (only if available=False)
        - platform: Current platform info (for diagnostics)
    """
    import platform
    import sys

    platform_info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
    }

    if _CPP_AVAILABLE:
        return {
            "backend": "cpp",
            "version": router_cpp.version(),
            "available": True,
            "platform": platform_info,
        }

    # Build detailed unavailability info
    reason = _CPP_IMPORT_ERROR or "Unknown error"

    # Provide helpful diagnostics for common issues
    diagnostic_hint = None
    if "arm64" in platform.machine().lower() or "aarch64" in platform.machine().lower():
        if "darwin" in platform.system().lower():
            diagnostic_hint = (
                "On Apple Silicon, ensure the C++ extension was built with: "
                "'kct build-native' or 'python -m build'. "
                "The native router provides 10-100x speedup for fine-grid routing."
            )
    elif "cannot open shared object" in reason.lower() or "dll" in reason.lower():
        diagnostic_hint = (
            "The C++ router extension was not found. "
            "Build with 'kct build-native' or install kicad-tools[native]."
        )

    result = {
        "backend": "python",
        "version": "pure-python",
        "available": False,
        "unavailable_reason": reason,
        "platform": platform_info,
    }

    if diagnostic_hint:
        result["diagnostic_hint"] = diagnostic_hint

    return result


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
        # Initialize layer mappings (identity by default, overridden by from_routing_grid)
        self._index_to_layer: dict[int, int] = {i: i for i in range(layers)}
        self._layer_to_index: dict[int, int] = {i: i for i in range(layers)}
        # Routable layer indices (all layers by default, refined by from_routing_grid)
        self._routable_layers: list[int] = list(range(layers))

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

        # Copy layer index mappings for layer conversion
        cpp_grid._index_to_layer = dict(grid._index_to_layer)
        cpp_grid._layer_to_index = dict(grid._layer_to_index)

        # Copy routable layer indices from Python grid
        cpp_grid._routable_layers = grid.get_routable_indices()

        # Copy blocked cells from Python grid to C++ grid
        for layer in range(grid.num_layers):
            for y in range(grid.rows):
                for x in range(grid.cols):
                    py_cell = grid.grid[layer][y][x]
                    if py_cell.blocked:
                        cpp_grid._impl.mark_blocked(x, y, layer, py_cell.net, py_cell.is_obstacle)

        return cpp_grid

    def index_to_layer(self, index: int) -> int:
        """Convert grid index to Layer enum value."""
        return self._index_to_layer.get(index, index)

    def get_routable_indices(self) -> list[int]:
        """Get indices of routable layers (matching RoutingGrid interface)."""
        return self._routable_layers

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

    def _is_layer_allowed(self, layer_idx: int) -> bool:
        """Check if routing on this layer is allowed by allowed_layers constraint.

        Args:
            layer_idx: Grid layer index

        Returns:
            True if layer is allowed (or no restriction), False if blocked
        """
        from .layers import Layer

        if self._rules.allowed_layers is None:
            return True  # No restriction

        # Convert grid index to Layer enum value, then to KiCad name for comparison
        layer_value = self._grid.index_to_layer(layer_idx)
        layer = Layer(layer_value)
        return layer.kicad_name in self._rules.allowed_layers

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

        # Compute start/end layers for through-hole pads if not provided
        # Through-hole pads can be accessed on any routable layer
        routable_layers = self._grid.get_routable_indices()
        if start_layers is None:
            start_layers = (
                routable_layers if getattr(start, "through_hole", False) else [start_layer]
            )
        if end_layers is None:
            end_layers = routable_layers if getattr(end, "through_hole", False) else [end_layer]

        # Filter start/end layers by allowed_layers constraint
        if self._rules.allowed_layers is not None:
            start_layers = [l for l in start_layers if self._is_layer_allowed(l)]
            end_layers = [l for l in end_layers if self._is_layer_allowed(l)]
            # If no valid layers remain, routing is impossible
            if not start_layers or not end_layers:
                return None

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
            # Convert grid index to Layer enum value
            layer_enum_value = self._grid.index_to_layer(cpp_seg.layer)
            seg = Segment(
                x1=cpp_seg.x1,
                y1=cpp_seg.y1,
                x2=cpp_seg.x2,
                y2=cpp_seg.y2,
                width=cpp_seg.width,
                layer=Layer(layer_enum_value),
                net=cpp_seg.net,
                net_name=start.net_name,
            )
            route.segments.append(seg)

        for cpp_via in result.vias:
            # Convert grid indices to Layer enum values
            layer_from_value = self._grid.index_to_layer(cpp_via.layer_from)
            layer_to_value = self._grid.index_to_layer(cpp_via.layer_to)
            via = Via(
                x=cpp_via.x,
                y=cpp_via.y,
                drill=cpp_via.drill,
                diameter=cpp_via.diameter,
                layers=(Layer(layer_from_value), Layer(layer_to_value)),
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

    def find_blocking_nets(
        self,
        start: Pad,
        end: Pad,
        layer: int | None = None,
    ) -> set[int]:
        """Find which nets block the direct path from start to end.

        Uses Bresenham's line algorithm to trace the ideal direct path,
        then identifies which net IDs are blocking cells along that path.
        This is used for targeted rip-up in negotiated routing.

        Args:
            start: Source pad
            end: Destination pad
            layer: Optional layer index (uses pad layer if not specified)

        Returns:
            Set of net IDs that block the path (excluding net 0 and the source net)
        """
        blocking_nets: set[int] = set()
        source_net = start.net

        # Convert to grid coordinates
        start_gx, start_gy = self._grid._impl.world_to_grid(start.x, start.y)
        end_gx, end_gy = self._grid._impl.world_to_grid(end.x, end.y)

        if layer is None:
            layer = start.layer.value % self._grid.num_layers

        # Trace a direct line from start to end using Bresenham's algorithm
        gx1, gy1 = start_gx, start_gy
        gx2, gy2 = end_gx, end_gy

        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy
        gx, gy = gx1, gy1

        # Determine trace half width in cells (same calculation as C++)
        trace_half_width_cells = max(
            1,
            int(
                (self._rules.trace_width / 2 + self._rules.trace_clearance) / self._grid.resolution
                + 0.5
            ),
        )

        while True:
            # Check this cell and nearby cells (accounting for trace width)
            for check_dy in range(-trace_half_width_cells, trace_half_width_cells + 1):
                for check_dx in range(-trace_half_width_cells, trace_half_width_cells + 1):
                    cx, cy = gx + check_dx, gy + check_dy
                    if 0 <= cx < self._grid.cols and 0 <= cy < self._grid.rows:
                        if self._grid._impl.is_valid(cx, cy, layer):
                            cell = self._grid._impl.at(cx, cy, layer)
                            if cell.blocked and cell.net != source_net and cell.net != 0:
                                # This cell is blocked by another net's route
                                if cell.usage_count > 0:
                                    blocking_nets.add(cell.net)

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return blocking_nets


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
