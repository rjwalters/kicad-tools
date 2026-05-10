"""
C++ router backend with Python fallback.

This module provides a unified interface to the router that automatically
uses the C++ implementation when available, falling back to pure Python.

The C++ backend provides 10-100x speedup for the core A* loop and grid
operations, making fine-grid routing (0.0635mm) practical for production use.

If you are seeing slow routing performance, the C++ backend is likely not
installed. Build it with:

    kct build-native

Or check its status with:

    kct build-native --check
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from .grid import RoutingGrid
    from .pathfinder import Router
    from .primitives import Pad, Route
    from .rules import DesignRules, NetClassRouting

logger = logging.getLogger(__name__)

# Required C++ binding-surface version (Issue #2501).
#
# This MUST match ``ROUTER_CPP_BUILD_VERSION`` in
# ``src/kicad_tools/router/cpp/include/types.hpp``.  Bump both constants in any
# PR that changes the bindings.cpp surface (added/removed/renamed symbols,
# struct fields, function signatures).
#
# When ``router_cpp.BUILD_VERSION`` does not match this value, the compiled
# ``.so`` is older than the source tree and would otherwise raise
# ``AttributeError`` deep in the routing code (e.g. ``router_cpp.PadBounds``
# missing).  The guard below catches that at import time and falls back to the
# pure-Python router with an actionable ``kct build-native`` hint.
_REQUIRED_CPP_BUILD_VERSION = 4

# Try to import C++ module with detailed error tracking
_CPP_IMPORT_ERROR: str | None = None
try:
    from . import router_cpp

    _CPP_AVAILABLE = True
except ImportError as e:
    _CPP_AVAILABLE = False
    _CPP_IMPORT_ERROR = str(e)
    router_cpp = None  # type: ignore

    # Check for common cause: .so built for a different Python version
    import glob
    import pathlib
    import sys

    _so_files = glob.glob(
        str(pathlib.Path(__file__).parent / "router_cpp.cpython-*-darwin.so")
    ) + glob.glob(str(pathlib.Path(__file__).parent / "router_cpp.cpython-*-linux-*.so"))
    # Issue #2514: Distinguish "no compiled extension" from a genuine
    # circular import.  When ``from . import router_cpp`` runs while
    # ``kicad_tools.router.__init__`` is still mid-import, Python's
    # ``ImportError.__str__`` mentions "partially initialized module
    # (most likely due to a circular import)" -- even when the actual
    # root cause is a missing ``.so`` file (e.g. fresh checkout where
    # ``kct build-native`` was never run).  Replace the misleading
    # message with an actionable hint when no ``.so`` is present at all.
    if not _so_files:
        _CPP_IMPORT_ERROR = (
            "C++ router extension not built (no router_cpp.*.so found in "
            f"{pathlib.Path(__file__).parent}). "
            "Run: kct build-native"
        )
    else:
        _running_tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
        if _running_tag not in " ".join(_so_files):
            _CPP_IMPORT_ERROR = (
                f"C++ backend was built for {', '.join(pathlib.Path(f).name for f in _so_files)} "
                f"but running Python {sys.version_info.major}.{sys.version_info.minor} "
                f"({_running_tag}). Rebuild with: kicad-tools build-native"
            )

# Stale-.so guard (Issue #2501): the import succeeded, but the compiled
# binding surface may predate the current source tree.  A mismatch here
# means new symbols (e.g. ``PadBounds``, ``FAILURE_NONE``) referenced by
# this module are absent from the loaded ``.so`` and would otherwise
# raise ``AttributeError`` at routing time.  Disable the backend cleanly
# with a clear rebuild hint that routes through the existing fallback path.
if _CPP_AVAILABLE:
    _actual_build_version = getattr(router_cpp, "BUILD_VERSION", None)
    if _actual_build_version != _REQUIRED_CPP_BUILD_VERSION:
        _CPP_AVAILABLE = False
        _CPP_IMPORT_ERROR = (
            f"router_cpp build version {_actual_build_version!r} does not match "
            f"required {_REQUIRED_CPP_BUILD_VERSION}. The compiled .so is stale "
            f"relative to the C++ source tree. Rebuild with: kct build-native"
        )
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


def _reload_cpp_backend() -> bool:
    """Reload the cpp_backend module after a successful build.

    After ``build_native()`` writes a new ``router_cpp.*.so`` into the
    package directory, the in-process module-level globals
    ``_CPP_AVAILABLE`` and ``router_cpp`` are still ``False`` / ``None``
    from the original failed import.  Reloading the module re-runs the
    top-level import block, populating those globals so subsequent calls
    to :func:`is_cpp_available` return ``True`` and the C++ backend is
    actually used by the routing code.

    Issue #2594: ``importlib.reload(cpp_backend)`` alone is *not* enough.
    The original ``from . import router_cpp`` failed at startup, leaving:

      1. A negative/partially-initialised entry for
         ``kicad_tools.router.router_cpp`` in :data:`sys.modules` that
         Python re-uses on subsequent imports instead of re-running the
         module finder.
      2. A cached directory listing on the parent package's
         ``FileFinder`` that does not yet contain the freshly-written
         ``router_cpp.*.so``.

    To pick up the new ``.so`` in the same process we MUST drop the
    stale ``sys.modules`` entry AND call
    :func:`importlib.invalidate_caches` before the reload.  This is the
    safe case: the previous attempt never succeeded, so there is no
    initialised native module to clash with.

    Returns:
        ``True`` if the backend is available after reload, ``False`` otherwise.
    """
    global _CPP_AVAILABLE, _CPP_IMPORT_ERROR, router_cpp

    import importlib
    import sys as _sys

    # 1. Drop any stale negative/partial entry for the C++ extension itself.
    #    A failed ``from . import router_cpp`` at startup leaves a None or
    #    partially-initialised module in sys.modules that Python re-uses
    #    on subsequent imports.
    _sys.modules.pop("kicad_tools.router.router_cpp", None)

    # 2. Tell the import machinery to re-scan filesystem-based finders
    #    for the freshly-written .so. Without this, the parent package's
    #    FileFinder may keep its cached directory listing from before the
    #    build wrote the new file.
    importlib.invalidate_caches()

    module_name = __name__  # "kicad_tools.router.cpp_backend"
    module = _sys.modules.get(module_name)
    if module is None:
        return _CPP_AVAILABLE

    try:
        importlib.reload(module)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to reload cpp_backend after build: %s", exc)
        return _CPP_AVAILABLE

    # Mirror the freshly-imported globals into this module's namespace so
    # callers that already have references to functions in this module
    # (e.g. ``is_cpp_available``) see the new state.
    _CPP_AVAILABLE = getattr(module, "_CPP_AVAILABLE", False)
    _CPP_IMPORT_ERROR = getattr(module, "_CPP_IMPORT_ERROR", None)
    router_cpp = getattr(module, "router_cpp", None)
    return _CPP_AVAILABLE


def ensure_cpp_backend_available(
    *,
    backend: str = "auto",
    quiet: bool = False,
    allow_auto_build: bool = True,
) -> tuple[bool, bool, int | None]:
    """Resolve C++ backend availability, auto-building if needed (Issue #2549).

    Consolidates the four near-identical "Handle backend selection" blocks
    that previously lived in ``route_cmd.py``.  Honors the user's explicit
    ``--backend`` choice and silently auto-builds the C++ extension on first
    use when ``--backend`` is ``auto`` (the default) and the ``router_cpp.*.so``
    is missing or stale.

    Auto-build is skipped under any of:
      - ``backend == "python"`` (user explicitly chose Python)
      - ``backend == "cpp"`` (handled separately: hard error if unavailable)
      - ``allow_auto_build=False`` (caller opt-out, e.g. ``--no-auto-build-native``)
      - ``KICAD_TOOLS_NO_AUTO_BUILD=1`` env var (sandbox / CI escape hatch)
      - cmake or a C++ compiler not present on PATH (cheap pre-check)

    On build failure (any reason: missing toolchain, timeout, verification
    error, sandbox without write permission), this function falls through
    to the existing Python-fallback warning path.  It never raises -- the
    intent is to keep ``kct route`` working even when the build fails.

    Args:
        backend: One of ``"auto"``, ``"cpp"``, or ``"python"``.  Mirrors the
            ``--backend`` CLI flag.
        quiet: Suppress informational and warning output.
        allow_auto_build: If ``False``, never attempt auto-build.  Used by
            tests and ``--no-auto-build-native``.

    Returns:
        A tuple ``(ok, force_python, exit_code)`` where:
          - ``ok``: ``True`` if the caller should proceed with routing.
            ``False`` only when ``backend=="cpp"`` and the backend is
            unavailable (caller should ``return exit_code``).
          - ``force_python``: ``True`` when the Python backend should be
            used (``--backend python`` or auto-fallback after build failure).
          - ``exit_code``: ``1`` when ``backend=="cpp"`` and unavailable,
            ``None`` otherwise.
    """
    import sys

    def _emit(msg: str, *, file=None) -> None:
        if quiet:
            return
        if file is None:
            print(msg, flush=True)
        else:
            print(msg, file=file, flush=True)

    # --backend python: honor user intent, never attempt auto-build.
    if backend == "python":
        return True, True, None

    # --backend cpp: hard error if unavailable (existing behavior).  We
    # still attempt an auto-build when the user explicitly selected cpp,
    # because that matches the user's intent ("I want C++"), but we
    # surface a hard error if the build fails so the user knows the
    # request was honored as best as possible.
    if backend == "cpp":
        if is_cpp_available():
            return True, False, None
        if allow_auto_build and _auto_build_allowed_by_env():
            built = _attempt_auto_build(quiet=quiet)
            if built:
                return True, False, None
        # Build either disallowed or failed -- preserve the original
        # hard-error behavior so ``--backend cpp`` never silently falls
        # back to Python.
        _emit(
            "Error: C++ backend requested but not available.\n"
            "Build the C++ extension or use --backend auto/python.\n"
            "See README for build instructions.",
            file=sys.stderr,
        )
        return False, False, 1

    # --backend auto (default): try to auto-build if not available.
    if is_cpp_available():
        return True, False, None

    if allow_auto_build and _auto_build_allowed_by_env():
        built = _attempt_auto_build(quiet=quiet)
        if built:
            return True, False, None

    # Backend still unavailable: emit the existing warning and continue
    # with Python.  Build failures (toolchain missing, timeout, etc.)
    # share this path so ``kct route`` never crashes due to a failed
    # auto-build attempt.
    if not quiet:
        _emit("WARNING: C++ router backend not installed -- using Python (10-100x slower).")
        _emit("  Build it now:  kct build-native")
        _emit("  Check status:  kct build-native --check")
        _emit("")
    return True, False, None


def _auto_build_allowed_by_env() -> bool:
    """Check the ``KICAD_TOOLS_NO_AUTO_BUILD`` environment opt-out.

    Returns ``False`` when the variable is set to a truthy value
    (``1``, ``true``, ``yes``, ``on`` -- case-insensitive).
    """
    import os

    val = os.environ.get("KICAD_TOOLS_NO_AUTO_BUILD", "").strip().lower()
    return val not in ("1", "true", "yes", "on")


def _toolchain_available() -> bool:
    """Cheap pre-check for cmake + C++ compiler on PATH.

    Avoids spending ~5-10s invoking cmake in environments where the build
    is guaranteed to fail (sandboxes, CI runners without dev tools).
    """
    import shutil

    if shutil.which("cmake") is None:
        return False
    return any(shutil.which(compiler) is not None for compiler in ("clang++", "g++"))


def _attempt_auto_build(*, quiet: bool) -> bool:
    """Run ``build_native(force=False)`` and reload this module on success.

    Returns ``True`` if the C++ backend is available after the attempt
    (either it was already built and the call short-circuited, or it
    built successfully).  Returns ``False`` on any failure -- never
    raises.

    The ``build_native`` call short-circuits in milliseconds when the
    backend is already loaded (see ``build_native_cmd.py:193-208``), so
    repeat invocations are effectively free once the ``.so`` exists.
    """
    if not _toolchain_available():
        if not quiet:
            print(
                "Note: C++ router toolchain (cmake + clang++/g++) not found; skipping auto-build.",
                flush=True,
            )
        return False

    if not quiet:
        print(
            "C++ router extension missing -- building (~30s, one-time)...",
            flush=True,
        )

    try:
        from kicad_tools.cli.build_native_cmd import build_native

        result = build_native(verbose=False, force=False)
    except Exception as exc:
        if not quiet:
            print(
                f"Note: auto-build raised {type(exc).__name__}: {exc}; falling back to Python.",
                flush=True,
            )
        return False

    if not getattr(result, "success", False):
        if not quiet:
            err = getattr(result, "error_message", None) or "unknown error"
            print(
                f"Note: C++ auto-build failed: {err}; falling back to Python.",
                flush=True,
            )
        return False

    # Build succeeded -- reload this module so module-level globals
    # (``_CPP_AVAILABLE``, ``router_cpp``) reflect the freshly written
    # ``.so``.  Without this, ``is_cpp_available()`` continues to return
    # ``False`` for the rest of the process.
    #
    # Issue #2594: ``_reload_cpp_backend`` now invalidates the import
    # caches and pops the stale ``router_cpp`` entry from ``sys.modules``
    # before reloading, so this is expected to succeed in the same
    # process.  The ``not available`` branch is retained as a defensive
    # diagnostic for genuinely pathological reload failures (e.g. a
    # platform where dlopen of the new .so itself fails after the build
    # wrote it to disk).
    available = _reload_cpp_backend()
    if not available and not quiet:
        print(
            "Note: C++ build succeeded but module reload did not pick it up; "
            "falling back to Python.",
            flush=True,
        )
    return available


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

    # Always provide an actionable build hint
    build_hint = (
        "Build the C++ extension for 10-100x faster routing:\n"
        "  kct build-native\n"
        "Or install with native support:\n"
        "  pip install kicad-tools[native]"
    )

    # Provide platform-specific diagnostics for common issues
    diagnostic_hint = None
    if "arm64" in platform.machine().lower() or "aarch64" in platform.machine().lower():
        if "darwin" in platform.system().lower():
            diagnostic_hint = (
                "On Apple Silicon, the C++ extension must be built locally. "
                "Run 'kct build-native' to build (requires Xcode Command Line Tools)."
            )
    elif "cannot open shared object" in reason.lower() or "dll" in reason.lower():
        diagnostic_hint = (
            "The compiled C++ extension was not found for this platform. "
            "Run 'kct build-native' to build from source."
        )

    result = {
        "backend": "python",
        "version": "pure-python",
        "available": False,
        "unavailable_reason": reason,
        "build_hint": build_hint,
        "platform": platform_info,
    }

    if diagnostic_hint:
        result["diagnostic_hint"] = diagnostic_hint

    return result


# Threshold for warning about Python backend performance (grid cells)
LARGE_GRID_THRESHOLD = 50_000


def format_backend_status(backend_info: dict, grid_cells: int = 0) -> str:
    """Format a human-readable backend status string for CLI output.

    Provides actionable guidance when the C++ backend is not available,
    especially when routing large grids where performance matters.

    Args:
        backend_info: Dictionary from get_backend_info().
        grid_cells: Total number of grid cells being routed (0 to skip
            the large-grid performance warning).

    Returns:
        Formatted status string suitable for printing to console.
    """
    active = backend_info.get("active", backend_info["backend"])
    available = backend_info.get("available", False)

    if active == "cpp":
        version = backend_info.get("version", "unknown")
        return f"cpp v{version} (native, 10-100x faster)"

    # Python backend - provide helpful context
    parts = ["python (pure Python)"]

    if not available:
        parts.append("C++ backend not installed")
        if grid_cells > LARGE_GRID_THRESHOLD:
            parts.append(
                f"WARNING: Routing {grid_cells:,} grid cells with Python backend "
                f"will be slow. Build C++ backend for 10-100x speedup: kct build-native"
            )
        else:
            parts.append("Tip: Run 'kct build-native' for 10-100x faster routing")

    return " | ".join(parts)


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
        # Reference to original Python grid (set by from_routing_grid for
        # post-route validation and pad_blocked lookups during relaxed
        # blocker identification, see find_blocking_nets_relaxed).
        self._py_grid: RoutingGrid | None = None
        # Track synced route count for incremental stored segment/via updates
        # (Issue #2439: C++ geometric validation)
        self._synced_route_count: int = 0

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

        # Store reference to original Python grid for post-route validation
        cpp_grid._py_grid = grid

        # Issue #2481: Establish the back-reference from the Python grid
        # to this CppGrid so ``RoutingGrid.unmark_route`` can invalidate
        # the C++ stored-via/segment snapshot whenever a route is ripped
        # up.  Without this, ``Pathfinder::is_via_blocked_diag`` would
        # consult stale ``stored_vias_`` entries and either reject
        # legitimate via candidates or silently allow placements that
        # collide with newly-placed sibling vias the cpp side never saw.
        grid._cpp_grid = cpp_grid

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

        # Issue #2439: Populate pad data for C++ geometric validation.
        # Pre-compute per-component clearance overrides and FNV-1a ref hashes
        # so the C++ side never calls back into Python during validation.
        component_pitches = grid.compute_component_pitches()
        for pad in grid._pads:
            # Compute layer index (-1 for through-hole pads = all layers)
            if pad.through_hole:
                layer_idx = -1
            else:
                try:
                    layer_idx = grid.layer_to_index(pad.layer.value)
                except (KeyError, ValueError):
                    layer_idx = 0

            # Pre-compute clearance for this pad's component (Issue #1016)
            pin_pitch = component_pitches.get(pad.ref) if pad.ref else None
            clearance_override = grid.rules.get_clearance_for_component(pad.ref, pin_pitch)

            # Deterministic FNV-1a hash of component reference
            ref_hash = router_cpp.fnv1a_hash(pad.ref) if pad.ref else 0

            cpp_grid._impl.add_pad(
                pad.x,
                pad.y,
                pad.width,
                pad.height,
                pad.net,
                layer_idx,
                ref_hash,
                clearance_override,
            )

        return cpp_grid

    def index_to_layer(self, index: int) -> int:
        """Convert grid index to Layer enum value."""
        return self._index_to_layer.get(index, index)

    def layer_to_index(self, layer_enum_value: int) -> int:
        """Map Layer enum value to grid index.

        Mirrors :meth:`RoutingGrid.layer_to_index` for parity. Falls back
        to identity mapping when the enum value is not in the mapping
        (e.g. a CppGrid built without ``from_routing_grid``).
        """
        return self._layer_to_index.get(layer_enum_value, layer_enum_value)

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

    def invalidate_stored_routes(self) -> None:
        """Drop the cached stored-routes snapshot used by validation.

        Issue #2481: ``CppPathfinder._sync_stored_routes`` is append-only
        and tracks ``self._synced_route_count`` to skip already-copied
        routes.  When the Python side rips up a route via
        :meth:`RoutingGrid.unmark_route`, the C++ ``stored_vias_`` /
        ``stored_segments_`` vectors retain the ripped-up route's
        entries, and the next call to ``_sync_stored_routes`` would
        early-return because ``len(py_grid.routes)`` may have decreased.

        This method clears the C++ side's stored routes and resets the
        sync watermark to 0 so the *next* call to ``_sync_stored_routes``
        rebuilds the full snapshot from the surviving
        ``py_grid.routes``.  It is intentionally cheap: clearing two
        vectors and resetting a counter; the actual rebuild only happens
        the next time the cpp pathfinder needs to validate a candidate.

        This is called from ``RoutingGrid.unmark_route`` via the
        ``_cpp_grid`` back-reference established by ``from_routing_grid``.
        """
        self._impl.clear_stored_routes()
        self._synced_route_count = 0


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
        net_class_map: dict[str, NetClassRouting] | None = None,
        max_search_iterations: int = 0,
    ):
        if not _CPP_AVAILABLE:
            raise RuntimeError("C++ router backend not available")

        # Net class map for per-net trace width lookup
        self._net_class_map = net_class_map or {}

        # Issue #2610: Override for the C++ A* iteration backstop.  ``0`` (the
        # default) preserves the historical ``cols * rows * 4`` cap; positive
        # values let users trade memory for completeness on dense boards via
        # the ``--max-search-iterations`` CLI flag.  Threaded into every
        # ``route_resumable()`` call below so a per-pathfinder override
        # covers all subsequent nets without per-call plumbing through
        # the strategy layer.
        self._max_search_iterations = int(max_search_iterations) if max_search_iterations else 0

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
        self._diagonal_routing = diagonal_routing

        # Lazy Python fallback router (constructed on first fallback)
        self._py_router: Router | None = None
        # Fallback statistics
        self._fallback_count: int = 0
        self._fallback_nets: list[str] = []

        # Issue #2476: Capture structured failure diagnostics from the most
        # recent failed route() call so the negotiated strategy can
        # dispatch targeted retry/rip-up.  Reset at the start of every
        # route() invocation; populated when route() returns None.
        #
        # Schema: {
        #   "failure_reason": int,           # FAILURE_* constant
        #   "blocking_via_net": int,         # 0 if not via-blocked
        #   "failure_x": float,              # world-coord (mm)
        #   "failure_y": float,
        # } or ``None`` if no diagnostic was captured.
        self._last_failure_info: dict | None = None

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

    def _compute_pad_bounds(self, pad: Pad) -> "router_cpp.PadBounds":
        """Compute pad metal area and approach zone bounds in grid coordinates.

        This mirrors the Python pathfinder's ``_get_pad_metal_bounds()`` logic
        (Issue #956/#977) so the C++ A* search can:
        - Accept any cell within the end pad's metal area as a goal (Phase 1)
        - Seed start nodes from all cells within the start pad's metal area
        - Define geometry-derived approach zones for clearance relaxation (Phase 2)

        Args:
            pad: The pad to compute bounds for.

        Returns:
            A ``router_cpp.PadBounds`` struct with metal and approach grid bounds.
        """
        # Calculate effective pad dimensions (same logic as grid._add_pad_unsafe)
        if getattr(pad, "through_hole", False):
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif getattr(pad, "drill", 0) > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        # Metal area bounds in world coordinates
        metal_x1 = pad.x - effective_width / 2
        metal_y1 = pad.y - effective_height / 2
        metal_x2 = pad.x + effective_width / 2
        metal_y2 = pad.y + effective_height / 2

        # Convert to grid coordinates using ceil/floor to include only cells
        # whose CENTER is inside the metal area (Issue #996).
        resolution = self._grid.resolution
        origin_x = self._grid.origin_x
        origin_y = self._grid.origin_y

        gx1 = max(0, math.ceil((metal_x1 - origin_x) / resolution))
        gy1 = max(0, math.ceil((metal_y1 - origin_y) / resolution))
        gx2 = min(self._grid.cols - 1, math.floor((metal_x2 - origin_x) / resolution))
        gy2 = min(self._grid.rows - 1, math.floor((metal_y2 - origin_y) / resolution))

        # Approach zone: metal area + 2-cell escape margin (Issue #1618)
        pad_escape_margin = 2
        bounds = router_cpp.PadBounds()
        bounds.metal_gx1 = int(gx1)
        bounds.metal_gy1 = int(gy1)
        bounds.metal_gx2 = int(gx2)
        bounds.metal_gy2 = int(gy2)
        bounds.approach_gx1 = int(gx1) - pad_escape_margin
        bounds.approach_gy1 = int(gy1) - pad_escape_margin
        bounds.approach_gx2 = int(gx2) + pad_escape_margin
        bounds.approach_gy2 = int(gy2) + pad_escape_margin
        return bounds

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
        per_net_timeout: float | None = None,
        extra_goal_cells: set[tuple[int, int, int]] | None = None,
    ) -> Route | None:
        """Route between two pads.

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters (used to
                compute per-net trace/via clearance radii for the C++ A* search)
            negotiated_mode: Enable negotiated congestion routing
            present_cost_factor: Multiplier for sharing penalty
            weight: A* weight (1.0 = optimal, >1.0 = faster)
            start_layers: Valid start layers (for PTH pads)
            end_layers: Valid end layers (for PTH pads)
            extra_goal_cells: Additional goal cells for early termination
                (accepted for API compatibility but not yet used by C++ backend)

        Returns:
            Route object if successful, None if no path found
        """
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

        # Issue #1702 Gap 2: Compute per-net trace and via clearance radii.
        # Use the net class trace width / via size (if available) instead of
        # the global defaults so wider nets correctly reserve space during
        # pathfinding in the C++ A* search.
        net_class = self._net_class_map.get(start.net_name)
        net_trace_width = net_class.trace_width if net_class else self._rules.trace_width
        net_trace_clearance = net_class.clearance if net_class else self._rules.trace_clearance
        trace_radius_cells = max(
            1,
            math.ceil((net_trace_width / 2 + net_trace_clearance) / self._grid.resolution),
        )

        net_via_size = net_class.via_size if net_class else self._rules.via_diameter
        via_radius_cells = max(
            1,
            math.ceil((net_via_size / 2 + self._rules.via_clearance) / self._grid.resolution),
        )

        # Issue #2427: Compute pad metal bounds and approach zones.
        # This mirrors the Python pathfinder's _get_pad_metal_bounds() logic
        # so the C++ A* search can use expanded goal/start regions and
        # geometry-derived approach zone relaxation.
        start_pad_bounds = self._compute_pad_bounds(start)
        end_pad_bounds = self._compute_pad_bounds(end)

        # Issue #2447: Use resumable A* so that when post-route validation
        # fails, the search continues from the preserved open set rather
        # than restarting from scratch. This mirrors the Python pathfinder's
        # behavior where validation failure leads to `continue` in the A*
        # main loop, finding an alternative approach path.
        #
        # Maximum number of resume attempts before giving up.
        max_resume_attempts = 5

        # Issue #2476: Reset failure-info at the start of every route() call
        # so the negotiated strategy never sees stale diagnostics from a
        # previous net.
        self._last_failure_info = None

        # Issue #2610: Convert the per-net timeout into the (seconds, float)
        # contract the C++ binding expects.  ``None`` or ``0`` => no deadline
        # (the C++ search runs until success / open-set exhaustion / the
        # iteration backstop).  Positive values establish a wall-clock
        # deadline that is shared across the initial ``route_resumable()``
        # call and all subsequent ``resume()`` invocations, so a single
        # per-net budget covers the whole retry sequence.
        timeout_seconds = float(per_net_timeout) if per_net_timeout else 0.0

        try:
            result = self._impl.route_resumable(
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
                trace_radius_cells,
                via_radius_cells,
                start_pad_bounds,
                end_pad_bounds,
                # Issue #2559 / Epic #2556 Phase 1C: defaults preserve pre-#2559 behavior.
                -1,  # partner_net (diff-pair plumbing not used here)
                0,   # intra_pair_radius_cells
                # Issue #2610: per-net wall-clock deadline + iteration override.
                timeout_seconds,
                self._max_search_iterations,
            )

            if not result.success:
                # Issue #2476: Capture structured failure diagnostics from
                # the C++ pathfinder.  In particular, FAILURE_VIA_VIA_BLOCKED
                # carries the offending stored-via net so the negotiated
                # strategy can target rip-up at that net rather than a
                # blanket retry.  We capture even when the Python fallback
                # also fails, since the cpp diagnostic is still actionable.
                self._capture_failure_info(result)
                return self._try_python_fallback(
                    start,
                    end,
                    net_class=net_class,
                    negotiated_mode=negotiated_mode,
                    present_cost_factor=present_cost_factor,
                    weight=weight,
                    per_net_timeout=per_net_timeout,
                    extra_goal_cells=extra_goal_cells,
                )

            for attempt in range(max_resume_attempts + 1):
                route = self._convert_result_to_route(result, start, net_class)

                # Issue #1702 Gap 3 + Issue #2439: Post-route geometric
                # clearance validation via C++ validate_route().
                violation_location = self._validate_route_clearance(
                    route, start, end, trace_radius_cells
                )

                if violation_location is None:
                    # Route passed validation
                    return route

                # Validation failed. Boost avoidance cost at violation location
                # (complementary mechanism to resumable search).
                self._boost_avoidance_at(violation_location, trace_radius_cells)

                if attempt >= max_resume_attempts:
                    # Exhausted resume attempts, try Python fallback.
                    # Issue #2476: Capture failure-info before falling back
                    # so the negotiated strategy can still see the cpp-side
                    # via-blocked diagnostic.
                    self._capture_failure_info(result)
                    return self._try_python_fallback(
                        start,
                        end,
                        net_class=net_class,
                        negotiated_mode=negotiated_mode,
                        present_cost_factor=present_cost_factor,
                        weight=weight,
                        per_net_timeout=per_net_timeout,
                        extra_goal_cells=extra_goal_cells,
                    )

                # Find the goal cell of the failed path and reject it.
                # The last segment's endpoint (converted to grid coords) is the
                # goal cell that A* reached.
                last_seg = result.segments[-1] if result.segments else None
                if last_seg is not None:
                    reject_gx, reject_gy = self._grid._impl.world_to_grid(last_seg.x2, last_seg.y2)
                    reject_layer = last_seg.layer
                else:
                    # Fallback: use end pad grid coords
                    reject_gx, reject_gy = self._grid._impl.world_to_grid(end.x, end.y)
                    reject_layer = end_layer

                # Resume A* from the preserved open set, skipping the
                # rejected goal cell.
                result = self._impl.resume(reject_gx, reject_gy, reject_layer)

                if not result.success:
                    # Issue #2476: Capture failure diagnostics; resume()
                    # accumulates trackers from the original
                    # route_resumable() call so the most recent via-blocker
                    # is still reported.
                    self._capture_failure_info(result)
                    return self._try_python_fallback(
                        start,
                        end,
                        net_class=net_class,
                        negotiated_mode=negotiated_mode,
                        present_cost_factor=present_cost_factor,
                        weight=weight,
                        per_net_timeout=per_net_timeout,
                        extra_goal_cells=extra_goal_cells,
                    )

            return None
        finally:
            # Always clear search state to release memory (Issue #2447 risk).
            self._impl.clear_search_state()

    def _capture_failure_info(self, result: "router_cpp.RouteResult") -> None:
        """Record structured failure diagnostics from a failed C++ route.

        Issue #2476: When the C++ A* search fails (open set exhausted or
        all via candidates were refused by stored-via geometry), the
        ``RouteResult`` carries a ``failure_reason`` and -- for the
        via-vs-via case -- the offending stored-via net.  We stash this on
        the pathfinder so ``get_last_failure_info()`` can return it to the
        negotiated strategy after ``route()`` returns ``None``.

        Issue #2610: ``failure_reason`` may now be ``FAILURE_TIMEOUT`` when
        the wall-clock per-net deadline expired (vs. ``FAILURE_ITERATION_LIMIT``
        for the memory-backstop cap, vs. ``FAILURE_NO_PATH`` for a genuine
        unreachable goal).  We also record the C++ pathfinder's iteration
        counter so callers can log "aborted at N iterations" cleanly.

        Note: We capture even when we are about to fall back to the Python
        router.  If the Python fallback also fails, the cpp-side
        diagnostic is still actionable (the via blocker has not moved).

        Args:
            result: The failed C++ ``RouteResult`` to capture diagnostics
                from.  ``failure_reason == FAILURE_NONE`` is silently
                ignored (no useful info).
        """
        # router_cpp may be None if the import failed; guard so this method
        # is safe to call from anywhere on the failure path.
        if router_cpp is None:
            return

        reason = getattr(result, "failure_reason", router_cpp.FAILURE_NONE)
        if reason == router_cpp.FAILURE_NONE:
            return

        # Issue #2610: iteration counter is a Pathfinder property, not a
        # field of RouteResult.  Read it off ``self._impl`` so callers can
        # see how close the search came to the memory cap.
        iterations = int(getattr(self._impl, "iterations", 0))

        self._last_failure_info = {
            "failure_reason": int(reason),
            "blocking_via_net": int(getattr(result, "blocking_via_net", 0)),
            "failure_x": float(getattr(result, "failure_x", 0.0)),
            "failure_y": float(getattr(result, "failure_y", 0.0)),
            "iterations": iterations,
        }

    def get_last_failure_info(self) -> dict | None:
        """Return structured failure diagnostics from the most recent failed route().

        Issue #2476: The negotiated strategy uses this to decide between
        blanket retry and targeted rip-up of a specific blocking net.
        Returns ``None`` if the last route() succeeded or the failure had
        no actionable signal (e.g. an unrelated grid-cell rejection).

        Returns:
            Dict with keys ``failure_reason``, ``blocking_via_net``,
            ``failure_x``, ``failure_y`` -- or ``None`` if no diagnostic
            is available.  ``failure_reason`` matches the ``FAILURE_*``
            constants in the ``router_cpp`` module (see ``types.hpp``).
        """
        return self._last_failure_info

    def _convert_result_to_route(
        self,
        result: "router_cpp.RouteResult",
        start: "Pad",
        net_class: "NetClassRouting | None",
    ) -> "Route":
        """Convert a C++ RouteResult to a Python Route object.

        Args:
            result: C++ route result with segments and vias.
            start: Source pad (provides net and net_name).
            net_class: Optional net class for trace width override.

        Returns:
            Python Route object with segments, vias, and validated layer transitions.
        """
        from .layers import Layer
        from .primitives import Route, Segment, Via

        route = Route(net=start.net, net_name=start.net_name)

        # Issue #1543: Apply net-class-aware trace width to segments.
        trace_width = net_class.trace_width if net_class else None

        for cpp_seg in result.segments:
            layer_enum_value = self._grid.index_to_layer(cpp_seg.layer)
            seg = Segment(
                x1=cpp_seg.x1,
                y1=cpp_seg.y1,
                x2=cpp_seg.x2,
                y2=cpp_seg.y2,
                width=trace_width if trace_width is not None else cpp_seg.width,
                layer=Layer(layer_enum_value),
                net=cpp_seg.net,
                net_name=start.net_name,
            )
            route.segments.append(seg)

        for cpp_via in result.vias:
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

        route.validate_layer_transitions(
            via_drill=self._rules.via_drill,
            via_diameter=self._rules.via_diameter,
        )
        return route

    def _validate_route_clearance(
        self,
        route: "Route",
        start: "Pad",
        end: "Pad",
        trace_radius_cells: int,
    ) -> tuple[float, float] | None:
        """Validate post-route geometric clearance using C++ validation.

        Issue #2439: Uses the C++ validate_route() call which runs all 4
        validation checks (segment-pad, segment-segment, via-segment,
        via-via, same-net drill spacing) in a single C++ call, eliminating
        Python callback overhead.

        Args:
            route: Route to validate.
            start: Source pad (for component reference exclusion).
            end: Destination pad (for component reference exclusion).
            trace_radius_cells: Trace half-width in grid cells (for avoidance).

        Returns:
            (x, y) world coordinates of violation, or None if route is valid.
        """
        py_grid = getattr(self._grid, "_py_grid", None)
        if py_grid is None:
            return None

        # Sync stored segments/vias from completed routes to C++
        self._sync_stored_routes(py_grid)

        # Build exclude_ref_hashes for start/end pad components (Issue #1764)
        exclude_ref_hashes: list[int] = []
        for pad in (start, end):
            if pad.ref:
                exclude_ref_hashes.append(router_cpp.fnv1a_hash(pad.ref))

        # Build C++ segment/via lists from route
        cpp_segs: list[router_cpp.Segment] = []
        for seg in route.segments:
            cs = router_cpp.Segment()
            cs.x1, cs.y1, cs.x2, cs.y2 = seg.x1, seg.y1, seg.x2, seg.y2
            cs.width = seg.width
            cs.layer = self._grid.layer_to_index(seg.layer.value)
            cs.net = seg.net
            cpp_segs.append(cs)

        cpp_vias: list[router_cpp.Via] = []
        for via in route.vias:
            cv = router_cpp.Via()
            cv.x, cv.y = via.x, via.y
            cv.drill = via.drill
            cv.diameter = via.diameter
            cv.layer_from = self._grid.layer_to_index(via.layers[0].value)
            cv.layer_to = self._grid.layer_to_index(via.layers[1].value)
            cv.net = via.net
            cpp_vias.append(cv)

        vresult = self._grid._impl.validate_route(
            cpp_segs,
            cpp_vias,
            start.net,
            exclude_ref_hashes,
            self._rules.trace_clearance,
            self._rules.via_clearance,
            self._rules.min_drill_clearance,
        )

        if not vresult.valid:
            return (vresult.violation_x, vresult.violation_y)

        return None

    def _boost_avoidance_at(
        self,
        location: tuple[float, float] | None,
        trace_radius_cells: int,
    ) -> None:
        """Boost avoidance cost around a DRC violation location.

        When post-route validation detects a clearance violation, this method
        marks the region in the C++ grid so subsequent A* searches incur a
        cost penalty and explore alternative paths.

        Args:
            location: (x, y) world coordinates of the violation, or None.
            trace_radius_cells: Trace half-width in grid cells (used to
                scale the avoidance radius).
        """
        if location is None:
            return
        vx, vy = float(location[0]), float(location[1])
        gx, gy = self._grid._impl.world_to_grid(vx, vy)
        # Boost on all layers since violations may affect via transitions
        radius = max(1, trace_radius_cells * 3)
        amount = 20.0
        for layer in range(self._grid.num_layers):
            self._grid._impl.boost_region_cost(gx, gy, layer, radius, amount)

    def clear_avoidance_costs(self) -> None:
        """Clear all avoidance costs from the grid.

        Should be called after a net is fully routed (success or failure)
        to prevent avoidance costs from polluting subsequent net routing.
        """
        self._grid._impl.clear_avoidance_costs()

    def _try_python_fallback(
        self,
        start: Pad,
        end: Pad,
        *,
        net_class: NetClassRouting | None = None,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        weight: float = 1.0,
        per_net_timeout: float | None = None,
        extra_goal_cells: set[tuple[int, int, int]] | None = None,
    ) -> Route | None:
        """Attempt to route using the Python pathfinder as a fallback.

        Called when the C++ A* search fails to find a path.  The Python
        pathfinder uses different neighbor expansion and clearance relaxation
        logic that can handle single-corridor geometries where the C++
        cost-based avoidance steering cannot.

        The Python ``Router`` operates on ``_py_grid`` which is kept in sync
        by :meth:`core.Autorouter._mark_route` (updates both grids after
        every committed route).

        Args:
            start: Source pad
            end: Destination pad
            net_class: Optional net class for routing parameters
            negotiated_mode: Enable negotiated congestion routing
            present_cost_factor: Multiplier for sharing penalty
            weight: A* weight
            per_net_timeout: Optional per-net timeout in seconds
            extra_goal_cells: Additional goal cells for early termination

        Returns:
            Route object if fallback succeeds, None if also fails.
        """
        py_grid = self._grid._py_grid
        if py_grid is None:
            return None

        # Lazy-construct the Python Router on first fallback
        if self._py_router is None:
            from .pathfinder import Router

            self._py_router = Router(
                py_grid,
                self._rules,
                net_class_map=self._net_class_map,
                diagonal_routing=self._diagonal_routing,
            )

        t0 = time.monotonic()
        # Python Router.route() does not accept start_layers/end_layers;
        # it derives layer information internally from pad attributes.
        route = self._py_router.route(
            start,
            end,
            net_class=net_class,
            negotiated_mode=negotiated_mode,
            present_cost_factor=present_cost_factor,
            weight=weight,
            per_net_timeout=per_net_timeout,
            extra_goal_cells=extra_goal_cells,
        )
        dt = time.monotonic() - t0

        net_name = getattr(start, "net_name", "?")
        if route is not None:
            self._fallback_count += 1
            self._fallback_nets.append(net_name)
            logger.info(
                "Net %s: C++ pathfinder failed, routed via Python fallback (%.1fs)",
                net_name,
                dt,
            )
        else:
            logger.debug(
                "Net %s: C++ pathfinder failed, Python fallback also failed (%.1fs)",
                net_name,
                dt,
            )

        return route

    @property
    def fallback_stats(self) -> dict:
        """Get statistics about Python fallback usage.

        Returns:
            Dictionary with:
                - fallback_count: Number of nets routed via Python fallback
                - fallback_nets: List of net names that used fallback
        """
        return {
            "fallback_count": self._fallback_count,
            "fallback_nets": list(self._fallback_nets),
        }

    @property
    def iterations(self) -> int:
        """Number of iterations in last route."""
        return self._impl.iterations

    @property
    def nodes_explored(self) -> int:
        """Number of nodes explored in last route."""
        return self._impl.nodes_explored

    def _sync_stored_routes(self, py_grid: RoutingGrid) -> None:
        """Sync stored segments and vias from completed routes to C++.

        Issue #2439: Incrementally adds new route data to the C++ Grid3D
        so that validate_route() can check clearances without Python callbacks.
        Only copies routes added since the last sync.
        """
        current_count = len(py_grid.routes)
        if current_count <= self._grid._synced_route_count:
            return

        # Add segments/vias from newly completed routes
        for route in py_grid.routes[self._grid._synced_route_count :]:
            for seg in route.segments:
                layer_idx = py_grid.layer_to_index(seg.layer.value)
                self._grid._impl.add_stored_segment(
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    seg.width,
                    layer_idx,
                    seg.net,
                )
            for via in route.vias:
                self._grid._impl.add_stored_via(
                    via.x,
                    via.y,
                    via.drill,
                    via.diameter,
                    via.net,
                )

        self._grid._synced_route_count = current_count

    def find_blocking_nets(
        self,
        start: Pad,
        end: Pad,
        layer: int | None = None,
        net_class: "NetClassRouting | None" = None,
    ) -> set[int]:
        """Find which nets block the direct path from start to end.

        Uses Bresenham's line algorithm to trace the ideal direct path,
        then identifies which net IDs are blocking cells along that path.
        This is used for targeted rip-up in negotiated routing.

        Args:
            start: Source pad
            end: Destination pad
            layer: Optional layer index (uses pad layer if not specified)
            net_class: Optional net class for per-net trace width (Issue #1692).

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

        # Determine trace half width in cells
        # Issue #1692: Use per-net-class trace width when available,
        # falling back to the global rules.trace_width.
        net_trace_width = net_class.trace_width if net_class else self._rules.trace_width
        net_trace_clearance = net_class.clearance if net_class else self._rules.trace_clearance
        trace_half_width_cells = max(
            1,
            int((net_trace_width / 2 + net_trace_clearance) / self._grid.resolution + 0.5),
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

    def find_blocking_nets_relaxed(
        self,
        start: Pad,
        end: Pad,
        saved_blocked: np.ndarray,
        saved_net: np.ndarray,
        per_net_timeout: float | None = None,
    ) -> set[int]:
        """Find blocking nets using relaxed A* (Issue #2274 / #2386).

        Python-side mirror of :meth:`Router.find_blocking_nets_relaxed` for
        the C++ backend. Re-uses the existing C++ ``route`` path; only the
        segment-walking and original-grid lookup happens in Python.

        Runs A* with routed-net obstacles temporarily removed (the caller is
        responsible for invoking this inside a
        ``grid.temporarily_unblock_routed_nets()`` context manager). If a
        path is found, examines the *original* blocked/net arrays to
        determine which routed nets occupy cells along that path.

        Args:
            start: Source pad.
            end: Destination pad.
            saved_blocked: The *original* blocked array before unblocking
                (numpy bool 3D: layers x rows x cols).
            saved_net: The *original* net array before unblocking
                (numpy int32 3D: layers x rows x cols).
            per_net_timeout: Optional timeout for the relaxed A* search.

        Returns:
            Set of routed-net IDs whose cells lie along the relaxed path.
        """
        # 1. Run C++ A* with negotiated_mode and zero present-cost to find a
        #    relaxed path. Grid has been unblocked by the caller's
        #    temporarily_unblock_routed_nets() context.
        route = self.route(
            start,
            end,
            negotiated_mode=True,
            present_cost_factor=0.0,
            per_net_timeout=per_net_timeout,
        )
        if route is None:
            return set()

        blocking: set[int] = set()
        source_net = start.net

        # Compute trace half-width in cells (mirrors pathfinder.py:117).
        # CppPathfinder doesn't currently cache _trace_half_width_cells,
        # so compute it here on demand.
        trace_half_width_cells = max(
            1,
            math.ceil(
                round(
                    (self._rules.trace_width / 2 + self._rules.trace_clearance)
                    / self._grid.resolution,
                    6,
                )
            ),
        )

        # _pad_blocked lookup requires the original Python RoutingGrid.
        # CppGrid.from_routing_grid stores it on self._py_grid. If the
        # CppGrid was built without a Python source, fall back to False
        # (slightly looser, but safe -- at worst a few extra blockers).
        py_grid = getattr(self._grid, "_py_grid", None)

        def _pad_blocked_at(layer_idx: int, cy: int, cx: int) -> bool:
            if py_grid is None:
                return False
            return bool(py_grid._pad_blocked[layer_idx, cy, cx])

        # 2. Walk every cell along every segment of the relaxed path and
        #    check the *original* (saved) blocked/net arrays to find which
        #    routed nets occupied those cells.
        for seg in route.segments:
            gx1, gy1 = self._grid.world_to_grid(seg.x1, seg.y1)
            gx2, gy2 = self._grid.world_to_grid(seg.x2, seg.y2)
            layer_idx = self._grid.layer_to_index(seg.layer.value)

            # Walk segment cells (Bresenham)
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                # Check this cell and clearance envelope
                for cdy in range(-trace_half_width_cells, trace_half_width_cells + 1):
                    for cdx in range(-trace_half_width_cells, trace_half_width_cells + 1):
                        cx, cy = gx + cdx, gy + cdy
                        if 0 <= cx < self._grid.cols and 0 <= cy < self._grid.rows:
                            was_blocked = bool(saved_blocked[layer_idx, cy, cx])
                            orig_net = int(saved_net[layer_idx, cy, cx])
                            if (
                                was_blocked
                                and orig_net != 0
                                and orig_net != source_net
                                and not _pad_blocked_at(layer_idx, cy, cx)
                            ):
                                blocking.add(orig_net)

                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        # 3. Also check via locations on every layer
        for via in route.vias:
            vgx, vgy = self._grid.world_to_grid(via.x, via.y)
            for layer_idx in range(self._grid.num_layers):
                if 0 <= vgx < self._grid.cols and 0 <= vgy < self._grid.rows:
                    was_blocked = bool(saved_blocked[layer_idx, vgy, vgx])
                    orig_net = int(saved_net[layer_idx, vgy, vgx])
                    if (
                        was_blocked
                        and orig_net != 0
                        and orig_net != source_net
                        and not _pad_blocked_at(layer_idx, vgy, vgx)
                    ):
                        blocking.add(orig_net)

        return blocking


def create_hybrid_router(
    grid: RoutingGrid,
    rules: DesignRules,
    diagonal_routing: bool = True,
    force_python: bool = False,
    net_class_map: dict[str, NetClassRouting] | None = None,
    max_search_iterations: int = 0,
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
        net_class_map: Optional net class map for per-net trace widths
        max_search_iterations: Issue #2610 -- override for the C++ A*
            iteration backstop.  ``0`` (default) preserves the historical
            ``cols * rows * 4`` cap.  Positive values let dense boards
            trade memory for completeness via ``--max-search-iterations``.

    Returns:
        Either CppPathfinder or Python Router instance
    """
    if _CPP_AVAILABLE and not force_python:
        try:
            cpp_grid = CppGrid.from_routing_grid(grid)
            return CppPathfinder(
                cpp_grid,
                rules,
                diagonal_routing,
                net_class_map=net_class_map,
                max_search_iterations=max_search_iterations,
            )
        except Exception:
            # Fall back to Python if C++ initialization fails
            pass

    # Fall back to Python implementation
    if not force_python:
        reason = _CPP_IMPORT_ERROR or "unknown reason"
        logger.warning(
            "C++ router backend not available -- using pure Python (10-100x slower). Reason: %s",
            reason,
        )

    from .pathfinder import Router

    return Router(grid, rules, net_class_map=net_class_map, diagonal_routing=diagonal_routing)
