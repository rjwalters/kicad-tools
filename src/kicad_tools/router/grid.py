"""
Routing grid for PCB autorouting.

This module provides:
- RoutingGrid: 3D grid for routing with obstacle tracking and congestion awareness

Performance optimizations:
- NumPy arrays for cell attributes (blocked, net, usage_count, etc.)
- Vectorized operations for bulk cell updates
- Pre-computed clearance masks for obstacle marking
- Expanded obstacle mode for coarser grids with pre-computed clearances
- GPU acceleration for large grids (via CuPy/MLX backends)

Grid Resolution Strategies:
- Fine grid (clearance/2): Maximum accuracy, highest memory/time cost
- Standard grid (trace_width): Good balance for most boards
- Expanded obstacles: Pre-expand obstacles, use coarser grid (~4x faster)

GPU Acceleration:
- Automatically enabled for grids above threshold (default 100k cells)
- Bulk operations (obstacle marking, history costs) run on GPU
- A* pathfinding stays on CPU (sequential algorithm)
- Lazy sync between GPU and CPU to minimize transfers

Thread Safety:
- Optional thread-safe mode for parallel routing operations
- RLock-based synchronization to prevent race conditions
- Minimal overhead when disabled (default)
"""

from __future__ import annotations

import logging
import math
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

import numpy as np

# Try to import rtree for spatial indexing, gracefully handle missing dependency.
# Matches the pattern established in drc/incremental.py.
try:
    from rtree import index as rtree_index

    RTREE_AVAILABLE = True
except ImportError:
    RTREE_AVAILABLE = False
    rtree_index = None  # type: ignore[assignment]

# Minimum number of indexed segments before R-tree queries are used.
# Below this threshold, brute-force iteration is faster due to R-tree overhead.
# Based on Parkour's empirical threshold for spatial index break-even.
RTREE_SEGMENT_THRESHOLD = 32

if TYPE_CHECKING:
    from kicad_tools.performance import PerformanceConfig
    from kicad_tools.schema.pcb import Zone

from kicad_tools.acceleration import (
    BackendType,
    check_memory_available,
    estimate_memory_bytes,
    get_backend,
    should_use_gpu,
    to_numpy,
)
from kicad_tools.exceptions import RoutingError

from .geometry import (
    point_to_segment_distance as _geom_point_to_seg_dist,
    segment_to_segment_distance as _geom_seg_to_seg_dist,
    segments_intersect as _geom_segments_intersect,
)
from .layers import Layer, LayerStack
from .primitives import Obstacle, Pad, Route, Segment, Via
from .rules import DesignRules


# Issue #2908: Plane-net name patterns for same-component validator carve-out.
#
# A plane-net pad is one whose net carries copper power/ground topology
# (a flooded zone or large rail, NOT a single-drop signal trace).
# These pads must always participate in pad-vs-segment clearance
# validation even when the pad's component is in the routing context's
# exclude set (Issue #1764 reachability allowed signal-pin perimeter
# escapes by skipping same-component pads; this exception preserves
# that behaviour for signal pads while keeping plane pads in the
# validator so traces cannot clip their copper).
#
# The classification must be NARROW enough not to misclassify a
# signal net that happens to carry a voltage in its name (board 01's
# ``VIN``/``VOUT`` are single-drop signal nets, NOT plane pours,
# and including them in the exact-match set would over-block all
# 2-pin resistor routes).  The discriminating property is that the
# net is intended to be a flooded copper plane: that is reliably
# the case for ``GND``, dedicated numbered power rails (``+3.3V``,
# ``+5V``, ``+1V2``, ``+12V``), and the canonical IC power-pin
# names (``VCC``, ``VDD``, ``VSS``, ``VEE``, ``VBAT``, ``VDDA``,
# ``VDDIO``, ``AVDD``, ``AVSS``, ``DVDD``, ``DVSS``).  ``VIN``,
# ``VOUT``, ``VBUS`` are intentionally NOT in the set -- they are
# typically point-to-point signal nets.
_PLANE_NET_PREFIXES: tuple[str, ...] = (
    "+",  # +3.3V, +5V, +12V, +1V2, +0V9, etc.
)
_PLANE_NET_EXACT: frozenset[str] = frozenset({
    "GND",
    "GROUND",
    "EARTH",
    "AGND",
    "DGND",
    "PGND",
    "SGND",
    "VSS",
    "VSSA",
    "AVSS",
    "DVSS",
    "VCC",
    "VCCA",
    "AVCC",
    "DVCC",
    "VDD",
    "VDDA",
    "VDDIO",
    "AVDD",
    "DVDD",
    "VEE",
    "VBAT",
    "VAA",
})


def _sync_pad_to_cpp_grid(
    py_grid: "RoutingGrid",
    cpp_grid: Any,
    pad: "Pad",
    pin_pitch: float | None,
) -> None:
    """Push a single pad's geometry into the paired C++ grid's validator state.

    Issue #2908: ``CppGrid.from_routing_grid`` populates the C++ ``pads_``
    vector once at ``Autorouter.__init__`` time, but ``Autorouter.add_component``
    (the actual pad-loading code path used by the router pipeline) adds pads
    via ``RoutingGrid.add_pad`` LATER -- after the C++ grid is already
    constructed.  Without an incremental sync, the C++ ``validate_route``
    iterates an empty pads list and silently accepts segment-vs-pad
    violations.  This helper mirrors the same per-pad payload that
    ``from_routing_grid`` computes (layer index, clearance override, ref
    hash, plane-net flag) so the C++ validator has accurate pad geometry
    immediately on each ``add_pad`` call.
    """
    # Avoid hard dependency on cpp_backend at import time (cpp_backend
    # imports grid.py).  Defer the router_cpp import to call time.
    try:
        from . import router_cpp  # type: ignore[attr-defined]
    except ImportError:
        return

    # Compute layer index (-1 for through-hole = all layers).
    if pad.through_hole:
        layer_idx = -1
    else:
        try:
            layer_idx = py_grid.layer_to_index(pad.layer.value)
        except (KeyError, ValueError):
            layer_idx = 0

    clearance_override = py_grid.rules.get_clearance_for_component(pad.ref, pin_pitch)
    ref_hash = router_cpp.fnv1a_hash(pad.ref) if pad.ref else 0
    is_plane_net = _is_plane_net_pad(pad)

    try:
        cpp_grid._impl.add_pad(
            pad.x,
            pad.y,
            pad.width,
            pad.height,
            pad.net,
            layer_idx,
            ref_hash,
            clearance_override,
            is_plane_net,
        )
    except (AttributeError, TypeError):
        # Older C++ binding without is_plane_net argument; ignore -- the
        # build-version mismatch guard in cpp_backend.py disables the
        # C++ backend in that case anyway.
        return


def _sync_pad_cells_to_cpp_grid(
    py_grid: "RoutingGrid",
    cpp_grid: Any,
    layers_to_block: list[int],
    gx1: int,
    gy1: int,
    gx2: int,
    gy2: int,
) -> None:
    """Push a pad's blocked-cell envelope to the paired C++ grid.

    Issue #3224: ``CppGrid.from_routing_grid`` snapshots the Python grid at
    ``Autorouter.__init__`` time -- when no pads have been added yet (the
    typical CLI flow constructs the empty grid, then ``add_component`` is
    called per-component AFTER the C++ grid exists).  Without this
    incremental sync, the C++ A* searches against an empty obstacle grid
    and relies entirely on post-route validation to reject pad-clearance
    violations.  That works for most patterns but leaks 16
    ``clearance_pad_segment`` errors on board 05 (fine-pitch QFN-56 / LQFP-32)
    because the pad-exit exemption at ``pathfinder.cpp:680`` / ``:1173``
    can step the trace centerline through foreign pad metal when
    ``cell.pad_blocked`` is uniformly ``false``.

    This helper walks the rectangular envelope the pad just claimed on
    the Python grid and forwards each blocked cell -- with its post-update
    ``net``, ``is_obstacle``, and ``pad_blocked`` flags -- to the C++ grid.
    The Python-side ``_add_pad_unsafe`` MUST have completed its in-place
    array updates before this is called; we read the freshly-written
    state out of ``py_grid._blocked``, ``py_grid._net``,
    ``py_grid._is_obstacle``, and ``py_grid._pad_blocked``.

    Args:
        py_grid: The Python ``RoutingGrid`` whose pad-cells were just
            updated by ``_add_pad_unsafe``.
        cpp_grid: The paired ``CppGrid`` (back-reference established by
            ``CppGrid.from_routing_grid``).
        layers_to_block: Layer indices the pad affected (typically a
            single layer for SMD pads, all routable layers for THT pads).
        gx1, gy1, gx2, gy2: Inclusive grid-cell bounding box of the pad's
            blocked envelope.  Already clamped to grid bounds by the
            caller's ``_add_pad_unsafe`` loop.
    """
    try:
        from . import router_cpp  # type: ignore[attr-defined]  # noqa: F401
    except ImportError:
        return

    # Use numpy slices to read the post-update Python grid state.  Reading
    # via the slow per-cell ``Cell`` proxy (``grid.grid[layer][y][x]``) for
    # every cell would re-traverse the GPU-aware accessor path; the raw
    # arrays are the same data source the proxy reads from.
    py_blocked = py_grid._blocked
    py_net = py_grid._net
    py_is_obstacle = py_grid._is_obstacle
    py_pad_blocked = py_grid._pad_blocked

    # Clamp the envelope defensively -- callers already clamp, but the
    # numpy slice would silently return empty arrays if the caller's
    # ``_add_pad_unsafe`` ever shipped an unclamped rectangle.
    gx1 = max(0, gx1)
    gy1 = max(0, gy1)
    gx2 = min(py_grid.cols - 1, gx2)
    gy2 = min(py_grid.rows - 1, gy2)
    if gx2 < gx1 or gy2 < gy1:
        return

    try:
        impl_mark_blocked = cpp_grid._impl.mark_blocked
    except AttributeError:
        return

    for layer_idx in layers_to_block:
        # Iterate the envelope.  Per-cell ``mark_blocked`` calls match the
        # bulk-sync pattern in ``CppGrid.from_routing_grid`` -- there is
        # no rectangular bulk setter that takes per-cell ``pad_blocked``
        # bits, so a small inner loop is the cleanest port.  The envelope
        # is bounded (typically <= 30x30 cells for a 1mm pad at 0.1mm
        # resolution) so the Python overhead is negligible relative to a
        # full board's route time.
        for gy in range(gy1, gy2 + 1):
            for gx in range(gx1, gx2 + 1):
                if not py_blocked[layer_idx, gy, gx]:
                    continue
                impl_mark_blocked(
                    int(gx),
                    int(gy),
                    int(layer_idx),
                    int(py_net[layer_idx, gy, gx]),
                    bool(py_is_obstacle[layer_idx, gy, gx]),
                    bool(py_pad_blocked[layer_idx, gy, gx]),
                )


