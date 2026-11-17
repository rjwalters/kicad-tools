"""Package-body outline geometry for footprints.

Resolves "where is the physical component body" for a placed footprint, in
board coordinates, honoring the footprint's position and rotation.  Consumed
by DRC rules that reason about what sits *under* a package (e.g. vias hidden
beneath a QFN body) as opposed to what touches its pads or courtyard.

Resolution order:

1. The fabrication-layer outline on the footprint's own side (``F.Fab`` for
   front-side parts, ``B.Fab`` for back-side parts).  KiCad's library
   convention draws the package body there, usually as a single ``fp_poly``
   (with the pin-1 chamfer), an ``fp_rect``, or a closed loop of ``fp_line``
   segments.  Every closed shape found on the layer is built and the
   **largest** one is taken as the body, so stray fab marks (pin-1 ticks,
   lead outlines) do not win.
2. The courtyard polygon on the same side, via
   :func:`kicad_tools.geometry.courtyard._courtyard_polygon`.  The courtyard
   over-approximates the body (it includes pads and the assembly margin),
   so it is only a fallback.

A footprint with neither resolves to ``None``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.types import Layer

from .courtyard import _courtyard_polygon, _fp_transform

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Footprint

# Where a resolved body outline came from.
BODY_SOURCE_FAB = "fab"
BODY_SOURCE_COURTYARD = "courtyard"

# Segments used to approximate an ``fp_circle`` body outline.
_CIRCLE_RESOLUTION = 16


def footprint_side(footprint: Footprint) -> str:
    """Return ``"B"`` for a back-side footprint, else ``"F"``."""
    return "B" if footprint.layer.startswith("B.") else "F"


def _fab_body_polygon(footprint: Footprint, side: str) -> Any | None:
    """Build the largest closed body shape on the footprint's Fab layer."""
    from shapely.geometry import LineString, Point, Polygon  # type: ignore[import-untyped]
    from shapely.ops import polygonize  # type: ignore[import-untyped]

    target_layer = Layer.F_FAB.value if side == "F" else Layer.B_FAB.value
    transform = _fp_transform(footprint)

    candidates: list[Any] = []
    lines: list[Any] = []
    for graphic in footprint.graphics:
        if graphic.layer != target_layer:
            continue
        kind = graphic.graphic_type
        if kind == "rect":
            sx, sy = graphic.start
            ex, ey = graphic.end
            ring = [(sx, sy), (ex, sy), (ex, ey), (sx, ey)]
            candidates.append(Polygon([transform(p) for p in ring]))
        elif kind == "poly" and len(graphic.points) >= 3:
            candidates.append(Polygon([transform(p) for p in graphic.points]))
        elif kind == "circle" and graphic.center is not None:
            radius = graphic.radius
            if radius is None:
                radius = math.dist(graphic.center, graphic.end)
            if radius > 0:
                candidates.append(
                    Point(transform(graphic.center)).buffer(radius, _CIRCLE_RESOLUTION)
                )
        elif kind == "line":
            if graphic.start != graphic.end:
                lines.append(LineString([transform(graphic.start), transform(graphic.end)]))
        elif kind == "arc":
            # A start-mid-end polyline is enough to close a chamfered or
            # rounded body loop; the body test does not need arc precision.
            pts = [graphic.start] + ([graphic.mid] if graphic.mid else []) + [graphic.end]
            lines.append(LineString([transform(p) for p in pts]))

    if lines:
        candidates.extend(polygonize(lines))

    best = None
    for polygon in candidates:
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.area <= 0:
            continue
        if best is None or polygon.area > best.area:
            best = polygon
    return best


def package_body_polygon(
    footprint: Footprint,
    *,
    fallback_to_courtyard: bool = True,
) -> tuple[Any | None, str | None]:
    """Return ``(polygon, source)`` for a footprint's package body.

    ``polygon`` is a shapely polygon in board coordinates; ``source`` is
    :data:`BODY_SOURCE_FAB` or :data:`BODY_SOURCE_COURTYARD`.  Both are
    ``None`` when no body outline can be resolved.
    """
    require_shapely("package body geometry")
    from shapely.geometry import Polygon

    side = footprint_side(footprint)
    polygon = _fab_body_polygon(footprint, side)
    if polygon is not None:
        return polygon, BODY_SOURCE_FAB
    if fallback_to_courtyard:
        polygon = _courtyard_polygon(footprint, side, Polygon)
        if polygon is not None:
            return polygon, BODY_SOURCE_COURTYARD
    return None, None
