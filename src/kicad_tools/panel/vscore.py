"""Copper clearance to V-score lines (Issue #6164).

A V-scored panel butts its copies edge to edge.  The score line sits on
the shared edge, and the scoring blade removes material on both faces in
a V around it.  Fabs therefore ask for more copper clearance from a score
line than from a routed edge, typically 0.3--0.5 mm.  None of the
manufacturer profiles in :mod:`kicad_tools.manufacturers` publishes a
V-score figure yet, so :attr:`VCutConfig.clearance
<kicad_tools.panel.config.VCutConfig.clearance>` defaults to 0.4 mm.

The check runs once on the *source* board: for each of its four edges it
finds the closest copper (track, arc, via, pad, zone fill).  The panel then
maps each scored edge of each copy back to a source edge through the copy's
rotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# Board edges, named in panel (screen) orientation: Y grows downward.
SIDES = ("left", "top", "right", "bottom")

_SIDE_VECTORS: dict[str, tuple[int, int]] = {
    "left": (-1, 0),
    "right": (1, 0),
    "top": (0, -1),
    "bottom": (0, 1),
}
_VECTOR_SIDES = {v: k for k, v in _SIDE_VECTORS.items()}


@dataclass(frozen=True)
class EdgeCopper:
    """The copper item closest to one source-board edge.

    Attributes:
        gap: Copper-to-edge distance in mm (0 when copper touches or
            crosses the edge).
        item: Human-readable description of the closest item.
    """

    gap: float
    item: str


@dataclass(frozen=True)
class VScoreClearanceFinding:
    """Board copper closer to a score line than the fab allows.

    Attributes:
        source_side: The source board's edge that is scored.
        gap: Copper-to-score distance in mm.
        required: Required clearance in mm.
        item: The closest copper item.
        copies: Panel copy indices whose scored edge this is.
    """

    source_side: str
    gap: float
    required: float
    item: str
    copies: tuple[int, ...]

    def message(self) -> str:
        boards = ", ".join(f"B{i}" for i in self.copies)
        text = (
            f"V-score clearance: {self.item} is {self.gap:.3f} mm from the source "
            f"board's {self.source_side} edge, which is V-scored on {boards}; "
            f"the fab needs >= {self.required:.2f} mm from copper to a score line"
        )
        if self.item.startswith("zone fill"):
            text += " (the panel's V-score keepout pulls zone fill back on refill)"
        else:
            text += " (move the copper on the source board)"
        return text


def rotate_side(side: str, rotation: float) -> str:
    """Return the panel side a source *side* faces after *rotation*.

    *rotation* is a quarter turn in KiCad's CCW-positive convention, the
    same map :func:`kicad_tools.panel.panel._rigid_mapper` applies to
    points (``x' = x cos a + y sin a``, ``y' = -x sin a + y cos a``).
    """
    quarter = {0.0: (1, 0), 90.0: (0, 1), 180.0: (-1, 0), 270.0: (0, -1)}
    c, s = quarter[float(rotation) % 360.0]
    dx, dy = _SIDE_VECTORS[side]
    return _VECTOR_SIDES[(dx * c + dy * s, -dx * s + dy * c)]


def source_side_for(panel_side: str, rotation: float) -> str:
    """Inverse of :func:`rotate_side`: which source edge lands on *panel_side*."""
    for side in SIDES:
        if rotate_side(side, rotation) == panel_side:
            return side
    raise ValueError(f"unknown side {panel_side!r}")  # pragma: no cover


def edge_copper_clearances(
    pcb: PCB,
    bounds: tuple[float, float, float, float],
) -> dict[str, EdgeCopper]:
    """Closest copper to each edge of the board's bounding rectangle.

    Args:
        pcb: The source board, loaded with :meth:`PCB.load` (board-relative
            coordinates).
        bounds: ``(min_x, min_y, max_x, max_y)`` of the board outline in the
            same board-relative frame.

    Returns:
        ``{side: EdgeCopper}`` for every side that has copper on the board.
    """
    from kicad_tools.router.clearance_kernel import KSegment
    from kicad_tools.validate.clearance_shapes import (
        min_gap_to_outline,
        outline_shapes,
        pad_shape,
        segment_shape,
        via_shape,
        zone_shape,
    )

    x0, y0, x1, y1 = bounds
    edges = {
        "left": outline_shapes([((x0, y0), (x0, y1))]),
        "right": outline_shapes([((x1, y0), (x1, y1))]),
        "top": outline_shapes([((x0, y0), (x1, y0))]),
        "bottom": outline_shapes([((x0, y1), (x1, y1))]),
    }

    shapes: list[tuple[object, str]] = []
    for seg in pcb.segments:
        shapes.append((segment_shape(seg), f"track on {seg.layer}"))
    for arc in getattr(pcb, "arcs", []):
        # Two chords through the arc's midpoint: close enough for a
        # clearance warning (the bulge beyond the chords is tiny for the
        # short arcs routers emit).
        for a, b in ((arc.start, arc.mid), (arc.mid, arc.end)):
            chord = KSegment(x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=arc.width)
            shapes.append((chord, f"arc track on {arc.layer}"))
    for via in pcb.vias:
        shapes.append((via_shape(via), "via"))
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.type == "np_thru_hole":
                continue
            if not any(layer.endswith(".Cu") for layer in pad.layers):
                continue
            shapes.append((pad_shape(pad, fp), f"pad {fp.reference}.{pad.number}"))
    for zone in pcb.zones:
        if getattr(zone, "keepout", None) is not None:
            continue
        for ring in zone.filled_polygons:
            if len(ring) >= 3:
                label = zone.net_name or f"net {zone.net_number}"
                shapes.append((zone_shape([ring]), f"zone fill ({label}) on {zone.layer}"))

    result: dict[str, EdgeCopper] = {}
    for side, edge in edges.items():
        best: EdgeCopper | None = None
        for shape, label in shapes:
            gap = max(0.0, float(min_gap_to_outline(shape, edge)))  # type: ignore[arg-type]
            if best is None or gap < best.gap:
                best = EdgeCopper(gap=gap, item=label)
        if best is not None:
            result[side] = best
    return result
