"""Shared, checker-agnostic courtyard-polygon geometry (Issue #4182).

This module is the single source of truth for "what is this footprint's
courtyard" — the real ``F.CrtYd`` / ``B.CrtYd`` polygon geometry, honoring
the footprint's position and rotation.  It is consumed by:

* :mod:`kicad_tools.validate.rules.courtyard` — the ``kct check``
  courtyard-overlap DRC rule (extracted from there verbatim in #4182; behavior
  unchanged).
* :mod:`kicad_tools.placement.analyzer` — the ``kct placement check``
  ``PlacementAnalyzer``, which previously approximated each courtyard as a
  pads-bounding-box expanded by a fixed margin and therefore found only a
  strict subset of KiCad's courtyard overlaps.

Having both checkers resolve courtyards through the same helpers is exactly
what the reconciliation issue (#4182) asks for: ``kct placement check`` and
``kct check`` should agree on courtyard overlaps.

Courtyard-outline extraction supports three representations:

* ``fp_rect`` — a single axis-aligned rectangle (fast path).
* ``fp_poly`` — a closed polygon whose vertices are recorded in
  :attr:`FootprintGraphic.points`.
* ``fp_line`` — a closed loop of line segments chained by endpoint matching.

A courtyard that cannot be resolved from these (no CrtYd geometry, a
non-closing ``fp_line`` chain, or fewer than three vertices) resolves to
``None`` so callers can fall back or surface the gap explicitly rather than
silently skipping the footprint.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from kicad_tools.core.types import Layer

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Footprint, FootprintGraphic

# Endpoint-matching tolerance (mm) when chaining ``fp_line`` segments into a
# closed courtyard ring.  Courtyard vertices are authored on a coarse grid;
# 1e-3 mm is comfortably below any real feature yet absorbs float noise.
_CHAIN_EPSILON_MM = 1e-3


def _fp_transform(footprint: Footprint):
    """Return a ``(x, y) -> (X, Y)`` local->board transform for a footprint.

    Mirrors ``validate.rules.silkscreen._fp_transform``: KiCad negates the
    footprint orientation angle relative to CCW math (verified in #3739), so
    the rotation applied here is ``radians(-rotation)``.
    """
    fp_x, fp_y = footprint.position
    fp_rotation = math.radians(-footprint.rotation)
    cos_rot = math.cos(fp_rotation)
    sin_rot = math.sin(fp_rotation)

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        lx, ly = point
        return (
            fp_x + (lx * cos_rot - ly * sin_rot),
            fp_y + (lx * sin_rot + ly * cos_rot),
        )

    return transform


def _courtyard_side(layer: str) -> str | None:
    """Return ``"F"`` / ``"B"`` for a courtyard layer, else ``None``."""
    if layer == Layer.F_CRTYD.value:
        return "F"
    if layer == Layer.B_CRTYD.value:
        return "B"
    return None


def _rect_ring(graphic: FootprintGraphic) -> list[tuple[float, float]]:
    """Return the 5-point closed ring for an ``fp_rect`` graphic (local space)."""
    sx, sy = graphic.start
    ex, ey = graphic.end
    return [(sx, sy), (ex, sy), (ex, ey), (sx, ey), (sx, sy)]


def _chain_lines(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]] | None:
    """Chain line segments into a single closed ring by endpoint matching.

    Returns the ordered ring of vertices (first == last) when the segments
    form exactly one closed loop, else ``None`` (open chain, branching, or
    disconnected components -- not a resolvable simple courtyard).
    """
    if not segments:
        return None

    def close(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= _CHAIN_EPSILON_MM

    remaining = list(segments)
    start, current = remaining.pop(0)
    ring: list[tuple[float, float]] = [start, current]

    while remaining:
        for idx, (a, b) in enumerate(remaining):
            if close(a, current):
                current = b
                ring.append(current)
                remaining.pop(idx)
                break
            if close(b, current):
                current = a
                ring.append(current)
                remaining.pop(idx)
                break
        else:
            # No segment continues the chain -> not a single closed loop.
            return None

    # A closed loop returns to its start.
    if not close(ring[0], ring[-1]):
        return None
    return ring


def _courtyard_polygon(footprint: Footprint, side: str, Polygon: Any):
    """Build a shapely polygon for a footprint's courtyard on ``side``.

    ``side`` is ``"F"`` or ``"B"``.  Returns ``None`` when no courtyard
    geometry on that side can be resolved into a valid closed polygon.
    """
    target_layer = Layer.F_CRTYD.value if side == "F" else Layer.B_CRTYD.value
    transform = _fp_transform(footprint)

    rects: list[FootprintGraphic] = []
    polys: list[FootprintGraphic] = []
    line_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for graphic in footprint.graphics:
        if graphic.layer != target_layer:
            continue
        if graphic.graphic_type == "rect":
            rects.append(graphic)
        elif graphic.graphic_type == "poly":
            polys.append(graphic)
        elif graphic.graphic_type == "line":
            line_segments.append((graphic.start, graphic.end))
        # circle / arc courtyards are not modeled (rare for courtyards).

    ring: list[tuple[float, float]] | None = None
    if rects:
        # A single rect fully describes the courtyard; if multiple exist we
        # take the first (the common single-rect case).
        ring = _rect_ring(rects[0])
    elif polys and len(polys[0].points) >= 3:
        ring = list(polys[0].points)
    elif line_segments:
        ring = _chain_lines(line_segments)

    if ring is None or len(ring) < 3:
        return None

    board_ring = [transform(p) for p in ring]
    polygon = Polygon(board_ring)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.area <= 0:
        return None
    return polygon


def _has_courtyard_geometry(footprint: Footprint) -> bool:
    """True if the footprint has any F/B CrtYd graphic at all."""
    return any(_courtyard_side(graphic.layer) is not None for graphic in footprint.graphics)


def _side_has_geometry(footprint: Footprint, side: str) -> bool:
    """True if the footprint has any CrtYd graphic on the given ``side``."""
    target_layer = Layer.F_CRTYD.value if side == "F" else Layer.B_CRTYD.value
    return any(graphic.layer == target_layer for graphic in footprint.graphics)


# ---------------------------------------------------------------------------
# Courtyard coverage (Issue #7204)
# ---------------------------------------------------------------------------

#: ``(attr ...)`` flags that opt a footprint out of needing a courtyard.
#: ``allow_missing_courtyard`` is the opt-out KiCad's own ``missing_courtyard``
#: rule honours; ``board_only`` marks board furniture with no assembled part.
COURTYARD_EXEMPT_ATTR_FLAGS: frozenset[str] = frozenset({"allow_missing_courtyard", "board_only"})

#: KiCad libraries whose footprints carry no component body to place.
COURTYARD_EXEMPT_LIBRARIES: tuple[str, ...] = ("MountingHole:", "Fiducial:", "NetTie:")


def courtyard_exempt(footprint: Footprint) -> bool:
    """True when a footprint need not carry a courtyard (Issue #7204).

    Deliberately narrow: an explicit ``allow_missing_courtyard`` /
    ``board_only`` attr flag, a footprint with no pads (logos, graphics), or a
    lib id in the ``MountingHole:``, ``Fiducial:`` or ``NetTie:`` libraries.
    DNP parts are **not** exempt: their land pattern still occupies the board.
    """
    flags: frozenset[str] = getattr(footprint, "attr_flags", frozenset())
    if flags & COURTYARD_EXEMPT_ATTR_FLAGS:
        return True
    if not getattr(footprint, "pads", None):
        return True
    return (footprint.name or "").startswith(COURTYARD_EXEMPT_LIBRARIES)


def unevaluated_courtyards(footprints: Any, Polygon: Any) -> tuple[list[str], list[str], int]:
    """Footprints whose courtyard overlap cannot be evaluated (Issue #7204).

    Returns ``(missing, unresolved, total)`` over the non-exempt footprints
    (see :func:`courtyard_exempt`): the references with no ``F.CrtYd`` /
    ``B.CrtYd`` geometry at all, the references whose courtyard geometry
    exists but resolves to no polygon on any side, and how many non-exempt
    footprints there are.  ``missing + unresolved`` is the "N of M" that
    ``kct check`` coverage reports.
    """
    missing: list[str] = []
    unresolved: list[str] = []
    total = 0
    for fp in footprints:
        if courtyard_exempt(fp):
            continue
        total += 1
        if not _has_courtyard_geometry(fp):
            missing.append(fp.reference)
            continue
        sides = [side for side in ("F", "B") if _side_has_geometry(fp, side)]
        if not any(_courtyard_polygon(fp, side, Polygon) is not None for side in sides):
            unresolved.append(fp.reference)
    return missing, unresolved, total
