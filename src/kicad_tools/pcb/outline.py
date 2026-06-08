"""
PCB board-outline mutators for auto-pcb-size escalation (Issue #3352, P_AS3).

This module provides the *board-grow* primitive used by the auto-pcb-size
escalation loop: :func:`grow_board_outline_corner_anchored`.  The grow is
*corner-anchored* (Q2 decision): the existing Edge.Cuts outline's
bottom-left corner is held fixed, and the outline is extended right and/or
up to the new dimensions.  Footprints, traces, vias, zones, and any other
PCB geometry remain at their declared (x, y) positions -- only the
Edge.Cuts contour changes.

This implementation rewrites the primary outline contour using the existing
:meth:`kicad_tools.schema.pcb.PCB.replace_outline` primitive, which
preserves mounting-hole contours (small Edge.Cuts shapes below the
mounting-hole area threshold).  The bottom-left corner of the primary
outline is detected from :meth:`PCB.list_edge_contours`.

Q3 reframe (mounting-hole group):
  The :class:`~kicad_tools.pcb.mounting_holes.MountingHoleGroup` primitive
  encapsulates a placeable mounting-hole pattern whose *anchor* lives in
  board coordinates.  Because the corner-anchored grow preserves the
  origin, the group's anchor does *not* need to move -- callers should
  simply re-check :meth:`MountingHoleGroup.fits_in_envelope` against the
  new dimensions before deciding to grow.  The check is the consumer's
  responsibility (see ``can_escalate_with_holes`` in
  :mod:`kicad_tools.router.auto_pcb_size`); this module enforces no
  hole-group constraint at the geometry level.

Coordinate convention:
  - All dimensions in millimetres (KiCad convention).
  - "Bottom-left" follows KiCad PCB convention: smallest (x, y) corner of
    the outline bbox.  KiCad uses screen-style y-down coordinates inside
    the file, but every consumer of this module treats the smallest-y
    corner as the anchor for the corner-anchored grow.

Issue: https://github.com/rjwalters/kicad-tools/issues/3352
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "OutlineGrowError",
    "find_primary_outline_origin",
    "grow_board_outline_corner_anchored",
]


class OutlineGrowError(RuntimeError):
    """Raised when the board outline cannot be grown to the requested size.

    The most common cause is that the PCB has no detectable Edge.Cuts
    outline contour (a fresh PCB without :meth:`PCB.add_board_outline` ever
    called, or a PCB whose outline was manually deleted).  The auto-pcb-size
    escalation loop converts this into an actionable refusal at the
    route_cmd level rather than crashing the routing attempt.
    """


def find_primary_outline_origin(pcb: PCB) -> tuple[float, float, float, float]:
    """Return the bottom-left corner and current dimensions of the primary outline.

    The "primary outline" is the largest-area non-mounting-hole contour on
    Edge.Cuts.  Multiple outlines are uncommon (a board normally has a
    single closed perimeter plus zero-or-more mounting holes), but we
    handle the case defensively by picking the largest by bbox area.

    The returned origin is the smallest-(x, y) corner of the outline's
    bbox -- the corner-anchored grow uses this as the immovable reference
    point.  Width and height are the current envelope dimensions; callers
    growing the board pass *new* dimensions (>= current) to
    :func:`grow_board_outline_corner_anchored`.

    Args:
        pcb: The loaded PCB instance (from
            :class:`kicad_tools.schema.pcb.PCB`).

    Returns:
        ``(origin_x, origin_y, width_mm, height_mm)`` of the primary
        outline.  Origin is the bottom-left corner (min-x, min-y of the
        bbox).

    Raises:
        OutlineGrowError: If no outline contour can be detected.
    """
    contours = pcb.list_edge_contours()
    primary = None
    for c in contours:
        if c.is_mounting_hole:
            continue
        if primary is None or c.bbox_area > primary.bbox_area:
            primary = c

    if primary is None:
        raise OutlineGrowError(
            "PCB has no detectable Edge.Cuts board outline; "
            "auto-pcb-size escalation cannot grow the envelope without an "
            "existing outline to anchor from.  Add an outline with "
            "`kct pcb edit-outline --set-outline rect ...` (or PCBEditor."
            "add_board_outline()) before invoking --auto-pcb-size."
        )

    min_x, min_y, max_x, max_y = primary.bbox
    return (min_x, min_y, max_x - min_x, max_y - min_y)


def grow_board_outline_corner_anchored(
    pcb: PCB,
    new_width_mm: float,
    new_height_mm: float,
) -> tuple[float, float, float, float]:
    """Grow the PCB outline corner-anchored to the bottom-left.

    The grow holds the *bottom-left* corner (min-x, min-y of the existing
    outline bbox) fixed and extends the outline right and/or up to the
    new dimensions.  This is the Q2 decision from Issue #3352: components,
    traces, vias, and zones at their existing (x, y) positions remain in
    place -- only the Edge.Cuts contour changes.

    The new dimensions must be **at least** the current dimensions in
    *both* axes.  Shrinking is disallowed because the auto-pcb-size
    escalation loop only grows the envelope; a shrink would risk
    truncating existing copper geometry, which this primitive will not
    attempt.

    Mounting-hole contours (small Edge.Cuts circles / shapes below the
    PCB-level mounting-hole area threshold) are preserved by the
    underlying :meth:`kicad_tools.schema.pcb.PCB.replace_outline` call.

    Args:
        pcb: The loaded PCB instance (from
            :class:`kicad_tools.schema.pcb.PCB`).  Mutated in-place; the
            caller is responsible for saving back to disk.
        new_width_mm: New board width in mm; must be >= current width.
        new_height_mm: New board height in mm; must be >= current height.

    Returns:
        ``(origin_x, origin_y, new_width_mm, new_height_mm)`` describing
        the new outline.  Origin matches the detected bottom-left of the
        pre-grow outline.

    Raises:
        OutlineGrowError: If the PCB has no detectable outline, or the
            requested dimensions are smaller than the current outline in
            either axis (with a tolerance of 1e-6 mm to absorb FP error).
        ValueError: If ``new_width_mm`` or ``new_height_mm`` is negative.

    Example:
        >>> # Pseudo-code; real usage requires a loaded PCB instance
        >>> origin = grow_board_outline_corner_anchored(pcb, 150.0, 100.0)
        >>> # PCB now has a 150x100 outline anchored at the original
        >>> # bottom-left; all components unchanged.
    """
    if new_width_mm < 0 or new_height_mm < 0:
        raise ValueError(
            f"new dimensions must be non-negative, got ({new_width_mm}, {new_height_mm})"
        )

    origin_x, origin_y, cur_w, cur_h = find_primary_outline_origin(pcb)

    # Tolerance absorbs floating-point round-trip from kicad_pcb serialisation.
    tol = 1e-6
    if new_width_mm + tol < cur_w or new_height_mm + tol < cur_h:
        raise OutlineGrowError(
            f"grow_board_outline_corner_anchored requires non-shrinking "
            f"dimensions; current outline is {cur_w:g}x{cur_h:g} mm but "
            f"requested {new_width_mm:g}x{new_height_mm:g} mm.  The "
            f"auto-pcb-size escalation loop only grows the envelope -- "
            f"a shrink would risk truncating existing copper."
        )

    # Replace the outline at the same origin with the new dimensions.
    # PCB.replace_outline removes existing non-mounting-hole contours and
    # inserts a fresh gr_rect at (origin_x, origin_y) of the requested size.
    pcb.replace_outline(origin_x, origin_y, new_width_mm, new_height_mm)

    return (origin_x, origin_y, new_width_mm, new_height_mm)
