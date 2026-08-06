"""
Shared geometry for schematic symbol field placement.

This module is the single source of truth for the "field distance from the
symbol body" metric and the deterministic default field positions used by
``kct sch tidy`` (and reusable by schematic lint rules) -- pure geometry with
no CLI dependencies.

Coordinate conventions
----------------------

* Library symbol geometry (``LibrarySymbol.graphics`` / ``pins``) is in
  library coordinates: Y-up, relative to the symbol origin.
* Placed (sheet) coordinates are Y-down. The transform applied here matches
  :meth:`LibrarySymbol.get_pin_position`: mirror, then rotation (both in
  library coordinates), then Y negation, then translation to the instance
  position.
* Bounding boxes are ``(min_x, min_y, max_x, max_y)`` in sheet coordinates,
  so "above the symbol" is *smaller* y and "below" is *larger* y.
"""

from __future__ import annotations

import math

from .library import (
    KICAD_GRID,
    LibrarySymbol,
    SymbolArc,
    SymbolCircle,
    SymbolPolyline,
    SymbolRectangle,
)

__all__ = [
    "DEFAULT_FIELD_CLEARANCE_MM",
    "placed_body_bbox",
    "field_offset_mm",
    "default_field_positions",
]

#: Bounding box in sheet coordinates: (min_x, min_y, max_x, max_y).
BBox = tuple[float, float, float, float]

#: Clearance between the symbol body bbox edge and a tidied field anchor.
DEFAULT_FIELD_CLEARANCE_MM = 1.27


def _snap_to_grid(value: float, grid: float = KICAD_GRID) -> float:
    """Snap *value* to the nearest grid multiple, rounded to 2 decimals.

    KiCad grid multiples of 1.27 mm are exact 2-decimal values, so the
    rounding only strips float noise (e.g. ``3.8099999999999996`` -> ``3.81``).
    """
    return round(round(value / grid) * grid, 2)


def _transform_point(
    x: float,
    y: float,
    rotation: float,
    mirror: str,
) -> tuple[float, float]:
    """Transform a library-coordinate point to a sheet-coordinate offset.

    Applies mirror, then rotation (both in library Y-up coordinates), then
    negates Y to convert to sheet Y-down coordinates. This mirrors the
    transform in :meth:`LibrarySymbol.get_pin_position` so bboxes and pin
    positions stay consistent.
    """
    if mirror == "x":
        x = -x
    elif mirror == "y":
        y = -y

    if rotation != 0:
        angle_rad = math.radians(rotation)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        x, y = x * cos_a - y * sin_a, x * sin_a + y * cos_a

    return x, -y


def _local_extent_points(
    lib_symbol: LibrarySymbol,
    unit: int | None = None,
) -> list[tuple[float, float]]:
    """Collect library-coordinate points spanning the symbol body extents.

    For multi-unit symbols with a known *unit*, the extents of that unit's
    pins are used (``LibrarySymbol.graphics`` merges all units, so graphics
    extents would span every unit). Otherwise body graphics extents are
    preferred, falling back to pin extents for symbols without body graphics
    (e.g. power symbols).
    """
    if unit is not None and lib_symbol.units > 1:
        unit_pins = [p.position for p in lib_symbol.pins if p.unit == unit]
        if unit_pins:
            return unit_pins

    points: list[tuple[float, float]] = []
    for g in lib_symbol.graphics:
        if isinstance(g, SymbolRectangle):
            points.append(g.start)
            points.append(g.end)
        elif isinstance(g, SymbolPolyline):
            points.extend(g.points)
        elif isinstance(g, SymbolCircle):
            cx, cy = g.center
            points.append((cx - g.radius, cy - g.radius))
            points.append((cx + g.radius, cy + g.radius))
        elif isinstance(g, SymbolArc):
            # Start/mid/end under-approximate the true arc extents slightly;
            # good enough for field placement.
            points.append(g.start)
            points.append(g.mid)
            points.append(g.end)

    if not points:
        points = [p.position for p in lib_symbol.pins]

    return points


def placed_body_bbox(
    lib_symbol: LibrarySymbol,
    position: tuple[float, float],
    rotation: float = 0.0,
    mirror: str = "",
    unit: int | None = None,
) -> BBox:
    """Compute the placed body bounding box of a symbol in sheet coordinates.

    Args:
        lib_symbol: The resolved library symbol definition.
        position: The instance ``(at x y)`` position on the sheet.
        rotation: The instance rotation in degrees.
        mirror: The instance mirror mode ("", "x", "y").
        unit: The placed unit for multi-unit symbols (see
            :func:`_local_extent_points`).

    Returns:
        ``(min_x, min_y, max_x, max_y)`` in sheet coordinates. Degenerate
        (zero-size at *position*) if the symbol has neither graphics nor pins.
    """
    points = _local_extent_points(lib_symbol, unit=unit)
    if not points:
        return (position[0], position[1], position[0], position[1])

    xs: list[float] = []
    ys: list[float] = []
    for lx, ly in points:
        dx, dy = _transform_point(lx, ly, rotation, mirror)
        xs.append(position[0] + dx)
        ys.append(position[1] + dy)

    return (min(xs), min(ys), max(xs), max(ys))


def field_offset_mm(field_at: tuple[float, float], bbox: BBox) -> float:
    """Distance in mm from a field anchor point to a body bbox.

    Returns 0.0 when the point lies inside (or on the edge of) the bbox;
    otherwise the Euclidean distance to the nearest bbox point.
    """
    x, y = field_at
    min_x, min_y, max_x, max_y = bbox
    dx = max(min_x - x, 0.0, x - max_x)
    dy = max(min_y - y, 0.0, y - max_y)
    return math.hypot(dx, dy)


def default_field_positions(
    bbox: BBox,
    clearance: float = DEFAULT_FIELD_CLEARANCE_MM,
) -> dict[str, tuple[float, float, float]]:
    """Deterministic default field positions relative to a placed body bbox.

    ``Reference`` is centered above the bbox top edge and ``Value`` centered
    below the bbox bottom edge (sheet coordinates, Y-down), each *clearance*
    mm away and snapped to the KiCad 1.27 mm grid. Because the grid snap
    moves a point by at most half a grid step (0.635 mm) and the default
    clearance is a full step, the snapped anchors always remain outside the
    bbox interior.

    Returns:
        Mapping of field name to ``(x, y, angle)`` with angle always 0
        (horizontal text).
    """
    min_x, min_y, max_x, max_y = bbox
    center_x = _snap_to_grid((min_x + max_x) / 2.0)
    reference_y = _snap_to_grid(min_y - clearance)
    value_y = _snap_to_grid(max_y + clearance)
    return {
        "Reference": (center_x, reference_y, 0.0),
        "Value": (center_x, value_y, 0.0),
    }
