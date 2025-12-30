"""
KiCad Grid Constants and Snapping Utilities

Provides grid size constants and functions for snapping coordinates to grid points.
"""

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

# Default KiCad library paths
KICAD_SYMBOL_PATHS = [
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
    Path("/usr/share/kicad/symbols"),
    Path.home() / ".local/share/kicad/symbols",
]


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
