"""
KiCad Grid Constants and Snapping Utilities

Provides grid size constants and functions for snapping coordinates to grid points.
"""

import os
import warnings
from enum import Enum
from pathlib import Path


class GridSize(Enum):
    """Standard KiCad grid sizes."""

    # Schematic grids (in mm)
    SCH_COARSE = 2.54  # 100 mil - large component spacing
    SCH_STANDARD = 1.27  # 50 mil - standard schematic grid
    SCH_FINE = 0.635  # 25 mil - fine placement
    SCH_ULTRA_FINE = 0.254  # 10 mil - text/label alignment

    # PCB grids (in mm)
    PCB_COARSE = 1.0  # 1mm - coarse placement
    PCB_STANDARD = 0.5  # 0.5mm - standard placement
    PCB_FINE = 0.25  # 0.25mm - fine placement
    PCB_ULTRA_FINE = 0.1  # 0.1mm - precision placement


# Default grid for schematic operations
DEFAULT_GRID = GridSize.SCH_STANDARD.value  # 1.27mm


def get_symbol_search_paths() -> list[Path]:
    """Get KiCad symbol library search paths, honoring ``KICAD_SYMBOL_DIR``.

    This is the single source of truth for symbol library discovery,
    mirroring how ``detect_kicad_library_path()`` resolves footprint
    paths from ``KICAD_FOOTPRINT_DIR``. The environment variable is read
    at call time (not import time), so it works regardless of when it is
    set — important on systems like NixOS where libraries live in
    non-standard store paths.

    Priority order:
    1. ``KICAD_SYMBOL_DIR`` environment variable
    2. Platform default locations (macOS app bundle, Linux system dirs,
       user-local share)

    Returns:
        List of existing directories, highest priority first. Paths that
        do not exist on this machine are filtered out.
    """
    paths: list[Path] = []

    # 1. Environment variable override (highest priority)
    env_dir = os.environ.get("KICAD_SYMBOL_DIR")
    if env_dir:
        paths.append(Path(env_dir))

    # 2. Platform defaults
    # macOS
    paths.append(Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"))
    # Linux
    paths.append(Path("/usr/share/kicad/symbols"))
    paths.append(Path("/usr/local/share/kicad/symbols"))
    # User local (both platforms)
    paths.append(Path.home() / ".local/share/kicad/symbols")

    return [p for p in paths if p.exists()]


def snap_to_grid(value: float, grid: float = DEFAULT_GRID) -> float:
    """Snap a coordinate to the nearest grid point.

    Args:
        value: Coordinate value in mm
        grid: Grid spacing in mm (default: 1.27mm for schematics)

    Returns:
        Snapped coordinate value, rounded to 2 decimal places
    """
    snapped = round(value / grid) * grid
    return round(snapped, 2)


def snap_point(point: tuple[float, float], grid: float = DEFAULT_GRID) -> tuple[float, float]:
    """Snap a point (x, y) to the nearest grid intersection.

    Args:
        point: (x, y) coordinate tuple
        grid: Grid spacing in mm

    Returns:
        Snapped (x, y) tuple
    """
    return (snap_to_grid(point[0], grid), snap_to_grid(point[1], grid))


def is_on_grid(value: float, grid: float = DEFAULT_GRID, tolerance: float = 0.001) -> bool:
    """Check if a coordinate is on the grid.

    Args:
        value: Coordinate value to check
        grid: Grid spacing in mm
        tolerance: Allowed deviation from grid (default: 0.001mm)

    Returns:
        True if value is within tolerance of a grid point
    """
    remainder = abs(value % grid)
    return remainder < tolerance or (grid - remainder) < tolerance


def check_grid_alignment(
    point: tuple[float, float], grid: float = DEFAULT_GRID, context: str = "", warn: bool = True
) -> bool:
    """Check if a point is on the grid, optionally warning if not.

    Args:
        point: (x, y) coordinate tuple
        grid: Grid spacing in mm
        context: Context string for warning message
        warn: Whether to emit a warning if off-grid

    Returns:
        True if point is on grid
    """
    x_ok = is_on_grid(point[0], grid)
    y_ok = is_on_grid(point[1], grid)

    if not (x_ok and y_ok) and warn:
        snapped = snap_point(point, grid)
        ctx = f" ({context})" if context else ""
        warnings.warn(
            f"Off-grid coordinate{ctx}: ({point[0]}, {point[1]}) -> "
            f"nearest grid: ({snapped[0]}, {snapped[1]})",
            stacklevel=3,
        )

    return x_ok and y_ok
