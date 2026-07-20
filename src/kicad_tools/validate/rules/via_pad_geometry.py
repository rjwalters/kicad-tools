"""Shared geometry helpers for via-in-pad detection.

Single source of truth for the "is this via drilled inside an SMD pad?"
geometry, reused by:

* the via-in-pad DRC rule (:mod:`kicad_tools.validate.rules.via_in_pad`), and
* the ``fix-vias --relocate-in-pad`` command
  (:mod:`kicad_tools.cli.relocate_in_pad_vias`).

Keeping the pad-bbox / containment math in one module prevents the two
consumers from drifting (the previous copy lived privately in
``via_in_pad.py``; the DRC rule now imports these functions so both paths
share exactly one implementation).

Geometry of "via inside pad":

* SMD pads are modelled as axis-aligned rectangles (post footprint
  rotation transformation).  For rotated footprints the axis-aligned
  bounding box is used; pads with non-cardinal rotations are
  conservatively reported when the BB contains the via even if the actual
  pad polygon does not.
* A via is "in the pad" when its drill circle is fully contained inside
  the pad rectangle, i.e. every point on the drill circle lies on or
  inside the pad edge.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

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
    angle_rad = math.radians(footprint.rotation)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    local_x, local_y = pad.position
    rotated_x, rotated_y = rotate_pad_offset(local_x, local_y, footprint.rotation)
    abs_x = footprint.position[0] + rotated_x
    abs_y = footprint.position[1] + rotated_y

    width, height = pad.size
    total_rotation = footprint.rotation % 360

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


def via_inside_pad(via: Via, pad_bbox: tuple[float, float, float, float]) -> bool:
    """Return True if the via's drill circle is fully inside ``pad_bbox``.

    Args:
        via: The via to test.  ``via.position`` is the center, ``via.drill``
            is the drill diameter.
        pad_bbox: Axis-aligned (min_x, min_y, max_x, max_y) of the pad.

    Returns:
        ``True`` when every point on the drill circle is on or inside
        the pad bounding box.  A small DRC tolerance is applied so that
        edge-touching vias (within manufacturing rounding) are not
        flagged.
    """
    cx, cy = via.position
    radius = via.drill / 2.0
    min_x, min_y, max_x, max_y = pad_bbox

    # Every point on the drill circle lies in [cx - r, cx + r] x [cy - r, cy + r].
    # The drill is fully inside the pad iff the circle's bounding box is.
    # We require strict containment minus DRC_TOLERANCE so a via whose
    # drill edge merely touches the pad edge is allowed (it would be a
    # neckdown rather than an in-pad via).
    return (
        cx - radius >= min_x - DRC_TOLERANCE
        and cx + radius <= max_x + DRC_TOLERANCE
        and cy - radius >= min_y - DRC_TOLERANCE
        and cy + radius <= max_y + DRC_TOLERANCE
    )
