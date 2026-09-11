"""Shared geometry helpers for via-in-pad detection.

Single source of truth for the "is this via drilled inside an SMD pad?"
geometry, reused by:

* the via-in-pad DRC rule (:mod:`kicad_tools.validate.rules.via_in_pad`), and
* the ``fix-vias --relocate-in-pad`` command
  (:mod:`kicad_tools.cli.relocate_in_pad_vias`).

Keeping the pad-bbox / overlap math in one module prevents the two
consumers from drifting (the previous copy lived privately in
``via_in_pad.py``; the DRC rule now imports these functions so both paths
share exactly one implementation).

A drilled via needs the via-in-pad process whenever its hole overlaps SMT
copper, including holes whose centers lie outside the land. Tangency within
DRC tolerance is excluded. Consumers with pad/footprint context use the same
true copper outline as the clearance checker; the two-argument compatibility
helper tests an axis-aligned rectangle.

"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools._shapely import require_shapely

from .base import DRC_TOLERANCE

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Footprint, Pad, Via


def pad_absolute_bbox(
    pad: Pad,
    footprint: Footprint,
) -> tuple[float, float, float, float]:
    """Return the axis-aligned bounding box for ``pad`` in board coords.

    Mirrors the transformation used by the clearance rule: rotates the
    pad center about the footprint origin and, for cardinal rotations,
    swaps width/height (otherwise uses the rotated rectangle's AABB).

    Returns:
        (min_x, min_y, max_x, max_y) tuple in mm.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    # cos/sin magnitudes for the (orientation-independent) AABB below; the
    # signed center rotation goes through the shared KiCad-convention helper.
    total_rotation = getattr(pad, "rotation", footprint.rotation) % 360
    angle_rad = math.radians(total_rotation)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    local_x, local_y = pad.position
    rotated_x, rotated_y = rotate_pad_offset(local_x, local_y, footprint.rotation)
    abs_x = footprint.position[0] + rotated_x
    abs_y = footprint.position[1] + rotated_y

    width, height = pad.size

    # For cardinal rotations, swap dimensions.
    if abs(total_rotation - 90) < 0.001 or abs(total_rotation - 270) < 0.001:
        bbox_w, bbox_h = height, width
    elif abs(total_rotation) < 0.001 or abs(total_rotation - 180) < 0.001:
        bbox_w, bbox_h = width, height
    else:
        # Axis-aligned bounding box of the rotated rectangle.
        abs_cos = abs(cos_a)
        abs_sin = abs(sin_a)
        bbox_w = width * abs_cos + height * abs_sin
        bbox_h = width * abs_sin + height * abs_cos

    half_w = bbox_w / 2
    half_h = bbox_h / 2
    return (abs_x - half_w, abs_y - half_h, abs_x + half_w, abs_y + half_h)


def is_smd_pad(pad: Pad) -> bool:
    """Return True if ``pad`` is a surface-mount pad (no plated hole)."""
    # KiCad pad types: "smd", "thru_hole", "np_thru_hole", "connect"
    return pad.type == "smd"


def via_inside_pad(
    via: Via,
    pad_bbox: tuple[float, float, float, float],
    pad: Pad | None = None,
    footprint: Footprint | None = None,
) -> bool:
    """Return whether a drilled hole overlaps pad copper beyond DRC tolerance.

    The historical name is retained for callers. Full containment is not
    required: partial overlap also permits solder to wick into the drill.
    With pad context, rounded corners and absolute pad angles are honored.
    """
    cx, cy = via.position
    radius = via.drill / 2.0
    if radius <= DRC_TOLERANCE:
        return False
    min_x, min_y, max_x, max_y = pad_bbox
    distance = math.hypot(max(min_x - cx, 0.0, cx - max_x), max(min_y - cy, 0.0, cy - max_y))
    if distance >= radius - DRC_TOLERANCE:
        return False
    if pad is not None and footprint is not None and hasattr(pad, "shape"):
        require_shapely("via-in-pad copper overlap geometry")
        from shapely.geometry import Point  # type: ignore[import-untyped]

        from .clearance import _pad_polygon

        copper = _pad_polygon(pad, footprint)
        return copper is not None and copper.distance(Point(cx, cy)) < radius - DRC_TOLERANCE
    return True
