"""Cell ceiling for an explicit routing grid (Issue #6875).

Issue #6741 made ``kct route --grid X`` honour ``X`` exactly instead of letting
the Autorouter's #972 adaptive cap coarsen it.  That removed the only thing
standing between a very fine explicit grid on a big board and an allocation
that runs the machine out of memory.  This module puts a per-backend ceiling
back -- as a loud, overridable error rather than a silent coarsening.

The limits were chosen so no committed board recipe (boards 00-09) comes near
them.  The largest is board 09's routing-investigation ``--grid 0.05`` on a
100 x 65 mm board: 2001 x 1301 x 6 = 15.6M cells at the full ``--auto-layers``
ladder.

Measured footprint (macOS arm64, 20M-cell board): the Python ``RoutingGrid``
holds ~18 bytes/cell of state arrays; the C++ grid adds ~53 bytes/cell.
"""

from __future__ import annotations

import math

#: Python backend: ~18 B/cell of grid state, so ~0.4 GB of arrays before the
#: search itself -- and the pure-Python A* is far too slow to route that many
#: cells in practice anyway.
PYTHON_EXPLICIT_GRID_CELL_LIMIT = 20_000_000

#: C++ backend: the grid cell is 52 B, the pathfinder's search arrays add
#: ~12 B/cell and the per-column/row via arrays ~20 B, so the peak is about
#: 85-90 B/cell -- ~8.5-9 GB at the limit, the largest grid a typical
#: workstation can still allocate (Issue #6948).
CPP_EXPLICIT_GRID_CELL_LIMIT = 100_000_000


def explicit_grid_cell_limit(backend: str) -> int:
    """The cell ceiling for ``backend`` (``"cpp"`` or ``"python"``)."""
    return CPP_EXPLICIT_GRID_CELL_LIMIT if backend == "cpp" else PYTHON_EXPLICIT_GRID_CELL_LIMIT


def estimate_grid_cells(width: float, height: float, resolution: float, num_layers: int) -> int:
    """Cells a ``RoutingGrid`` of this size would allocate (``int(w/r)+1`` per axis)."""
    cols = int(width / resolution) + 1
    rows = int(height / resolution) + 1
    return cols * rows * max(1, int(num_layers))


def finest_pitch_within(width: float, height: float, num_layers: int, limit: int) -> float:
    """The finest pitch (rounded up to 0.005 mm) whose grid fits under ``limit``."""
    pitch = math.sqrt(width * height * max(1, num_layers) / limit)
    pitch = math.ceil(pitch / 0.005) * 0.005
    while estimate_grid_cells(width, height, pitch, num_layers) > limit:
        pitch += 0.005
    return round(pitch, 3)


def resolve_limit_backend(backend_arg: str | None) -> str:
    """Which backend's ceiling applies to a ``--backend`` value.

    ``auto`` (the default) routes on C++ when the extension is importable;
    a missing extension gets the Python ceiling, whose error names
    ``kct build-native``.
    """
    if backend_arg == "python":
        return "python"
    if backend_arg == "cpp":
        return "cpp"
    from .cpp_backend import is_cpp_available

    return "cpp" if is_cpp_available() else "python"


def explicit_grid_ceiling_error(
    *,
    width: float,
    height: float,
    resolution: float,
    num_layers: int,
    backend: str,
    clearance: float | None = None,
) -> str | None:
    """The error text when an explicit grid exceeds the ceiling, else None.

    ``clearance`` (mm), when known, bounds the suggested coarser pitch: a
    grid coarser than the clearance is refused separately, so a suggestion
    past it would only trade one error for another (Issue #6948).
    """
    cells = estimate_grid_cells(width, height, resolution, num_layers)
    limit = explicit_grid_cell_limit(backend)
    if cells <= limit:
        return None
    backend_name = "C++" if backend == "cpp" else "Python"
    options: list[str] = []
    # Building the C++ backend only helps when the grid fits its limit.
    if backend != "cpp" and cells <= CPP_EXPLICIT_GRID_CELL_LIMIT:
        options.append(
            "Build the C++ backend: kct build-native "
            f"(raises the limit to {CPP_EXPLICIT_GRID_CELL_LIMIT:,} cells)"
        )
    coarser = finest_pitch_within(width, height, num_layers, limit)
    if clearance is None or coarser <= clearance:
        options.append(f"Use a coarser grid: --grid {coarser:g} (the finest that fits)")
    options.append("Use --grid auto for budget-aware automatic selection")
    options.append("Use --force to route at the requested pitch anyway (may run out of memory)")
    numbered = "\n".join(f"  {i}. {text}" for i, text in enumerate(options, 1))
    return (
        f"Error: --grid {resolution:g}mm on this {width:.1f} x {height:.1f} mm board "
        f"needs {cells:,} grid cells ({num_layers} layers), above the "
        f"{limit:,}-cell limit for the {backend_name} backend (Issue #6875).\n\n"
        f"Options:\n{numbered}\n"
    )


def format_grid_line(report: dict) -> str:
    """The text ``Grid:`` value for a :meth:`Autorouter.grid_report` dict."""
    line = (
        f"{report['used_mm']:.4g}mm pitch"
        + (f" ({report['selection']})" if report.get("selection") else "")
        + f", {report['cols']}x{report['rows']}x{report['layers']} = {report['cells']:,} cells"
    )
    if report.get("coarsened"):
        line += (
            f" -- coarsened from the requested {report['requested_mm']:.4g}mm: "
            f"{report['coarsened_reason']}"
        )
    return line


__all__ = [
    "CPP_EXPLICIT_GRID_CELL_LIMIT",
    "PYTHON_EXPLICIT_GRID_CELL_LIMIT",
    "estimate_grid_cells",
    "explicit_grid_ceiling_error",
    "explicit_grid_cell_limit",
    "finest_pitch_within",
    "format_grid_line",
    "resolve_limit_backend",
]
