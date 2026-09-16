"""Obstacle-aware variant selection for 45-degree quantization.

Issue #5333.  :func:`kicad_tools.router.quantize.quantize_pcb_file` replaces an
off-angle ``(segment ...)`` with a two-leg dogleg that shares the original
endpoints.  Connectivity is preserved exactly, but the path between the
endpoints moves by up to ``min(|dx|, |dy|)`` perpendicular to the original
chord -- so a chord that cleared every neighbour can become a dogleg that does
not.  ``quantize_pcb_file`` already accepts ``axis_first_uuids`` (mirror the
bulge to the other side of the chord) and ``skip_uuids`` (leave the segment
off-angle), but it has never been able to decide *which* segments need them:
its docstring places that burden on the caller, and every in-tree caller passed
neither set.

Board07's ``2c9bcb95`` full-recipe artifact is the measured consequence.  A GND
pour-repair escape chord ran from ``(155.38, 95.92)`` to ``(155.77, 96.85)`` on
``F.Cu``, clearing the foreign ``TMDS_D2_P`` via at ``(156.022, 95.88)``
(0.6 mm diameter) by 0.2075 mm.  Quantization's default diagonal-first dogleg
put the intermediate vertex at ``(155.77, 96.31)`` -- 0.0822 mm from that via,
under the 0.1016 mm jlcpcb floor, and KiCad's native refill reported it as a
blocking ``clearance`` finding.  The axis-first variant of the *same* chord
(vertex ``(155.38, 96.46)``) clears the via by 0.2432 mm.  The information
needed to pick it was present in the file the whole time; nothing consumed it.

This module supplies that decision.  :func:`plan_quantization` evaluates both
dogleg variants of every off-angle segment against the real foreign copper on
its layer (segments, vias, pads) and the board outline, and returns the
``axis_first_uuids`` / ``skip_uuids`` sets ``quantize_pcb_file`` already
understands.

Two deliberate properties:

* **Never worse than the chord.**  A variant is acceptable when it meets the
  clearance floor *or* when it is no closer than the original chord already
  was.  A board that ships a pre-existing violation next to an off-angle
  segment therefore still gets quantized (the quantizer is not a clearance
  repair pass and must not silently refuse to run because of an unrelated
  defect), but quantization can never be the pass that introduces one.
* **Skipping is a last resort.**  Both variants are tried before a segment is
  left off-angle, so the 45-census ratchet only loses a segment when the
  surrounding copper genuinely occupies both sides of the chord.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .quantize import ANGLE_TOL_DEG, SEGMENT_BLOCK_RE, is_45_aligned

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.schema.pcb import PCB

#: Floating-point slack when comparing a measured gap against a floor.  Board
#: coordinates are serialized on a 0.1 um grid, so 1 nm of slack absorbs
#: shapely's distance round-off without admitting a real violation.
_EPS_MM = 1e-9


@dataclass(frozen=True)
class QuantizationPlan:
    """Per-segment dogleg-variant decisions for :func:`quantize_pcb_file`.

    ``axis_first_uuids`` and ``skip_uuids`` are keyed exactly the way
    ``quantize_pcb_file`` keys segments: the segment's ``uuid`` when it has
    one, otherwise ``"x1,y1-x2,y2"`` built from the raw serialized
    coordinate tokens.
    """

    axis_first_uuids: frozenset[str] = frozenset()
    skip_uuids: frozenset[str] = frozenset()
    #: One human-readable line per segment that needed a non-default
    #: decision.  Callers print these so a mirrored/skipped dogleg is
    #: visible in the recipe log rather than silently applied.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Obstacle:
    """Foreign copper considered for one layer."""

    geom: Any  # shapely BaseGeometry
    net: int


def _shapely():
    from shapely.geometry import (  # type: ignore[import-untyped]  # noqa: PLC0415
        LineString,
        MultiLineString,
        Point,
    )

    return LineString, MultiLineString, Point


def _via_layers(via_layers: list[str], copper_layers: list[str]) -> list[str]:
    """Copper layers a via barrel physically occupies.

    A via declares only its endpoint pair; the barrel is copper on every
    layer between them (issue #3487's model).  An unknown endpoint name
    degrades to "every copper layer", which is the conservative choice for
    a clearance decision.
    """
    try:
        indices = [copper_layers.index(name) for name in via_layers]
    except ValueError:
        return list(copper_layers)
    if not indices:
        return list(copper_layers)
    return copper_layers[min(indices) : max(indices) + 1]


def _collect_obstacles(
    pcb: PCB,
    text_segments: list[tuple[str, float, float, float, float, float, str, int]],
    copper_layers: list[str],
) -> dict[str, list[_Obstacle]]:
    """Build per-layer foreign-copper geometry in SHEET-ABSOLUTE coordinates.

    Segments come from the caller's text parse (already sheet-absolute, and
    keyed identically to the quantizer's own view of the file).  Pads and
    vias come from the parsed :class:`PCB`, whose coordinates are
    board-relative, so they are translated by ``pcb.board_origin``.

    Zone fills are intentionally excluded: every in-tree caller re-fills
    after quantization, and a stale fill polygon would veto variants that
    the refill will carve clearance around anyway.
    """
    from shapely.affinity import translate  # type: ignore[import-untyped]  # noqa: PLC0415

    from kicad_tools.validate.rules.clearance import _pad_on_layer, _pad_polygon  # noqa: PLC0415

    LineString, _MultiLineString, Point = _shapely()
    ox, oy = pcb.board_origin
    by_layer: dict[str, list[_Obstacle]] = {name: [] for name in copper_layers}

    for _key, x1, y1, x2, y2, width, layer, net in text_segments:
        bucket = by_layer.get(layer)
        if bucket is None:
            continue
        if x1 == x2 and y1 == y2:
            continue
        bucket.append(_Obstacle(LineString([(x1, y1), (x2, y2)]).buffer(width / 2.0), net))

    for via in pcb.vias:
        geom = Point(via.position[0] + ox, via.position[1] + oy).buffer(via.size / 2.0)
        for layer in _via_layers(list(via.layers), copper_layers):
            bucket = by_layer.get(layer)
            if bucket is not None:
                bucket.append(_Obstacle(geom, via.net_number))

    for footprint in pcb.footprints:
        for pad in footprint.pads:
            polygon = _pad_polygon(pad, footprint)
            if polygon is None or polygon.is_empty:
                continue
            geom = translate(polygon, xoff=ox, yoff=oy)
            for layer in copper_layers:
                if _pad_on_layer(pad, layer):
                    by_layer[layer].append(_Obstacle(geom, pad.net_number))

    return by_layer


def _min_foreign_gap(path_geom: Any, obstacles: list[_Obstacle], net: int, limit: float) -> float:
    """Smallest distance from ``path_geom`` to foreign copper, clamped at ``limit``.

    ``limit`` is an early-exit ceiling: any obstacle farther away than the
    clearance floor cannot change a decision, so the scan stops caring once
    the running minimum is above it.  Same-net copper is not a clearance
    obstacle (KiCad's model) and is skipped.
    """
    best = math.inf
    bounds = path_geom.bounds
    window = (
        bounds[0] - limit,
        bounds[1] - limit,
        bounds[2] + limit,
        bounds[3] + limit,
    )
    for obstacle in obstacles:
        if obstacle.net == net:
            continue
        obounds = obstacle.geom.bounds
        if (
            obounds[2] < window[0]
            or obounds[0] > window[2]
            or obounds[3] < window[1]
            or obounds[1] > window[3]
        ):
            continue
        gap = path_geom.distance(obstacle.geom)
        if gap < best:
            best = gap
    return best


def _path_geometry(points: list[tuple[float, float]], width: float) -> Any:
    LineString, _MultiLineString, _Point = _shapely()
    return LineString(points).buffer(width / 2.0)


def plan_quantization(
    pcb_path: Path | str,
    *,
    clearance_mm: float,
    edge_clearance_mm: float,
    tol_deg: float = ANGLE_TOL_DEG,
) -> QuantizationPlan:
    """Choose a dogleg variant per off-angle segment in *pcb_path*.

    Args:
        pcb_path: Board to inspect (never modified).
        clearance_mm: Copper-to-copper floor for different-net pairs.
        edge_clearance_mm: Copper-to-board-outline floor.
        tol_deg: Angle tolerance for the off-angle test -- must match the
            value handed to :func:`quantize_pcb_file` so the two passes
            agree on which segments are candidates.

    Returns:
        A :class:`QuantizationPlan` whose sets can be passed straight
        through to :func:`quantize_pcb_file`.
    """
    from kicad_tools.schema.pcb import PCB  # noqa: PLC0415

    from .optimizer.pcb import parse_net_names  # noqa: PLC0415

    path = Path(pcb_path)
    text = path.read_text()
    name_to_id = {name: nid for nid, name in parse_net_names(text).items()}

    segments: list[tuple[str, float, float, float, float, float, str, int]] = []
    candidates: list[tuple[str, float, float, float, float, float, str, int]] = []
    for match in SEGMENT_BLOCK_RE.finditer(text):
        x1, y1, x2, y2 = (float(match.group(i)) for i in (2, 3, 4, 5))
        width = float(match.group(6))
        layer = match.group(7)
        net_num = match.group(9)
        net = int(net_num) if net_num is not None else name_to_id.get(match.group(10), 0)
        key = match.group(8) or match.group(11)
        if key is None:
            key = f"{match.group(2)},{match.group(3)}-{match.group(4)},{match.group(5)}"
        record = (key, x1, y1, x2, y2, width, layer, net)
        segments.append(record)
        dx, dy = x2 - x1, y2 - y1
        if (dx == 0 and dy == 0) or is_45_aligned(dx, dy, tol_deg):
            continue
        candidates.append(record)

    if not candidates:
        return QuantizationPlan()

    pcb = PCB.load(path)
    copper_layers = [layer.name for layer in pcb.copper_layers]
    obstacles = _collect_obstacles(pcb, segments, copper_layers)

    _LineString, MultiLineString, _Point = _shapely()
    ox, oy = pcb.board_origin
    outline = [
        ((a[0] + ox, a[1] + oy), (b[0] + ox, b[1] + oy))
        for a, b in pcb.get_board_outline_segments()
        if a != b
    ]
    edge_geom = MultiLineString(outline) if outline else None

    axis_first: set[str] = set()
    skipped: set[str] = set()
    notes: list[str] = []

    for key, x1, y1, x2, y2, width, layer, net in candidates:
        # Same-net copper is not a clearance obstacle, and the chord being
        # quantized is itself in the text scan -- the net filter drops both.
        layer_obstacles = [obstacle for obstacle in obstacles.get(layer, ()) if obstacle.net != net]
        copper_limit = clearance_mm + width

        chord = _path_geometry([(x1, y1), (x2, y2)], width)
        chord_copper = _min_foreign_gap(chord, layer_obstacles, net, copper_limit)
        chord_edge = (
            chord.distance(edge_geom) if edge_geom is not None and not chord.is_empty else math.inf
        )
        copper_floor = min(clearance_mm, chord_copper)
        edge_floor = min(edge_clearance_mm, chord_edge)

        decision: bool | None = None
        gaps: dict[bool, tuple[float, float]] = {}
        for variant in (False, True):
            points = _dogleg_points_exact(x1, y1, x2, y2, axis_first=variant)
            geom = _path_geometry(points, width)
            copper_gap = _min_foreign_gap(geom, layer_obstacles, net, copper_limit)
            edge_gap = (
                geom.distance(edge_geom)
                if edge_geom is not None and not geom.is_empty
                else math.inf
            )
            gaps[variant] = (copper_gap, edge_gap)
            if copper_gap >= copper_floor - _EPS_MM and edge_gap >= edge_floor - _EPS_MM:
                decision = variant
                break

        def _fmt(variant: bool) -> str:
            copper_gap, edge_gap = gaps[variant]
            return f"copper {copper_gap:.4f} mm / edge {edge_gap:.4f} mm"

        if decision is None:
            skipped.add(key)
            notes.append(
                f"{key}: left off-angle on {layer} -- neither dogleg clears "
                f"(diagonal-first {_fmt(False)}; axis-first {_fmt(True)}; "
                f"floors {copper_floor:.4f}/{edge_floor:.4f} mm)"
            )
        elif decision is True:
            axis_first.add(key)
            notes.append(
                f"{key}: axis-first dogleg on {layer} -- diagonal-first left "
                f"{_fmt(False)} against floors "
                f"{copper_floor:.4f}/{edge_floor:.4f} mm; axis-first leaves {_fmt(True)}"
            )

    return QuantizationPlan(
        axis_first_uuids=frozenset(axis_first),
        skip_uuids=frozenset(skipped),
        notes=tuple(notes),
    )


def _dogleg_points_exact(
    x1: float, y1: float, x2: float, y2: float, *, axis_first: bool
) -> list[tuple[float, float]]:
    """Dogleg vertices matching :func:`quantize_pcb_file`'s Decimal arithmetic.

    :func:`kicad_tools.router.quantize.dogleg_points` computes the same
    vertex in float64; the file pass uses :class:`~decimal.Decimal` so the
    serialized legs are exactly 45-aligned.  The two agree to ~1e-14 mm,
    far below any clearance decision, so the float form is used here and
    this wrapper exists only to pin that equivalence in one place.
    """
    from .quantize import dogleg_points  # noqa: PLC0415

    return dogleg_points(x1, y1, x2, y2, axis_first=axis_first)
