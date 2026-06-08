"""
Mounting hole group primitive (Issue #3352, P_AS1).

This module provides :class:`MountingHoleGroup`, a placeable primitive that
captures a rigid pattern of mounting holes (e.g. four M3 corner holes for a
chassis fit).  The auto-pcb-size escalation loop uses this primitive to
decide whether a grown envelope still admits the recipe's mounting hole
pattern -- the group moves as a unit, preserving relative geometry, and
either fits in the new envelope at its anchor or escalation refuses.

The primitive is intentionally minimal at the P_AS1 boundary:
  - No KiCad PCB writer integration yet (that lives in
    :mod:`kicad_tools.pcb.editor` and lands in P_AS3).
  - No router-side keepout enforcement yet (P_AS3 wires it in via the
    existing keepout primitives).
  - The :meth:`to_footprint_dict` helper produces a dict the PCB writer
    can consume in P_AS3 without round-trip surprises.

Coordinate convention:
  - Hole positions stored relative to the group's local origin (NOT board
    coordinates).
  - ``anchor`` is the position in board coordinates where the local origin
    sits.  On-board hole position = ``anchor + hole``.
  - All dimensions in millimetres (KiCad convention).

Issue: https://github.com/rjwalters/kicad-tools/issues/3352
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "MountingHoleGroup",
]


@dataclass
class MountingHoleGroup:
    """A rigid placeable group of mounting holes.

    Holes are declared in the group's *local* coordinate frame (relative to
    the group's anchor).  The ``anchor`` field is the position in *board*
    coordinates where the local frame's origin sits.  Moving the group (via
    :meth:`move_to`) updates the anchor and shifts all holes in lockstep.

    The primitive is the data backbone of the Issue #3352 Q3 reframe:
    instead of "refuse auto-pcb-size escalation when any mounting hole is
    present", the system now treats the entire pattern as a single placeable
    object that either fits in the escalated envelope or doesn't.

    Attributes:
        holes: List of ``(x, y)`` hole positions in mm, relative to anchor.
        anchor: Current anchor position ``(x, y)`` in mm, in board coords.
        hole_diameter_mm: Clearance hole diameter (default 3.2 mm = M3).
        keepout_radius_mm: No-copper keepout radius around each hole
            (default 5.0 mm).

    Example:
        >>> # Four corner holes on a 100x100 board with 5 mm edge inset
        >>> group = MountingHoleGroup(
        ...     holes=[(0, 0), (90, 0), (0, 90), (90, 90)],
        ...     anchor=(5.0, 5.0),
        ... )
        >>> group.fits_in_envelope(100, 100)
        True
        >>> # Move to a 200x150 envelope keeping the 5 mm inset
        >>> group.move_to((5.0, 5.0))
        >>> group.fits_in_envelope(200, 150)
        True
    """

    holes: list[tuple[float, float]]
    anchor: tuple[float, float]
    hole_diameter_mm: float = 3.2
    keepout_radius_mm: float = 5.0

    def __post_init__(self) -> None:
        """Validate the group on construction."""
        if not self.holes:
            raise ValueError(
                "MountingHoleGroup.holes must be non-empty; "
                "groups with zero holes are meaningless"
            )
        if self.hole_diameter_mm <= 0:
            raise ValueError(
                f"hole_diameter_mm must be positive, got {self.hole_diameter_mm}"
            )
        if self.keepout_radius_mm <= 0:
            raise ValueError(
                f"keepout_radius_mm must be positive, got {self.keepout_radius_mm}"
            )

    @classmethod
    def from_spec(cls, spec: Any) -> MountingHoleGroup:
        """Construct from a :class:`MountingHoleGroupSpec` (pydantic model).

        Convenience constructor for the spec-loading path.  ``spec`` is duck-
        typed (the schema lives in ``kicad_tools.spec.schema`` and importing
        it here would create a circular import), so we only require the
        attributes documented on ``MountingHoleGroupSpec``.

        Args:
            spec: A :class:`MountingHoleGroupSpec` instance (or any object
                exposing ``holes``, ``anchor``, ``hole_diameter_mm``, and
                ``keepout_radius_mm`` attributes).

        Returns:
            A new ``MountingHoleGroup`` with values copied from the spec.
        """
        return cls(
            holes=list(spec.holes),
            anchor=tuple(spec.anchor),  # type: ignore[arg-type]
            hole_diameter_mm=float(spec.hole_diameter_mm),
            keepout_radius_mm=float(spec.keepout_radius_mm),
        )

    def move_to(self, new_anchor: tuple[float, float]) -> None:
        """Move the group's anchor to a new board-coordinate position.

        Hole positions (in the group's local frame) are unchanged; only the
        anchor moves.  Callers wanting board-coordinate positions of the
        holes after the move should iterate :meth:`board_positions`.

        Args:
            new_anchor: The new anchor position ``(x, y)`` in mm, board coords.
        """
        self.anchor = (float(new_anchor[0]), float(new_anchor[1]))

    def board_positions(self) -> list[tuple[float, float]]:
        """Compute the holes' on-board (x, y) positions.

        Returns:
            List of ``(x, y)`` positions in mm, in board coordinates,
            one per hole, in the same order as :attr:`holes`.
        """
        ax, ay = self.anchor
        return [(ax + hx, ay + hy) for (hx, hy) in self.holes]

    def bbox_local(self) -> tuple[float, float, float, float]:
        """Compute the holes' bounding box in the group's local frame.

        The bounding box includes the keepout radius around each hole, so
        consumers performing envelope-fit checks see the full footprint
        the holes occupy (not just the centerline-to-centerline extent).

        Returns:
            ``(min_x, min_y, max_x, max_y)`` in mm, local frame.
        """
        r = self.keepout_radius_mm
        xs = [x for (x, _) in self.holes]
        ys = [y for (_, y) in self.holes]
        return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)

    def bbox_board(self) -> tuple[float, float, float, float]:
        """Compute the holes' bounding box in board coordinates (with keepout).

        Returns:
            ``(min_x, min_y, max_x, max_y)`` in mm, board frame.
        """
        ax, ay = self.anchor
        lmin_x, lmin_y, lmax_x, lmax_y = self.bbox_local()
        return (lmin_x + ax, lmin_y + ay, lmax_x + ax, lmax_y + ay)

    def fits_in_envelope(self, envelope_width: float, envelope_height: float) -> bool:
        """Check whether the group fits inside a rectangular envelope.

        The envelope is assumed to start at ``(0, 0)`` and extend to
        ``(envelope_width, envelope_height)`` (KiCad board-origin convention).
        The check uses the group's *current* anchor position -- callers who
        want to test a hypothetical anchor should call :meth:`move_to` first
        (or use the lower-level :meth:`bbox_local` to translate manually).

        The keepout radius around each hole is included, so a hole at the
        envelope edge with a 5 mm keepout would fail this check (the keepout
        would extend outside the envelope).

        Args:
            envelope_width: Envelope width in mm.
            envelope_height: Envelope height in mm.

        Returns:
            True iff every hole (with its keepout) lies entirely within
            ``[0, envelope_width] x [0, envelope_height]``.
        """
        min_x, min_y, max_x, max_y = self.bbox_board()
        return (
            min_x >= 0.0
            and min_y >= 0.0
            and max_x <= envelope_width
            and max_y <= envelope_height
        )

    def intersects(self, other: MountingHoleGroup) -> bool:
        """Check whether this group's keepout footprint overlaps another's.

        Used during placement validation (e.g. to verify that a moved
        mounting hole group doesn't collide with a fixed keepout zone or
        another group).  Both groups' bounding boxes (including keepout)
        are compared in board coordinates.

        Args:
            other: The other mounting hole group.

        Returns:
            True iff the two groups' keepout bboxes overlap.
        """
        a_min_x, a_min_y, a_max_x, a_max_y = self.bbox_board()
        b_min_x, b_min_y, b_max_x, b_max_y = other.bbox_board()
        # Standard AABB overlap test
        return not (
            a_max_x < b_min_x
            or b_max_x < a_min_x
            or a_max_y < b_min_y
            or b_max_y < a_min_y
        )

    def to_footprint_dict(self) -> dict[str, Any]:
        """Emit footprint placement data suitable for the PCB writer.

        Produces a dictionary the future PCB writer integration (P_AS3) can
        consume.  Each hole appears as an entry with absolute board-frame
        position, drill diameter, and keepout radius.  The shape matches the
        existing :mod:`kicad_tools.pcb.footprints` patterns so the integration
        can adopt this data without bespoke parsing.

        Returns:
            A dict with keys:
              - ``"anchor"``: current anchor position ``(x, y)``.
              - ``"hole_diameter_mm"``: drill diameter.
              - ``"keepout_radius_mm"``: keepout radius.
              - ``"holes"``: list of dicts each with ``"position"`` (board
                coords), ``"local_position"`` (group-frame coords),
                ``"drill_mm"``, and ``"keepout_radius_mm"`` fields.

        Example:
            >>> group = MountingHoleGroup(
            ...     holes=[(0, 0), (10, 0)],
            ...     anchor=(5.0, 5.0),
            ... )
            >>> data = group.to_footprint_dict()
            >>> data["holes"][0]["position"]
            (5.0, 5.0)
            >>> data["holes"][1]["position"]
            (15.0, 5.0)
        """
        ax, ay = self.anchor
        return {
            "anchor": self.anchor,
            "hole_diameter_mm": self.hole_diameter_mm,
            "keepout_radius_mm": self.keepout_radius_mm,
            "holes": [
                {
                    "position": (ax + hx, ay + hy),
                    "local_position": (hx, hy),
                    "drill_mm": self.hole_diameter_mm,
                    "keepout_radius_mm": self.keepout_radius_mm,
                }
                for (hx, hy) in self.holes
            ],
        }