def _is_plane_net_pad(pad: "Pad") -> bool:
    """Return True if ``pad`` belongs to a plane net (power/ground topology).

    Issue #2908: Used by ``validate_segment_clearance`` to decide whether
    a same-component-ref pad should still participate in pad-vs-segment
    clearance validation.  The pre-#2908 code only kept plane-net pads
    when their net id had been rewritten to ``0`` by ``skip_nets`` in
    ``io.py``; boards routed without ``--skip-nets`` (e.g. board 04 which
    routes ``+3.3V`` / ``GND`` as real nets to support zone stitching)
    therefore silently exempted same-component plane pads from the
    validator, allowing trace clips against U2.1 / U2.8 / U2.23 / U2.24
    (Issue #2880).

    The plane-net check is keyed on ``pad.net_name`` (exact match
    against ``_PLANE_NET_EXACT`` or starts with a member of
    ``_PLANE_NET_PREFIXES``) so the semantics are consistent
    regardless of whether the schematic uses ``skip_nets``
    (``pad.net == 0``) or not (``pad.net != 0``).

    Args:
        pad: The pad to classify.

    Returns:
        True if the pad's net name matches the plane-net classification.
    """
    if pad.net == 0:
        # The skipped-pour-net convention already marks this as a plane.
        return True
    name = pad.net_name.upper() if pad.net_name else ""
    if not name:
        return False
    if name in _PLANE_NET_EXACT:
        return True
    for prefix in _PLANE_NET_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _rect_segment_centerline_distance(
    cx: float,
    cy: float,
    w: float,
    h: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Signed centerline distance between an axis-aligned rectangle and a segment.

    Issue #2908: Mirror of the validator-side helper introduced in PR #2787
    (``src/kicad_tools/validate/rules/clearance.py::
    _rect_segment_centerline_distance``).  The router-side validator was
    previously modelling rectangular SMD pads as discs of radius
    ``max(w, h) / 2``; on rectangular pads where ``w != h`` the disc model
    over-blocks along the SHORT axis (phantom inflation) AND under-detects
    at the LONG-axis corners (the disc's rounded corner clips inside
    the rectangle's sharp corner).  Use true axis-aligned-rectangle
    geometry to mirror the post-route DRC's geometric semantics.

    Returns the minimum distance from the segment's centerline to the
    rectangle.  The sign convention matches
    ``_rect_circle_clearance`` (validate/rules/clearance.py):

    - **Positive** -- segment is entirely outside the rectangle.
    - **Zero**     -- segment touches/crosses the rectangle boundary.
    - **Negative** -- segment centerline lies inside the rectangle; the
      magnitude is the deepest signed-depth along the segment.

    Args:
        cx, cy: Center of rectangle.
        w, h: Width and height of rectangle.
        x1, y1: Segment start.
        x2, y2: Segment end.

    Returns:
        Signed centerline-to-rectangle distance in millimetres.
    """
    half_w = w / 2
    half_h = h / 2
    left = cx - half_w
    right = cx + half_w
    bot = cy - half_h
    top = cy + half_h

    def _inside(px: float, py: float) -> bool:
        return left <= px <= right and bot <= py <= top

    p1_in = _inside(x1, y1)
    p2_in = _inside(x2, y2)

    if p1_in and p2_in:
        # Whole centerline inside rect -- return deepest signed-depth.

        def _signed_depth(px: float, py: float) -> float:
            gap_x = max(px - right, left - px)
            gap_y = max(py - top, bot - py)
            return max(gap_x, gap_y)

        deepest = min(_signed_depth(x1, y1), _signed_depth(x2, y2))
        steps = 32
        dx = x2 - x1
        dy = y2 - y1
        for i in range(1, steps):
            t = i / steps
            d = _signed_depth(x1 + t * dx, y1 + t * dy)
            if d < deepest:
                deepest = d
        return deepest

    if p1_in != p2_in:
        # Endpoint straddles the boundary -- centerline crosses an edge.
        return 0.0

    # Both endpoints outside.  Check edge crossings.
    rect_edges = (
        (left, bot, right, bot),
        (right, bot, right, top),
        (right, top, left, top),
        (left, top, left, bot),
    )
    for ex1, ey1, ex2, ey2 in rect_edges:
        if _geom_segments_intersect(x1, y1, x2, y2, ex1, ey1, ex2, ey2):
            return 0.0

    # No crossing -- min over (segment endpoints to rect, rect corners to seg).
    def _point_to_rect(px: float, py: float) -> float:
        closest_x = max(left, min(px, right))
        closest_y = max(bot, min(py, top))
        return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)

    candidates = [
        _point_to_rect(x1, y1),
        _point_to_rect(x2, y2),
    ]
    for corner_x, corner_y in (
        (left, bot),
        (right, bot),
        (right, top),
        (left, top),
    ):
        candidates.append(_geom_point_to_seg_dist(corner_x, corner_y, x1, y1, x2, y2))

    return min(candidates)

logger = logging.getLogger(__name__)


class RoutedNetsUnblocker:
    """Context manager that temporarily unblocks routed-net cells.

    Used by relaxed A* (Issue #2274) to find a path ignoring routed nets.
    Static obstacles (pads, board edges) are preserved; only cells blocked
    by routed traces are cleared on entry and restored on exit.
    """

    def __init__(self, grid: "RoutingGrid") -> None:
        self._grid = grid
        self._saved_blocked: np.ndarray | None = None
        self._saved_net: np.ndarray | None = None

    def __enter__(self) -> "RoutedNetsUnblocker":
        # Save full copies of the blocked and net arrays
        self._saved_blocked = self._grid._blocked.copy()
        self._saved_net = self._grid._net.copy()

        # Build mask: cells that are blocked by routed nets (not by pads/obstacles)
        # A routed-net cell has: blocked=True, pad_blocked=False, net != 0
        routed_mask = self._grid._blocked & ~self._grid._pad_blocked & (self._grid._net != 0)

        # Clear those cells
        self._grid._blocked[routed_mask] = False
        self._grid._net[routed_mask] = 0

        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        # Restore saved arrays
        if self._saved_blocked is not None:
            np.copyto(self._grid._blocked, self._saved_blocked)
        if self._saved_net is not None:
            np.copyto(self._grid._net, self._saved_net)


class _CellView:
    """Lightweight view into grid arrays, providing GridCell-like interface."""

    __slots__ = ("_grid", "_x", "_y", "_layer")

    def __init__(self, grid: "RoutingGrid", x: int, y: int, layer: int):
        self._grid = grid
        self._x = x
        self._y = y
        self._layer = layer

    @property
    def x(self) -> int:
        return self._x

    @property
    def y(self) -> int:
        return self._y

    @property
    def layer(self) -> int:
        return self._layer

    @property
    def blocked(self) -> bool:
        return bool(self._grid._blocked[self._layer, self._y, self._x])

    @blocked.setter
    def blocked(self, value: bool) -> None:
        self._grid._blocked[self._layer, self._y, self._x] = value

    @property
    def net(self) -> int:
        return int(self._grid._net[self._layer, self._y, self._x])

    @net.setter
    def net(self, value: int) -> None:
        self._grid._net[self._layer, self._y, self._x] = value

    @property
    def cost(self) -> float:
        return 1.0  # Default cost, not stored in arrays

    @property
    def usage_count(self) -> int:
        return int(self._grid._usage_count[self._layer, self._y, self._x])

    @usage_count.setter
    def usage_count(self, value: int) -> None:
        self._grid._usage_count[self._layer, self._y, self._x] = value

    @property
    def history_cost(self) -> float:
        return float(self._grid._history_cost[self._layer, self._y, self._x])

    @history_cost.setter
    def history_cost(self, value: float) -> None:
        self._grid._history_cost[self._layer, self._y, self._x] = value

    @property
    def is_obstacle(self) -> bool:
        return bool(self._grid._is_obstacle[self._layer, self._y, self._x])

    @is_obstacle.setter
    def is_obstacle(self, value: bool) -> None:
        self._grid._is_obstacle[self._layer, self._y, self._x] = value

    @property
    def is_zone(self) -> bool:
        return bool(self._grid._is_zone[self._layer, self._y, self._x])

    @is_zone.setter
    def is_zone(self, value: bool) -> None:
        self._grid._is_zone[self._layer, self._y, self._x] = value

    @property
    def zone_id(self) -> str | None:
        return self._grid._zone_ids.get((self._layer, self._y, self._x))

    @zone_id.setter
    def zone_id(self, value: str | None) -> None:
        key = (self._layer, self._y, self._x)
        if value is None:
            self._grid._zone_ids.pop(key, None)
        else:
            self._grid._zone_ids[key] = value

    @property
    def pad_blocked(self) -> bool:
        return bool(self._grid._pad_blocked[self._layer, self._y, self._x])

    @pad_blocked.setter
    def pad_blocked(self, value: bool) -> None:
        self._grid._pad_blocked[self._layer, self._y, self._x] = value

    @property
    def original_net(self) -> int:
        return int(self._grid._original_net[self._layer, self._y, self._x])

    @original_net.setter
    def original_net(self, value: int) -> None:
        self._grid._original_net[self._layer, self._y, self._x] = value


class _LayerView:
    """View into a single layer of the grid."""

    __slots__ = ("_grid", "_layer")

    def __init__(self, grid: "RoutingGrid", layer: int):
        self._grid = grid
        self._layer = layer

    def __getitem__(self, y: int) -> "_RowView":
        return _RowView(self._grid, self._layer, y)


class _RowView:
    """View into a single row of the grid."""

    __slots__ = ("_grid", "_layer", "_y")

    def __init__(self, grid: "RoutingGrid", layer: int, y: int):
        self._grid = grid
        self._layer = layer
        self._y = y

    def __getitem__(self, x: int) -> _CellView:
        return _CellView(self._grid, x, self._y, self._layer)


class _GridView:
    """Provides backward-compatible grid[layer][y][x] access to NumPy arrays."""

    __slots__ = ("_grid",)

    def __init__(self, grid: "RoutingGrid"):
        self._grid = grid

    def __getitem__(self, layer: int) -> _LayerView:
        return _LayerView(self._grid, layer)


class RoutingGrid:
    """3D grid for routing with obstacle tracking and congestion awareness.

    Uses NumPy arrays for high-performance cell access and vectorized operations.

    Grid Modes:
    - Standard: Uses rules.grid_resolution, adds clearance during routing
    - Expanded: Pre-expands obstacles by full clearance, allows coarser grid

    The expanded mode achieves ~4x speedup by:
    1. Using trace_width as grid resolution instead of clearance/2
    2. Pre-expanding all obstacles to include clearance zones
    3. Eliminating per-segment clearance checks during routing
    """

    def __init__(
        self,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: LayerStack | None = None,
        expanded_obstacles: bool = False,
        resolution_override: float | None = None,
        thread_safe: bool = False,
        config: PerformanceConfig | None = None,
        grid_origin_offset: tuple[float, float] | None = None,
    ):
        """Initialize routing grid.

        Args:
            width, height: Board dimensions in mm
            rules: Design rules for routing
            origin_x, origin_y: Board origin
            layer_stack: Layer configuration
            expanded_obstacles: If True, pre-expand obstacles by clearance
                               and allow coarser grid resolution
            resolution_override: Override grid resolution (None = auto from rules)
            thread_safe: If True, enable thread-safe mode with locking for
                        concurrent access. Disabled by default for performance.
            config: Performance configuration for GPU acceleration settings.
                   If None, GPU is disabled and CPU (NumPy) is always used.
            grid_origin_offset: Optional (x, y) offset in mm for grid origin.
                   When provided, grid points are shifted so that
                   ``origin + offset + k * resolution`` covers pad positions
                   more accurately.  Computed by
                   ``auto_select_grid_resolution()`` for mixed-pitch boards.
        """
        self.width = width
        self.height = height
        self.rules = rules
        # Apply grid origin offset to the board origin so that grid points
        # are shifted to align with pad positions on mixed-pitch boards.
        # Priority: explicit parameter > rules > default (0,0)
        if grid_origin_offset is not None:
            self.grid_origin_offset = grid_origin_offset
        elif hasattr(rules, "grid_origin_offset"):
            self.grid_origin_offset = rules.grid_origin_offset
        else:
            self.grid_origin_offset = (0.0, 0.0)
        self.origin_x = origin_x + self.grid_origin_offset[0]
        self.origin_y = origin_y + self.grid_origin_offset[1]
        self.expanded_obstacles = expanded_obstacles
        self._config = config

        # Calculate effective resolution
        if resolution_override is not None:
            self.resolution = resolution_override
        elif expanded_obstacles:
            # In expanded mode, we can use trace_width as resolution
            # since clearances are pre-computed in obstacle expansion
            self.resolution = max(rules.trace_width, rules.grid_resolution)
        else:
            self.resolution = rules.grid_resolution

        # Layer stack (default to 2-layer for backward compatibility)
        self.layer_stack = layer_stack or LayerStack.two_layer()
        self.num_layers = self.layer_stack.num_layers

        # Build layer enum to grid index mapping
        self._layer_to_index: dict[int, int] = {}
        self._index_to_layer: dict[int, int] = {}
        for layer_def in self.layer_stack.layers:
            for layer_enum in Layer:
                if layer_enum.kicad_name == layer_def.name:
                    self._layer_to_index[layer_enum.value] = layer_def.index
                    self._index_to_layer[layer_def.index] = layer_enum.value
                    break

        # Grid dimensions
        self.cols = int(width / self.resolution) + 1
        self.rows = int(height / self.resolution) + 1

        # Determine backend (GPU or CPU)
        self._backend_type, self._backend = self._select_backend()

        # Initialize arrays using selected backend
        grid_shape = (self.num_layers, self.rows, self.cols)
        self._init_arrays(grid_shape)

        # Track dirty state for lazy GPU/CPU sync
        self._gpu_dirty = False  # GPU arrays modified, need sync to CPU
        self._cpu_dirty = False  # CPU arrays modified, need sync to GPU

        # Sparse storage for zone IDs (most cells don't have zones)
        self._zone_ids: dict[tuple[int, int, int], str] = {}

        # Backward-compatible grid accessor
        self.grid = _GridView(self)

        # Congestion tracking: coarser grid for density
        self.congestion_size = rules.congestion_grid_size
        self.congestion_cols = max(1, self.cols // self.congestion_size)
        self.congestion_rows = max(1, self.rows // self.congestion_size)

        # Congestion counts using NumPy: [layer, cy, cx]
        self._congestion = np.zeros(
            (self.num_layers, self.congestion_rows, self.congestion_cols), dtype=np.int32
        )

        # Track placed routes for net assignment
        self.routes: list[Route] = []

        # Issue #2481: Optional back-reference to a paired C++ grid.  When
        # set (by ``CppGrid.from_routing_grid``), rip-up paths invalidate
        # the C++ side's ``stored_vias_``/``stored_segments_`` snapshots so
        # the next geometric pre-search check (Issue #2466) does not
        # consult stale data from a route that was just ripped up.  The
        # attribute is loosely typed (``Any``) so this module does not
        # need to import the optional cpp backend.
        self._cpp_grid: object | None = None

        # Alias for backward compatibility
        self.layers = self.num_layers

        # Pre-computed clearance masks for common radii
        self._clearance_masks: dict[int, np.ndarray] = {}

        # Thread safety support
        self._thread_safe = thread_safe
        self._lock: threading.RLock | None = threading.RLock() if thread_safe else None

        # Corridor preference tracking for two-phase routing
        # Maps net ID to Corridor object (from sparse.py)
        # Use Any type hint to avoid circular import; actual type checked at runtime
        self._corridor_preferences: dict[int, any] = {}
        self._corridor_penalty: float = 5.0  # Default penalty for leaving corridor

        # Store original pad geometry for geometric clearance validation
        # Issue #750: Grid-based checking is approximate; we need precise geometry
        # for post-route validation to catch diagonal segment violations
        self._pads: list[Pad] = []

        # Issue #2452: Track pads by component reference for same-component
        # clearance relaxation. When pads share the same component (e.g.,
        # crystal Y1's OSC_IN and OSC_OUT), the clearance between them can be
        # reduced since the component footprint already guarantees physical
        # manufacturability at that pitch.
        self._component_pads: dict[str, list[Pad]] = {}

        # Issue #2604 follow-up: track per-pad pin_pitch so reverse lookups
        # (``find_pad_ref_at``) can mirror the reduced clearance envelope
        # ``_add_pad_unsafe`` applies for fine-pitch pads.  Without this the
        # query side would use the standard envelope and risk attributing a
        # cell blocked by a *neighbour* (e.g. a passive on chorus-test near
        # U5/U7/U9 BGAs) to the fine-pitch pad whose smaller real envelope
        # never actually covered that cell.  Keyed by ``id(pad)`` so we can
        # store None entries for pads with no recorded pitch.
        self._pad_pin_pitch: dict[int, float | None] = {}

        # R-tree spatial index for segment clearance queries (Issue #1249).
        # Per-layer rtree.index.Index for fast envelope-based candidate pruning
        # in validate_segment_clearance, replacing O(R*S) brute-force iteration.
        # Falls back to brute-force when rtree is unavailable or segment count
        # is below RTREE_SEGMENT_THRESHOLD.
        self._seg_rtree: dict[int, Any] = {}  # layer_idx -> rtree Index
        self._seg_rtree_items: dict[int, dict[int, Segment]] = {}  # layer_idx -> id -> Segment
        self._seg_rtree_count: int = 0  # total indexed segments across all layers
        self._rtree_available = RTREE_AVAILABLE

        # Issue #2960: Via R-tree spatial index.
        #
        # PR #2958 (issue #2955) added a correctness fix to
        # ``VectorCollisionChecker.path_is_clear`` that iterates
        # ``grid.routes × route.vias`` on every call to detect foreign-net
        # via grazing.  The optimizer pipeline (``optimizer/algorithms.py``)
        # invokes ``path_is_clear`` thousands of times per net, so the
        # unindexed double loop produced a fleet-wide ~3x slowdown on the
        # C++ router (13s -> 40s per net on boards 06 / 07).
        #
        # This R-tree indexes all vias in ``self.routes`` by their AABB
        # envelope (inflated by ``via_radius + max_clearance + max_trace_width/2``
        # so that a bbox intersection against the query path's envelope
        # returns all candidate vias that could violate clearance).  The
        # ``VectorCollisionChecker`` queries this index instead of the
        # double loop, dropping per-call cost from O(V) to O(log V).
        #
        # The index is maintained in lock-step with ``self.routes`` by
        # ``mark_route`` / ``unmark_route``.  ``invalidate_spatial_index``
        # rebuilds it when design rules change.  Out-of-band mutations
        # (e.g. ``drc_nudge`` merging duplicate vias post-routing) happen
        # AFTER the optimizer has run, so the index is correct during the
        # only performance-critical phase.
        self._via_rtree: Any = None  # single rtree.Index (vias indexed by 2D bbox)
        self._via_rtree_items: dict[int, Via] = {}  # rtree id -> Via
        self._via_rtree_count: int = 0
        # Inflation: via_radius + max trace half-width + max clearance.
        # ``max_clearance`` already covers the worst-case via clearance,
        # and adding the largest plausible trace half-width keeps the
        # envelope conservative for narrow-phase point-to-segment checks.
        # Computed lazily on first insert so changes to design rules
        # before any insertion are picked up by ``invalidate_spatial_index``.
        self._via_rtree_inflation: float = 0.0

        # Issue #2335: Clearance-compensated spatial indexing.
        # R-tree envelopes are inflated by max_clearance so that intersection
        # queries return all segments that *could* violate clearance, without
        # per-query clearance arithmetic.  The inflation amount is cached so
        # that insert/remove use a consistent value; invalidate_spatial_index()
        # rebuilds the index when rules change.
        self._rtree_clearance_inflation: float = self.rules.max_clearance

        # Issue #2677: Hard corridor reservations.
        # Sparse mapping of (layer_idx, y, x) -> {set of net IDs that "own" this cell}.
        # Used by ``_mark_via`` to skip blocking cells that have been reserved for
        # a specific diff-pair continuation corridor on an inner layer.  An empty
        # dict (the default) preserves pre-#2677 behaviour exactly.  The net-ID
        # set semantics are: "this cell is reserved for ONE OF these net IDs;
        # any via belonging to a net NOT in the set must not consume the cell".
        # Diff pairs need a 2-element set (P and N nets); match groups (#2661)
        # need a larger set.  Use ``int`` keys for net IDs (not net names) to
        # match the rest of the grid's Via.net / Segment.net typing.
        self._reserved_for_nets: dict[tuple[int, int, int], frozenset[int]] = {}

    def _select_backend(self) -> tuple[BackendType, Any]:
        """Select the appropriate backend based on config and grid size.

        Returns:
            Tuple of (BackendType, backend_module).
        """
        # No config = always use CPU
        if self._config is None:
            return BackendType.CPU, np

        # Check if GPU should be used based on grid size
        grid_cells = self.cols * self.rows * self.num_layers
        if not should_use_gpu(self._config, grid_cells, "grid"):
            logger.debug(f"Grid size {grid_cells} below threshold, using CPU backend")
            return BackendType.CPU, np

        # Check memory availability
        required_bytes = estimate_memory_bytes(self.cols, self.rows, self.num_layers)
        if not check_memory_available(required_bytes, self._config):
            logger.warning(
                f"Insufficient GPU memory for grid ({required_bytes / 1e6:.1f}MB), "
                "falling back to CPU"
            )
            return BackendType.CPU, np

        # Try to get GPU backend
        try:
            backend = get_backend(config=self._config)
            # Determine backend type from the module
            if hasattr(backend, "cuda"):
                backend_type = BackendType.CUDA
            elif hasattr(backend, "_mx"):  # MLXBackend wrapper
                backend_type = BackendType.METAL
            else:
                backend_type = BackendType.CPU

            logger.info(
                f"Grid using {backend_type.value} backend for {grid_cells} cells "
                f"({required_bytes / 1e6:.1f}MB)"
            )
            return backend_type, backend
        except Exception as e:
            logger.warning(f"Failed to initialize GPU backend: {e}, using CPU")
            return BackendType.CPU, np

    def _init_arrays(self, grid_shape: tuple[int, int, int]) -> None:
        """Initialize grid arrays using the selected backend.

        Args:
            grid_shape: Shape tuple (layers, rows, cols).
        """
        xp = self._backend

        # Use backend's array creation functions
        self._blocked = xp.zeros(grid_shape, dtype=np.bool_)
        self._net = xp.zeros(grid_shape, dtype=np.int32)
        self._usage_count = xp.zeros(grid_shape, dtype=np.int16)
        self._history_cost = xp.zeros(grid_shape, dtype=np.float32)
        self._present_cost_ema: np.ndarray | None = (
            None  # Lazy; allocated on first use (Issue #2333)
        )
        self._is_obstacle = xp.zeros(grid_shape, dtype=np.bool_)
        self._is_zone = xp.zeros(grid_shape, dtype=np.bool_)
        self._pad_blocked = xp.zeros(grid_shape, dtype=np.bool_)
        self._original_net = xp.zeros(grid_shape, dtype=np.int32)

    def sync_to_cpu(self) -> None:
        """Transfer GPU arrays to CPU for A* operations.

        Call this before performing A* pathfinding or other operations
        that require random cell access. The transfer is skipped if
        arrays are already on CPU or if GPU hasn't been modified.
        """
        if self._backend_type == BackendType.CPU:
            return

        if not self._gpu_dirty:
            return

        logger.debug("Syncing grid arrays from GPU to CPU")
        self._blocked = to_numpy(self._blocked)
        self._net = to_numpy(self._net)
        self._usage_count = to_numpy(self._usage_count)
        self._history_cost = to_numpy(self._history_cost)
        if self._present_cost_ema is not None:
            self._present_cost_ema = to_numpy(self._present_cost_ema)
        self._is_obstacle = to_numpy(self._is_obstacle)
        self._is_zone = to_numpy(self._is_zone)
        self._pad_blocked = to_numpy(self._pad_blocked)
        self._original_net = to_numpy(self._original_net)

        self._gpu_dirty = False
        self._backend_type = BackendType.CPU
        self._backend = np

    def sync_to_gpu(self) -> None:
        """Transfer CPU arrays to GPU for bulk operations.

        Call this before performing bulk operations like obstacle
        expansion or history cost updates. The transfer is skipped
        if arrays are already on GPU or if CPU hasn't been modified.
        """
        if self._config is None:
            return  # No GPU config, stay on CPU

        if self._backend_type != BackendType.CPU:
            return  # Already on GPU

        if not self._cpu_dirty:
            return

        # Check if we should use GPU
        grid_cells = self.cols * self.rows * self.num_layers
        if not should_use_gpu(self._config, grid_cells, "grid"):
            return

        logger.debug("Syncing grid arrays from CPU to GPU")
        try:
            backend = get_backend(config=self._config)

            # Transfer arrays to GPU
            self._blocked = backend.asarray(self._blocked)
            self._net = backend.asarray(self._net)
            self._usage_count = backend.asarray(self._usage_count)
            self._history_cost = backend.asarray(self._history_cost)
            if self._present_cost_ema is not None:
                self._present_cost_ema = backend.asarray(self._present_cost_ema)
            self._is_obstacle = backend.asarray(self._is_obstacle)
            self._is_zone = backend.asarray(self._is_zone)
            self._pad_blocked = backend.asarray(self._pad_blocked)
            self._original_net = backend.asarray(self._original_net)

            self._cpu_dirty = False
            if hasattr(backend, "cuda"):
                self._backend_type = BackendType.CUDA
            elif hasattr(backend, "_mx"):
                self._backend_type = BackendType.METAL
            self._backend = backend
        except Exception as e:
            logger.warning(f"Failed to sync to GPU: {e}")

    @property
    def backend_type(self) -> BackendType:
        """Return the current backend type."""
        return self._backend_type

    @property
    def uses_gpu(self) -> bool:
        """Return whether GPU acceleration is active."""
        return self._backend_type != BackendType.CPU

    @property
    def congestion(self) -> np.ndarray:
        """Return congestion array (backward compatible)."""
        return self._congestion

    @property
    def thread_safe(self) -> bool:
        """Return whether thread-safe mode is enabled."""
        return self._thread_safe

    @contextmanager
    def locked(self) -> Iterator["RoutingGrid"]:
        """Context manager for exclusive grid access.

        Use this when performing multiple grid operations that must be atomic.
        In non-thread-safe mode, this is a no-op that yields immediately.

        Example:
            with grid.locked():
                grid.mark_route(route1)
                grid.mark_route(route2)

        Yields:
            self: The grid instance for method chaining
        """
        if self._lock is not None:
            with self._lock:
                yield self
        else:
            yield self

    @contextmanager
    def _acquire_lock(self) -> Iterator[None]:
        """Internal context manager for acquiring lock if thread-safe mode is enabled.

        This is used internally by grid methods that modify state.
        """
        if self._lock is not None:
            with self._lock:
                yield
        else:
            yield

    def _get_clearance_mask(self, radius: int) -> np.ndarray:
        """Get or create a circular clearance mask for given radius."""
        if radius not in self._clearance_masks:
            y, x = np.ogrid[-radius : radius + 1, -radius : radius + 1]
            mask = x * x + y * y <= radius * radius
            self._clearance_masks[radius] = mask
        return self._clearance_masks[radius]

    def layer_to_index(self, layer_enum_value: int) -> int:
        """Map Layer enum value to grid index."""
        if layer_enum_value in self._layer_to_index:
            return self._layer_to_index[layer_enum_value]
        raise RoutingError(
            "Layer value not in stack",
            context={
                "layer_value": layer_enum_value,
                "available": list(self._layer_to_index.keys()),
            },
        )

    def index_to_layer(self, index: int) -> int:
        """Map grid index to Layer enum value."""
        if index in self._index_to_layer:
            return self._index_to_layer[index]
        raise RoutingError(
            "Grid index not in stack",
            context={"index": index, "available": list(self._index_to_layer.keys())},
        )

    def get_routable_indices(self) -> list[int]:
        """Get grid indices of routable signal layers."""
        return self.layer_stack.get_routable_indices()

    def is_plane_layer(self, index: int) -> bool:
        """Check if grid index is a plane layer (no routing)."""
        return self.layer_stack.is_plane_layer(index)

    def _update_congestion(self, gx: int, gy: int, layer: int, delta: int = 1) -> None:
        """Update congestion count for the region containing (gx, gy)."""
        cx = min(gx // self.congestion_size, self.congestion_cols - 1)
        cy = min(gy // self.congestion_size, self.congestion_rows - 1)
        self._congestion[layer, cy, cx] += delta

    def get_congestion(self, gx: int, gy: int, layer: int) -> float:
        """Get congestion level [0, 1] for a grid cell's region."""
        cx = min(gx // self.congestion_size, self.congestion_cols - 1)
        cy = min(gy // self.congestion_size, self.congestion_rows - 1)
        count = self._congestion[layer, cy, cx]
        max_cells = self.congestion_size * self.congestion_size
        return min(1.0, count / max_cells)

    def get_congestion_map(self) -> dict[str, float]:
        """Get congestion statistics for all regions using vectorized operations."""
        max_cells = self.congestion_size * self.congestion_size
        density = self._congestion / max_cells

        return {
            "max_congestion": float(np.max(density)),
            "avg_congestion": float(np.mean(density)),
            "congested_regions": int(np.sum(density > self.rules.congestion_threshold)),
        }

    def world_to_grid(self, x: float, y: float) -> tuple[int, int]:
        """Convert world coordinates to grid indices.

        Uses round() instead of int() to avoid floating point precision errors.
        For example, (112.6 - 75.0) / 0.1 = 375.9999999999999 should map to 376,
        but int() would truncate to 375, causing off-by-one grid cell errors.
        """
        gx = round((x - self.origin_x) / self.resolution)
        gy = round((y - self.origin_y) / self.resolution)
        return (max(0, min(gx, self.cols - 1)), max(0, min(gy, self.rows - 1)))

    def grid_to_world(self, gx: int, gy: int) -> tuple[float, float]:
        """Convert grid indices to world coordinates.

        Coordinates are rounded to 4 decimal places (0.1 micron precision)
        to avoid floating point representation issues with fine grid resolutions.
        Without rounding, operations like `75.0 + 7 * 0.025` can produce
        values like `75.17500000000001` instead of `75.175`, which can cause
        KiCad to fail loading the PCB file.
        """
        return (
            round(self.origin_x + gx * self.resolution, 4),
            round(self.origin_y + gy * self.resolution, 4),
        )

    def add_obstacle(self, obs: Obstacle) -> None:
        """Mark grid cells as blocked by an obstacle.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            # Include trace half-width so trace edges maintain clearance from obstacle
            clearance = obs.clearance + self.rules.trace_clearance + self.rules.trace_width / 2

            # Calculate affected grid region
            x1 = obs.x - obs.width / 2 - clearance
            y1 = obs.y - obs.height / 2 - clearance
            x2 = obs.x + obs.width / 2 + clearance
            y2 = obs.y + obs.height / 2 + clearance

            gx1, gy1 = self.world_to_grid(x1, y1)
            gx2, gy2 = self.world_to_grid(x2, y2)

            layer_idx = self.layer_to_index(obs.layer.value)

            for gy in range(gy1, gy2 + 1):
                for gx in range(gx1, gx2 + 1):
                    if 0 <= gx < self.cols and 0 <= gy < self.rows:
                        self.grid[layer_idx][gy][gx].blocked = True

    def _clearance_for_pin_pitch(self, pin_pitch: float | None) -> float:
        """Return the grid-level clearance halo to apply around a pad.

        Mirrors the per-call logic in ``_add_pad_unsafe`` so callers that
        need to *reproduce* the envelope (notably ``find_pad_ref_at``) get
        the exact same value.  This factoring fixes an asymmetry flagged
        in Issue #2604 review: previously the lookup side always used the
        standard envelope, which on chorus-test U5/U7/U9 (fine-pitch BGAs
        surrounded by standard-pitch passives) could mistakenly attribute
        a passive's clearance halo to the BGA pad whose *real* envelope
        was the smaller fine-pitch one.

        Issue #2865 -- narrow-channel guard: the fine-pitch shrink
        (``min_trace_width / 2``) is only a sound optimisation when the
        resulting *inter-pad* channel is wide enough that a trace centred
        in it still satisfies full manufacturer clearance against both
        flanking pads.  On crowded packages such as LQFP-48 0.5 mm pitch
        with jlcpcb-tier1 rules (``min_trace=min_clearance=0.127 mm``),
        threading a signal trace between adjacent pads is geometrically
        infeasible: the required channel is ``2*clearance + trace_width =
        0.381 mm`` but only ~0.25 mm of edge-to-edge gap exists.  In that
        situation shrinking the halo to ``0.0635 mm`` only fools the
        pathfinder into producing a route that DRC then rejects -- the
        observed pathology behind 44 ``clearance_pad_segment`` errors on
        board 04's STM32 west edge.  When the shrunk channel is too
        narrow we therefore decline the optimisation and fall back to
        the standard envelope, which causes the pathfinder to look for
        escape routes around the package instead of through-channel.

        Args:
            pin_pitch: The component pin pitch in mm, or None when not
                available.  When below ``rules.fine_pitch_threshold``
                (and ``rules.min_trace_width`` is configured) the envelope
                shrinks to ``min_trace_width / 2`` -- the minimum needed
                to keep a necked-down trace from overlapping pad copper --
                *provided* the resulting channel can still satisfy full
                clearance per the narrow-channel guard.  Full manufacturer
                clearance is validated in post-routing DRC.

        Returns:
            Clearance distance in mm to pad outside the pad's metal.
        """
        standard = self.rules.trace_clearance + self.rules.trace_width / 2
        if (
            pin_pitch is not None
            and pin_pitch < self.rules.fine_pitch_threshold
            and self.rules.min_trace_width is not None
        ):
            shrunk = self.rules.min_trace_width / 2
            # Issue #2865 narrow-channel guard.  Reject the shrink when
            # the resulting inter-pad channel cannot host a trace at full
            # clearance.  The geometry (pitch-based, mirroring the
            # Curator's recommended formula) is:
            #
            #     effective_channel = pitch - 2 * shrunk - trace_width
            #     required_channel  = 2 * trace_clearance + trace_width
            #
            # ``effective_channel`` is the band available for a trace
            # centered between two halo edges; ``required_channel`` is
            # the minimum copper-to-copper distance the manufacturer
            # rule demands (clearance on each side of the trace).  When
            # the channel cannot fit the trace + 2 x clearance the
            # shrunk halo is geometrically infeasible -- the pathfinder
            # would place a trace that DRC must reject.  Fall back to
            # the standard envelope so the router routes *around* the
            # package via the escape mechanism instead.
            effective_channel = pin_pitch - 2.0 * shrunk - self.rules.trace_width
            required_channel = 2.0 * self.rules.trace_clearance + self.rules.trace_width
            if effective_channel >= required_channel:
                return shrunk
            # Narrow channel -- shrink is geometrically infeasible.
            # Fall through to the standard envelope so the router does
            # not try to thread between these pads.
        return standard

    def add_pad(self, pad: Pad, pin_pitch: float | None = None) -> None:
        """Add a pad as an obstacle (except for its own net).

        Thread-safe when thread_safe=True.

        Args:
            pad: Pad to add as obstacle.
            pin_pitch: Optional pin pitch in mm for this pad's component.
                When provided and below the fine-pitch threshold, uses reduced
                clearance (manufacturer minimum) to allow pathfinder access
                between adjacent fine-pitch pads.
        """
        with self._acquire_lock():
            self._add_pad_unsafe(pad, pin_pitch=pin_pitch)

    def _add_pad_unsafe(self, pad: Pad, pin_pitch: float | None = None) -> None:
        """Internal pad addition without locking."""
        # Store pad geometry for geometric clearance validation (Issue #750)
        self._pads.append(pad)

        # Issue #2908: Sync the pad to the paired C++ grid (if present) so the
        # C++ ``validate_route`` segment-vs-pad clearance check has up-to-date
        # pad data.  The C++ side's ``pads_`` vector was historically only
        # populated by ``CppGrid.from_routing_grid``, which runs ONCE at
        # ``Autorouter.__init__`` -- before ``add_component()`` triggers any
        # ``grid.add_pad()`` call.  Without this incremental sync, the C++
        # validator iterated over an empty pads list and silently accepted
        # routes that violated pad clearance (Issue #2908 root cause -- 44
        # ``clearance_pad_segment`` errors on board 04 even though the C++
        # validator had the disc-bound + same-component-ref skip code
        # branch).
        cpp_grid = self._cpp_grid
        if cpp_grid is not None:
            _sync_pad_to_cpp_grid(self, cpp_grid, pad, pin_pitch)

        # Issue #2452: Track pads by component reference for same-component
        # clearance relaxation.
        if pad.ref:
            self._component_pads.setdefault(pad.ref, []).append(pad)

        # Issue #2604 follow-up: remember the pin_pitch this pad was added
        # with so ``find_pad_ref_at`` can reproduce the reduced fine-pitch
        # clearance envelope and avoid false-positive ref attribution near
        # BGA / fine-pitch clusters.
        self._pad_pin_pitch[id(pad)] = pin_pitch

        # Clearance model: trace clearance + trace half-width from pad edge.
        # The pathfinder checks if the trace CENTER can be placed at a cell,
        # so we must block cells where the trace edge would violate clearance.
        # If we only blocked trace_clearance, a trace center placed at the boundary
        # would have its edge at (trace_clearance - trace_width/2) from the pad,
        # violating the required clearance.
        #
        # Issue #1778: For fine-pitch packages (<=0.65mm pitch), the standard
        # clearance envelope causes adjacent pad zones to completely overlap,
        # leaving zero unblocked cells for pathfinding. When pin_pitch is below
        # the fine-pitch threshold, use a reduced clearance envelope that only
        # prevents the necked-down trace edge from overlapping pad copper.
        #
        # Standard clearance: trace_clearance + trace_width/2 (blocks where trace
        # center would cause edge to violate clearance from pad).
        #
        # Fine-pitch clearance: min_trace_width/2 (blocks only where trace center
        # would cause its edge to overlap the pad metal). This is the minimum
        # needed for grid-level blocking -- the actual manufacturer clearance is
        # validated during DRC after routing. This ensures at least 1-3 passable
        # grid cells between adjacent fine-pitch pads for A* to find paths.
        clearance = self._clearance_for_pin_pitch(pin_pitch)

        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif pad.drill > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        x1 = pad.x - effective_width / 2 - clearance
        y1 = pad.y - effective_height / 2 - clearance
        x2 = pad.x + effective_width / 2 + clearance
        y2 = pad.y + effective_height / 2 + clearance

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        # PTH pads block all layers, SMD pads block only their layer
        if pad.through_hole:
            layers_to_block = list(range(self.num_layers))
        else:
            layers_to_block = [self.layer_to_index(pad.layer.value)]

        # Get center cell coordinates
        center_gx, center_gy = self.world_to_grid(pad.x, pad.y)

        # Calculate pad metal area bounds (without clearance)
        # Issue #996: Use ceil/floor to ensure we only mark cells whose CENTER
        # is inside the metal area, not cells that are merely nearby.
        # round() would include cells whose center is outside the metal area.
        metal_x1 = pad.x - effective_width / 2
        metal_y1 = pad.y - effective_height / 2
        metal_x2 = pad.x + effective_width / 2
        metal_y2 = pad.y + effective_height / 2
        metal_gx1 = int(math.ceil((metal_x1 - self.origin_x) / self.resolution))
        metal_gy1 = int(math.ceil((metal_y1 - self.origin_y) / self.resolution))
        metal_gx2 = int(math.floor((metal_x2 - self.origin_x) / self.resolution))
        metal_gy2 = int(math.floor((metal_y2 - self.origin_y) / self.resolution))

        for layer_idx in layers_to_block:
            for gy in range(gy1, gy2 + 1):
                for gx in range(gx1, gx2 + 1):
                    if 0 <= gx < self.cols and 0 <= gy < self.rows:
                        cell = self.grid[layer_idx][gy][gx]
                        cell.blocked = True
                        cell.original_net = pad.net

                        is_metal_area = (
                            metal_gx1 <= gx <= metal_gx2 and metal_gy1 <= gy <= metal_gy2
                        )

                        # Issue #996: Only mark cells in the metal area as pad-blocked,
                        # not clearance zone cells. This allows the router to distinguish
                        # between actual pad copper (which must always block) and clearance
                        # zones (which can be traversed when exiting a pad for sub-grid
                        # connections). Clearance cells remain blocked but pad_blocked=False.
                        #
                        # Issue #2915 / #2920: Pad metal cells must ALWAYS be marked
                        # ``is_obstacle = True`` on first touch, regardless of whether a
                        # neighbour pad has already painted ``cell.net``. The prior code
                        # only flipped ``is_obstacle`` on the SECOND pad-touch
                        # (``elif cell.net != pad.net``), which left isolated pads
                        # (TO-220, 2.54 mm THT, audio jacks, 0402 caps) with
                        # ``is_obstacle = False`` because their clearance envelopes
                        # never overlap a neighbour. In the pathfinder's negotiated
                        # mode the ``static_blocks`` branch then released the pad
                        # to foreign-net traces once a single trace touched it
                        # (``_usage_count > 0``), producing trace-through-pad DRC
                        # violations on chorus-test (#2915) and board-05's TO-220
                        # MOSFETs (#2920). BGAs accidentally masked this bug because
                        # their fine pitch forces neighbour-envelope overlap, which
                        # took the second-touch branch.
                        #
                        # Marking pad metal as ``is_obstacle = True`` does NOT regress
                        # same-net escape (#2880/#2908): the pathfinder filters with
                        # ``different_net = cell.net != routing_net``, so own-net pad
                        # cells (``cell.net == routing_net``) are still passable.
                        if is_metal_area:
                            cell.pad_blocked = True
                            if cell.net == 0:
                                cell.net = pad.net
                            # Pad metal is physically copper -- foreign nets must
                            # NEVER traverse it.  Set ``is_obstacle = True``
                            # unconditionally (on first touch as well as on
                            # second-touch overlap) so that the pathfinder's
                            # negotiated-mode ``static_blocks`` branch
                            # (which releases blocks once ``usage_count > 0``)
                            # cannot admit foreign-net traces to pad metal.
                            # The own-net escape (#2880 / #2908) is unaffected:
                            # ``different_net = (cell.net != routing_net)`` is
                            # False for the pad owner, so the cell remains
                            # passable for its own net.
                            # ``pad.net != 0`` is required because plane / skipped-
                            # pour pads need their static block contract
                            # (``different_net`` from any routing net via
                            # ``cell.net == 0``) which is enforced by the
                            # ``static_blocks`` branch with ``usage_count == 0``
                            # and by the ``pad_blocked`` short-circuit in the
                            # collision checkers (Issue #2758).
                            if pad.net != 0:
                                cell.is_obstacle = True
                        else:
                            # Issue #2940: rect-aware full-footprint obstacle
                            # marking for isolated pads.  The pre-fix code only
                            # flipped ``is_obstacle = True`` on clearance-halo
                            # cells on the SECOND pad-touch path
                            # (``elif cell.net != pad.net``).  For isolated
                            # pads (board 03's USB-C 0.25 x 0.35 mm pads,
                            # joystick THT pads, west-edge U1 pads), no
                            # neighbour-pad envelope overlaps the halo, so
                            # cells stayed ``is_obstacle = False`` and the
                            # pathfinder's negotiated-mode ``static_blocks``
                            # branch released them to foreign-net traces once
                            # ``_usage_count > 0`` -- producing 6 residual
                            # ``clearance_pad_segment`` violations against the
                            # J2 joystick THT pad cluster on board 03 (the
                            # foreign-net through-pad family #2928 targeted
                            # but did not fully cover for clearance-halo
                            # geometry).
                            #
                            # The fix mirrors the metal-area branch above:
                            # set ``is_obstacle = True`` on first touch for
                            # signal pads.  Net-aware semantics are preserved
                            # by setting ``cell.net = pad.net`` first, so the
                            # pathfinder's ``different_net = cell.net !=
                            # routing_net`` mask is False for own-net traces
                            # (same-net escape #2880 / #2908 remains intact).
                            # The clearance halo's ``blocked = True`` flag is
                            # what keeps foreign-net traces out; making
                            # ``is_obstacle = True`` simply prevents the
                            # negotiated-mode loophole from releasing the
                            # cell once it has been touched.
                            #
                            # For plane pads (``pad.net == 0``), the existing
                            # first branch already marked ``is_obstacle =
                            # True`` whenever the cell carried a foreign net,
                            # and a no-net-overlap first touch falls through
                            # naturally to the ``cell.net == 0`` second branch
                            # which still does not need ``is_obstacle`` (the
                            # ``static_blocks`` clause with ``usage_count ==
                            # 0`` and the ``pad_blocked`` short-circuit handle
                            # plane-net halo cells; see #2758).
                            if pad.net == 0:
                                if cell.net != 0:
                                    cell.is_obstacle = True
                            elif cell.net == 0:
                                cell.net = pad.net
                                cell.is_obstacle = True
                            elif cell.net != pad.net:
                                cell.is_obstacle = True

            # Always mark the center cell with this pad's net
            if 0 <= center_gx < self.cols and 0 <= center_gy < self.rows:
                center_cell = self.grid[layer_idx][center_gy][center_gx]
                center_cell.net = pad.net
                center_cell.original_net = pad.net

        # Issue #2452: Same-component pad clearance relaxation.
        # When two pads share the same component reference (e.g., Y1) but are
        # on different nets, the clearance corridor between them is often too
        # narrow after grid discretization. The component footprint already
        # guarantees physical manufacturability at the component's pitch, so we
        # can safely reduce the blocking envelope in the corridor between
        # same-component pads.
        #
        # For each previously-added pad on the same component (different net),
        # compute the overlap of their full-clearance zones and unblock
        # clearance-only cells (not metal cells) in that overlap region, using
        # a reduced clearance of trace_width/2 (just enough to prevent copper
        # overlap from the trace edge).
        if pad.ref and pad.net > 0:
            self._relax_same_component_clearance(
                pad, effective_width, effective_height, clearance, layers_to_block
            )

        # Issue #2842: Stitch-via halo reservation for plane-net pads.
        # Plane-net pads (``pad.net == 0``) will be bonded to the plane by a
        # stitch via dropped during ``kct stitch``.  That via needs
        # ``via_diameter/2 + clearance`` (~0.425 mm with the stitcher's
        # default 0.45/0.2 via) of clear space around the pad center -- much
        # more than the trace-only halo that ``_clearance_for_pin_pitch``
        # provides for fine-pitch pads (~0.05 mm).  Reserve the larger halo
        # for foreign-net traces so the stitch pass has somewhere to land.
        if (
            pad.net == 0
            and getattr(self.rules, "stitch_via_halo", True)
            and hasattr(self.rules, "stitch_via_halo_radius")
        ):
            self._apply_stitch_via_halo(
                pad,
                effective_width=effective_width,
                effective_height=effective_height,
                base_clearance=clearance,
                layers_to_block=layers_to_block,
            )

        # Issue #2878: proactive narrow-channel halo.
        #
        # When two same-component pads sit at fine pitch but the
        # manufacturer clearance rules make the inter-pad channel too
        # narrow to host a foreign trace at full clearance,
        # ``_clearance_for_pin_pitch`` (PR #2866) already declines the
        # shrink and returns the standard envelope so the channel ends up
        # blocked.  But ``_relax_same_component_clearance`` (PR for
        # #2452, called above) then UNBLOCKS the overlap region between
        # same-component pads to permit chip escape routing.  That
        # relaxation, while necessary for boards like Y1 crystal escapes,
        # also re-opens the channel to FOREIGN nets on fine-pitch
        # packages -- the root cause of the 44 ``clearance_pad_segment``
        # errors on board 04's STM32 LQFP-48 west edge (foreign NRST /
        # OSC_OUT / SWCLK traces threading through U2's own plane-net
        # pad clearance).
        #
        # The fix: after the relaxation, walk the channel between the
        # newly-added pad and each previously-added same-component pad
        # on a different net.  If the geometric narrow-channel guard
        # would have rejected the shrink (same predicate as
        # ``_clearance_for_pin_pitch``), re-block the channel cells in
        # a NET-AWARE way: cells owned by either same-component pad's
        # net are marked ``_is_obstacle = True`` (preserving own-net
        # escape -- the cell's net == routing net path), while foreign
        # nets see the cells as blocked obstacles.  This mirrors the
        # net-aware sibling-envelope carve-out in
        # ``_apply_stitch_via_halo`` (#2869 / PR #2870).
        # Trigger for every pad on a known component with a known pin_pitch
        # (signal AND plane-net pads alike).  Narrow-channel infeasibility
        # is symmetric: a foreign trace cannot thread between two
        # same-component pads regardless of whether either is plane or
        # signal.  The helper itself decides which neighbour pairs trip
        # the guard.
        if pad.ref and pin_pitch is not None:
            self._apply_narrow_channel_halo(
                pad,
                effective_width=effective_width,
                effective_height=effective_height,
                pin_pitch=pin_pitch,
                layers_to_block=layers_to_block,
            )

        # Issue #3224: Push the freshly-updated pad envelope into the paired
        # C++ grid so the C++ A* sees the same blocked cells (including the
        # ``pad_blocked`` metal bit) the Python A* does.  Without this, the
        # ``Autorouter.__init__`` -> ``create_hybrid_router`` -> ``add_component``
        # ordering leaves the C++ grid with zero pad obstacles -- relying on
        # post-route validation to reject pad-clearance violations.  That
        # works for most cases but leaks 16 ``clearance_pad_segment``
        # errors on board 05 (QFN-56 / LQFP-32 fine-pitch corridors) because
        # the pad-exit exemption at ``pathfinder.cpp:680`` / ``:1173`` can
        # step the trace centerline through foreign pad metal when
        # ``cell.pad_blocked`` is uniformly ``false``.
        #
        # The sync is intentionally done AFTER the same-component / stitch
        # halo / narrow-channel helpers above so the C++ grid receives the
        # FINAL post-update state (carve-outs applied, halos extended, etc.).
        # The standard envelope ``(gx1, gy1) - (gx2, gy2)`` covers the pad
        # metal and standard clearance; the auxiliary halo helpers
        # (``_apply_stitch_via_halo``, ``_apply_narrow_channel_halo``)
        # extend outward but only operate on plane-net or same-component
        # pads -- those zones are visited again when their owning pads are
        # added (or already covered by the standard envelope of the
        # neighbour pad).  The pad-metal protection that closes the
        # foreign-pad-metal A* gap is the standard-envelope zone, which is
        # what we sync here.
        cpp_grid = self._cpp_grid
        if cpp_grid is not None:
            _sync_pad_cells_to_cpp_grid(
                self, cpp_grid, layers_to_block, gx1, gy1, gx2, gy2
            )

    def _apply_stitch_via_halo(
        self,
        pad: Pad,
        effective_width: float,
        effective_height: float,
        base_clearance: float,
        layers_to_block: list[int],
    ) -> None:
        """Reserve a foreign-net keep-out halo around a plane-net pad (Issue #2842).

        The stitch pass (``kct stitch``) drops one via per plane-net pad to
        bond the plane to the pin.  That via needs ``via_diameter/2 +
        clearance`` of clear space around the pad center.  The router's
        trace-only halo (``_clearance_for_pin_pitch``) is much smaller --
        for fine-pitch LQFP/QFN pads it shrinks to ``min_trace_width/2``
        (~0.05 mm) to keep escape routing feasible.  That tiny halo leaves
        no room for a stitch via, which is the root cause of the U2.8 /
        U2.23 / U2.35 stitch failures on board 04.

        This helper extends the blocked region around plane-net pads out
        to ``rules.stitch_via_halo_radius()`` for *foreign* nets only.
        Same-net (plane) crossings are not affected.  The standard halo
        already laid down by ``_add_pad_unsafe`` is preserved -- this only
        adds cells beyond it.

        Safety constraints (preserve existing routing yield):
        - Cells inside another pad's metal area are NEVER overwritten
          (``pad_blocked == True``).  Same-component signal pads must keep
          their metal accessible for escape routing.
        - Cells already owned by another routable net (``cell.net > 0``)
          are NOT overwritten.  Their existing pad-clearance contract
          stands; we only mark them as ``is_obstacle = True`` so foreign
          traces (other nets) cannot route through them in negotiated mode
          -- mirroring the existing plane-net pad clearance behaviour at
          ``_add_pad_unsafe`` lines 920-927.
        - Cells inside the halo but already inside another pad's clearance
          envelope (``blocked == True`` AND ``net == 0``) keep their
          existing state.
        - Issue #2869: the same-component sibling-envelope carve-out is
          *net-aware*.  A halo cell that falls inside a same-component
          signal pad's standard envelope is only carved out (i.e. left
          passable) when the cell currently belongs to the sibling pad's
          own net (its escape corridor).  Foreign-net cells inside the
          sibling envelope still see the halo as blocked, preventing a
          foreign signal trace from threading the LQFP edge alongside the
          chip's own escape routing (root cause of 44 residual
          ``clearance_pad_segment`` errors on board 04).

        The halo expands the *clearance* envelope from
        ``base_clearance`` (which may be the fine-pitch ``min_trace_width/2``)
        to ``rules.stitch_via_halo_radius()``.  We only iterate over the
        *new* cells -- the annular ring between the standard envelope and
        the halo envelope -- to avoid re-touching cells that the main
        ``_add_pad_unsafe`` loop already handled.

        Args:
            pad: The plane-net pad whose halo we are reserving
                (``pad.net == 0``).
            effective_width: The pad's effective width in mm (mirrors the
                ``_add_pad_unsafe`` computation -- through-hole pads get
                their drill-derived dimensions when no rectangular
                geometry is set).
            effective_height: As above for height.
            base_clearance: The standard clearance returned by
                ``_clearance_for_pin_pitch``; we extend beyond it to the
                via-aware halo.
            layers_to_block: Layer indices to apply the halo to (PTH pads
                hit all layers; SMD pads hit only their layer).
        """
        # Issue #2842 geometry: the stitch via lands on the pad center,
        # not the pad edge.  Required clearance is measured from the via
        # center.  So the foreign-net keep-out region is a circle of
        # radius ``via_radius + clearance`` around the pad center.
        # Equivalently, it extends ``halo_from_center - pad_half_extent``
        # *beyond the pad edge* along each axis.
        #
        # Issue #2865 follow-up: the pre-check that used to short-circuit
        # "standard-pitch pads already have the full clearance envelope"
        # by comparing ``base_clearance`` to ``standard_envelope`` is no
        # longer correct.  After #2865's narrow-channel guard, *fine-pitch*
        # pads on crowded packages (LQFP-48 0.5 mm pitch + jlcpcb-tier1)
        # also receive ``base_clearance == standard_envelope``, and they
        # still need the via halo applied along the narrow axis (board 04
        # U2.8/U2.23/U2.35 GND pins).  The per-axis check below is the
        # correct gate: it compares the via halo extension along each
        # axis to ``base_clearance`` and returns when *both* axes are
        # already covered.  Standard-pitch wide pads (e.g. 0805 caps,
        # half-extents >= ``halo_from_center``) still short-circuit there
        # because ``ext_x`` and ``ext_y`` clamp to zero -- the board 04
        # passive-array yield concern (9/9 -> 3/9) the original early
        # return was guarding remains protected.
        halo_from_center = self.rules.stitch_via_halo_radius()
        # Halo extension is measured separately per axis: an
        # LQFP-48-style pad is wide (1.5 mm) on one axis but narrow
        # (0.3 mm) on the other.  The narrow axis is where neighbour
        # signal pins sit at 0.5 mm pitch -- that is where the halo
        # needs to extend BEYOND the pad metal.  The wide axis is
        # parallel to the chip edge; neighbours along that axis are
        # already on the other side of the package body and not at
        # risk of crowding.
        half_w = effective_width / 2.0
        half_h = effective_height / 2.0
        # Halo extension per axis (clamped at zero so we never shrink
        # the standard envelope).
        ext_x = max(0.0, halo_from_center - half_w)
        ext_y = max(0.0, halo_from_center - half_h)
        if ext_x <= base_clearance and ext_y <= base_clearance:
            # The standard envelope already covers the via halo on
            # both axes.  Nothing to do.
            return

        # Halo envelope (world coordinates) -- use the per-axis extents
        # so we apply the via halo precisely where it is needed (e.g.
        # short-axis on an LQFP pad) and leave the long-axis untouched.
        halo_x1 = pad.x - half_w - ext_x
        halo_y1 = pad.y - half_h - ext_y
        halo_x2 = pad.x + half_w + ext_x
        halo_y2 = pad.y + half_h + ext_y

        # Standard envelope already processed by _add_pad_unsafe
        std_x1 = pad.x - half_w - base_clearance
        std_y1 = pad.y - half_h - base_clearance
        std_x2 = pad.x + half_w + base_clearance
        std_y2 = pad.y + half_h + base_clearance

        hgx1, hgy1 = self.world_to_grid(halo_x1, halo_y1)
        hgx2, hgy2 = self.world_to_grid(halo_x2, halo_y2)

        # Issue #2842 regression-guard: skip halo cells that fall inside
        # the standard clearance envelope of any *same-component* signal
        # pad already added.  Without this exclusion the halo on a
        # fine-pitch LQFP GND pin would block the same chip's neighbour
        # signal pin escape (board 04: pin 8 GND halo blocks pin 7 NRST
        # escape).  The component footprint already guarantees physical
        # manufacturability at the pitch -- the standard envelope of the
        # signal pad is the right authority for that pin's routing
        # corridor.  Mirrors the same-component clearance relaxation at
        # :meth:`_relax_same_component_clearance` (Issue #2452).
        #
        # Issue #2869: the carve-out is *net-aware*.  We only skip the
        # halo for cells owned by the sibling pad's own net (its escape
        # corridor).  Foreign-net cells inside the sibling envelope must
        # still see the halo as blocked, otherwise a foreign signal trace
        # can thread the LQFP edge alongside the chip's own escape
        # routing and produce a ``clearance_pad_segment`` DRC error
        # against the plane-net pad (44 errors on routed board 04 before
        # this fix).  Each envelope entry now carries its owning net so
        # the cell-by-cell test below can compare against the cell's
        # current net assignment.
        same_component_envelopes: list[tuple[float, float, float, float, int]] = []
        if pad.ref:
            for other in self._component_pads.get(pad.ref, []):
                if other is pad or other.net == 0:
                    continue
                # Compute the other pad's effective rectangle + base
                # clearance envelope (same dilation as
                # ``_clearance_for_pin_pitch`` would apply).
                if other.through_hole:
                    if other.width > 0 and other.height > 0:
                        oew, oeh = other.width, other.height
                    elif other.drill > 0:
                        oew = oeh = other.drill + 0.7
                    else:
                        oew = oeh = 1.7
                else:
                    oew, oeh = other.width, other.height
                # Use the same pin_pitch envelope; without ``_pad_pin_pitch``
                # data we fall back to the standard envelope.
                other_clearance = self._clearance_for_pin_pitch(self._pad_pin_pitch.get(id(other)))
                same_component_envelopes.append(
                    (
                        other.x - oew / 2.0 - other_clearance,
                        other.y - oeh / 2.0 - other_clearance,
                        other.x + oew / 2.0 + other_clearance,
                        other.y + oeh / 2.0 + other_clearance,
                        other.net,
                    )
                )

        for layer_idx in layers_to_block:
            for gy in range(hgy1, hgy2 + 1):
                for gx in range(hgx1, hgx2 + 1):
                    if not (0 <= gx < self.cols and 0 <= gy < self.rows):
                        continue

                    # Skip cells already inside the standard envelope --
                    # those were handled by the main _add_pad_unsafe loop.
                    wx, wy = self.grid_to_world(gx, gy)
                    if std_x1 <= wx <= std_x2 and std_y1 <= wy <= std_y2:
                        continue

                    # Issue #2869: net-aware sibling-envelope carve-out.
                    # Skip the halo only for cells that fall inside a
                    # sibling envelope AND are currently owned by that
                    # sibling's net.  Foreign-net (or unclaimed) cells in
                    # the sibling envelope still get the halo applied so
                    # foreign signal traces cannot thread the plane-net
                    # pad's clearance band.  The sibling pad's own
                    # envelope already assigned ``cell.net = sibling.net``
                    # at line 960/968 in ``_add_pad_unsafe`` so the
                    # comparison is well-defined.
                    cell_net = int(self._net[layer_idx, gy, gx])
                    in_sibling_envelope = False
                    for sx1, sy1, sx2, sy2, sibling_net in same_component_envelopes:
                        if sx1 <= wx <= sx2 and sy1 <= wy <= sy2:
                            if cell_net == sibling_net:
                                in_sibling_envelope = True
                            break
                    if in_sibling_envelope:
                        continue

                    # Never overwrite another pad's metal area.
                    if self._pad_blocked[layer_idx, gy, gx]:
                        continue

                    # ``cell_net`` was already read above for the
                    # sibling-envelope check (Issue #2869); reuse it.

                    if cell_net == 0:
                        # Unclaimed cell (or another plane-net halo cell):
                        # reserve it for the plane net.  cell.blocked = True
                        # with cell.net == 0 is the standard "static no-net
                        # obstacle" pattern that blocks foreign traces in
                        # both standard and negotiated modes (see
                        # pathfinder ``_is_trace_blocked`` and
                        # ``allow_sharing`` paths).
                        self._blocked[layer_idx, gy, gx] = True
                    else:
                        # Cell already owned by a routable signal net
                        # (almost certainly a neighbour pad's clearance
                        # envelope).  Mirror the existing plane-net
                        # behaviour at ``_add_pad_unsafe`` lines 920-927:
                        # mark the cell as a hard obstacle so foreign
                        # nets cannot share it in negotiated mode, but
                        # leave its net assignment intact so its owner can
                        # still route through it.
                        self._is_obstacle[layer_idx, gy, gx] = True

    def _apply_narrow_channel_halo(
        self,
        pad: Pad,
        effective_width: float,
        effective_height: float,
        pin_pitch: float,
        layers_to_block: list[int],
    ) -> None:
        """Re-block the channel between two same-component pads when the
        manufacturer clearance rules make it geometrically infeasible to
        host a foreign trace there (Issue #2878).

        Background.  PR #2866's ``_clearance_for_pin_pitch`` narrow-channel
        guard already detects the infeasibility condition at the pad-add
        site and returns the *standard* envelope instead of the
        fine-pitch shrink.  That alone would close the channel -- except
        that ``_relax_same_component_clearance`` (PR for #2452, called
        immediately before us in ``_add_pad_unsafe``) UNBLOCKS the
        overlap region between same-component pads to preserve chip
        escape routing.  The relaxation is correct for Y1-style crystal
        escapes (wide pitch, OSC_IN / OSC_OUT routed through the same
        component), but on fine-pitch packages such as LQFP-48 0.5 mm
        pitch with jlcpcb-tier1 rules (``trace = clearance = 0.127 mm``)
        the relaxation re-opens a channel that DRC then rejects when a
        foreign net (e.g. NRST, OSC_OUT, SWCLK) threads through.  Result:
        44 ``clearance_pad_segment`` errors on routed board 04 before
        this fix.

        Strategy.  After the relaxation runs, walk each same-component
        sibling pad on a different net.  Test the same infeasibility
        predicate that ``_clearance_for_pin_pitch`` uses; if the channel
        cannot host a trace at full clearance, mark cells in the
        inter-pad rectangle as ``_blocked = True`` AND
        ``_is_obstacle = True``, preserving each cell's existing
        ``cell.net`` assignment.  This is the same net-aware blocking
        pattern that ``_apply_stitch_via_halo`` (#2842) uses for foreign
        traces in plane-net halos:

        - Cell owned by either same-component pad's net
          (``cell.net == pad.net`` or ``cell.net == sibling.net``):
          ``_is_obstacle`` keeps the cell rejected for foreign nets
          (``cell.blocked & cell.is_obstacle & cell.net != routing_net``
          in pathfinder ``_is_trace_blocked`` standard mode and the
          C++ ``Pathfinder::is_trace_blocked`` mirror) while leaving
          the cell passable for its OWN net (``cell.net == net``).
          The chip's own escape between the two pads survives.
        - Cell currently unclaimed (``cell.net == 0``): re-block it
          for everyone with the standard static-obstacle pattern
          (``blocked = True`` with ``cell.net == 0`` rejects all
          non-zero nets in both standard and negotiated modes).
        - Cell owned by a foreign component / foreign net: leave
          alone.  That cell is already part of another pad's
          clearance contract; we have no right to alter its state.

        Safety constraints (mirror the contracts of ``_add_pad_unsafe``
        and ``_apply_stitch_via_halo``):
        - Cells inside any pad's metal area (``_pad_blocked == True``)
          are NEVER touched -- they are already maximally blocked and
          their state must not be perturbed.
        - The cell's ``net`` assignment is preserved.  We only flip
          two booleans: ``_blocked`` and ``_is_obstacle``.  Validator
          code reads ``cell.original_net`` for post-route DRC truth;
          we do not touch that either.

        Net-pair semantics.  The helper is called once per ``add_pad``
        and inspects every *previously-added* same-component pad on a
        different net (mirrors ``_relax_same_component_clearance`` at
        line 1289 / ``_apply_stitch_via_halo`` sibling iteration).
        When the newly-added pad is the second of a pair, the iteration
        sees the first pad and the channel between them is processed.
        When subsequent pads are added the helper re-evaluates every
        pair; the operations are idempotent (setting an already-set
        boolean to True has no effect).

        Args:
            pad: The newly-added pad on a known component.
            effective_width: The pad's effective width in mm (mirrors the
                ``_add_pad_unsafe`` computation -- through-hole pads get
                their drill-derived dimensions when no rectangular
                geometry is set).
            effective_height: As above for height.
            pin_pitch: The component pin pitch in mm.  Drives the
                same-component infeasibility predicate.
            layers_to_block: Layer indices to apply the halo to (PTH pads
                hit all layers; SMD pads hit only their layer).
        """
        # Predicate: would the narrow-channel guard at
        # ``_clearance_for_pin_pitch`` (PR #2866) reject the fine-pitch
        # shrink at this pin_pitch?  We use the same gate so the two
        # pieces stay synchronised: the guard returns the standard
        # envelope (closes the channel) and *we* re-close it after the
        # same-component relaxation re-opens it.  No-op when the
        # geometry is feasible at the shrunk envelope (chorus-test
        # 0.65 mm BGA escapes etc).
        if pin_pitch >= self.rules.fine_pitch_threshold:
            return
        if self.rules.min_trace_width is None:
            return
        shrunk = self.rules.min_trace_width / 2.0
        effective_channel = pin_pitch - 2.0 * shrunk - self.rules.trace_width
        required_channel = 2.0 * self.rules.trace_clearance + self.rules.trace_width
        if effective_channel >= required_channel:
            # Channel is geometrically wide enough at the fine-pitch
            # shrink -- the relaxation is sound and we have nothing to
            # tighten.  This matches the same predicate
            # ``_clearance_for_pin_pitch`` uses at lines 833-836.
            return

        # Walk each previously-added same-component pad on a different
        # net.  This mirrors the iteration shape of
        # ``_relax_same_component_clearance`` (line 1289) -- the
        # symmetry guarantees both helpers see the same neighbour set.
        component_pads = self._component_pads.get(pad.ref, [])
        if len(component_pads) < 2:
            # Only this pad exists for the component; no neighbour to
            # form a channel with.  Defensive guard against the
            # single-pad component case (e.g. a lone test point).
            return

        for other_pad in component_pads:
            if other_pad is pad:
                continue
            if other_pad.net == pad.net:
                # Same net on both pads (rare for signal pads, common
                # for plane fan-outs) -- there is no foreign-net
                # channel here, the cells are all available to the
                # shared net.  Skip.
                continue

            # Reproduce the other pad's effective dimensions exactly
            # as ``_add_pad_unsafe`` did when it was added.  Mirrors
            # the same computation in ``_relax_same_component_clearance``
            # and ``_apply_stitch_via_halo`` sibling iteration.
            if other_pad.through_hole:
                if other_pad.width > 0 and other_pad.height > 0:
                    other_ew = other_pad.width
                    other_eh = other_pad.height
                elif other_pad.drill > 0:
                    other_ew = other_pad.drill + 0.7
                    other_eh = other_ew
                else:
                    other_ew = 1.7
                    other_eh = 1.7
            else:
                other_ew = other_pad.width
                other_eh = other_pad.height

            # Inter-pad rectangle in world coordinates -- bounded by
            # the two pads' inner metal edges along the pitch axis and
            # the union of their metal extents along the perpendicular
            # axis.  This is the strip a foreign trace would have to
            # cross to thread between them; if the narrow-channel guard
            # rejected the shrink, every cell here must be foreign-net
            # blocked.
            inner_x1 = min(pad.x + effective_width / 2.0, other_pad.x + other_ew / 2.0)
            inner_x2 = max(pad.x - effective_width / 2.0, other_pad.x - other_ew / 2.0)
            inner_y1 = min(pad.y + effective_height / 2.0, other_pad.y + other_eh / 2.0)
            inner_y2 = max(pad.y - effective_height / 2.0, other_pad.y - other_eh / 2.0)

            # Two channel orientations: pads stacked vertically (the
            # gap is between top-of-lower and bottom-of-upper) OR
            # stacked horizontally (gap between left-of-right and
            # right-of-left).  The metal extents along the channel
            # axis bound the channel rectangle; along the pitch axis
            # the bounds are the inner pad edges.
            #
            # We classify by which axis has positive separation
            # between the two metals; the orthogonal axis bounds the
            # channel strip width.  Diagonal pad arrangements (where
            # both axes have positive separation) get treated as the
            # axis with the larger gap (less risk of mis-bounding).
            gap_x = inner_x2 - inner_x1  # positive iff horizontally separated
            gap_y = inner_y2 - inner_y1  # positive iff vertically separated
            if gap_x <= 0.0 and gap_y <= 0.0:
                # Pads overlap on both axes (e.g. metal collision --
                # should never happen on a manufacturable board but
                # be defensive).  No channel to define.
                continue

            # Adjacency guard.  ``component_pads`` contains EVERY pad
            # on the component, not just geometric neighbours -- on an
            # LQFP-48 that is 48 pads, but only ~4 of them are the
            # current pad's pitch-axis neighbours.  Without this guard
            # we would re-block the FULL inter-pad rectangle for
            # widely-separated pairs (e.g. pin 1 and pin 7 on the same
            # LQFP edge, 3 mm apart), which would consume a huge band
            # of the chip's exterior routing space and break legitimate
            # foreign-trace escape routes.  The narrow-channel
            # infeasibility predicate is about the channel between
            # *adjacent* pads at ``pin_pitch``; if the channel-axis
            # gap is much wider than ``pin_pitch``, there is no narrow
            # channel and the helper has nothing to do.  Use a slack
            # tolerance of 1.5x the pitch to absorb minor measurement
            # rounding (we want to catch true neighbours and skip
            # everything else).
            gap = max(gap_x, gap_y)
            if gap > 1.5 * pin_pitch:
                continue

            if gap_y >= gap_x:
                # Vertical stack: pitch axis is y, channel strip
                # spans the union of the two pads' x extents.
                channel_x1 = min(
                    pad.x - effective_width / 2.0,
                    other_pad.x - other_ew / 2.0,
                )
                channel_x2 = max(
                    pad.x + effective_width / 2.0,
                    other_pad.x + other_ew / 2.0,
                )
                channel_y1 = inner_y1
                channel_y2 = inner_y2
            else:
                # Horizontal stack: pitch axis is x, channel strip
                # spans the union of the two pads' y extents.
                channel_x1 = inner_x1
                channel_x2 = inner_x2
                channel_y1 = min(
                    pad.y - effective_height / 2.0,
                    other_pad.y - other_eh / 2.0,
                )
                channel_y2 = max(
                    pad.y + effective_height / 2.0,
                    other_pad.y + other_eh / 2.0,
                )

            cgx1, cgy1 = self.world_to_grid(channel_x1, channel_y1)
            cgx2, cgy2 = self.world_to_grid(channel_x2, channel_y2)

            for layer_idx in layers_to_block:
                for gy in range(cgy1, cgy2 + 1):
                    for gx in range(cgx1, cgx2 + 1):
                        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
                            continue

                        # Never overwrite another pad's metal area.
                        # ``_pad_blocked`` cells are already in the
                        # strongest state (blocked + pad-owned) and
                        # must not be perturbed -- they are the chip's
                        # own pin metal which the chip's escape
                        # routing must start from.
                        if self._pad_blocked[layer_idx, gy, gx]:
                            continue

                        cell_net = int(self._net[layer_idx, gy, gx])
                        # Net-aware re-block.  The cell's existing net
                        # assignment classifies it into one of three
                        # buckets:
                        if cell_net == pad.net or cell_net == other_pad.net:
                            # Bucket A: owned by one of the two
                            # same-component pads' nets.  Mark it
                            # ``blocked`` + ``is_obstacle`` so foreign
                            # nets are rejected in standard mode
                            # (``cell.net != routing_net``) and
                            # negotiated mode (``is_obstacle &&
                            # different_net``), while the cell's own
                            # net can still traverse it
                            # (``cell.net == routing_net`` passes both
                            # checks).  Preserve cell.net.
                            self._blocked[layer_idx, gy, gx] = True
                            self._is_obstacle[layer_idx, gy, gx] = True
                        elif cell_net == 0:
                            # Bucket B: unclaimed cell.  Re-block it
                            # with the standard static-obstacle
                            # pattern (``blocked=True`` with
                            # ``cell.net == 0``) which rejects all
                            # non-zero nets in both modes.  This is
                            # the same pattern ``_apply_stitch_via_halo``
                            # uses at line 1286 for unclaimed halo
                            # cells.  Leave ``is_obstacle`` alone so
                            # the cell remains a passive obstacle
                            # rather than a hard one (preserves
                            # nuance for the negotiated-mode shared
                            # net flow).
                            self._blocked[layer_idx, gy, gx] = True
                        else:
                            # Bucket C: foreign component / foreign
                            # net already owns this cell.  Leave it
                            # alone -- another pad has already
                            # claimed the cell under its own
                            # clearance contract and we have no
                            # business altering its state.
                            continue

    def _relax_same_component_clearance(
        self,
        pad: Pad,
        effective_width: float,
        effective_height: float,
        clearance: float,
        layers_to_block: list[int],
    ) -> None:
        """Relax clearance between pads on the same component.

        Issue #2452: When two pads share the same component reference (e.g.,
        crystal Y1 with OSC_IN and OSC_OUT) but are on different nets, their
        full clearance envelopes can overlap so much that no passable grid cells
        remain in the corridor between them.  The component footprint already
        guarantees physical manufacturability at the designed pitch, so we can
        safely reduce the blocking envelope in the overlap region.

        For each previously-added pad on the same component with a different
        net, we compute the rectangular overlap of the two pads' full-clearance
        zones.  Within that overlap we unblock any cell that is:
          - a clearance-only cell (not pad metal, i.e. ``pad_blocked == False``)
          - currently blocked
          - assigned to *either* pad's net (not a third-party obstacle)

        After unblocking, the cell's ``original_net`` is preserved so that
        post-route DRC can still detect true violations, but the A* search
        can now route through the corridor.

        A reduced clearance of ``trace_width / 2`` is maintained around each
        pad's metal area so traces cannot physically overlap pad copper.
        """
        component_pads = self._component_pads.get(pad.ref, [])
        reduced_clearance = self.rules.trace_width / 2

        for other_pad in component_pads:
            if other_pad is pad or other_pad.net == pad.net:
                continue
            if other_pad.net <= 0:
                continue

            # Compute the other pad's effective dimensions (mirrors add_pad logic)
            if other_pad.through_hole:
                if other_pad.width > 0 and other_pad.height > 0:
                    other_ew = other_pad.width
                    other_eh = other_pad.height
                elif other_pad.drill > 0:
                    other_ew = other_pad.drill + 0.7
                    other_eh = other_ew
                else:
                    other_ew = 1.7
                    other_eh = 1.7
            else:
                other_ew = other_pad.width
                other_eh = other_pad.height

            # Full clearance envelope for the current pad
            pad_env_x1 = pad.x - effective_width / 2 - clearance
            pad_env_y1 = pad.y - effective_height / 2 - clearance
            pad_env_x2 = pad.x + effective_width / 2 + clearance
            pad_env_y2 = pad.y + effective_height / 2 + clearance

            # Full clearance envelope for the other pad (same clearance model)
            other_env_x1 = other_pad.x - other_ew / 2 - clearance
            other_env_y1 = other_pad.y - other_eh / 2 - clearance
            other_env_x2 = other_pad.x + other_ew / 2 + clearance
            other_env_y2 = other_pad.y + other_eh / 2 + clearance

            # Compute overlap rectangle in world coordinates
            overlap_x1 = max(pad_env_x1, other_env_x1)
            overlap_y1 = max(pad_env_y1, other_env_y1)
            overlap_x2 = min(pad_env_x2, other_env_x2)
            overlap_y2 = min(pad_env_y2, other_env_y2)

            if overlap_x1 >= overlap_x2 or overlap_y1 >= overlap_y2:
                continue  # No overlap -- nothing to relax

            # Reduced-clearance metal zones: keep cells blocked within
            # trace_width/2 of either pad's metal edge.
            pad_reduced_x1 = pad.x - effective_width / 2 - reduced_clearance
            pad_reduced_y1 = pad.y - effective_height / 2 - reduced_clearance
            pad_reduced_x2 = pad.x + effective_width / 2 + reduced_clearance
            pad_reduced_y2 = pad.y + effective_height / 2 + reduced_clearance

            other_reduced_x1 = other_pad.x - other_ew / 2 - reduced_clearance
            other_reduced_y1 = other_pad.y - other_eh / 2 - reduced_clearance
            other_reduced_x2 = other_pad.x + other_ew / 2 + reduced_clearance
            other_reduced_y2 = other_pad.y + other_eh / 2 + reduced_clearance

            # Convert overlap region to grid coordinates
            ogx1, ogy1 = self.world_to_grid(overlap_x1, overlap_y1)
            ogx2, ogy2 = self.world_to_grid(overlap_x2, overlap_y2)

            for layer_idx in layers_to_block:
                for gy in range(ogy1, ogy2 + 1):
                    for gx in range(ogx1, ogx2 + 1):
                        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
                            continue

                        # Never unblock pad metal cells
                        if self._pad_blocked[layer_idx, gy, gx]:
                            continue

                        # Only unblock cells that belong to one of the two
                        # same-component nets (don't touch third-party blocks)
                        cell_net = int(self._net[layer_idx, gy, gx])
                        if cell_net != pad.net and cell_net != other_pad.net:
                            continue

                        # Issue #2961: never clear _blocked on cells already
                        # marked is_obstacle. Pad-metal first-touch (#2915) and
                        # rect-aware halo first-touch (#2940) set is_obstacle =
                        # True specifically to keep foreign-net traces out; the
                        # C++ pathfinder gates the is_obstacle check inside
                        # ``if (cell.blocked)``, so clearing ``blocked`` would
                        # silently disable the obstacle test and let foreign
                        # traces clip neighbor pads (e.g. chorus-test J2 GPIO
                        # header at 2.54mm pitch, above the narrow-channel
                        # halo threshold). Own-net traces are unaffected:
                        # ``cell.net == routing_net`` already makes
                        # ``is_obstacle`` cells passable for their own net via
                        # pathfinder.cpp line 104 short-circuit.
                        if self._is_obstacle[layer_idx, gy, gx]:
                            continue

                        # Keep the cell blocked if it falls within the reduced
                        # clearance zone of either pad's metal
                        wx, wy = self.grid_to_world(gx, gy)
                        in_pad_reduced = (
                            pad_reduced_x1 <= wx <= pad_reduced_x2
                            and pad_reduced_y1 <= wy <= pad_reduced_y2
                        )
                        in_other_reduced = (
                            other_reduced_x1 <= wx <= other_reduced_x2
                            and other_reduced_y1 <= wy <= other_reduced_y2
                        )
                        if in_pad_reduced or in_other_reduced:
                            continue

                        # Unblock the cell so A* can route through
                        self._blocked[layer_idx, gy, gx] = False

    def add_keepout(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layers: list[Layer] | None = None,
    ) -> None:
        """Add a keepout region.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            if layers is None:
                layer_indices = self.get_routable_indices()
            else:
                layer_indices = [self.layer_to_index(layer.value) for layer in layers]

            gx1, gy1 = self.world_to_grid(x1, y1)
            gx2, gy2 = self.world_to_grid(x2, y2)

            for layer_idx in layer_indices:
                for gy in range(gy1, gy2 + 1):
                    for gx in range(gx1, gx2 + 1):
                        if 0 <= gx < self.cols and 0 <= gy < self.rows:
                            self.grid[layer_idx][gy][gx].blocked = True

    def is_blocked(self, gx: int, gy: int, layer: Layer, net: int = 0) -> bool:
        """Check if a cell is blocked for routing."""
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return True
        layer_idx = self.layer_to_index(layer.value)
        cell = self.grid[layer_idx][gy][gx]
        if cell.blocked:
            return cell.net == 0 or cell.net != net
        return False

    def validate_segment_clearance(
        self,
        seg: Segment,
        exclude_net: int,
        min_clearance: float | None = None,
        component_pitches: dict[str, float] | None = None,
        exclude_refs: set[str] | None = None,
        partner_net: int | None = None,
        partner_clearance: float | None = None,
    ) -> tuple[bool, float, tuple[float, float] | None]:
        """Validate geometric clearance of a segment against all obstacles.

        This performs precise geometric distance calculations to catch violations
        that grid-based checking misses, particularly for diagonal segments.
        Issue #750: Grid discretization causes diagonal segments to pass through
        obstacle corners that weren't detected during A* search.

        Issue #1016: Now supports per-component clearance overrides. When checking
        clearance against a pad, uses the clearance for that pad's component
        (from DesignRules.component_clearances or fine_pitch_clearance).

        Issue #2559 / Epic #2556 Phase 1C: Adds optional ``partner_net`` /
        ``partner_clearance`` parameters.  When set, a segment-vs-segment
        comparison against the partner net uses ``partner_clearance`` in
        place of the wider ``min_clearance``.  This implements within-pair
        clearance for differential pairs.

        Args:
            seg: The segment to validate
            exclude_net: Net ID to exclude (same-net elements don't violate clearance)
            min_clearance: Minimum required clearance (default: rules.trace_clearance).
                          This is used when no component-specific clearance applies.
            component_pitches: Optional dict mapping component ref to pin pitch in mm.
                             Used for automatic fine-pitch clearance detection.
            partner_net: Issue #2559 / Phase 1C -- when set, the named net id
                         is the diff-pair partner of ``exclude_net`` and the
                         seg-vs-seg / seg-vs-via comparisons use
                         ``partner_clearance`` instead of ``min_clearance``.
            partner_clearance: Tighter clearance applied only to elements
                               whose net matches ``partner_net``.

        Returns:
            Tuple of (is_valid, actual_clearance, violation_location)
            - is_valid: True if segment meets clearance requirements
            - actual_clearance: Minimum clearance found (negative if overlapping)
            - violation_location: (x, y) of worst violation, or None if valid
        """
        import math

        if min_clearance is None:
            min_clearance = self.rules.trace_clearance

        # Issue #2559: Tighter clearance is only applied when both arguments
        # are present and the partner clearance is tighter than the default.
        partner_active = (
            partner_net is not None
            and partner_net >= 0
            and partner_net != exclude_net
            and partner_clearance is not None
        )

        # Segment half-width for edge-to-edge distance calculation
        seg_half_width = seg.width / 2

        min_actual_clearance = float("inf")
        violation_loc: tuple[float, float] | None = None
        has_violation = False  # Issue #1016: track if any violation was found

        # Check against all stored pads
        for pad in self._pads:
            # Skip same-net pads (clearance not required within same net)
            if pad.net == exclude_net:
                continue

            # Issue #1764 + #2874 + #2908 + #2933: The same-component-ref
            # exclusion is intended to permit signal-pin escape routing
            # through the chip's own perimeter (Issue #1764 reachability fix).
            # It must NOT permit signal traces to clip plane-net pads on the
            # same chip, nor to physically overlap pad metal on any pad.
            #
            # PR #2873 (C++) / PR #2875 (this file, Python) narrowed the
            # exclusion to ``pad.net != 0`` so the SKIPPED-net convention
            # (``skip_nets`` rewriting in ``io.py:2819-2820``) kept plane-net
            # pads in the validator. That convention only covers boards that
            # pass ``--skip-nets``; board 04 routes ``+3.3V`` / ``GND`` as
            # real nets (so the GND zone can stitch up after routing), so
            # ``U2.1 +3.3V`` retains ``net == 2`` and was being silently
            # exempted -- producing 44 ``clearance_pad_segment`` violations
            # against same-component plane pads (Issue #2908, successor to
            # #2902).
            #
            # Issue #2908 broadens the carve-out: a same-component pad is
            # excluded ONLY when it is a signal net (not a plane net).
            # Plane-net pads -- whether ``net == 0`` (skipped-pour) or
            # ``net > 0`` with a power/ground name -- are kept in the
            # validator. The rect-aware geometry below ensures the disc-bound
            # over-rejection on the SHORT axis does not regress legitimate
            # signal-vs-signal corridor traces (the corridor relaxation in
            # ``_relax_same_component_clearance`` only operates between
            # signal pads, so this stricter validation for plane pads is
            # compatible with the existing reachability fix).
            #
            # Issue #2933: The same-component signal-pad carve-out is further
            # narrowed -- it now only suppresses clearance complaints when the
            # trace stays OUTSIDE the neighbour pad's metal.  On standard-pitch
            # passives like 0805 resistors (R1 at 2mm pitch on board 02), the
            # router was emitting traces whose centerline passed THROUGH the
            # opposite pad's metal because the carve-out silently exempted
            # them.  This produced 144 ``clearance_pad_segment`` errors on
            # board 02 with negative actual_clearance (overlapping copper).
            # The metal-overlap check (clearance < 0) is preserved for all
            # same-component pads, so the trace-through-pad pathology is now
            # caught at the validator while the fine-pitch escape exemption
            # (positive clearance < required_clearance) is unchanged.
            same_component_signal_carveout = (
                exclude_refs
                and pad.ref in exclude_refs
                and not _is_plane_net_pad(pad)
            )

            # Skip pads on different layers (unless PTH)
            if not pad.through_hole:
                # Convert layer for comparison
                pad_layer_idx = self.layer_to_index(pad.layer.value)
                seg_layer_idx = self.layer_to_index(seg.layer.value)
                if pad_layer_idx != seg_layer_idx:
                    continue

            # Issue #1016: Get per-component clearance if available
            # When validating against a pad, use the clearance for that component
            pad_ref = pad.ref
            pin_pitch = component_pitches.get(pad_ref) if component_pitches else None
            required_clearance = self.rules.get_clearance_for_component(pad_ref, pin_pitch)

            # Issue #2908: Rect-aware geometry for rectangular SMD pads. The
            # previous disc bound (``radius = max(w, h) / 2``) over-rejected
            # along the pad's SHORT axis (a 1.475 x 0.3 mm LQFP-48 pad became
            # a 0.7375 mm-radius disc, 0.587 mm of phantom inflation above /
            # below the pad metal) and under-detected at long-axis corners
            # (the disc's rounded corner clips inside the rectangle's sharp
            # corner). Vias and square pads (w == h within 1 micron) keep
            # the disc model -- it is exact for circular obstacles and
            # cheaper to evaluate. This mirrors PR #2787's fix at
            # ``validate/rules/clearance.py::_segment_circle_clearance``.
            is_circular_pad = abs(pad.width - pad.height) < 0.001
            if is_circular_pad:
                pad_radius = max(pad.width, pad.height) / 2
                dist = self._point_to_segment_distance(
                    pad.x, pad.y, seg.x1, seg.y1, seg.x2, seg.y2
                )
                clearance = dist - seg_half_width - pad_radius
            else:
                # Rect-aware: signed centerline-to-rect distance.  Negative
                # means the segment centerline lies inside the pad rectangle
                # (a real DRC defect; the magnitude is the deepest signed
                # depth).
                center_dist = _rect_segment_centerline_distance(
                    pad.x, pad.y, pad.width, pad.height,
                    seg.x1, seg.y1, seg.x2, seg.y2,
                )
                clearance = center_dist - seg_half_width

            # Issue #2933: Apply the same-component carve-out only when the
            # trace clears the neighbour pad's metal (clearance >= 0).
            # Negative clearance means the trace overlaps pad copper -- a
            # true defect that no carve-out should silence.
            if same_component_signal_carveout and clearance >= 0:
                continue

            if clearance < min_actual_clearance:
                min_actual_clearance = clearance

            # Issue #1016: Check violation against component-specific clearance
            if clearance < required_clearance:
                has_violation = True
                violation_loc = (pad.x, pad.y)

        # Check against segments from existing routes.
        # Issue #1249: Use R-tree spatial index when available and segment count
        # exceeds RTREE_SEGMENT_THRESHOLD. The R-tree narrows candidates via
        # envelope intersection, then we do exact distance checks on candidates.
        seg_layer_idx = self.layer_to_index(seg.layer.value)
        use_rtree = (
            self._rtree_available
            and self._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD
            and seg_layer_idx in self._seg_rtree
        )

        if use_rtree:
            # Issue #2335: R-tree envelopes are already inflated by
            # _rtree_clearance_inflation (= max_clearance).  The query
            # envelope only needs the query segment's own half-width plus a
            # small margin for accurate min_actual_clearance reporting on
            # nearby (non-violating) segments.  Segments whose inflated
            # envelopes do not intersect this query region are guaranteed to
            # have edge-to-edge clearance >= max_clearance and can be safely
            # skipped.  The extra ``min_clearance`` term ensures that all
            # potential violators are captured even when the query segment's
            # required clearance differs from the index inflation.
            search_margin = seg_half_width + min_clearance
            query_envelope = (
                min(seg.x1, seg.x2) - search_margin,
                min(seg.y1, seg.y2) - search_margin,
                max(seg.x1, seg.x2) + search_margin,
                max(seg.y1, seg.y2) + search_margin,
            )
            candidate_ids = list(self._seg_rtree[seg_layer_idx].intersection(query_envelope))
            layer_items = self._seg_rtree_items.get(seg_layer_idx, {})

            for cand_id in candidate_ids:
                other_seg = layer_items.get(cand_id)
                if other_seg is None:
                    continue
                # Skip same-net segments
                if other_seg.net == exclude_net:
                    continue

                # Exact segment-to-segment distance
                dist = self._segment_to_segment_distance(
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    other_seg.x1,
                    other_seg.y1,
                    other_seg.x2,
                    other_seg.y2,
                )

                # Edge-to-edge clearance (both segment half-widths)
                clearance = dist - seg_half_width - other_seg.width / 2

                # Issue #2559 / Phase 1C: tighter clearance for the diff-pair
                # partner only.
                effective_clearance = (
                    partner_clearance
                    if partner_active and other_seg.net == partner_net
                    else min_clearance
                )

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                if clearance < effective_clearance:
                    has_violation = True
                    violation_loc = (
                        (seg.x1 + seg.x2 + other_seg.x1 + other_seg.x2) / 4,
                        (seg.y1 + seg.y2 + other_seg.y1 + other_seg.y2) / 4,
                    )
        else:
            # Brute-force path: iterate all routes and segments.
            for route in self.routes:
                # Skip same-net routes
                if route.net == exclude_net:
                    continue

                for other_seg in route.segments:
                    # Skip segments on different layers
                    if other_seg.layer != seg.layer:
                        continue

                    # Segment-to-segment distance
                    dist = self._segment_to_segment_distance(
                        seg.x1,
                        seg.y1,
                        seg.x2,
                        seg.y2,
                        other_seg.x1,
                        other_seg.y1,
                        other_seg.x2,
                        other_seg.y2,
                    )

                    # Edge-to-edge clearance (both segment half-widths)
                    clearance = dist - seg_half_width - other_seg.width / 2

                    # Issue #2559 / Phase 1C: tighter clearance for partner.
                    effective_clearance = (
                        partner_clearance
                        if partner_active and route.net == partner_net
                        else min_clearance
                    )

                    if clearance < min_actual_clearance:
                        min_actual_clearance = clearance
                    if clearance < effective_clearance:
                        # Violation location at midpoint
                        has_violation = True
                        violation_loc = (
                            (seg.x1 + seg.x2 + other_seg.x1 + other_seg.x2) / 4,
                            (seg.y1 + seg.y2 + other_seg.y1 + other_seg.y2) / 4,
                        )

        # Check against vias from existing routes (not in R-tree; typically few)
        for route in self.routes:
            if route.net == exclude_net:
                continue

            for via in route.vias:
                via_radius = via.diameter / 2

                # Point-to-segment distance for via
                dist = self._point_to_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)

                clearance = dist - seg_half_width - via_radius

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                    if clearance < min_clearance:
                        has_violation = True
                        violation_loc = (via.x, via.y)

        # Issue #1016: is_valid is True only if no violations were found
        is_valid = not has_violation
        return is_valid, min_actual_clearance, violation_loc

    def validate_via_clearance(
        self,
        via: "Via",
        exclude_net: int,
        min_clearance: float | None = None,
    ) -> tuple[bool, float, tuple[float, float] | None]:
        """Validate geometric clearance of a via against all other-net segments.

        Issue #1667: Complements validate_segment_clearance() by checking vias
        against other-net segments. Without this, a via's annular ring can be
        placed too close to an existing segment on the same layer, creating a
        DRC violation that grid-based checking misses.

        For each copper layer the via spans, checks the via center-to-segment
        distance minus (via_radius + segment_half_width) against the required
        clearance.

        Args:
            via: The via to validate
            exclude_net: Net ID to exclude (same-net elements don't violate clearance)
            min_clearance: Minimum required clearance (default: rules.via_clearance)

        Returns:
            Tuple of (is_valid, actual_clearance, violation_location)
            - is_valid: True if via meets clearance requirements
            - actual_clearance: Minimum clearance found (negative if overlapping)
            - violation_location: (x, y) of worst violation, or None if valid
        """
        if min_clearance is None:
            min_clearance = self.rules.via_clearance

        via_radius = via.diameter / 2
        min_actual_clearance = float("inf")
        violation_loc: tuple[float, float] | None = None
        has_violation = False

        # Determine which layer indices the via spans
        via_layer_indices: set[int] = set()
        for layer in via.layers:
            try:
                via_layer_indices.add(self.layer_to_index(layer.value))
            except (KeyError, ValueError):
                pass

        # Check against segments from existing routes
        for route in self.routes:
            if route.net == exclude_net:
                continue

            for seg in route.segments:
                # Only check segments on layers the via spans
                seg_layer_idx = self.layer_to_index(seg.layer.value)
                if seg_layer_idx not in via_layer_indices:
                    continue

                seg_half_width = seg.width / 2

                # Point-to-segment distance from via center to segment
                dist = self._point_to_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)

                # Edge-to-edge clearance: center distance minus radii
                clearance = dist - via_radius - seg_half_width

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                if clearance < min_clearance:
                    has_violation = True
                    violation_loc = (via.x, via.y)

        is_valid = not has_violation
        return is_valid, min_actual_clearance, violation_loc

    def validate_via_to_via_clearance(
        self,
        via: "Via",
        exclude_net: int,
        min_clearance: float | None = None,
    ) -> tuple[bool, float, tuple[float, float] | None]:
        """Validate geometric clearance of a via against all other-net vias.

        Issue #1693: Complements validate_via_clearance() by checking vias
        against other-net vias. Without this, two vias from different nets can
        be placed too close together, creating a DRC violation that grid-based
        checking misses because the grid marking radius is tuned for trace
        avoidance, not via-to-via proximity.

        Uses simple center-to-center Euclidean distance minus both via radii
        to compute edge-to-edge clearance. No layer filtering is needed since
        vias are through-hole.

        Args:
            via: The via to validate
            exclude_net: Net ID to exclude (same-net vias don't violate clearance)
            min_clearance: Minimum required clearance (default: rules.via_clearance)

        Returns:
            Tuple of (is_valid, actual_clearance, violation_location)
            - is_valid: True if via meets clearance requirements
            - actual_clearance: Minimum clearance found (negative if overlapping)
            - violation_location: (x, y) of worst violation, or None if valid
        """
        import math

        if min_clearance is None:
            min_clearance = self.rules.via_clearance

        via_radius = via.diameter / 2
        min_actual_clearance = float("inf")
        violation_loc: tuple[float, float] | None = None
        has_violation = False

        for route in self.routes:
            if route.net == exclude_net:
                continue

            for existing_via in route.vias:
                distance = math.sqrt((via.x - existing_via.x) ** 2 + (via.y - existing_via.y) ** 2)
                existing_via_radius = existing_via.diameter / 2
                clearance = distance - via_radius - existing_via_radius

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                if clearance < min_clearance:
                    has_violation = True
                    violation_loc = (via.x, via.y)

        is_valid = not has_violation
        return is_valid, min_actual_clearance, violation_loc

    def validate_same_net_drill_spacing(
        self,
        via: "Via",
        same_net: int,
        min_drill_clearance: float | None = None,
    ) -> tuple[bool, float, tuple[float, float] | None]:
        """Validate drill-to-drill spacing between same-net vias.

        Issue #1782: Even same-net vias must maintain minimum drill-to-drill
        spacing to be manufacturable. Overlapping or near-overlapping drills
        from the same net cause fabrication defects.

        Uses center-to-center distance minus both drill radii to compute
        edge-to-edge drill clearance. Only checks vias belonging to the
        specified net (opposite logic to validate_via_to_via_clearance which
        *excludes* the same net).

        Args:
            via: The via to validate
            same_net: Net ID to check against (only same-net vias are checked)
            min_drill_clearance: Minimum required drill-to-drill spacing
                (default: rules.min_drill_clearance)

        Returns:
            Tuple of (is_valid, actual_clearance, violation_location)
            - is_valid: True if via meets drill spacing requirements
            - actual_clearance: Minimum drill clearance found
            - violation_location: (x, y) of worst violation, or None if valid
        """
        import math

        if min_drill_clearance is None:
            min_drill_clearance = self.rules.min_drill_clearance

        drill_radius = via.drill / 2
        min_actual_clearance = float("inf")
        violation_loc: tuple[float, float] | None = None
        has_violation = False

        for route in self.routes:
            if route.net != same_net:
                continue

            for existing_via in route.vias:
                # Skip checking against self (exact same position and drill)
                if abs(via.x - existing_via.x) < 1e-6 and abs(via.y - existing_via.y) < 1e-6:
                    continue

                distance = math.sqrt((via.x - existing_via.x) ** 2 + (via.y - existing_via.y) ** 2)
                existing_drill_radius = existing_via.drill / 2
                clearance = distance - drill_radius - existing_drill_radius

                if clearance < min_actual_clearance:
                    min_actual_clearance = clearance
                if clearance < min_drill_clearance:
                    has_violation = True
                    violation_loc = (via.x, via.y)

        is_valid = not has_violation
        return is_valid, min_actual_clearance, violation_loc

    def _point_to_segment_distance(
        self,
        px: float,
        py: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> float:
        """Calculate the distance from a point to a line segment."""
        return _geom_point_to_seg_dist(px, py, x1, y1, x2, y2)

    def _segment_to_segment_distance(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        x3: float,
        y3: float,
        x4: float,
        y4: float,
    ) -> float:
        """Calculate minimum distance between two line segments."""
        return _geom_seg_to_seg_dist(x1, y1, x2, y2, x3, y3, x4, y4)

    def compute_component_pitches(self) -> dict[str, float]:
        """Compute minimum pin pitch for each component.

        Issue #1016: Used for automatic fine-pitch clearance detection.
        Analyzes pad positions to calculate the minimum pitch (center-to-center
        distance) between adjacent pins for each component.

        Returns:
            Dictionary mapping component reference to minimum pitch in mm.

        Example:
            >>> pitches = grid.compute_component_pitches()
            >>> pitches.get("U1")  # Returns 0.65 for TSSOP-20
        """
        import math

        # Group pads by component reference
        pads_by_ref: dict[str, list[Pad]] = {}
        for pad in self._pads:
            ref = pad.ref
            if ref:
                if ref not in pads_by_ref:
                    pads_by_ref[ref] = []
                pads_by_ref[ref].append(pad)

        # Calculate minimum pitch for each component
        pitches: dict[str, float] = {}
        for ref, comp_pads in pads_by_ref.items():
            if len(comp_pads) < 2:
                continue

            min_pitch = float("inf")
            for i, p1 in enumerate(comp_pads):
                for p2 in comp_pads[i + 1 :]:
                    dist = math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)
                    if dist > 0.01:  # Ignore overlapping pads
                        min_pitch = min(min_pitch, dist)

            if min_pitch != float("inf"):
                pitches[ref] = min_pitch

        return pitches

    # =========================================================================
    # R-TREE SPATIAL INDEX MANAGEMENT (Issue #1249)
    # =========================================================================

    def _get_rtree_for_layer(self, layer_idx: int) -> Any:
        """Get or create an R-tree index for the given layer.

        Returns None if rtree is not available.
        """
        if not self._rtree_available:
            return None
        if layer_idx not in self._seg_rtree:
            # Create a new in-memory R-tree index with default properties
            p = rtree_index.Property()
            p.dimension = 2
            self._seg_rtree[layer_idx] = rtree_index.Index(properties=p)
            self._seg_rtree_items[layer_idx] = {}
        return self._seg_rtree[layer_idx]

    @staticmethod
    def _segment_envelope(
        seg: Segment, clearance_inflation: float = 0.0
    ) -> tuple[float, float, float, float]:
        """Compute the axis-aligned bounding box for a segment.

        The envelope is expanded by the segment half-width plus an optional
        clearance inflation value.  When ``clearance_inflation`` is non-zero
        (Issue #2335), the envelope grows so that a simple intersection test
        against a query point/segment can replace per-candidate clearance
        arithmetic.

        Args:
            seg: The segment whose envelope to compute.
            clearance_inflation: Additional expansion in mm beyond the
                segment half-width.  Typically set to
                ``DesignRules.max_clearance`` for conservative indexing.

        Returns:
            (min_x, min_y, max_x, max_y) tuple suitable for R-tree insertion/query.
        """
        margin = seg.width / 2 + clearance_inflation
        return (
            min(seg.x1, seg.x2) - margin,
            min(seg.y1, seg.y2) - margin,
            max(seg.x1, seg.x2) + margin,
            max(seg.y1, seg.y2) + margin,
        )

    def _rtree_insert_segment(self, seg: Segment, layer_idx: int) -> None:
        """Insert a segment into the per-layer R-tree index.

        Issue #2335: The envelope is inflated by ``_rtree_clearance_inflation``
        so that intersection queries return all segments that could violate
        clearance without per-query arithmetic.
        """
        idx = self._get_rtree_for_layer(layer_idx)
        if idx is None:
            return
        seg_id = id(seg)
        envelope = self._segment_envelope(seg, self._rtree_clearance_inflation)
        idx.insert(seg_id, envelope)
        self._seg_rtree_items[layer_idx][seg_id] = seg
        self._seg_rtree_count += 1

    def _rtree_remove_segment(self, seg: Segment, layer_idx: int) -> None:
        """Remove a segment from the per-layer R-tree index.

        Issue #2335: Uses the same ``_rtree_clearance_inflation`` that was
        applied at insertion time so that the R-tree deletion matches the
        stored envelope.
        """
        if not self._rtree_available:
            return
        if layer_idx not in self._seg_rtree:
            return
        seg_id = id(seg)
        if seg_id not in self._seg_rtree_items.get(layer_idx, {}):
            return
        envelope = self._segment_envelope(seg, self._rtree_clearance_inflation)
        self._seg_rtree[layer_idx].delete(seg_id, envelope)
        del self._seg_rtree_items[layer_idx][seg_id]
        self._seg_rtree_count = max(0, self._seg_rtree_count - 1)

    def _rtree_insert_route(self, route: Route) -> None:
        """Insert all segments of a route into the R-tree index."""
        for seg in route.segments:
            layer_idx = self.layer_to_index(seg.layer.value)
            self._rtree_insert_segment(seg, layer_idx)

    def _rtree_remove_route(self, route: Route) -> None:
        """Remove all segments of a route from the R-tree index."""
        for seg in route.segments:
            layer_idx = self.layer_to_index(seg.layer.value)
            self._rtree_remove_segment(seg, layer_idx)

    # ------------------------------------------------------------------
    # Issue #2960: Via R-tree spatial index
    # ------------------------------------------------------------------
    #
    # The via index is a single 2D rtree (one shared index, not per-layer)
    # because vias are points and through-hole vias span every copper
    # layer.  The narrow-phase query in ``VectorCollisionChecker`` then
    # filters candidates by layer via ``_via_on_layer``.  Indexing in 2D
    # (instead of 3D x/y/layer) keeps the R-tree simple and matches the
    # data shape: through-hole vias would each appear on every layer in
    # a per-layer index, costing the same memory as a single 2D index
    # without any query-cost win.

    def _compute_via_rtree_inflation(self) -> float:
        """Inflation amount for via envelopes (mm).

        The bbox query in ``VectorCollisionChecker.path_is_clear`` is
        constructed from the path's AABB expanded by
        ``half_width + min_clearance``.  To guarantee the broad phase
        returns every via whose narrow-phase distance check could fire,
        each indexed via envelope must be expanded by
        ``via_radius + max_clearance + max_trace_half_width`` so that the
        union of the two envelopes is at least as large as the actual
        clearance envelope.

        We use ``rules.max_clearance`` for clearance (the widest possible
        clearance across net classes, mirroring ``_rtree_clearance_inflation``)
        and ``rules.max_trace_width / 2`` for the trace half-width.
        ``via_radius`` is added per-via at insertion time.
        """
        max_trace = getattr(self.rules, "max_trace_width", self.rules.trace_width)
        return self.rules.max_clearance + max_trace / 2

    @staticmethod
    def _via_envelope(
        via: Via, extra_inflation: float
    ) -> tuple[float, float, float, float]:
        """Compute the inflated AABB for a via.

        Args:
            via: The via whose envelope to compute.
            extra_inflation: Additional expansion beyond the via radius
                (typically clearance + max trace half-width).

        Returns:
            (min_x, min_y, max_x, max_y) tuple suitable for R-tree insertion.
        """
        margin = via.diameter / 2 + extra_inflation
        return (via.x - margin, via.y - margin, via.x + margin, via.y + margin)

    def _get_or_create_via_rtree(self) -> Any:
        """Lazily create the via R-tree.  Returns None if rtree unavailable."""
        if not self._rtree_available:
            return None
        if self._via_rtree is None:
            p = rtree_index.Property()
            p.dimension = 2
            self._via_rtree = rtree_index.Index(properties=p)
            # Initialize inflation on first use so it reflects current rules.
            self._via_rtree_inflation = self._compute_via_rtree_inflation()
        return self._via_rtree

    def _rtree_insert_via(self, via: Via) -> None:
        """Insert a via into the via R-tree.

        Skipped silently when rtree is unavailable.  Each via is keyed by
        ``id(via)`` (consistent with the segment R-tree convention).
        """
        idx = self._get_or_create_via_rtree()
        if idx is None:
            return
        via_id = id(via)
        if via_id in self._via_rtree_items:
            # Defensive: avoid double-insert (would leak an entry).
            return
        envelope = self._via_envelope(via, self._via_rtree_inflation)
        idx.insert(via_id, envelope)
        self._via_rtree_items[via_id] = via
        self._via_rtree_count += 1

    def _rtree_remove_via(self, via: Via) -> None:
        """Remove a via from the via R-tree.

        No-op when the via is not currently indexed or rtree is unavailable.
        """
        if not self._rtree_available or self._via_rtree is None:
            return
        via_id = id(via)
        if via_id not in self._via_rtree_items:
            return
        envelope = self._via_envelope(via, self._via_rtree_inflation)
        self._via_rtree.delete(via_id, envelope)
        del self._via_rtree_items[via_id]
        self._via_rtree_count = max(0, self._via_rtree_count - 1)

    def _rtree_insert_route_vias(self, route: Route) -> None:
        """Insert every via of a route into the via R-tree."""
        if not self._rtree_available:
            return
        for via in route.vias:
            self._rtree_insert_via(via)

    def _rtree_remove_route_vias(self, route: Route) -> None:
        """Remove every via of a route from the via R-tree."""
        if not self._rtree_available or self._via_rtree is None:
            return
        for via in route.vias:
            self._rtree_remove_via(via)

    def rebuild_via_index(self) -> None:
        """Rebuild the via R-tree from scratch from ``self.routes``.

        Used by ``invalidate_spatial_index`` and as a recovery path when
        out-of-band mutations (e.g. ``drc_nudge`` merging vias) leave
        the index stale.  Idempotent.
        """
        if not self._rtree_available:
            return

        # Drop any existing index state.
        self._via_rtree = None
        self._via_rtree_items.clear()
        self._via_rtree_count = 0

        # Re-read inflation from (possibly changed) design rules.
        self._via_rtree_inflation = self._compute_via_rtree_inflation()

        # Re-populate from current routes.
        for route in self.routes:
            for via in route.vias:
                self._rtree_insert_via(via)

    def invalidate_spatial_index(self) -> None:
        """Rebuild the R-tree spatial index with current clearance values.

        Issue #2335: Call this when ``DesignRules`` or net-class clearance
        values change after segments have been indexed (e.g., during
        two-phase routing rule adjustments).  The method re-reads
        ``rules.max_clearance``, clears the existing R-tree structures, and
        re-inserts every indexed segment with the updated inflation.

        Issue #2960: Also rebuilds the via R-tree so its envelopes use the
        new clearance / max-trace-width values.
        """
        if not self._rtree_available:
            return

        # Collect all currently indexed segments before clearing.
        segments_by_layer: dict[int, list[Segment]] = {}
        for layer_idx, items in self._seg_rtree_items.items():
            segments_by_layer[layer_idx] = list(items.values())

        # Clear the R-tree structures.
        self._seg_rtree.clear()
        self._seg_rtree_items.clear()
        self._seg_rtree_count = 0

        # Update inflation from (possibly changed) design rules.
        self._rtree_clearance_inflation = self.rules.max_clearance

        # Re-insert all segments with the new inflation value.
        for layer_idx, segments in segments_by_layer.items():
            for seg in segments:
                self._rtree_insert_segment(seg, layer_idx)

        # Rebuild the via index using the current rules.
        self.rebuild_via_index()

    def mark_route(self, route: Route, max_trace_width: float | None = None) -> None:
        """Mark a route's cells as used.

        Thread-safe when thread_safe=True.

        Issue #1666: Add a 1-cell safety margin to the blocking radius to
        prevent seg-seg clearance violations caused by grid quantization.
        Two parallel traces that each pass grid-level clearance checks can
        still have world-coordinate centerlines closer than
        ``trace_width + 2 * trace_clearance`` when grid snap rounds their
        positions inward.  The extra cell ensures the blocked envelope is
        always at least as large as the geometric clearance requirement.

        Args:
            route: The route to mark on the grid.
            max_trace_width: Maximum trace width across all net classes.
                Passed through to ``_mark_via()`` so via blocking envelopes
                account for the widest trace that may be routed nearby
                (Issue #1692).  Falls back to ``rules.trace_width``.
        """
        with self._acquire_lock():
            for seg in route.segments:
                # Issue #1674: Use seg.width instead of rules.trace_width
                # so wider net-class traces block the correct number of cells.
                total_clearance = seg.width / 2 + self.rules.trace_clearance
                clearance_cells = int(total_clearance / self.resolution) + 1
                # Issue #1666: Add safety margin to prevent grid-quantization
                # clearance violations between parallel traces.
                clearance_cells += 1
                self._mark_segment(seg, clearance_cells=clearance_cells)
            for via in route.vias:
                self._mark_via(via, max_trace_width=max_trace_width)
            self.routes.append(route)
            # Maintain R-tree index for fast clearance queries (Issue #1249)
            self._rtree_insert_route(route)
            # Issue #2960: Mirror via insertions into the via R-tree so
            # ``VectorCollisionChecker.path_is_clear`` can query them in
            # O(log V) instead of walking ``self.routes`` linearly.
            self._rtree_insert_route_vias(route)

    def _mark_segment(self, seg: Segment, clearance_cells: int = 1) -> None:
        """Mark cells along a segment as blocked (with clearance buffer)."""
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)

        layer_idx = self.layer_to_index(seg.layer.value)
        marked_cells: set[tuple[int, int]] = set()

        def mark_with_clearance(gx: int, gy: int) -> None:
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if not cell.blocked:
                            # First time blocking - this is a route cell
                            marked_cells.add((nx, ny))
                            cell.net = seg.net
                        # else: cell already blocked (by pad), don't change net
                        cell.blocked = True

        # Simple line marking
        if gx1 == gx2:  # Vertical
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                mark_with_clearance(gx1, gy)
        elif gy1 == gy2:  # Horizontal
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                mark_with_clearance(gx, gy1)
        else:  # Diagonal - use Bresenham
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                mark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        # Update congestion for all newly marked cells
        for nx, ny in marked_cells:
            self._update_congestion(nx, ny, layer_idx)

    def _mark_via(self, via: Via, max_trace_width: float | None = None) -> None:
        """Mark cells around a via as blocked on ALL layers (through-hole via).

        Issue #2677: Cells reserved via ``reserve_corridor_cells`` are
        skipped for vias whose ``via.net`` is not in the cell's reservation
        set.  This protects inner-layer continuation corridors for paired
        escapes from being colonised by partner-net through-hole vias.
        Reservations are advisory for matching-net vias (the cell is still
        blocked, with the via's net taking ownership).  Vias from
        non-matching nets are blocked as normal on layers WHERE the cell
        is not reserved, and skipped on layers where it IS reserved.

        Issue #2709 (Python-only reservation contract): the corridor
        reservation map (``self._reserved_for_nets``) is consulted ONLY by
        this Python implementation.  The C++ sibling
        ``router::Grid3D::mark_via`` (``cpp/src/grid.cpp``) deliberately
        ignores reservations because the escape phase is Python-grid-only
        today -- ``EscapeRouter`` calls ``Grid.mark_route`` /
        ``Grid._mark_via`` directly and never routes via marking through
        the C++ backend during the paired pre-pass.  The C++ grid does
        receive partner-net vias indirectly (via
        ``RoutingCore._mark_route_on_cpp_grid`` after the escape pass),
        but that path runs AFTER the corridor reservation has served its
        purpose, so cell-block parity does not matter for board 06's
        USB3_TX1+/- fix today.  If/when the escape pass moves into C++
        (likely tied to Epic #2661 Phase 2's group-of-pairs serpentine),
        the C++ ``mark_via`` MUST grow an equivalent reservation check
        or board 06 (and DDR-style boards using the same primitive) will
        regress.  See ``tests/test_grid_cpp_parity.py`` for a regression
        test that pins the current contract.

        Args:
            via: The via to mark.
            max_trace_width: Maximum trace width across all net classes.
                When provided, used as the trace half-width buffer so that
                wide-trace nets routed near this via maintain clearance
                (Issue #1692).  Falls back to ``rules.trace_width``.
        """
        gx, gy = self.world_to_grid(via.x, via.y)
        # Include trace half-width so trace edges maintain via_clearance from via edge
        # Issue #1692: Use the maximum trace width (across all net classes)
        # so that wide-trace nets routed later still clear this via.
        trace_w = max_trace_width if max_trace_width is not None else self.rules.trace_width
        radius = int((via.diameter / 2 + self.rules.via_clearance + trace_w / 2) / self.resolution)
        # Issue #1797: Add safety margin to prevent grid-quantization
        # clearance violations between traces and vias (mirrors the +1
        # applied in mark_route() for segments, see Issue #1666).
        radius += 1

        # Issue #2677: Fast-path when no reservations exist (preserves
        # byte-identical behaviour to pre-fix).
        has_reservations = bool(self._reserved_for_nets)
        via_net = int(via.net) if via.net is not None else 0

        for layer_idx in range(self.num_layers):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        # Issue #2677: Skip cells reserved for a different
                        # net (or net set that excludes via.net).
                        if has_reservations:
                            owners = self._reserved_for_nets.get((layer_idx, ny, nx))
                            if owners is not None and via_net not in owners:
                                continue
                        cell = self.grid[layer_idx][ny][nx]
                        if not cell.blocked:
                            self._update_congestion(nx, ny, layer_idx)
                            cell.net = via.net
                        cell.blocked = True

    # ------------------------------------------------------------------
    # Issue #2677: Corridor reservation API
    # ------------------------------------------------------------------

    def reserve_corridor_cells(
        self,
        layer_idx: int,
        cells: list[tuple[int, int]] | set[tuple[int, int]],
        net_ids: frozenset[int] | set[int] | list[int] | tuple[int, ...],
    ) -> int:
        """Reserve a set of grid cells on a layer for one or more nets.

        Used by ``EscapeRouter._reserve_pair_continuation_corridor`` to
        carve out a hard keep-out region for diff-pair (or match-group)
        continuation through the inner-layer routing channels.  Cells
        marked here cause ``_mark_via`` to skip cells whose ``via.net`` is
        NOT in ``net_ids`` -- so partner-net through-hole vias cannot
        colonise the corridor.

        Cells that are already reserved for a different net set are NOT
        merged: the new reservation replaces the existing one.  This
        keeps the API predictable when multiple paired pre-passes claim
        overlapping cells (the latest claim wins).

        Args:
            layer_idx: Layer index (must be in ``range(self.num_layers)``).
            cells: Iterable of (x, y) grid coordinates to reserve.
            net_ids: One or more net IDs (int) that may consume the
                reserved cells.  For a diff pair this is exactly two IDs
                (P-net and N-net).  For a match group (#2661) this can be
                three or more.  Must not be empty.

        Returns:
            Number of cells newly reserved (existing reservations
            overwritten are counted as new for instrumentation purposes).
        """
        if not (0 <= layer_idx < self.num_layers):
            raise ValueError(
                f"reserve_corridor_cells: layer_idx {layer_idx} out of range [0, {self.num_layers})"
            )
        owners = frozenset(int(n) for n in net_ids)
        if not owners:
            raise ValueError("reserve_corridor_cells: net_ids must not be empty")

        count = 0
        for x, y in cells:
            if 0 <= x < self.cols and 0 <= y < self.rows:
                self._reserved_for_nets[(layer_idx, y, x)] = owners
                count += 1
        return count

    def clear_corridor_reservations(self) -> None:
        """Clear all corridor reservations made via ``reserve_corridor_cells``.

        Intended for test setup / teardown when reusing a grid across
        scenarios.  Production code should not need to call this -- the
        reservations only affect via placement and are harmless once the
        escape pass has completed.
        """
        self._reserved_for_nets.clear()

    def reserved_cell_count(self) -> int:
        """Return the number of currently reserved cells (instrumentation).

        Used by tests to verify that ``EscapeRouter`` made the expected
        corridor reservations before the partner-via marking pass.
        """
        return len(self._reserved_for_nets)

    def is_reserved_for(self, layer_idx: int, x: int, y: int, net_id: int) -> bool:
        """Check whether a cell is reserved for the given net.

        Args:
            layer_idx: Layer index.
            x, y: Grid coordinates.
            net_id: Net ID to check ownership for.

        Returns:
            True if the cell is reserved AND ``net_id`` is in the
            reservation set.  False if the cell is not reserved OR is
            reserved for a different net set.
        """
        owners = self._reserved_for_nets.get((layer_idx, y, x))
        return owners is not None and int(net_id) in owners

    def get_corridor_attractor_bonus(
        self,
        layer_idx: int,
        gx: int,
        gy: int,
        net_id: int,
        bonus: float,
    ) -> float:
        """Return the attractor bonus magnitude for a paired-corridor cell.

        Issue #2911: The corridor reservation primitive from #2677 protects
        cells against partner-net vias but does not tell the main pathfinder
        to ROUTE through the reserved channel.  Without an attractor the
        pathfinder treats a reserved corridor as just another empty cell and
        may complete the pair on a different (more congested) layer or fail
        outright on dense BGAs like board 06.

        Mechanism: A* is corridor-aware as an "attractor" — every cell on
        the reserved layer that owns ``net_id`` returns ``bonus`` from this
        method.  The pathfinder SUBTRACTS this from the cell's step cost
        (clamping at zero to keep g_scores non-negative), nudging the search
        toward dropping a via into the corridor and continuing on the
        reserved layer.  Cells NOT reserved for ``net_id`` (whether
        unreserved or reserved for a different pair) return 0.0.

        N-member-friendly by construction: the underlying reservation
        already supports N-net owner sets (#2661), so a match group with
        three or more members all receive the bonus equally on cells whose
        owners include their net.

        Fast-path: when ``self._reserved_for_nets`` is empty the function
        returns 0.0 immediately, preserving byte-identical pre-#2911
        pathfinder behaviour for boards without any paired corridors.

        Args:
            layer_idx: Grid layer index.
            gx, gy: Grid cell coordinates.
            net_id: Net ID being routed (usually ``start.net`` at the call
                site).
            bonus: Attractor magnitude in cost units, normally
                ``rules.cost_corridor_attractor``.  Returned as-is when
                the cell qualifies; the caller is responsible for the
                sign convention (subtract from positive cost).

        Returns:
            ``bonus`` if the cell is reserved AND ``net_id`` is in the
            owner set.  ``0.0`` otherwise (including the no-reservation
            fast path).
        """
        if not self._reserved_for_nets or bonus <= 0.0:
            return 0.0
        owners = self._reserved_for_nets.get((layer_idx, gy, gx))
        if owners is None:
            return 0.0
        if int(net_id) in owners:
            return bonus
        return 0.0

    def unmark_route(self, route: Route, max_trace_width: float | None = None) -> None:
        """Unmark a route's cells (rip-up). Reverses mark_route().

        Thread-safe when thread_safe=True.

        Issue #1674: Computes clearance per-segment from ``seg.width``
        to match the per-segment marking done by ``mark_route()``.

        Issue #2481: When a paired C++ grid exists (``_cpp_grid``), this
        method also invalidates the C++ stored-via / stored-segment
        snapshot.  Without the invalidation,
        ``Pathfinder::is_via_blocked_diag`` would continue to consult
        already-ripped-up via positions and reject legitimate placements,
        and the post-route validator would compare against stale data.

        Args:
            route: The route to unmark.
            max_trace_width: Must match the value used in ``mark_route()``
                so via cells are cleared symmetrically (Issue #1692).
        """
        removed = False
        with self._acquire_lock():
            for seg in route.segments:
                total_clearance = seg.width / 2 + self.rules.trace_clearance
                clearance_cells = int(total_clearance / self.resolution) + 1
                # Issue #1666: Must match mark_route() safety margin
                clearance_cells += 1
                self._unmark_segment(seg, clearance_cells=clearance_cells)
            for via in route.vias:
                self._unmark_via(via, max_trace_width=max_trace_width)

            if route in self.routes:
                # Remove from R-tree index before removing from list (Issue #1249)
                self._rtree_remove_route(route)
                # Issue #2960: Remove vias from the via R-tree in lock-step
                # with the route list to keep the index consistent for
                # subsequent ``VectorCollisionChecker.path_is_clear`` calls.
                self._rtree_remove_route_vias(route)
                self.routes.remove(route)
                removed = True

        # Issue #2481: After releasing the grid lock, propagate the rip-up
        # to the paired C++ grid (if any) so its ``stored_vias_`` /
        # ``stored_segments_`` no longer reference this route.  We only
        # invalidate when the route was actually in ``self.routes`` --
        # otherwise the snapshot is unaffected by this call.
        if removed:
            cpp_grid = self._cpp_grid
            if cpp_grid is not None:
                invalidate = getattr(cpp_grid, "invalidate_stored_routes", None)
                if invalidate is not None:
                    invalidate()

    def _unmark_segment(self, seg: Segment, clearance_cells: int = 1) -> None:
        """Unmark cells along a segment (clear blocked status and net)."""
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)

        layer_idx = self.layer_to_index(seg.layer.value)

        def unmark_with_clearance(gx: int, gy: int) -> None:
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if cell.pad_blocked:
                            # Don't unblock pad cells, just restore original net
                            cell.net = cell.original_net
                        elif cell.net == seg.net:
                            cell.blocked = False
                            cell.net = 0

        if gx1 == gx2:
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                unmark_with_clearance(gx1, gy)
        elif gy1 == gy2:
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                unmark_with_clearance(gx, gy1)
        else:
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                unmark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

    def _unmark_via(self, via: Via, max_trace_width: float | None = None) -> None:
        """Unmark cells around a via on ALL layers.

        Args:
            via: The via to unmark.
            max_trace_width: Must match the value used in ``_mark_via()``
                so the same cells are cleared (Issue #1692).
        """
        gx, gy = self.world_to_grid(via.x, via.y)
        # Include trace half-width to match _mark_via calculation
        # Issue #1692: Use the same max_trace_width as _mark_via
        trace_w = max_trace_width if max_trace_width is not None else self.rules.trace_width
        radius = int((via.diameter / 2 + self.rules.via_clearance + trace_w / 2) / self.resolution)
        # Issue #1797: Must match _mark_via safety margin so the same
        # cells are cleared during rip-up.
        radius += 1

        for layer_idx in range(self.num_layers):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if cell.pad_blocked:
                            # Don't unblock pad cells, just restore original net
                            cell.net = cell.original_net
                        elif cell.net == via.net:
                            cell.blocked = False
                            cell.net = 0

    # =========================================================================
    # NEGOTIATED CONGESTION ROUTING SUPPORT
    # =========================================================================

    def reset_route_usage(self) -> None:
        """Reset all usage counts (start of new negotiation iteration).

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            self._usage_count.fill(0)

    def mark_route_usage(
        self, route: Route, net_cells: dict[int, set] | None = None
    ) -> set[tuple[int, int, int]]:
        """Mark cells used by a route, incrementing usage count.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            cells_used: set[tuple[int, int, int]] = set()

            for seg in route.segments:
                seg_cells = self._get_segment_cells(seg)
                cells_used.update(seg_cells)

            for via in route.vias:
                via_cells = self._get_via_cells(via)
                cells_used.update(via_cells)

            for gx, gy, layer_idx in cells_used:
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    self.grid[layer_idx][gy][gx].usage_count += 1

            if net_cells is not None:
                if route.net not in net_cells:
                    net_cells[route.net] = set()
                net_cells[route.net].update(cells_used)

            return cells_used

    def unmark_route_usage(self, route: Route, net_cells: dict[int, set] | None = None) -> None:
        """Remove a route's usage (rip-up), decrementing usage count.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            cells_used: set[tuple[int, int, int]] = set()

            for seg in route.segments:
                seg_cells = self._get_segment_cells(seg)
                cells_used.update(seg_cells)

            for via in route.vias:
                via_cells = self._get_via_cells(via)
                cells_used.update(via_cells)

            for gx, gy, layer_idx in cells_used:
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    cell = self.grid[layer_idx][gy][gx]
                    cell.usage_count = max(0, cell.usage_count - 1)

            if net_cells is not None and route.net in net_cells:
                net_cells[route.net] -= cells_used

    def _get_segment_cells(self, seg: Segment) -> set[tuple[int, int, int]]:
        """Get all grid cells occupied by a segment."""
        cells: set[tuple[int, int, int]] = set()
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)
        layer_idx = self.layer_to_index(seg.layer.value)

        if gx1 == gx2:
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                cells.add((gx1, gy, layer_idx))
        elif gy1 == gy2:
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                cells.add((gx, gy1, layer_idx))
        else:
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                cells.add((gx, gy, layer_idx))
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy
        return cells

    def _get_via_cells(self, via: Via) -> set[tuple[int, int, int]]:
        """Get all grid cells occupied by a via (all layers for through-hole)."""
        cells: set[tuple[int, int, int]] = set()
        gx, gy = self.world_to_grid(via.x, via.y)
        for layer_idx in range(self.num_layers):
            cells.add((gx, gy, layer_idx))
        return cells

    def find_pad_ref_at(
        self,
        wx: float,
        wy: float,
        layer_idx: int | None = None,
        max_distance: float | None = None,
    ) -> str | None:
        """Find the component reference whose pad/clearance envelope covers
        the given world coordinate.

        Used by failure analysis to recover the owner of a component-blocked
        grid cell without storing a per-cell ``ref`` field (which would cost
        ~16-32B per cell for millions of cells).

        Args:
            wx: World x-coordinate (mm).
            wy: World y-coordinate (mm).
            layer_idx: Optional layer index to filter SMD pads.  PTH pads
                ignore the layer filter (they block all layers).
            max_distance: Optional bounded search radius from (wx, wy).
                When None, searches the full pad list (O(n)).  Set to
                ``rules.trace_clearance + rules.trace_width`` for fast
                proximity matching.

        Returns:
            The owning component reference, or None if no pad envelope
            (including the clearance halo) covers the point.
        """
        if not self._pads:
            return None

        best_ref: str | None = None
        best_dist = float("inf")

        for pad in self._pads:
            if not pad.ref:
                continue

            # Layer filter: PTH pads block all layers, SMD pads only their own.
            if layer_idx is not None and not pad.through_hole:
                pad_layer_idx = self.layer_to_index(pad.layer.value)
                if pad_layer_idx != layer_idx:
                    continue

            # Issue #2604 follow-up: mirror ``_add_pad_unsafe``'s reduced
            # clearance envelope for fine-pitch pads.  Without this, pads
            # added with ``pin_pitch < fine_pitch_threshold`` would be
            # queried with the larger standard envelope and could
            # false-positive on cells actually blocked by neighbouring
            # standard-pitch pads (relevant on chorus-test U5/U7/U9).
            pad_pitch = self._pad_pin_pitch.get(id(pad))
            clearance = self._clearance_for_pin_pitch(pad_pitch)

            # Compute effective pad bounds (mirrors _add_pad_unsafe logic)
            if pad.through_hole:
                if pad.width > 0 and pad.height > 0:
                    ew, eh = pad.width, pad.height
                elif pad.drill > 0:
                    ew = pad.drill + 0.7
                    eh = ew
                else:
                    ew = 1.7
                    eh = 1.7
            else:
                ew = pad.width
                eh = pad.height

            x1 = pad.x - ew / 2 - clearance
            y1 = pad.y - eh / 2 - clearance
            x2 = pad.x + ew / 2 + clearance
            y2 = pad.y + eh / 2 + clearance

            # Quick bounded-distance prune
            if max_distance is not None:
                dx = max(x1 - wx, 0.0, wx - x2)
                dy = max(y1 - wy, 0.0, wy - y2)
                if dx * dx + dy * dy > max_distance * max_distance:
                    continue

            # Inside the envelope: pick the pad whose center is closest to
            # the query point (handles overlapping clearance envelopes from
            # neighbouring components on the same layer).
            if x1 <= wx <= x2 and y1 <= wy <= y2:
                cdx = wx - pad.x
                cdy = wy - pad.y
                d2 = cdx * cdx + cdy * cdy
                if d2 < best_dist:
                    best_dist = d2
                    best_ref = pad.ref

        return best_ref

    def find_overused_cells(self) -> list[tuple[int, int, int, int]]:
        """Find cells with usage_count > 1 (resource conflicts).

        GPU arrays are synced to CPU for this operation since it returns
        individual cell coordinates.
        """
        # Ensure we're working with CPU arrays for indexing
        usage_arr = to_numpy(self._usage_count) if self.uses_gpu else self._usage_count

        # Find all overused cells using NumPy
        overused_mask = usage_arr > 1
        layer_indices, y_indices, x_indices = np.where(overused_mask)

        # Build result list with usage counts
        overused = []
        for layer_idx, gy, gx in zip(layer_indices, y_indices, x_indices, strict=True):
            usage = int(usage_arr[layer_idx, gy, gx])
            overused.append((int(gx), int(gy), int(layer_idx), usage))
        return overused

    def update_history_costs(self, history_increment: float = 1.0) -> None:
        """Increase history cost for overused cells (PathFinder-style).

        Thread-safe when thread_safe=True.
        GPU-accelerated for large grids.
        """
        with self._acquire_lock():
            # Use current backend's array operations (works for NumPy, CuPy, MLX)
            xp = self._backend

            # Vectorized update: add increment * (usage_count - 1) where usage_count > 1
            overused_mask = self._usage_count > 1

            # Handle dtype conversion for backend
            if self._backend_type == BackendType.CPU:
                usage_float = self._usage_count.astype(np.float32)
            else:
                # CuPy/MLX handle dtype conversion
                usage_float = xp.asarray(self._usage_count, dtype=np.float32)

            increment = history_increment * (usage_float - 1)
            self._history_cost += xp.where(overused_mask, increment, 0)

            # Mark GPU arrays as dirty if using GPU
            if self._backend_type != BackendType.CPU:
                self._gpu_dirty = True

    def update_present_cost_ema(
        self,
        present_cost_factor: float,
        alpha: float = 0.6,
    ) -> None:
        """Update the per-cell present-cost EMA (Issue #2333).

        Smooths the per-cell present cost using an exponential moving
        average to prevent bang-bang oscillation in the PathFinder cost
        model.  The EMA tracks ``present_cost_factor * usage_count``
        for each cell.

        The EMA array is allocated lazily on first call so there is zero
        memory overhead when EMA smoothing is not used.

        Args:
            present_cost_factor: Current present cost multiplier.
            alpha: Weight of the new value (default 0.6).
                ``ema = alpha * new + (1 - alpha) * ema``.
        """
        xp = self._backend

        # Compute current per-cell present cost
        if self._backend_type == BackendType.CPU:
            usage_float = self._usage_count.astype(np.float32)
        else:
            usage_float = xp.asarray(self._usage_count, dtype=np.float32)

        new_present = present_cost_factor * usage_float

        if self._present_cost_ema is None:
            # First call: initialise to the current present cost
            self._present_cost_ema = new_present.copy()
        else:
            self._present_cost_ema = alpha * new_present + (1.0 - alpha) * self._present_cost_ema

    def get_negotiated_cost(
        self,
        gx: int,
        gy: int,
        layer: int,
        present_cost_factor: float = 1.0,
        net: int | None = None,
    ) -> float:
        """Get the negotiated congestion cost for a cell.

        Args:
            gx, gy, layer: Grid coordinates / layer index of the cell.
            present_cost_factor: Adaptive present-cost weight.
            net: Optional routing-net context.  When provided AND the
                cell's net matches, an ``is_obstacle`` cell is treated
                as reachable (returns a finite cost) rather than hard
                ``inf``.  This is the cost-side mirror of the
                ``cell.net != net`` filter applied in the pathfinder's
                ``_is_trace_blocked`` / ``_is_via_blocked`` predicates
                (PR #2965 pattern).  Without this, post-PR #2928 the
                destination pad's own metal -- which is now marked
                ``is_obstacle=True`` on first touch -- yields ``inf``
                cost and A* cannot expand into it, leaving endpoint
                pads (e.g. NRST/BOOT0 on board 04) unreachable.
                Defaults to ``None`` (legacy behavior: any obstacle
                cell is ``inf``).
        """
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return float("inf")

        cell = self.grid[layer][gy][gx]

        # Issue #2963: own-net obstacle cells (e.g. the destination
        # pad's own metal after PR #2928's first-touch marking) must
        # remain reachable for the routing net.  Foreign-net obstacles
        # still hard-reject.
        if cell.is_obstacle and (net is None or cell.net != net):
            return float("inf")

        # Issue #2333: Use EMA-smoothed present cost when available
        if self._present_cost_ema is not None:
            present_cost = float(self._present_cost_ema[layer, gy, gx])
        else:
            present_cost = present_cost_factor * cell.usage_count
        history_cost = cell.history_cost

        return present_cost + history_cost

    def get_layer_fill_ratios(self) -> np.ndarray:
        """Return per-layer fill ratios as an array of shape ``(num_layers,)``.

        The fill ratio for each layer is the fraction of *routable* cells that
        are currently occupied (``_usage_count > 0``).  Permanently blocked
        cells (obstacles, pads of other nets) are excluded from the denominator
        so that layers with large keep-out areas still report an accurate
        utilization figure.

        The computation is a single vectorized NumPy reduction and is cheap
        enough to call after every net routes.

        Issue #2275: Used by the A* pathfinder to penalize over-utilized
        layers, encouraging the router to spread traces across all available
        layers.
        """
        xp = self._backend
        used_per_layer = xp.sum(self._usage_count > 0, axis=(1, 2))
        blocked_per_layer = xp.sum(self._blocked, axis=(1, 2))
        total_cells = self.rows * self.cols
        routable_per_layer = total_cells - blocked_per_layer
        # Avoid division by zero for fully-blocked layers
        routable_per_layer = xp.maximum(routable_per_layer, 1)
        ratios = used_per_layer.astype(np.float64) / routable_per_layer.astype(np.float64)
        # Ensure we return a plain NumPy array regardless of backend
        return to_numpy(ratios) if not isinstance(ratios, np.ndarray) else ratios

    def compute_expanded_blocked(
        self,
        radius: int,
        net: int,
        allow_sharing: bool = False,
        partner_net: int | None = None,
        partner_radius: int | None = None,
        partner_active: bool | None = None,
    ) -> np.ndarray:
        """Pre-compute an expanded blocked bitmap for trace-width clearance.

        Issue #2430: Instead of extracting NumPy sub-arrays per neighbor in
        ``_is_trace_blocked``, dilate the blocked bitmap once at the start
        of each ``route()`` call.  The per-neighbor check then becomes a
        single array lookup: ``expanded[layer, ny, nx]``.

        The dilation uses ``scipy.ndimage.maximum_filter`` when available,
        falling back to a pure-NumPy sliding-window maximum.

        Issue #2559 / Epic #2556 Phase 1C: When ``partner_net`` is provided
        with a tighter ``partner_radius`` (< ``radius``), partner-owned
        blocked cells are dilated by the tighter radius and OR-combined
        with the wider dilation of all other foreign-net blocked cells.
        This implements within-pair clearance for differential pairs while
        leaving the per-cell logic in ``_is_trace_blocked`` consistent.

        Args:
            radius: Half-width of the trace in grid cells (``_trace_half_width_cells``).
            net: Net ID of the route being planned.  Same-net cells are
                 *not* treated as blocked (consistent with ``_is_trace_blocked``).
            allow_sharing: If True (negotiated mode), only static obstacles
                           block; shared-usage cells are passable.
            partner_net: Issue #2559 / Phase 1C -- when set (non-None and
                         >= 0), cells whose net matches this id are dilated
                         using ``partner_radius`` instead of ``radius``.
                         When ``None``, the partner branch is dormant and
                         behavior matches pre-#2559 (single-radius dilation).
            partner_radius: Tighter half-width (in grid cells) for partner
                            cells.  Ignored when ``partner_net`` is ``None``
                            or ``partner_radius >= radius``.
            partner_active: Issue #2715 -- pre-computed dormant/active flag.
                            Callers that already know whether the partner
                            branch is dormant can pass the cached bool here
                            so the per-call 4-condition tuple evaluation is
                            skipped.  When ``None`` (legacy callers), the
                            boolean is derived from ``partner_net``/
                            ``partner_radius`` for backward compatibility.

        Returns:
            Boolean NumPy array of shape ``(num_layers, rows, cols)`` where
            ``True`` means the cell is blocked for this net considering
            trace width expansion.
        """
        if allow_sharing:
            # Negotiated mode: block only static obstacles with different net
            different_net = self._net != net
            obstacle_blocks = self._blocked & self._is_obstacle & different_net
            static_blocks = (
                self._blocked & ~self._is_obstacle & different_net & (self._usage_count == 0)
            )
            base_blocked = obstacle_blocks | static_blocks
        else:
            # Standard mode: block cells that are blocked AND different net
            base_blocked = self._blocked & (self._net != net)

        if partner_active is None:
            partner_active = (
                partner_net is not None
                and partner_net >= 0
                and partner_radius is not None
                and partner_radius < radius
            )

        if partner_active:
            # Split base_blocked into "partner cells" and "non-partner cells".
            # Partner cells get dilated by the tighter partner_radius; the
            # rest of the foreign-net cells get the wider radius.  The
            # final mask is the OR of the two dilations.
            partner_mask = base_blocked & (self._net == partner_net)
            other_mask = base_blocked & ~partner_mask

            other_expanded = self._dilate_blocked(other_mask, radius)
            partner_expanded = self._dilate_blocked(partner_mask, partner_radius)
            return other_expanded | partner_expanded

        return self._dilate_blocked(base_blocked, radius)

    def _dilate_blocked(self, base_blocked: np.ndarray, radius: int) -> np.ndarray:
        """Dilate a boolean blocked mask by ``radius`` cells (Chebyshev).

        Helper extracted from :meth:`compute_expanded_blocked` so the same
        kernel logic can be applied independently to the partner-cells
        mask and the non-partner-cells mask (Issue #2559 / Phase 1C).
        """
        if radius <= 0:
            return base_blocked.copy() if isinstance(base_blocked, np.ndarray) else base_blocked
        if radius <= 1:
            # No expansion needed; single-cell check is equivalent.
            return base_blocked

        # Dilate by *radius* using a square structuring element of side 2*radius+1.
        kernel_size = 2 * radius + 1
        try:
            from scipy.ndimage import maximum_filter  # type: ignore[import-untyped]

            # maximum_filter on bool is equivalent to dilation (any-True in window).
            expanded = maximum_filter(
                base_blocked.astype(np.uint8),
                size=(1, kernel_size, kernel_size),
            ).astype(np.bool_)
        except ImportError:
            # Fallback: pure-NumPy sliding-window maximum via np.lib.stride_tricks.
            # Pad the array so that out-of-bounds cells count as blocked.
            padded = np.ones(
                (
                    self.num_layers,
                    self.rows + 2 * radius,
                    self.cols + 2 * radius,
                ),
                dtype=np.bool_,
            )
            padded[
                :,
                radius : radius + self.rows,
                radius : radius + self.cols,
            ] = base_blocked

            expanded = np.zeros_like(base_blocked)
            for dy in range(kernel_size):
                for dx in range(kernel_size):
                    expanded |= padded[:, dy : dy + self.rows, dx : dx + self.cols]

        return expanded

    def get_total_overflow(self) -> int:
        """Get total overflow (sum of usage_count - 1 for overused cells).

        GPU-accelerated when using GPU backend (reduction operation).
        """
        xp = self._backend

        # Vectorized calculation: sum of (usage - 1) where usage > 1
        overused = self._usage_count > 1
        result = xp.sum(xp.where(overused, self._usage_count - 1, 0))

        # Convert to Python int (works for all backends)
        if hasattr(result, "get"):  # CuPy
            return int(result.get())
        elif hasattr(result, "item"):  # NumPy/MLX
            return int(result.item())
        return int(result)

    # =========================================================================
    # NEIGHBORHOOD RIP-UP SUPPORT (Issue #2274)
    # =========================================================================

    def temporarily_unblock_routed_nets(self) -> "RoutedNetsUnblocker":
        """Return a context manager that temporarily unblocks routed-net cells.

        Static obstacles (pads, board edges, zones marked with ``pad_blocked``)
        are preserved.  Only cells that are blocked by routed traces
        (``blocked=True``, ``pad_blocked=False``, ``net != 0``) are cleared
        on entry and restored on exit.

        This is used by relaxed A* to find a path ignoring routed nets so
        that neighborhood rip-up can identify true blockers.

        Returns:
            A context manager that saves/restores blocked and net arrays.
        """
        return RoutedNetsUnblocker(self)

    # =========================================================================
    # ZONE (COPPER POUR) SUPPORT
    # =========================================================================

    def add_zone_cells(
        self,
        zone: "Zone",
        filled_cells: set[tuple[int, int]],
        layer_index: int,
    ) -> None:
        """Mark grid cells as belonging to a zone.

        Thread-safe when thread_safe=True.

        Args:
            zone: Zone definition (for net and uuid)
            filled_cells: Set of (gx, gy) grid coordinates to mark
            layer_index: Grid layer index
        """
        from kicad_tools.schema.pcb import Zone as ZoneType  # noqa: F401

        with self._acquire_lock():
            for gx, gy in filled_cells:
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    cell = self.grid[layer_index][gy][gx]
                    cell.is_zone = True
                    cell.zone_id = zone.uuid
                    cell.net = zone.net_number
                    # Zone copper is not an obstacle - routes can pass through same-net zones

    def clear_zones(self, layer_index: int | None = None) -> None:
        """Remove all zone markings from the grid.

        Thread-safe when thread_safe=True.

        Args:
            layer_index: If specified, only clear this layer. Otherwise clear all.
        """
        with self._acquire_lock():
            if layer_index is not None:
                layers_to_clear = [layer_index]
            else:
                layers_to_clear = list(range(self.num_layers))

            for layer_idx in layers_to_clear:
                # Find zone cells that should have net cleared
                zone_mask = self._is_zone[layer_idx]
                clear_net_mask = (
                    zone_mask & ~self._is_obstacle[layer_idx] & ~self._blocked[layer_idx]
                )

                # Clear nets where applicable
                self._net[layer_idx] = np.where(clear_net_mask, 0, self._net[layer_idx])

                # Clear zone flags
                self._is_zone[layer_idx] = False

                # Clear zone IDs for this layer from sparse storage
                keys_to_remove = [k for k in self._zone_ids if k[0] == layer_idx]
                for key in keys_to_remove:
                    del self._zone_ids[key]

    def get_zone_cells(self, layer_index: int, zone_id: str | None = None) -> set[tuple[int, int]]:
        """Get all cells belonging to zones on a layer.

        Args:
            layer_index: Grid layer index
            zone_id: If specified, only return cells for this zone

        Returns:
            Set of (gx, gy) coordinates
        """
        if zone_id is None:
            # Get all zone cells using NumPy
            y_indices, x_indices = np.where(self._is_zone[layer_index])
            return {(int(x), int(y)) for x, y in zip(x_indices, y_indices, strict=True)}
        else:
            # Filter by zone_id using sparse storage
            return {
                (k[2], k[1])
                for k, v in self._zone_ids.items()
                if k[0] == layer_index and v == zone_id
            }

    def is_zone_cell(self, gx: int, gy: int, layer_index: int) -> bool:
        """Check if a cell is part of a zone.

        Args:
            gx, gy: Grid coordinates
            layer_index: Grid layer index

        Returns:
            True if cell is marked as zone copper
        """
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return False
        return bool(self._is_zone[layer_index, gy, gx])

    # =========================================================================
    # CORRIDOR PREFERENCE SUPPORT (TWO-PHASE ROUTING)
    # =========================================================================

    def set_corridor_preference(
        self, corridor: any, net: int, penalty: float | None = None
    ) -> None:
        """Set a corridor preference for a net during two-phase routing.

        The pathfinder will add a cost penalty when routing this net
        outside its assigned corridor.

        Thread-safe when thread_safe=True.

        Args:
            corridor: The Corridor from global routing (sparse.Corridor)
            net: Net ID this corridor is assigned to
            penalty: Cost penalty multiplier for leaving corridor (default: 5.0)
        """
        with self._acquire_lock():
            self._corridor_preferences[net] = corridor
            if penalty is not None:
                self._corridor_penalty = penalty

    def clear_corridor_preference(self, net: int) -> None:
        """Remove corridor preference for a net.

        Thread-safe when thread_safe=True.

        Args:
            net: Net ID whose corridor preference to remove
        """
        with self._acquire_lock():
            self._corridor_preferences.pop(net, None)

    def clear_all_corridor_preferences(self) -> None:
        """Remove all corridor preferences.

        Thread-safe when thread_safe=True.
        """
        with self._acquire_lock():
            self._corridor_preferences.clear()

    def get_corridor_cost(self, gx: int, gy: int, layer: int, net: int) -> float:
        """Get corridor cost penalty for a cell.

        Returns additional cost if the cell is outside the net's assigned
        corridor (if any). This guides detailed routing to stay within
        the corridor established during global routing.

        Args:
            gx, gy: Grid coordinates
            layer: Grid layer index
            net: Net being routed

        Returns:
            Additional cost (0 if inside corridor or no corridor assigned)
        """
        corridor = self._corridor_preferences.get(net)
        if corridor is None:
            return 0.0

        # Convert grid to world coordinates
        x, y = self.grid_to_world(gx, gy)

        # Check if point is inside corridor
        if corridor.contains_point(x, y, layer):
            return 0.0

        # Outside corridor - apply penalty
        return self._corridor_penalty

    def has_corridor_preference(self, net: int) -> bool:
        """Check if a net has an assigned corridor.

        Args:
            net: Net ID to check

        Returns:
            True if net has a corridor preference set
        """
        return net in self._corridor_preferences

    def get_corridor_statistics(self) -> dict:
        """Get statistics about corridor preferences.

        Returns:
            Dictionary with corridor stats
        """
        return {
            "corridors_assigned": len(self._corridor_preferences),
            "corridor_penalty": self._corridor_penalty,
            "nets_with_corridors": list(self._corridor_preferences.keys()),
        }

    # =========================================================================
    # BOARD EDGE CLEARANCE SUPPORT
    # =========================================================================

    def add_edge_keepout(
        self,
        edge_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        clearance: float,
    ) -> int:
        """Block cells within clearance distance of board edge segments.

        This prevents routes from being placed too close to the board edge,
        which would violate copper-to-edge clearance DRC rules.

        Thread-safe when thread_safe=True.

        Args:
            edge_segments: List of (start, end) tuples defining edge line segments.
                          Each segment is ((x1, y1), (x2, y2)) in world coordinates.
            clearance: Edge clearance distance in mm.

        Returns:
            Number of cells blocked.
        """
        with self._acquire_lock():
            if clearance <= 0 or not edge_segments:
                return 0

            blocked_count = 0
            clearance_cells = int(clearance / self.resolution) + 1

            # Get all routable layer indices
            layer_indices = self.get_routable_indices()

            for (x1, y1), (x2, y2) in edge_segments:
                # Mark cells along each edge segment with clearance buffer
                blocked_count += self._mark_edge_segment_keepout(
                    x1, y1, x2, y2, clearance_cells, layer_indices
                )

            return blocked_count

    def _mark_edge_segment_keepout(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        clearance_cells: int,
        layer_indices: list[int],
    ) -> int:
        """Mark cells within clearance of a single edge segment as blocked.

        Uses Bresenham's algorithm to walk along the segment and blocks all
        cells within the clearance distance on all routable layers.

        Args:
            x1, y1: Start point in world coordinates
            x2, y2: End point in world coordinates
            clearance_cells: Number of grid cells for clearance buffer
            layer_indices: Grid indices of layers to block

        Returns:
            Number of cells blocked.
        """
        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        blocked_count = 0
        blocked_cells: set[tuple[int, int]] = set()

        def mark_with_clearance(gx: int, gy: int) -> None:
            """Mark cells within clearance radius of a point."""
            nonlocal blocked_count
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if (nx, ny) in blocked_cells:
                        continue
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        # Check if within circular clearance (not square)
                        if dx * dx + dy * dy <= clearance_cells * clearance_cells:
                            blocked_cells.add((nx, ny))
                            for layer_idx in layer_indices:
                                cell = self.grid[layer_idx][ny][nx]
                                if not cell.blocked:
                                    cell.blocked = True
                                    cell.is_obstacle = True
                                    blocked_count += 1

        # Walk along the segment using Bresenham's algorithm
        if gx1 == gx2:  # Vertical line
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                mark_with_clearance(gx1, gy)
        elif gy1 == gy2:  # Horizontal line
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                mark_with_clearance(gx, gy1)
        else:  # Diagonal - use Bresenham
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                mark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        return blocked_count

    # =========================================================================
    # FACTORY METHODS FOR OPTIMIZED GRID CONFIGURATIONS
    # =========================================================================

    @classmethod
    def create_expanded(
        cls,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: "LayerStack | None" = None,
    ) -> "RoutingGrid":
        """Create a grid with expanded obstacles for faster routing.

        This factory method creates a grid optimized for performance:
        - Uses trace_width as grid resolution (coarser than clearance-based)
        - Pre-expands all obstacles to include clearance zones
        - Suitable for JLCPCB and similar tight-clearance designs

        Performance comparison (65x56mm board, 0.127mm clearance):
        - Standard grid (0.0635mm): ~900,000 cells, ~120s routing
        - Expanded grid (0.127mm): ~225,000 cells, ~30s routing

        Args:
            width, height: Board dimensions
            rules: Design rules
            origin_x, origin_y: Board origin
            layer_stack: Layer configuration

        Returns:
            RoutingGrid with expanded obstacle mode enabled
        """
        return cls(
            width=width,
            height=height,
            rules=rules,
            origin_x=origin_x,
            origin_y=origin_y,
            layer_stack=layer_stack,
            expanded_obstacles=True,
            resolution_override=max(rules.trace_width, rules.trace_clearance),
        )

    @classmethod
    def create_adaptive(
        cls,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: "LayerStack | None" = None,
        target_cells: int = 500000,
    ) -> "RoutingGrid":
        """Create a grid with adaptive resolution based on board size.

        Automatically calculates resolution to keep total cells near target,
        balancing routing accuracy against performance.

        For JLCPCB-compatible boards (5mil clearance):
        - Small boards (<50mm): Fine resolution for accuracy
        - Large boards (>100mm): Coarser resolution for performance

        Args:
            width, height: Board dimensions
            rules: Design rules
            origin_x, origin_y: Board origin
            layer_stack: Layer configuration
            target_cells: Target number of grid cells (default: 500k)

        Returns:
            RoutingGrid with adaptive resolution
        """
        # Calculate resolution needed to achieve target cell count
        # cells = (width / res) * (height / res) * layers
        num_layers = (layer_stack or LayerStack.two_layer()).num_layers
        area = width * height

        # Solve for resolution: res = sqrt(area * layers / target_cells)
        optimal_res = (area * num_layers / target_cells) ** 0.5

        # Clamp to reasonable bounds
        min_res = rules.trace_clearance / 2  # Never finer than clearance/2
        max_res = rules.trace_width * 2  # Never coarser than 2x trace width

        resolution = max(min_res, min(max_res, optimal_res))

        # Use expanded obstacles if resolution is coarser than clearance
        use_expanded = resolution > rules.trace_clearance

        return cls(
            width=width,
            height=height,
            rules=rules,
            origin_x=origin_x,
            origin_y=origin_y,
            layer_stack=layer_stack,
            expanded_obstacles=use_expanded,
            resolution_override=resolution,
        )

    def add_pad_vectorized(self, pad: Pad) -> None:
        """Add a pad using vectorized NumPy operations for better performance.

        This method uses pre-computed circular masks and array slicing
        instead of per-cell loops, providing ~5x speedup for pad addition.

        Thread-safe when thread_safe=True.

        Args:
            pad: Pad to add to the grid
        """
        with self._acquire_lock():
            self._add_pad_vectorized_unsafe(pad)

    def _add_pad_vectorized_unsafe(self, pad: Pad) -> None:
        """Internal vectorized pad addition without locking."""
        # Clearance model: trace clearance + trace half-width from pad edge.
        # The pathfinder checks if the trace CENTER can be placed at a cell,
        # so we must block cells where the trace edge would violate clearance.
        clearance = self.rules.trace_clearance + self.rules.trace_width / 2

        # Determine effective dimensions
        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif pad.drill > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        # Calculate affected region in grid coordinates
        half_w = effective_width / 2 + clearance
        half_h = effective_height / 2 + clearance

        x1, y1 = pad.x - half_w, pad.y - half_h
        x2, y2 = pad.x + half_w, pad.y + half_h

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        # Clamp to grid bounds
        gx1 = max(0, gx1)
        gy1 = max(0, gy1)
        gx2 = min(self.cols - 1, gx2)
        gy2 = min(self.rows - 1, gy2)

        # Determine affected layers
        if pad.through_hole:
            layers = list(range(self.num_layers))
        else:
            layers = [self.layer_to_index(pad.layer.value)]

        # Calculate pad metal area bounds (without clearance)
        # Issue #996: Use ceil/floor to ensure we only mark cells whose CENTER
        # is inside the metal area, not cells that are merely nearby.
        # round() would include cells whose center is outside the metal area.
        metal_half_w = effective_width / 2
        metal_half_h = effective_height / 2
        metal_x1, metal_y1 = pad.x - metal_half_w, pad.y - metal_half_h
        metal_x2, metal_y2 = pad.x + metal_half_w, pad.y + metal_half_h
        metal_gx1 = int(math.ceil((metal_x1 - self.origin_x) / self.resolution))
        metal_gy1 = int(math.ceil((metal_y1 - self.origin_y) / self.resolution))
        metal_gx2 = int(math.floor((metal_x2 - self.origin_x) / self.resolution))
        metal_gy2 = int(math.floor((metal_y2 - self.origin_y) / self.resolution))

        # Get center coordinates
        center_gx, center_gy = self.world_to_grid(pad.x, pad.y)

        # Vectorized update for each layer
        for layer_idx in layers:
            # Block the entire clearance zone
            self._blocked[layer_idx, gy1 : gy2 + 1, gx1 : gx2 + 1] = True
            self._original_net[layer_idx, gy1 : gy2 + 1, gx1 : gx2 + 1] = pad.net

            # Issue #996: Only mark metal area as pad-blocked, not clearance zone.
            # This allows the router to distinguish actual pad copper from clearance.
            # Set net for metal area
            metal_gy1_clamped = max(0, metal_gy1)
            metal_gy2_clamped = min(self.rows - 1, metal_gy2)
            metal_gx1_clamped = max(0, metal_gx1)
            metal_gx2_clamped = min(self.cols - 1, metal_gx2)

            # Only set net where it's currently 0 (avoid overwriting other pads)
            metal_slice = (
                layer_idx,
                slice(metal_gy1_clamped, metal_gy2_clamped + 1),
                slice(metal_gx1_clamped, metal_gx2_clamped + 1),
            )
            net_slice = self._net[metal_slice]
            self._net[metal_slice] = np.where(net_slice == 0, pad.net, net_slice)

            # Issue #996: Mark only metal area as pad-blocked (not clearance zone)
            self._pad_blocked[metal_slice] = True

            # Mark center cell with this pad's net
            if 0 <= center_gx < self.cols and 0 <= center_gy < self.rows:
                self._net[layer_idx, center_gy, center_gx] = pad.net
                self._original_net[layer_idx, center_gy, center_gx] = pad.net

    def get_grid_statistics(self) -> dict:
        """Get statistics about grid usage and memory.

        Returns:
            Dict with grid statistics for performance analysis
        """
        total_cells = self.cols * self.rows * self.num_layers

        # Ensure arrays are on CPU for statistics
        blocked_arr = to_numpy(self._blocked) if self.uses_gpu else self._blocked
        pad_arr = to_numpy(self._pad_blocked) if self.uses_gpu else self._pad_blocked

        blocked_cells = int(np.sum(blocked_arr))
        pad_cells = int(np.sum(pad_arr))

        # Get memory usage (works for both NumPy and CuPy)
        def get_nbytes(arr: Any) -> int:
            if hasattr(arr, "nbytes"):
                return arr.nbytes
            # MLX arrays don't have nbytes, estimate from shape and dtype
            return arr.size * np.dtype(arr.dtype).itemsize

        memory_bytes = sum(
            get_nbytes(arr)
            for arr in [
                self._blocked,
                self._net,
                self._usage_count,
                self._history_cost,
                self._is_obstacle,
                self._is_zone,
                self._pad_blocked,
                self._original_net,
            ]
        )

        return {
            "resolution_mm": self.resolution,
            "cols": self.cols,
            "rows": self.rows,
            "layers": self.num_layers,
            "total_cells": total_cells,
            "blocked_cells": blocked_cells,
            "blocked_percent": round(100 * blocked_cells / total_cells, 1),
            "pad_cells": pad_cells,
            "expanded_obstacles": self.expanded_obstacles,
            "thread_safe": self._thread_safe,
            "gpu_backend": self._backend_type.value,
            "uses_gpu": self.uses_gpu,
            "memory_mb": round(memory_bytes / (1024 * 1024), 2),
        }
