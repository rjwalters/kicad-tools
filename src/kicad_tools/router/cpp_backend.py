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
import os
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
_REQUIRED_CPP_BUILD_VERSION = 16

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

        # Copy blocked cells from Python grid to C++ grid.
        #
        # Issue #3224: Forward the ``pad_blocked`` bit (set by
        # ``RoutingGrid._add_pad_unsafe`` at grid.py:4458 for cells inside a
        # pad's metal area) so the C++ A* clearance branch at
        # ``pathfinder.cpp:680`` (one-shot) and ``pathfinder.cpp:1173``
        # (resumable / negotiated) can distinguish pad metal from pad
        # clearance halo.  Without this bit, ``cell.pad_blocked`` defaults to
        # ``false`` on every C++ cell and the pad-exit exemption admits
        # traces stepping through foreign pad copper -- the
        # ``clearance_pad_segment`` regression on board 05 (16 errors at HEAD
        # with --backend cpp vs 1 on python).  The bulk sync here is
        # complemented by the incremental sync at
        # ``grid.py::_sync_pad_to_cpp_grid`` for pads added AFTER this
        # bulk-copy completes (the typical ``Autorouter.add_component``
        # flow).
        py_pad_blocked = grid._pad_blocked
        for layer in range(grid.num_layers):
            for y in range(grid.rows):
                for x in range(grid.cols):
                    py_cell = grid.grid[layer][y][x]
                    if py_cell.blocked:
                        cpp_grid._impl.mark_blocked(
                            x,
                            y,
                            layer,
                            py_cell.net,
                            py_cell.is_obstacle,
                            bool(py_pad_blocked[layer, y, x]),
                        )

        # Issue #4071: marshal corridor reservations into the C++ grid.
        # ``RoutingGrid._reserved_for_nets`` maps ``(layer, y, x)`` -> owner
        # net frozenset, written by ``EscapeRouter``'s reservation helpers
        # (#2677 pair-continuation, #2983 inner-corner lane, #4053 bundle
        # river) BEFORE routing begins.  The C++ ``Grid3D::mark_via`` keep-out
        # skip and the A* corridor attractor both read the per-cell owner set,
        # so the reservations must be mirrored across the boundary.  Empty
        # dict => zero iterations (byte-identical to pre-#4071 boards).
        for (layer_idx, y, x), owners in grid._reserved_for_nets.items():
            cpp_grid._impl.reserve_cell(x, y, layer_idx, [int(n) for n in owners])

        # Issue #2439: Populate pad data for C++ geometric validation.
        # Pre-compute per-component clearance overrides and FNV-1a ref hashes
        # so the C++ side never calls back into Python during validation.
        # Issue #2908: Pre-compute plane-net classification on Python side
        # (C++ has no net-name string table) so the C++ validator can use
        # the same carve-out as the Python ``_is_plane_net_pad`` helper.
        from .fine_pitch_escape import resolve_clearance_with_escape_region
        from .grid import _is_plane_net_pad

        component_pitches = grid.compute_component_pitches()

        # Issue #3371 / P_FP2: Fine-pitch escape regions installed on the
        # grid (empty list when the detector has not run, which is the
        # default until P_FP3 wires the autorouter to populate them).
        # Threaded into ``resolve_clearance_with_escape_region`` for every
        # pad's clearance lookup so the per-net-class escape clearance can
        # land at the C++ pad-segment validator's clearance source.
        #
        # Important: this bulk-copy runs at grid construction time, BEFORE
        # any specific net is being routed.  We therefore cannot pass a
        # per-net :class:`NetClassRouting` here -- the bulk clearance is a
        # *board-wide default*.  ``net_class=None`` in the call below
        # means "use the region's escape_clearance default at detection
        # time" (which already encodes the manufacturer floor + safety
        # margin via :func:`get_default_escape_clearance`).  P_FP3 will
        # add the per-net override path via either (a) a separate set of
        # add_pad calls per net during the A* boundary, or (b) a tighter
        # ``clearance_override`` carrier on the C++ Pad struct that the
        # search-side can interpret per net.  P_FP2 only opens the seam.
        regions = grid.get_fine_pitch_regions()

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
            # threaded with the fine-pitch escape regions (Issue #3371 / P_FP2).
            # When ``regions`` is empty (the default) this call delegates to
            # the standard :meth:`DesignRules.get_clearance_for_component`
            # path -- byte-for-byte identical to the pre-#3371 line.
            pin_pitch = component_pitches.get(pad.ref) if pad.ref else None
            clearance_override = resolve_clearance_with_escape_region(
                grid.rules,
                pad,
                net_class=None,
                regions=regions,
                pin_pitch=pin_pitch,
            )

            # Deterministic FNV-1a hash of component reference
            ref_hash = router_cpp.fnv1a_hash(pad.ref) if pad.ref else 0

            # Issue #2908: Pre-compute plane-net classification on the Python
            # side (the C++ Pad struct has no net-name string table).  Used
            # by ``Grid3D::validate_route`` to keep plane pads in the
            # validator even when their component is in the exclude set --
            # the same fix as the Python-side ``_is_plane_net_pad`` carve-out
            # at ``grid.py::validate_segment_clearance``.
            is_plane_net = _is_plane_net_pad(pad)

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

    def is_blocked_for_net(self, x: int, y: int, layer: int, net: int) -> bool:
        """Check if a cell is blocked for routing a specific net.

        Mirrors the Python ``RoutingGrid.is_blocked(gx, gy, layer, net)``
        semantics: a cell occupied by the SAME net's copper is not
        considered blocked (A* may legally terminate or pass there),
        while net-0 obstacles (component bodies, pad keepouts) and
        foreign-net copper block.  Issue #3471: used by the Steiner
        branch-point relocation in ``route_net_negotiated`` so synthetic
        branch points are not parked on obstacle cells.
        """
        if not self._impl.is_valid(x, y, layer):
            return True
        cell = self._impl.at(x, y, layer)
        if not cell.blocked:
            return False
        return cell.net == 0 or cell.net != net

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

    # Issue #3441: the C++ pathfinder has NO waypoint-injection support
    # (#2330 was implemented only in the pure-Python ``Router``).  This
    # capability flag keeps ``Autorouter.use_waypoint_injection`` honest:
    # under the C++ backend the sub-grid escape pre-pass and PIN_ACCESS
    # sub-grid retry (#1603) remain the active off-grid pad recovery
    # mechanisms.  Flip to True if/when waypoints are ported to
    # ``cpp/src/pathfinder.cpp``.
    supports_waypoint_injection: bool = False

    def __init__(
        self,
        grid: CppGrid,
        rules: DesignRules,
        diagonal_routing: bool = True,
        net_class_map: dict[str, NetClassRouting] | None = None,
        max_search_iterations: int = 0,
        per_net_iterations: int = 0,
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

        # Issue #3881: the TUNED per-net iteration cap, distinct from the memory
        # backstop above.  When set (>0) it becomes the BINDING per-net cap: the
        # C++ A* is given ``min(per_net_iterations, backstop)`` node expansions,
        # so a hard net gives up deterministically at the tuned cap (returning
        # FAILURE_ITERATION_LIMIT) instead of grinding to the 12M backstop and
        # monopolising the budget.  ``_per_net_iteration_cap_active`` tells the
        # Python fallback to treat a FAILURE_ITERATION_LIMIT as a deterministic
        # give-up and SKIP the fallback (so a capped net does not then burn
        # minutes in the 10-100x-slower Python A*, re-introducing the slowness).
        self._per_net_iterations = int(per_net_iterations) if per_net_iterations else 0
        self._per_net_iteration_cap_active = self._per_net_iterations > 0
        # Effective cap threaded to the C++ search.  When the tuned per-net cap
        # is set it binds (clamped by the memory backstop when that is also
        # positive); otherwise the memory backstop / heuristic applies.
        if self._per_net_iterations > 0:
            if self._max_search_iterations > 0:
                self._effective_search_iterations = min(
                    self._per_net_iterations, self._max_search_iterations
                )
            else:
                self._effective_search_iterations = self._per_net_iterations
        else:
            self._effective_search_iterations = self._max_search_iterations

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
        # Issue #4071: soft corridor-attractor bonus (default 3.0).
        cpp_rules.cost_corridor_attractor = rules.cost_corridor_attractor

        self._impl = router_cpp.Pathfinder(grid._impl, cpp_rules, diagonal_routing)
        self._grid = grid
        self._rules = rules
        self._diagonal_routing = diagonal_routing

        # Issue #3622 follow-up: restrict the C++ via-expansion to the layers
        # actually permitted by ``allowed_layers`` (Issue #715 single-layer
        # constraint).  The pathfinder defaults ``routable_layers_`` to every
        # grid layer, so without this the via loop in ``pathfinder.cpp`` will
        # attempt a layer change even when only one copper layer is routable.
        #
        # Pre-#3622 this leaked no vias because the strict standard-mode
        # ``is_via_blocked`` rejected via candidates whose clearance disc
        # touched the routing net's OWN ``is_obstacle`` pad copper.  The #864
        # parity relaxation (own-net obstacle cells now passable for vias)
        # removes that incidental suppression, so a single-layer route would
        # begin emitting vias that land back on the only routable layer.
        # Filtering the routable-layer set here enforces the real single-layer
        # invariant ("a via requires >= 2 routable layers") WITHOUT touching
        # the parity predicate -- with a one-element routable set the via
        # loop's ``new_layer == current.layer`` skip leaves no via target.
        #
        # ``_routable_layers`` mirrors the C++ ``routable_layers_`` vector,
        # defaulting to the grid's routable indices (the pathfinder ctor's
        # own default) until/unless the allowed-layers filter narrows it.
        self._routable_layers: list[int] = list(grid.get_routable_indices())
        self._apply_allowed_layers_to_routable()

        # Lazy Python fallback router (constructed on first fallback)
        self._py_router: Router | None = None
        # Fallback statistics
        self._fallback_count: int = 0
        self._fallback_nets: list[str] = []
        # Issue #3456: per-net fallback reasons + warn-once bookkeeping.
        # ``_fallback_reasons`` records WHY the C++ search handed each net
        # to the Python fallback (first reason per net, including attempts
        # where the Python fallback ultimately failed too).
        # ``_fallback_warned`` dedupes the loud per-net WARNING so
        # negotiated-mode rip-up retries of the same net do not spam the
        # log -- each net warns at most once per run.
        self._fallback_reasons: dict[str, str] = {}
        self._fallback_warned: set[str] = set()

        # Issue #3923: per-net count of case-1 (clearance-exhaustion) fallbacks.
        # The FIRST time a net exhausts its resume attempts on a clearance
        # violation we still run the Python fallback -- a fresh full A* with the
        # 45-degree / waypoint expansion measurably rescues real nets/pads at
        # that point (e.g. board-07 GND pad U1.24).  Only on the SECOND+
        # identical clearance-exhaustion for the SAME net -- once the fresh
        # Python A* has already had its shot and the negotiated rip-up loop has
        # merely re-presented the same clearance obstruction -- do we
        # short-circuit, because that repeat is the genuine 60-200s/net dead
        # loss the optimization targets.
        self._resume_clearance_exhaustions: dict[str, int] = {}

        # Issue #3545: lazy cache for ``compute_component_pitches`` used
        # by the net-aware same-component carve-out gate in
        # ``_same_component_carveout_eligible``.
        self._component_pitches_cache: dict[str, float] | None = None

        # Issue #3438: relief-probe mode flag (mirrored into the C++
        # Pathfinder AND the lazy Python fallback router so both backends
        # apply identical relief semantics during a probe).
        self._relief_mode: bool = False

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

        # Issue #2587 / Epic #2556 Phase 1C-cont: Reverse map for diff-pair
        # partner resolution.  The autorouter populates this via
        # :meth:`set_net_name_to_id` before routing begins so that
        # ``NetClassRouting.diffpair_partner`` (a *name*) can be resolved to
        # the partner *id* required by the C++ ``partner_net`` plumbing.
        #
        # When the map is empty (the default) or the source net has no
        # ``diffpair_partner`` declaration, :meth:`_resolve_partner_net_id`
        # returns ``None`` and the C++ search uses the wider inter-pair
        # ``clearance`` for every other net (the pre-Phase-1C contract).
        self._net_name_to_id: dict[str, int] = {}

        # Issue #2929: Per-A*-call wall-clock instrumentation, mirroring the
        # Python pathfinder's instrumentation surface so callers can audit
        # deadline behavior regardless of which backend handles the call.
        # Disabled by default (zero overhead on the production hot path);
        # toggled via :meth:`enable_per_call_timing` and drained via
        # :meth:`get_and_clear_per_call_timings`.
        self._per_call_timing_enabled: bool = False
        # Issue #3474 R1: ``KCT_DEBUG_PNT=1`` enables the #2929 per-call
        # timing instrumentation (previously unreachable from the CLI)
        # and prints any route() call whose wall time grossly exceeds its
        # per-net budget.  This is the diagnostic that located the
        # chorus-test deadline leak (per-net cap honored by A* but blown
        # by un-budgeted failure analysis); keep it available for future
        # budget-integrity triage.
        if os.environ.get("KCT_DEBUG_PNT"):
            self._per_call_timing_enabled = True
        self._per_call_timings: list[dict] = []

        # Issue #3002 (PR #3006 follow-up): Foreign-net via context for the
        # segment-vs-foreign-via clearance gate.  Mirrors the Python
        # pathfinder's ``_foreign_vias`` attribute (see
        # ``pathfinder.py:set_segment_foreign_context``).  Populated by
        # ``Autorouter._update_router_segment_foreign_context`` via
        # :meth:`set_segment_foreign_context` below; consumed during
        # :meth:`_validate_route_clearance` as a Python-side post-check
        # after the C++ validator returns (the C++ validator already
        # walks ``self.routes`` vias, but the Python-side list lets the
        # Autorouter push richer context -- e.g. vias surfaced by the
        # negotiated post-iteration re-validation hook that may not yet
        # appear in ``grid.routes`` when a sibling iteration's segment
        # commits).
        self._foreign_vias: list = []  # list[Via]

        # Issue #3143: Per-pad lateral-channel budget storage.  Populated
        # by :meth:`set_pad_channel_budgets` from
        # ``router.core.Router.route_with_escape`` after the dense-package
        # escape pre-pass runs.  Each entry is a ``PadChannelBudget`` that
        # tags a rectangular escape-channel region with a soft per-cell
        # penalty consulted on every A* neighbor expansion.  Empty (the
        # default) means no per-pad budget is configured and the C++
        # search uses the pre-#3143 cost function identically.
        self._pad_channel_budgets: list = []  # list[router_cpp.PadChannelBudget]

    def set_segment_foreign_context(
        self,
        foreign_vias: list | None = None,
    ) -> None:
        """Set foreign-net via context for new-segment clearance gating.

        Issue #3002 (PR #3006 follow-up): C++ backend sibling of
        :meth:`pathfinder.Router.set_segment_foreign_context`.  Without
        this method on ``CppPathfinder`` the ``hasattr`` guard in
        ``Autorouter._update_router_segment_foreign_context`` silently
        no-ops the entire segment-vs-foreign-via gate when the C++
        backend is active (the production default).

        The C++ ``validate_route`` call invoked by
        :meth:`_validate_route_clearance` already walks the C++ side's
        stored vias against new segments, so vias that are *already* in
        ``grid.routes`` at the time a new route is validated will be
        caught by C++.  However, the negotiated post-iteration
        re-validation hook (Issue #3002) surfaces vias whose presence
        the pre-commit gate needs to react to in the NEXT iteration's
        re-routes -- and those vias may not be in ``grid.routes`` from
        the C++ side's perspective until the autorouter pushes the
        explicit foreign-via list here.  This setter stores that list
        for the Python-side post-validation pass in
        :meth:`_validate_route_clearance`.

        Same-net filtering is the CALLER's responsibility (matches
        :meth:`pathfinder.Router.set_segment_foreign_context`).

        Args:
            foreign_vias: List of :class:`Via` objects whose net differs
                from the segment's own net.  Pass ``None`` to clear.
        """
        self._foreign_vias = list(foreign_vias) if foreign_vias else []

    def _filter_pad_channel_budgets_for_net(self, net: int) -> list:
        """Return the per-pad budget list with the current net's own
        entries filtered out (Issue #3143).

        The pad channel budget is a per-cell cost penalty that nudges the
        A* search away from contested escape channels.  But the
        originating net of each escape pad cannot legitimately route
        anywhere except through its own escape endpoint -- so penalising
        cells around its own endpoint just adds dead-weight cost to the
        only valid exit.  We filter out budgets whose ``source_net``
        equals the current net before forwarding.

        Args:
            net: The integer net id of the route being computed.

        Returns:
            A filtered copy of ``self._pad_channel_budgets`` (or the
            original list when no filtering is needed -- the empty-list
            short-circuit avoids allocating a copy on the hot path when
            no budget is configured at all).
        """
        if not self._pad_channel_budgets:
            return self._pad_channel_budgets
        # Use list comprehension; the budget list is bounded by the
        # number of dense-package escape pads (typically < 30) so even
        # the O(N) scan is cheap relative to the surrounding A* cost.
        return [b for b in self._pad_channel_budgets if getattr(b, "source_net", 0) != net]

    def set_pad_channel_budgets(self, budgets: list | None) -> None:
        """Inject the per-pad lateral-channel budget list (Issue #3143).

        The budgets are consulted by the C++ A* search via the
        ``pad_channel_budgets`` parameter on
        :meth:`router_cpp.Pathfinder.route_resumable`.  Each budget tags
        a rectangular escape-channel region with a soft per-cell
        penalty; the search prefers less-contested escape paths in the
        lateral channels adjacent to dense-package pad rows.

        This setter is the per-board configuration entry point used by
        :meth:`router.core.Router.route_with_escape` after the dense-
        package escape pre-pass runs.  Calling with ``None`` or an empty
        list disables the per-pad-budget cost term (the C++ search uses
        the pre-#3143 cost function identically).

        Idempotent and may be called multiple times.  Each call replaces
        the previously-stored list.  The Python side stores the budgets
        and forwards them on every subsequent :meth:`_route_impl` call
        as a positional argument to ``route_resumable()``.

        Args:
            budgets: List of ``router_cpp.PadChannelBudget`` instances
                (or anything duck-typed against the binding's struct).
                Pass ``None`` or ``[]`` to clear.
        """
        self._pad_channel_budgets = list(budgets) if budgets else []

    def enable_per_call_timing(self, enabled: bool = True) -> None:
        """Enable or disable per-A*-call wall-clock instrumentation.

        Issue #2929: When enabled, every ``route()`` call records a timing
        entry capturing the wall-clock duration, the per-net deadline (if
        any), and whether the deadline was honored within the 1.2x slack
        bound from the issue's acceptance criteria.  Disabled by default
        so production routing pays zero overhead.

        Args:
            enabled: True to start recording; False to stop and drop any
                accumulated records.
        """
        self._per_call_timing_enabled = bool(enabled)
        if not enabled:
            self._per_call_timings = []

    def get_and_clear_per_call_timings(self) -> list[dict]:
        """Drain and return the recorded per-A*-call timing records.

        Issue #2929: Each record is a dict with keys
        ``net``, ``net_name``, ``elapsed``, ``per_net_timeout``,
        ``deadline_violated``, and ``succeeded``.  Note that a record may
        include time spent in the Python fallback (triggered after a C++
        failure); the elapsed clock starts when ``route()`` enters and
        stops when it returns.  This is the deliberate "what the caller
        actually waited" measurement (a deadline-honoring inner search
        will still be bracketed by the timeout, plus a small fixed setup
        cost).

        Returns:
            The list of timing records since the last drain (or since
            instrumentation was enabled).  Empty list if instrumentation
            is disabled or no calls have been made.
        """
        result = self._per_call_timings
        self._per_call_timings = []
        return result

    # ------------------------------------------------------------------
    # Diff-pair partner resolution (Issue #2587 / Epic #2556 Phase 1C-cont)
    # ------------------------------------------------------------------

    def set_net_name_to_id(self, mapping: dict[str, int]) -> None:
        """Inject a net-name -> net-id reverse map for partner resolution.

        Mirrors :meth:`Pathfinder.set_net_name_to_id`.  Phase 1C threads
        ``NetClassRouting.intra_pair_clearance`` through the C++ A* search
        via the ``partner_net`` parameter on ``route_resumable()`` and
        ``validate_route()``.  The clearance is configured per-source-net
        but applies only when the *other* net is the named partner.
        Resolving partner-name to partner-id requires this reverse map,
        which the ``Autorouter`` builds from its ``net_names`` dict.

        Idempotent and may be called multiple times.  Passing an empty
        dict disables partner detection (the C++ search falls back to
        ``clearance`` for every other net).
        """
        self._net_name_to_id = dict(mapping)

    def _resolve_partner_net_id(self, net_name: str) -> int | None:
        """Look up the integer net id of the diff-pair partner of *net_name*.

        Reads :attr:`NetClassRouting.diffpair_partner` and resolves the
        partner-name to a partner-id via :attr:`_net_name_to_id`.  Returns
        ``None`` when:

        * the source net has no net class (or the class has no
          ``diffpair_partner`` set), or
        * the partner-name is missing from :attr:`_net_name_to_id` (e.g.
          the autorouter has not populated the reverse map yet).

        ``None`` is the dormant signal for the C++ wiring sites: when
        partner is unknown, the search uses the wider ``clearance`` for
        every other net, matching pre-#2559 / pre-#2587 behavior.
        """
        net_class = self._net_class_map.get(net_name)
        if net_class is None or net_class.diffpair_partner is None:
            return None
        return self._net_name_to_id.get(net_class.diffpair_partner)

    def set_routable_layers(self, layers: list[int]) -> None:
        """Set which layers are routable (skip plane layers)."""
        self._impl.set_routable_layers(layers)
        # Mirror the value pushed to the C++ ``routable_layers_`` vector so
        # Python callers (and tests) can introspect the active set without a
        # dedicated C++ getter binding.
        self._routable_layers = list(layers)

    def _apply_allowed_layers_to_routable(self) -> None:
        """Restrict the C++ via-expansion to ``allowed_layers`` (Issue #715).

        When ``DesignRules.allowed_layers`` constrains routing to a subset of
        copper layers (e.g. a single-layer ``["F.Cu"]`` board), the C++
        pathfinder must not consider a layer change to a disallowed layer.
        The pathfinder defaults its routable-layer set to every grid layer, so
        we intersect that default with the ``allowed_layers``-permitted indices
        and push the result down to the C++ ``routable_layers_`` vector.

        With a single allowed layer the resulting set has one element, and the
        via loop's ``new_layer == current.layer`` skip leaves no via target --
        which is exactly the single-layer invariant ("a via requires >= 2
        routable layers"). This keeps the Issue #3622 / #864 own-net-obstacle
        via relaxation intact while preventing it from leaking vias onto
        single-layer routes.
        """
        if self._rules.allowed_layers is None:
            return  # No restriction; keep the pathfinder default (all layers).

        base = self._grid.get_routable_indices()
        permitted = [idx for idx in base if self._is_layer_allowed(idx)]
        # Defensive: never push an empty set (would make every route fail);
        # leave the default in place and let the start/end-layer filter in
        # the routing path reject impossible routes instead.
        if permitted and permitted != list(base):
            self.set_routable_layers(permitted)

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

    def _compute_pad_bounds(self, pad: Pad) -> router_cpp.PadBounds:
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

        # Issue #3471: degenerate (zero-area) pads -- Steiner branch
        # points are virtual pads with width == height == 0 -- produce
        # EMPTY metal bounds whenever ``(pad.x - origin) / resolution``
        # is not exactly integral in floating point: ceil(v) > floor(v)
        # leaves ``gx1 > gx2`` and the C++ A* seeds ZERO start nodes,
        # failing the edge with FAILURE_NO_PATH at 0 iterations.  Board
        # 05's ISENSE cluster (4-pad nets whose RSMT always synthesises
        # branch points) lost every Steiner-incident edge this way, on
        # every route of the board.  Clamp empty spans to the nearest
        # grid cell so a degenerate pad always seeds exactly one cell.
        if gx1 > gx2:
            gc = int(round((pad.x - origin_x) / resolution))
            gx1 = gx2 = max(0, min(self._grid.cols - 1, gc))
        if gy1 > gy2:
            gc = int(round((pad.y - origin_y) / resolution))
            gy1 = gy2 = max(0, min(self._grid.rows - 1, gc))

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

        Issue #2929: When per-A*-call timing instrumentation is enabled via
        :meth:`enable_per_call_timing`, this method records each call's
        elapsed wall-clock time alongside the deadline budget so callers
        can audit deadline-honor behavior.  The actual routing logic lives
        in :meth:`_route_impl`.

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
        if not self._per_call_timing_enabled:
            return self._route_impl(
                start,
                end,
                net_class=net_class,
                negotiated_mode=negotiated_mode,
                present_cost_factor=present_cost_factor,
                weight=weight,
                start_layers=start_layers,
                end_layers=end_layers,
                per_net_timeout=per_net_timeout,
                extra_goal_cells=extra_goal_cells,
            )

        t0 = time.monotonic()
        succeeded = False
        try:
            result = self._route_impl(
                start,
                end,
                net_class=net_class,
                negotiated_mode=negotiated_mode,
                present_cost_factor=present_cost_factor,
                weight=weight,
                start_layers=start_layers,
                end_layers=end_layers,
                per_net_timeout=per_net_timeout,
                extra_goal_cells=extra_goal_cells,
            )
            succeeded = result is not None
            return result
        finally:
            elapsed = time.monotonic() - t0
            # 1.2x slack matches the Issue #2929 acceptance criterion: the
            # C++ deadline check fires every 1024 iterations, plus the
            # Python wrapper has a fixed setup cost (~ms-scale), so a 20%
            # margin is enough that a deadline-honoring backend never
            # trips this flag.  Note: when the C++ search fails, the
            # Python fallback runs after with its OWN ``per_net_timeout``
            # budget; the elapsed clock covers BOTH, which is the
            # "wall-clock the caller actually waited" measurement.
            deadline_violated = (
                per_net_timeout is not None
                and per_net_timeout > 0
                and elapsed > per_net_timeout * 1.2 + 0.5
            )
            self._per_call_timings.append(
                {
                    "net": start.net,
                    "net_name": start.net_name,
                    "elapsed": elapsed,
                    "per_net_timeout": per_net_timeout,
                    "deadline_violated": deadline_violated,
                    "succeeded": succeeded,
                }
            )
            # Issue #3474 R1: live slow-call tracing under KCT_DEBUG_PNT.
            if os.environ.get("KCT_DEBUG_PNT") and (deadline_violated or elapsed > 5.0):
                print(
                    f"    [PNT-DEBUG] route() net={start.net_name} elapsed={elapsed:.1f}s "
                    f"budget={per_net_timeout} violated={deadline_violated} ok={succeeded}",
                    flush=True,
                )

    def _route_impl(
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
        """Inner C++-backed route implementation -- see :meth:`route` for the
        public wrapper that adds optional per-call wall-clock instrumentation.
        """
        # Get layer indices
        start_layer = self._grid.num_layers // 2  # Default to middle
        end_layer = self._grid.num_layers // 2

        # Try to get actual layer from pad.
        #
        # Issue #3304: Use the grid's ``layer_to_index`` mapping rather than
        # ``layer.value % num_layers``.  The modulo trick happens to give
        # the correct index for ``F.Cu`` (value=0) and for B.Cu only on
        # 6-layer stacks (5 % 6 == 5).  For ALL other layer counts it
        # produces the WRONG index for ``B.Cu`` (value=5):
        #
        #   - 2-layer: 5 % 2 = 1 ✓ (also happens to be correct)
        #   - 4-layer: 5 % 4 = 1 ✗ (should be 3; In1.Cu picked instead)
        #   - 6-layer: 5 % 6 = 5 ✓
        #
        # On 4-layer boards this caused the C++ A* to terminate on
        # ``In1.Cu`` (the wrongly-mapped goal layer) whenever the
        # destination virtual_pad was on ``B.Cu`` (the inner escape layer
        # the 4L SIG-GND-PWR-SIG stack falls back to when no inner SIGNAL
        # layers are present in ``_select_inner_escape_layer``).  The
        # escape route lays its inner stub on B.Cu but the main router
        # ends on In1.Cu -- they share an XY but no via bridges the layer
        # gap, so the union-find connectivity check counts the pad as
        # disconnected.  This was the root cause of the board 03
        # USB_CC2 regression after #3278 narrowed the escape stub: the
        # narrower stub stops blocking the main router's path so the
        # A* now finds the wrong-layer goal cell instead of failing
        # and falling back to a path that ends correctly.
        #
        # The ``layer_to_index`` lookup is what
        # :meth:`Router.route` in ``pathfinder.py`` already uses
        # (line 2348), so this brings the C++ backend into parity with
        # the Python reference path.
        if hasattr(start.layer, "value"):
            start_layer = self._grid.layer_to_index(start.layer.value)
        if hasattr(end.layer, "value"):
            end_layer = self._grid.layer_to_index(end.layer.value)

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

        # Issue #3130: per-net emit widths/diameters.  Forward the per-net
        # ``trace_width`` and ``via_size`` so the C++-internal Segment /
        # Via objects returned from route_resumable() carry per-net values
        # instead of the global ``rules_`` defaults.  Adapter overrides at
        # ``_convert_result_to_route`` remain in place as a defensive
        # fallback (used only when emit_* == 0).  ``via_drill`` is not yet
        # a per-net attribute on ``NetClassRouting``; fall back to the
        # global default so behavior matches pre-#3130 callers.
        emit_trace_width = float(net_trace_width) if net_class else 0.0
        emit_via_diameter = float(net_via_size) if net_class else 0.0
        emit_via_drill = 0.0

        # Issue #2587 / Epic #2556 Phase 1C-cont: Resolve the diff-pair partner
        # net id and compute a tighter within-pair search radius.  When the
        # source net's ``NetClassRouting`` declares a ``diffpair_partner`` AND
        # the partner-id is known (via ``set_net_name_to_id`` populated by the
        # autorouter), the C++ search uses ``intra_pair_radius_cells`` for
        # cells belonging to the partner net only.  All other foreign nets
        # continue to see the wider ``trace_radius_cells`` radius.  When
        # ``partner_net == -1`` (the dormant default) the C++ side preserves
        # pre-#2559 behavior identically.
        partner_net_id = -1
        intra_pair_radius_cells = 0
        if net_class is not None and net_class.diffpair_partner is not None:
            partner_id = self._net_name_to_id.get(net_class.diffpair_partner)
            if partner_id is not None:
                partner_net_id = int(partner_id)
                intra_pair_clearance = net_class.effective_intra_pair_clearance()
                intra_pair_radius_cells = max(
                    1,
                    math.ceil((net_trace_width / 2 + intra_pair_clearance) / self._grid.resolution),
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

        # Issue #3474 R1: the SAME deadline also bounds the Python
        # fallback.  Previously the fallback received a fresh
        # ``per_net_timeout`` budget after the C++ search had already
        # consumed its own, so one ``route()`` call could legally spend
        # 2x the cap (and the 10-100x-slower Python A* is exactly where
        # capped searches go to die).  ``None`` => unbudgeted (legacy).
        route_deadline = time.monotonic() + float(per_net_timeout) if per_net_timeout else None

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
                # Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair
                # clearance.  When ``partner_net == -1`` (the dormant default
                # before #2587 wired in the partner map), the C++ search uses
                # ``trace_radius_cells`` for every other net.  When set, cells
                # belonging to ``partner_net`` are checked against
                # ``intra_pair_radius_cells`` (the tighter within-pair radius).
                partner_net_id,
                intra_pair_radius_cells,
                # Issue #2610: per-net wall-clock deadline + iteration override.
                # Issue #3881: use the EFFECTIVE cap -- when the tuned per-net
                # iteration cap is set it binds (clamped by the memory
                # backstop); otherwise this is the memory backstop / heuristic.
                timeout_seconds,
                self._effective_search_iterations,
                # Issue #3130: per-net emit widths/diameters.  Forwarded so the
                # C++-internal RouteResult carries per-net Segment.width and
                # Via.diameter/drill matching the source net class instead of
                # the global ``rules_`` defaults.  When ``net_class is None``
                # all three are 0.0 and the C++ falls back to ``rules_.*``
                # (pre-#3130 behavior).
                emit_trace_width,
                emit_via_diameter,
                emit_via_drill,
                # Issue #3143: per-pad lateral-channel budget.  Populated
                # once per board by ``Router.route_with_escape`` via
                # :meth:`set_pad_channel_budgets`.  Empty list (the
                # default) keeps the C++ cost function pre-#3143
                # identical; populated list nudges the search away from
                # contested escape channels adjacent to dense-package
                # pad rows.  Filtered per-net so the originating net of
                # an escape pad is not penalised for routing through its
                # OWN escape endpoint -- only contention from OTHER nets
                # is shaped.
                self._filter_pad_channel_budgets_for_net(start.net),
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
                    deadline=route_deadline,
                    reason=self._describe_cpp_failure(result),
                    cpp_failure_reason=getattr(result, "failure_reason", None),
                )

            for attempt in range(max_resume_attempts + 1):
                route = self._convert_result_to_route(result, start, end, net_class)

                # Issue #3438: relief PROBES deliberately cross foreign
                # copper/halos -- post-route clearance validation would
                # reject every probe path (and pollute the avoidance-cost
                # state via _boost_avoidance_at).  Probe routes are never
                # committed, so skip validation entirely in relief mode.
                if self._relief_mode:
                    return route

                # Issue #1702 Gap 3 + Issue #2439: Post-route geometric
                # clearance validation via C++ validate_route().  Issue #2587
                # threads the partner net id + intra-pair clearance so the
                # validator does not reject diff-pair routes that legitimately
                # sit at the tighter within-pair distance.
                violation_location = self._validate_route_clearance(
                    route,
                    start,
                    end,
                    trace_radius_cells,
                    net_class=net_class,
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
                        deadline=route_deadline,
                        reason=(
                            "post-route clearance validation failed; "
                            f"exhausted {max_resume_attempts} resume attempts"
                        ),
                        cpp_failure_reason=getattr(result, "failure_reason", None),
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
                        deadline=route_deadline,
                        reason=(
                            "resume after rejected goal cell failed: "
                            + self._describe_cpp_failure(result)
                        ),
                        cpp_failure_reason=getattr(result, "failure_reason", None),
                    )

            return None
        finally:
            # Always clear search state to release memory (Issue #2447 risk).
            self._impl.clear_search_state()

    def _capture_failure_info(self, result: router_cpp.RouteResult) -> None:
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

    def _describe_cpp_failure(self, result: router_cpp.RouteResult) -> str:
        """Return a human-readable description of a failed C++ route result.

        Issue #3456: Used to surface WHY a net is being handed to the
        10-100x-slower Python fallback.  Maps the ``FAILURE_*`` constants
        from ``types.hpp`` to short explanations; unknown/absent reasons
        degrade to a generic message rather than raising.
        """
        if router_cpp is None:
            return "C++ backend unavailable"

        reason = int(getattr(result, "failure_reason", router_cpp.FAILURE_NONE))
        descriptions = {
            int(router_cpp.FAILURE_NO_PATH): ("no path (C++ A* open set exhausted)"),
            int(router_cpp.FAILURE_ITERATION_LIMIT): (
                "iteration limit reached (memory backstop cap)"
            ),
            int(router_cpp.FAILURE_TIMEOUT): ("per-net wall-clock deadline exceeded"),
            int(router_cpp.FAILURE_VIA_VIA_BLOCKED): (
                "all via candidates blocked by stored-via geometry"
            ),
        }
        desc = descriptions.get(reason)
        if desc is None:
            return f"C++ search failed (failure_reason={reason})"
        blocking_net = int(getattr(result, "blocking_via_net", 0))
        if reason == int(router_cpp.FAILURE_VIA_VIA_BLOCKED) and blocking_net:
            desc += f" (blocking net id {blocking_net})"
        return desc

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

    def _get_component_pitches(self) -> dict[str, float]:
        """Lazily compute and cache per-component pin pitches.

        Shared by the same-component carve-out gate (Issue #3545) and the
        Issue #1018 neck-down post-processing in
        :meth:`_convert_result_to_route`.  Returns an empty dict (without
        caching) when no Python source grid is attached, so a later
        attachment can still populate the cache.
        """
        pitches = self._component_pitches_cache
        if pitches is not None:
            return pitches
        py_grid = getattr(self._grid, "_py_grid", None)
        if py_grid is None:
            return {}
        try:
            pitches = py_grid.compute_component_pitches()
        except Exception:
            pitches = {}
        self._component_pitches_cache = pitches
        return pitches

    def _convert_result_to_route(
        self,
        result: router_cpp.RouteResult,
        start: Pad,
        end: Pad,
        net_class: NetClassRouting | None,
    ) -> Route:
        """Convert a C++ RouteResult to a Python Route object.

        Args:
            result: C++ route result with segments and vias.
            start: Source pad (provides net and net_name).
            end: Destination pad (needed for Issue #1018 neck-down taper).
            net_class: Optional net class for trace width override.

        Returns:
            Python Route object with segments, vias, and validated layer transitions.
        """
        from .layers import Layer
        from .primitives import Route, Segment, Via
        from .quantize import dogleg_points

        route = Route(net=start.net, net_name=start.net_name)

        # Issue #3456 follow-up: echo the requested float64 trace width
        # verbatim instead of reading back ``cpp_seg.width``.  The C++
        # Segment stores float32, so a requested 0.2 mm would round-trip
        # as 0.20000000298023224.  The width the C++ search emitted is
        # always known on the Python side: the per-net-class width when a
        # class is mapped (forwarded as ``emit_trace_width``, Issue #3130),
        # else the global ``rules.trace_width`` (the C++ ``rules_`` default
        # was populated from this same Python value).  This mirrors the
        # Python backend's ``_get_trace_width_for_net`` (Issue #1543).
        base_trace_width = float(net_class.trace_width if net_class else self._rules.trace_width)

        # Issue #1018 parity: the C++ pathfinder has no neck-down support.
        # Apply the same width taper as the Python backend
        # (``PathFinder._convert_path_to_route``) as post-processing on the
        # C++-returned segments.  Semantics match exactly: per-segment
        # width is the minimum over both pads of
        # ``rules.get_neck_down_width(min endpoint distance, pitch,
        # base_width=net-class width)``, gated per pad by
        # ``rules.should_apply_neck_down``.
        pitches = self._get_component_pitches()
        start_pitch = pitches.get(start.ref) if start.ref else None
        end_pitch = pitches.get(end.ref) if end.ref else None
        start_needs_neckdown = self._rules.should_apply_neck_down(start.ref, start_pitch)
        end_needs_neckdown = self._rules.should_apply_neck_down(end.ref, end_pitch)

        def _segment_width(x1: float, y1: float, x2: float, y2: float) -> float:
            """Trace width for one segment, with Issue #1018 neck-down taper."""
            if not start_needs_neckdown and not end_needs_neckdown:
                return base_trace_width
            min_width = base_trace_width
            if start_needs_neckdown:
                min_dist_start = min(
                    math.hypot(x1 - start.x, y1 - start.y),
                    math.hypot(x2 - start.x, y2 - start.y),
                )
                min_width = min(
                    min_width,
                    self._rules.get_neck_down_width(
                        min_dist_start, start_pitch, base_width=base_trace_width
                    ),
                )
            if end_needs_neckdown:
                min_dist_end = min(
                    math.hypot(x1 - end.x, y1 - end.y),
                    math.hypot(x2 - end.x, y2 - end.y),
                )
                min_width = min(
                    min_width,
                    self._rules.get_neck_down_width(
                        min_dist_end, end_pitch, base_width=base_trace_width
                    ),
                )
            return min_width

        for cpp_seg in result.segments:
            layer_enum_value = self._grid.index_to_layer(cpp_seg.layer)
            # Issue #3532: the C++ ``reconstruct_path`` connects the
            # exact (off-grid) pad centres to the first/last grid cell
            # with a single straight tail, which is off the 0/45/90/135
            # angle set in general.  Split such segments into an exact
            # two-leg dogleg HERE, in float64 -- the C++ side stores
            # float32 coordinates whose ulp at board scale (~1.5e-5 mm)
            # is too coarse to construct exactly-aligned legs.
            points = dogleg_points(
                float(cpp_seg.x1),
                float(cpp_seg.y1),
                float(cpp_seg.x2),
                float(cpp_seg.y2),
            )
            # Issue #1018: compute the (possibly necked-down) width once per
            # C++ segment, BEFORE the dogleg split -- matching the Python
            # backend, where ``_emit_segment`` computes the width on the
            # merged segment endpoints and applies it to both dogleg legs.
            seg_width = _segment_width(
                float(cpp_seg.x1),
                float(cpp_seg.y1),
                float(cpp_seg.x2),
                float(cpp_seg.y2),
            )
            for (sx, sy), (ex, ey) in zip(points, points[1:], strict=False):
                if sx == ex and sy == ey:
                    continue
                seg = Segment(
                    x1=sx,
                    y1=sy,
                    x2=ex,
                    y2=ey,
                    width=seg_width,
                    layer=Layer(layer_enum_value),
                    net=cpp_seg.net,
                    net_name=start.net_name,
                )
                route.segments.append(seg)

        # Issue #3130: Mirror the segment-width override for via diameter.
        # Previously C++ emitted ``rules_.via_diameter`` / ``rules_.via_drill``
        # regardless of net class; this caused POWER-class nets (which declare
        # ``via_size=0.8mm``) to emit vias at the global default.
        # Issue #3456 follow-up: like segment widths, echo the requested
        # float64 values verbatim instead of the C++ float32 round-trip.
        # Both are always known on the Python side: the per-net-class
        # ``via_size`` (forwarded as ``emit_via_diameter``) or the global
        # ``rules.via_diameter``; ``via_drill`` is not yet a per-net
        # attribute, so the C++ always emits ``rules_.via_drill`` (populated
        # from ``rules.via_drill``).
        via_diameter = float(net_class.via_size if net_class else self._rules.via_diameter)
        via_drill = float(self._rules.via_drill)

        for cpp_via in result.vias:
            layer_from_value = self._grid.index_to_layer(cpp_via.layer_from)
            layer_to_value = self._grid.index_to_layer(cpp_via.layer_to)
            via = Via(
                x=cpp_via.x,
                y=cpp_via.y,
                drill=via_drill,
                diameter=via_diameter,
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

    def _same_component_carveout_eligible(self, py_grid, ref: str) -> bool:
        """Return True when ``ref`` may keep the same-component carve-out.

        Issue #3545: the carve-out (skipping clearance checks against
        same-component pads) is only legitimate where a clearance
        relaxation is actually in effect for the component:

        1. The component's inter-pad corridor was relaxed by
           ``_relax_same_component_clearance`` (Issue #2452), or
        2. A fine-pitch / explicit per-component clearance relaxation
           applies (``get_clearance_for_component`` returns less than
           the default ``trace_clearance``, Issue #1764), or
        3. The component is fine-pitch (min pin pitch below
           ``rules.fine_pitch_threshold``) -- covers boards routed with
           ``fine_pitch_clearance`` unset (the default), where the
           clearance lookup cannot signal the relaxation.

        Standard-pitch components return False, so their FOREIGN-net
        pads stay in the C++ validator and sub-clearance copper is
        rejected at route construction time.  Mirrors
        ``RoutingGrid._same_component_carveout_active``.
        """
        relaxed_refs = getattr(py_grid, "_relaxed_clearance_refs", None)
        if relaxed_refs and ref in relaxed_refs:
            return True
        pitch = self._get_component_pitches().get(ref)
        required = self._rules.get_clearance_for_component(ref, pitch)
        if required < self._rules.trace_clearance:
            return True
        threshold = getattr(self._rules, "fine_pitch_threshold", None)
        return pitch is not None and threshold is not None and pitch < threshold

    def _validate_route_clearance(
        self,
        route: Route,
        start: Pad,
        end: Pad,
        trace_radius_cells: int,
        net_class: NetClassRouting | None = None,
    ) -> tuple[float, float] | None:
        """Validate post-route geometric clearance using C++ validation.

        Issue #2439: Uses the C++ validate_route() call which runs all 4
        validation checks (segment-pad, segment-segment, via-segment,
        via-via, same-net drill spacing) in a single C++ call, eliminating
        Python callback overhead.

        Issue #2587 / Epic #2556 Phase 1C-cont: When ``net_class`` declares a
        ``diffpair_partner`` AND the partner-id is resolvable via the reverse
        map populated by :meth:`set_net_name_to_id`, the C++ validator is
        instructed to compare against ``intra_pair_clearance`` (instead of
        ``trace_clearance``) for cells belonging to the partner net.  This is
        the post-route geometric companion to the search-time
        ``intra_pair_radius_cells`` plumbing in :meth:`route`.

        Args:
            route: Route to validate.
            start: Source pad (for component reference exclusion).
            end: Destination pad (for component reference exclusion).
            trace_radius_cells: Trace half-width in grid cells (for avoidance).
            net_class: Optional net class for the source net.  When set with a
                resolvable ``diffpair_partner``, supplies the tighter
                within-pair clearance to the C++ validator.

        Returns:
            (x, y) world coordinates of violation, or None if route is valid.
        """
        py_grid = getattr(self._grid, "_py_grid", None)
        if py_grid is None:
            return None

        # Sync stored segments/vias from completed routes to C++
        self._sync_stored_routes(py_grid)

        # Build exclude_ref_hashes for start/end pad components (Issue #1764)
        #
        # Issue #3545: NET-AWARE tightening.  The C++ validator's
        # same-component carve-out (``is_excluded_ref`` in
        # ``Grid3D::validate_route``) silently exempted FOREIGN-net pads
        # on the route's own components (same-net pads are skipped by
        # ``pad.net == exclude_net`` anyway), masking sub-clearance
        # copper like the routing-diagnostic NET3-vs-J1.1 0.127mm gap.
        # The carve-out's legitimate use is fine-pitch escape (#1764)
        # and relaxed same-component corridors (#2452) -- include a ref
        # in the exclusion set ONLY when one of those relaxations is
        # actually in effect for that component.  Mirrors the Python
        # validator gate in ``RoutingGrid.validate_segment_clearance``.
        exclude_ref_hashes: list[int] = []
        for pad in (start, end):
            if pad.ref and self._same_component_carveout_eligible(py_grid, pad.ref):
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

        # Issue #2587 / Phase 1C-cont: Resolve partner net id for the source
        # net so the C++ validator does not reject within-pair edges of a
        # legitimate diff pair.  Defaults preserve pre-#2559 behavior
        # identically (partner_net == -1 -> no relaxation).
        partner_net_id = -1
        intra_pair_clearance = 0.0
        if net_class is not None and net_class.diffpair_partner is not None:
            partner_id = self._net_name_to_id.get(net_class.diffpair_partner)
            if partner_id is not None:
                partner_net_id = int(partner_id)
                intra_pair_clearance = float(net_class.effective_intra_pair_clearance())

        vresult = self._grid._impl.validate_route(
            cpp_segs,
            cpp_vias,
            start.net,
            exclude_ref_hashes,
            self._rules.trace_clearance,
            self._rules.via_clearance,
            self._rules.min_drill_clearance,
            partner_net_id,
            intra_pair_clearance,
        )

        if not vresult.valid:
            return (vresult.violation_x, vresult.violation_y)

        # Issue #3002 (PR #3006 follow-up): Python-side segment-vs-foreign-via
        # post-check.  ``validate_route`` already walks the C++ side's
        # stored vias, but the autorouter can push additional foreign-net
        # vias via :meth:`set_segment_foreign_context` -- e.g. vias that
        # the negotiated post-iteration re-validation hook has surfaced
        # but that are not yet in the C++ side's ``stored_vias_`` snapshot.
        # Walks ``self._foreign_vias`` with the STANDARD threshold,
        # mirroring the predicate consumed by the Python pathfinder at
        # ``pathfinder.py:_validate_route_clearance``.
        if self._foreign_vias:
            from .via_clearance import segment_clears_foreign_via

            for seg in route.segments:
                for via in self._foreign_vias:
                    if via.net == start.net:
                        continue  # Same-net via -- skipped by convention.
                    if not segment_clears_foreign_via(
                        seg,
                        via,
                        trace_clearance=self._rules.trace_clearance,
                        hard_intersection_only=False,
                    ):
                        return (via.x, via.y)

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
        deadline: float | None = None,
        reason: str = "unknown",
        cpp_failure_reason: int | None = None,
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
            deadline: Optional ``time.monotonic()`` deadline shared with
                the C++ search that preceded this fallback (issue #3474
                R1).  The fallback only receives whatever budget the C++
                search left unspent; when none remains it is skipped so a
                single ``route()`` call can never double-spend its per-net
                cap inside the 10-100x-slower Python A*.
            reason: Human-readable explanation of WHY the C++ search
                handed this net to the Python fallback (issue #3456).
                Surfaced in the once-per-net WARNING and recorded in
                ``fallback_stats['fallback_reasons']``.
            cpp_failure_reason: The ``FAILURE_*`` code from the failed C++
                ``RouteResult`` (issue #3876).  When this is
                ``FAILURE_TIMEOUT`` -- a WALL-CLOCK artifact, not a
                geometric dead-end -- the Python fallback is short-circuited
                (returns ``None`` immediately, before constructing the
                Python ``Router`` or running the 10-100x-slower A*).  A
                wall-clock timeout means ~``per_net_timeout`` has already
                elapsed, so the fallback would start with ~0 budget and
                immediately time out itself, wasting deadline that
                subsequent nets need.  Geometric failures
                (``FAILURE_NO_PATH``, ``FAILURE_VIA_VIA_BLOCKED``) are NOT
                short-circuited -- the Python A*'s different neighbor expansion
                is exactly the value-add there (issue #3456).
                ``FAILURE_ITERATION_LIMIT`` is short-circuited ONLY when a tuned
                per-net iteration cap is active (``_per_net_iteration_cap_active``,
                issue #3881): a capped give-up is deterministic and running the
                slow Python A* would re-introduce the per-net slowness the cap
                prevents.  When the iteration limit is the 12M MEMORY backstop
                (no tuned cap) the fallback still runs.  ``None`` (the default)
                preserves pre-#3876 behavior.

        Returns:
            Route object if fallback succeeds, None if also fails (or the
            shared per-net deadline is already exhausted, or the C++ failure
            was a wall-clock timeout per issue #3876).
        """
        py_grid = self._grid._py_grid
        if py_grid is None:
            return None

        net_name = getattr(start, "net_name", "?")

        # Issue #3876: a wall-clock FAILURE_TIMEOUT is a load artifact, not a
        # geometric dead-end -- on an idle machine the C++ search would have
        # found the path at the same (deterministic) iteration count.  The
        # Python fallback shares the SAME per-net deadline, so on a timeout it
        # would start with ~0 remaining budget and immediately time out
        # itself, burning deadline that later nets need.  Short-circuit BEFORE
        # constructing the Python ``Router`` / running the A* so the deadline
        # is a hard budget rather than a reason to grind 10-100x longer.  This
        # is a strict subset of the #3474 ``remaining <= 0.05`` skip below and
        # cannot reduce routed reach.  ``router_cpp`` may be ``None`` (import
        # failed); guard the constant lookup as the other failure-path helpers
        # do.  Geometric failures fall through and keep the fallback.
        if (
            cpp_failure_reason is not None
            and router_cpp is not None
            and int(cpp_failure_reason) == int(router_cpp.FAILURE_TIMEOUT)
        ):
            logger.debug(
                "Net %s: C++ search hit the per-net wall-clock deadline "
                "(FAILURE_TIMEOUT); skipping Python fallback so the budget is "
                "hard (issue #3876)",
                net_name,
            )
            return None

        # Issue #3881: when a TUNED per-net iteration cap is active, a
        # FAILURE_ITERATION_LIMIT is a DETERMINISTIC give-up -- the C++ search
        # was deliberately bounded so the net fails fast and the NEXT net gets
        # budget.  Running the 10-100x-slower Python A* here would re-introduce
        # exactly the per-net slowness the cap exists to prevent (a capped net
        # burning minutes in Python), so short-circuit before constructing the
        # Python ``Router``.  This keeps the per-net cap a HARD per-net bound
        # and is load-independent (iteration count), so determinism is
        # preserved.  Note: this only fires when ``_per_net_iteration_cap_active``
        # -- when the iteration limit is the 12M MEMORY backstop (no tuned cap),
        # the fallback still runs as before so genuine dense escapes are not
        # silently dropped.
        if (
            self._per_net_iteration_cap_active
            and cpp_failure_reason is not None
            and router_cpp is not None
            and int(cpp_failure_reason) == int(router_cpp.FAILURE_ITERATION_LIMIT)
        ):
            logger.debug(
                "Net %s: C++ search hit the tuned per-net iteration cap "
                "(%d expansions, FAILURE_ITERATION_LIMIT); skipping Python "
                "fallback so the cap is a hard per-net bound and the next net "
                "gets budget (issue #3881)",
                net_name,
                self._effective_search_iterations,
            )
            return None

        # Issue #3923: resume-exhaustion cascade.  The C++ resumable
        # pathfinder runs a post-route clearance-validation loop: after each A*
        # result it checks the path for clearance violations, boosts avoidance
        # cost at the violation site, and resumes the search.  Two dead-end
        # outcomes hand the net to this fallback (see ``route()``):
        #
        #   CASE 1 -- "post-route clearance validation failed; exhausted 5
        #      resume attempts": 5 boosted resumes each produced a
        #      geometrically VALID path that still violated clearance.  A path
        #      EXISTS; the obstruction is a clearance the avoidance boosting
        #      could not steer around.  The pure-Python A* shares the SAME
        #      ``_py_grid`` and the SAME clearance model, so once its fresh
        #      full A* has failed the net it cannot honour a clearance the C++
        #      validator keeps rejecting -- repeated exhaustions merely
        #      reproduce the same clearance violation 10-100x more slowly
        #      (60-200s/net; board-07 spent the bulk of its pipeline seconds
        #      here in the 2026-07-05 sweep).  We short-circuit ONLY the
        #      REPEAT (2nd+) clearance-exhaustion of a given net (see the
        #      per-net counter below); the FIRST one still falls back, because
        #      the fresh Python A* demonstrably rescues real nets/pads there.
        #
        #   CASE 2 -- "resume after rejected goal cell failed: no path (...)":
        #      a RESUMED search exhausted its already-avoidance-boosted,
        #      partially-consumed open set with FAILURE_NO_PATH.  This is NOT a
        #      clearance dead-end: it is a *path-finding* failure on a search
        #      frontier that has been distorted by prior goal-cell rejections
        #      and avoidance boosts.  A FRESH full Python A* from scratch --
        #      with the 45-degree / waypoint neighbour expansion and an
        #      un-distorted open set -- explores differently and DOES rescue
        #      real nets here (measured: USB-joystick 5->7 nets).  So we NEVER
        #      skip case 2; it always keeps its Python fallback.
        #
        # (An earlier revision of this guard skipped case 2 AND skipped case 1
        # on the FIRST exhaustion; both regressed real routes -- PR #3956 judge
        # review: USB-joystick dropped to 5/16 and board-07 GND stranded pad
        # U1.24.  The narrowing below -- case-1 only, repeat-only -- fixes both
        # while keeping the bulk of the perf win, since the wasteful grind is
        # the REPEATED re-exhaustion of the same net across rip-up rounds.)
        #
        # The guard therefore keys ONLY on the case-1 clearance-exhaustion
        # marker ("... exhausted N resume attempts", i.e. "resume attempts" in
        # the reason string), NOT on FAILURE_NO_PATH and NOT on the case-2
        # "resume after rejected goal cell failed" phrasing.  It never fires
        # for the INITIAL-search failure ("no path (C++ A* open set
        # exhausted)", no "resume attempts" phrasing) -- single-corridor
        # geometries the Python expansion legitimately rescues still fall back.
        # It also excludes FAILURE_VIA_VIA_BLOCKED (all via candidates refused
        # by stored-via geometry -- a distinct obstruction the negotiated
        # strategy targets with rip-up, and one where the Python router's via
        # placement can differ) and FAILURE_TIMEOUT (already short-circuited
        # above -- a wall-clock artifact, not a geometric dead-end).  Opt out
        # with KICAD_ROUTER_SKIP_RESUME_FALLBACK=0 to restore the pre-#3923
        # grind.
        _resume_exhausted = "resume attempts" in reason
        _excluded_code = (
            cpp_failure_reason is not None
            and router_cpp is not None
            and (
                int(cpp_failure_reason)
                in (
                    int(router_cpp.FAILURE_TIMEOUT),
                    int(router_cpp.FAILURE_VIA_VIA_BLOCKED),
                )
            )
        )
        if (
            _resume_exhausted
            and not _excluded_code
            and os.environ.get("KICAD_ROUTER_SKIP_RESUME_FALLBACK", "1").strip() != "0"
        ):
            # Skip only on the SECOND+ clearance-exhaustion for THIS net.  The
            # first exhaustion still runs the Python fallback: the fresh full
            # A* (45-degree / waypoint expansion, un-distorted open set)
            # measurably rescues real nets/pads there (board-07 GND pad U1.24,
            # USB-joystick nets).  A repeat exhaustion of the SAME net means the
            # negotiated rip-up loop re-presented the same clearance
            # obstruction that the fresh Python A* already failed to resolve --
            # that is the 60-200s/net dead loss the optimization targets.
            _seen = self._resume_clearance_exhaustions.get(net_name, 0)
            self._resume_clearance_exhaustions[net_name] = _seen + 1
            if _seen >= 1:
                logger.debug(
                    "Net %s: C++ resumable search exhausted its clearance-validation "
                    "resume attempts again (occurrence %d, reason=%r); skipping "
                    "Python fallback -- the same grid + clearance model already "
                    "failed this net once and will violate the same clearance "
                    "10-100x slower (issue #3923 case 1, repeat)",
                    net_name,
                    _seen + 1,
                    reason,
                )
                return None

        # Issue #3456: the silent C++ -> Python downgrade is the bug.
        # A net grinding 3-7 minutes in the pure-Python A* is otherwise
        # indistinguishable from "router is slow" at default verbosity.
        # Warn LOUDLY, once per net per run (negotiated rip-up retries
        # the same net many times -- dedupe keeps the log readable), and
        # record the reason for ``fallback_stats`` consumers.  Skipped in
        # relief-probe mode: probes deliberately stress the search, are
        # never committed, and a probe-time fallback is not a
        # user-facing performance event.

        # Issue #3474 R1: clamp the fallback budget to the unspent
        # remainder of the shared per-net deadline.
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                logger.debug(
                    "Net %s: per-net budget exhausted by C++ search; "
                    "skipping Python fallback (issue #3474)",
                    net_name,
                )
                return None
            per_net_timeout = (
                min(per_net_timeout, remaining) if per_net_timeout is not None else remaining
            )

        # The fallback is actually going to run -- make it LOUD.  This
        # sits after the deadline-exhaustion early-return above so a
        # skipped fallback (no grind) stays quiet, and is suppressed in
        # relief-probe mode (probes deliberately stress the search, are
        # never committed, and a probe-time fallback is not a
        # user-facing performance event).
        if not self._relief_mode:
            self._fallback_reasons.setdefault(net_name, reason)
            if net_name not in self._fallback_warned:
                self._fallback_warned.add(net_name)
                logger.warning(
                    "Net %s: C++ pathfinder gave up (%s); falling back to "
                    "the pure-Python A* (typically 10-100x slower -- this "
                    "net may take minutes). See "
                    "router.backend_info['fallback_stats'] for details.",
                    net_name,
                    reason,
                )

        # Lazy-construct the Python Router on first fallback
        if self._py_router is None:
            from .pathfinder import Router

            self._py_router = Router(
                py_grid,
                self._rules,
                net_class_map=self._net_class_map,
                diagonal_routing=self._diagonal_routing,
            )

        # Issue #3438: keep the fallback router's relief-probe mode in
        # lock-step with the C++ side (set via ``set_relief_mode``).
        self._py_router.set_relief_mode(self._relief_mode)

        # Issue #3881: when a tuned per-net iteration cap is active, bound the
        # Python A* DETERMINISTICALLY by the same iteration budget.  A
        # geometric-failure net (post-route-clearance / no-path) is NOT
        # short-circuited above -- the Python A*'s different neighbor expansion
        # is the value-add (#3456) and legitimately recovers nets the C++
        # search cannot (e.g. board-03's USB diff pairs).  But under
        # --deterministic-budget there is NO per-net wall-clock deadline, so an
        # UNBOUNDED Python fallback grinds for minutes and monopolises the
        # overall --timeout exactly as the C++ search would have (chorus
        # observed nets jumping 192s -> 375s -> 471s in the fallback).  An
        # iteration cap keeps the fallback's reach where the net is quick to
        # route in Python while cutting off the grinders deterministically
        # (load-independent), so more nets get a turn.  ``None`` (no tuned cap)
        # preserves the historical ``cols*rows*4`` self-bound.
        if self._per_net_iteration_cap_active:
            self._py_router._max_iterations_override = self._effective_search_iterations
        else:
            self._py_router._max_iterations_override = None

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

    def set_relief_mode(self, enabled: bool) -> None:
        """Enable/disable the relief-probe mode (Issue #3438).

        In relief mode, sharing-mode foreign usage-0 non-obstacle cells
        (escape stubs, route clearance halos, via halo rings) become
        passable at a finite per-step penalty instead of hard-blocking,
        so a zero-overflow hard failure can produce a min-conflict probe
        path.  Applied to BOTH the C++ pathfinder and the lazy Python
        fallback router so probe semantics are backend-independent.

        Probe-only: the negotiated outer loop never commits a route found
        in relief mode -- it extracts the conflicted owner nets along the
        probe path and feeds them to the transactional targeted rip-up.
        """
        self._relief_mode = bool(enabled)
        self._impl.set_relief_mode(self._relief_mode)
        if self._py_router is not None:
            self._py_router.set_relief_mode(self._relief_mode)

    @property
    def fallback_stats(self) -> dict:
        """Get statistics about Python fallback usage.

        Returns:
            Dictionary with:
                - fallback_count: Number of nets routed via Python fallback
                - fallback_nets: List of net names that used fallback
                - fallback_reasons: Mapping of net name -> human-readable
                  reason the C++ search handed that net to the Python
                  fallback (issue #3456).  Includes nets where the Python
                  fallback was attempted but ALSO failed (those do not
                  appear in ``fallback_nets``), so slow failed grinds are
                  attributable too.
        """
        return {
            "fallback_count": self._fallback_count,
            "fallback_nets": list(self._fallback_nets),
            "fallback_reasons": dict(self._fallback_reasons),
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
        net_class: NetClassRouting | None = None,
    ) -> set[int]:
        """Find which nets block the direct path from start to end.

        Uses Bresenham's line algorithm to trace the ideal direct path,
        then identifies which net IDs are blocking cells along that path.
        This is used for targeted rip-up in negotiated routing.

        Issue #2587 / Epic #2556 Phase 1C-cont: When the source net has a
        diff-pair partner (resolvable via :meth:`_resolve_partner_net_id`),
        the partner net is excluded from the blocker set.  The partner's
        copper is *expected* to sit close to this route at the within-pair
        clearance; treating it as a blocker would trigger spurious rip-up
        of the partner during negotiated routing.  Unlike the search-time
        plumbing, this filter does not need a tighter radius -- the
        partner is simply not a candidate for rip-up.

        Args:
            start: Source pad
            end: Destination pad
            layer: Optional layer index (uses pad layer if not specified)
            net_class: Optional net class for per-net trace width (Issue #1692).

        Returns:
            Set of net IDs that block the path (excluding net 0, the source
            net, and -- when configured -- the diff-pair partner net).
        """
        blocking_nets: set[int] = set()
        source_net = start.net

        # Issue #2587 / Phase 1C-cont: Resolve the diff-pair partner net id
        # (or -1 when no partner is configured).  Cells belonging to the
        # partner are skipped below so they are not flagged for rip-up.
        partner_net_id = self._resolve_partner_net_id(start.net_name) or -1

        # Convert to grid coordinates
        start_gx, start_gy = self._grid._impl.world_to_grid(start.x, start.y)
        end_gx, end_gy = self._grid._impl.world_to_grid(end.x, end.y)

        if layer is None:
            # Issue #3304: use ``layer_to_index`` not ``layer.value %
            # num_layers``.  See the comment in ``_route_impl`` for the
            # full explanation of why the modulo trick mismaps B.Cu to
            # an inner layer on 4-layer stacks.
            layer = self._grid.layer_to_index(start.layer.value)

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
                            if (
                                cell.blocked
                                and cell.net != source_net
                                and cell.net != 0
                                and cell.net != partner_net_id
                            ):
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
    per_net_iterations: int = 0,
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
        per_net_iterations: Issue #3881 -- tuned per-net iteration cap
            (``0`` = unset).  When set, each net gives up deterministically at
            the cap and its Python fallback is skipped.

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
                per_net_iterations=per_net_iterations,
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


# ---------------------------------------------------------------------------
# Coupled diff-pair pathfinder (Issue #4065)
# ---------------------------------------------------------------------------


class CppCoupledPathfinder:
    """C++ wrapper for the coupled differential-pair joint-state A* search.

    Mirrors the marshalling contract of :class:`CppPathfinder`: a
    :class:`CppGrid` is built once from the Python :class:`RoutingGrid` (or
    reused), the derived clearance radii + rule scalars are passed in once,
    and each :meth:`route` call marshals the per-pair pad grid coordinates,
    the corridor bitset and the budgets across the nanobind boundary.

    The C++ side returns a joint grid-cell path (``CoupledRouteResult.path``);
    :meth:`route` returns that path as a list of
    ``(p_x, p_y, p_layer, n_x, n_y, n_layer, via_from_parent)`` tuples plus a
    diagnostics dict.  The pure-Python ``CoupledPathfinder.route_coupled``
    caller unpacks this into the same ``p_path`` / ``n_path`` lists its own
    ``_reconstruct_coupled_routes`` produces and builds the two
    :class:`Route` objects with the unchanged Python ``_build_route_from_path``
    -- so C++ and Python produce byte-identical routes for the same joint
    path (Issue #4065 curator guidance: do NOT port ``_reconstruct``).

    v1 scope (deferred features stay on the Python fallback): this wrapper is
    only used for the ``partner_aware`` heuristic with ``allow_swap_via`` off;
    the Python caller checks those preconditions before dispatching here.
    """

    def __init__(
        self,
        cpp_grid: CppGrid,
        rules: DesignRules,
        target_spacing_cells: int,
        min_spacing_cells: int,
        trace_half_width_cells: int,
        via_extra_cells: int,
        via_drill_cells: int,
        spacing_penalty_factor: float,
        heuristic_weight: float,
    ):
        if not _CPP_AVAILABLE:
            raise RuntimeError("C++ router backend not available")
        self._grid = cpp_grid
        # Marshal the DesignRules scalars into the C++ struct (mirrors the
        # single-ended CppPathfinder rules marshalling).
        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = float(rules.trace_width)
        cpp_rules.trace_clearance = float(rules.trace_clearance)
        cpp_rules.via_drill = float(rules.via_drill)
        cpp_rules.via_diameter = float(rules.via_diameter)
        cpp_rules.via_clearance = float(rules.via_clearance)
        cpp_rules.grid_resolution = float(rules.grid_resolution)
        cpp_rules.cost_straight = float(rules.cost_straight)
        cpp_rules.cost_turn = float(rules.cost_turn)
        cpp_rules.cost_via = float(rules.cost_via)
        # Issue #4071: marshal the attractor bonus for struct consistency.
        # NOTE: the coupled joint-state A* (#4069) does not yet consume the
        # corridor attractor -- porting it into ``coupled_pathfinder.cpp``'s
        # cost loop is deferred (single-ended ``Pathfinder`` is the consumer
        # this issue wires up).  Marshalled here so a future coupled-attractor
        # port needs no additional plumbing.
        cpp_rules.cost_corridor_attractor = float(rules.cost_corridor_attractor)
        self._impl = router_cpp.CoupledPathfinder(
            cpp_grid._impl,
            cpp_rules,
            int(target_spacing_cells),
            int(min_spacing_cells),
            int(trace_half_width_cells),
            int(via_extra_cells),
            int(via_drill_cells),
            float(spacing_penalty_factor),
            float(heuristic_weight),
        )

    def route(
        self,
        *,
        p_start_xy: tuple[int, int],
        n_start_xy: tuple[int, int],
        start_layer: int,
        p_goal_xy: tuple[int, int],
        n_goal_xy: tuple[int, int],
        end_layer: int,
        p_net: int,
        n_net: int,
        effective_target_spacing: int,
        effective_approach_radius: int,
        effective_departure_radius: int,
        routable_layers: list[int],
        corridor_bitset: list[int],
        max_iterations_budget: int,
        timeout_seconds: float,
    ) -> tuple[list[tuple[int, int, int, int, int, int, bool]] | None, dict]:
        """Run the coupled C++ search.

        Returns ``(path, diagnostics)`` where ``path`` is a list of
        ``(p_x, p_y, p_layer, n_x, n_y, n_layer, via_from_parent)`` tuples in
        root->goal order (or ``None`` when the search failed), and
        ``diagnostics`` carries ``iterations`` / ``best_progress`` /
        ``timeout_exceeded`` / ``iteration_limited`` for the caller's
        ``last_*`` bookkeeping.
        """
        res = self._impl.route(
            int(p_start_xy[0]),
            int(p_start_xy[1]),
            int(n_start_xy[0]),
            int(n_start_xy[1]),
            int(start_layer),
            int(p_goal_xy[0]),
            int(p_goal_xy[1]),
            int(n_goal_xy[0]),
            int(n_goal_xy[1]),
            int(end_layer),
            int(p_net),
            int(n_net),
            int(effective_target_spacing),
            int(effective_approach_radius),
            int(effective_departure_radius),
            list(routable_layers),
            corridor_bitset,
            int(max_iterations_budget),
            float(timeout_seconds),
        )
        diagnostics = {
            "iterations": int(res.iterations),
            "best_progress": float(res.best_progress),
            "timeout_exceeded": bool(res.timeout_exceeded),
            "iteration_limited": bool(res.iteration_limited),
        }
        if not res.success:
            return None, diagnostics
        path = [
            (n.p_x, n.p_y, n.p_layer, n.n_x, n.n_y, n.n_layer, n.via_from_parent) for n in res.path
        ]
        return path, diagnostics
