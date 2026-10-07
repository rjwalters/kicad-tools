"""Library-to-sheet transform for a placed schematic symbol.

The single source of truth for where a library-space point (a pin's
connection point, a body graphic vertex, a field anchor) or direction
vector lands once its symbol is placed with ``(at x y rot)`` and an optional
``(mirror x|y)`` (issue #6005).

KiCad builds a placed symbol's transform as rotation *then* mirror, in
library (Y-up) coordinates:

1. rotate the library point counter-clockwise by the symbol's angle;
2. ``(mirror x)`` flips across the X axis, negating the rotated **Y**;
   ``(mirror y)`` flips across the Y axis, negating the rotated **X**;
3. negate Y to convert library Y-up to sheet Y-down.

At 0 and 180 degrees the order of steps 1 and 2 does not matter (an axis
flip commutes with a half turn), but at 90 and 270 degrees mirroring first
lands on the position the *other* mirror axis would give.  Verified against
``kicad-cli sch export netlist`` (KiCad 10.0.1) for all 12 rotation x mirror
combinations on an asymmetric symbol.
"""

from __future__ import annotations

import math

__all__ = ["MIRROR_AXES", "normalize_mirror", "symbol_to_sheet_offset"]

#: Mirror tokens KiCad writes in ``(mirror ...)``; ``""`` means unmirrored.
MIRROR_AXES = ("", "x", "y")


def normalize_mirror(value: object) -> str:
    """Return ``"x"``, ``"y"`` or ``""`` for a raw ``(mirror ...)`` token."""
    token = str(value or "").strip().lower()
    return token if token in ("x", "y") else ""


def symbol_to_sheet_offset(
    x: float,
    y: float,
    rotation: float = 0.0,
    mirror: str = "",
) -> tuple[float, float]:
    """Map a library (Y-up) point or vector to a sheet (Y-down) offset.

    Args:
        x: Library X (relative to the symbol origin).
        y: Library Y (relative to the symbol origin, positive up).
        rotation: The placed symbol's rotation in degrees (CCW-positive).
        mirror: The placed symbol's mirror axis: ``"x"``, ``"y"`` or ``""``.

    Returns:
        ``(dx, dy)`` in sheet coordinates, to be added to the symbol's
        ``(at x y)``.  Unrounded; callers round or snap as they need.
    """
    if rotation:
        rad = math.radians(rotation)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        x, y = x * cos_r - y * sin_r, x * sin_r + y * cos_r
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return x, -y
