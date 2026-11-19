"""Layout-quality metrics over a parsed PCB (issue #9594).

One pass over a board's copper producing the numbers a layout reviewer
asks for first: the track-vs-ground via split, per-net corner counts,
total track length, outline area, and differential-pair skew. Consumed by
``kct board-metrics`` (``board.json``'s ``layout_quality`` block) and by
the fleet-router benchmark's per-case fingerprint.

Method credit: the PCBJam blind-PCB study
(https://www.pcbjam.com/blog/fable-blind-pcb-layout/) popularised this
metric set. Its scoring scripts (``scoring/lab-metrics.cjs`` and
siblings, MIT licence) informed the *definitions* only; this
implementation was written independently from the method description in
issue #9594, so no code is reused and no attribution obligation applies.

Corner classes
--------------

Corners are counted per net over same-net, same-layer joins of straight
segments, reusing :class:`~kicad_tools.explain.checks.acid_trap.
AcidTrapCheck`'s junction/angle logic -- the same logic the
``sharp_trace_corner`` DRC rule runs (no second copy). The angle is the
interior angle between the two legs at the shared endpoint:

* ``turn_back`` -- angle below :data:`TURN_BACK_MAX_ANGLE_DEG` (10 deg).
  Overlapping copper or a dead-end stub, usually router debris after
  rip-up. Passes DRC; it is what you clean up before ordering.
* ``acute`` -- from :data:`TURN_BACK_MAX_ANGLE_DEG` up to (excluding)
  ``MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG`` (89 deg): the acid-trap
  range ``sharp_trace_corner`` already warns on.
* ``right_angle`` -- 89..91 deg inclusive (``ANGLE_TOLERANCE_DEG`` around
  90): a rounded right angle. The study found router output carried 2-5x
  the right angles of hand or scripted copper.

Obtuse bends (a 45-deg chamfer has a 135-deg interior angle) and
straight pass-throughs (180 deg) are not corners and are not counted.

Vertices sitting under same-net pad/via copper are joints, not bends,
and are skipped exactly as :mod:`kicad_tools.validate.rules.
sharp_trace_corner` skips them. Stacked duplicate segments count once
(the issue #9037 handling). Arcs contribute length but no corners: a
curved track has no discrete bend vertex, and chord angles would
misreport smooth copper.

Via classification
------------------

A via is a *ground via* when its net name classifies as
``NetClass.GROUND`` under the router's name patterns
(:func:`~kicad_tools.router.net_class.classify_from_name`: GND, GNDA,
GNDD, AGND, DGND, PGND, SGND, VSS, GROUND, CGND, CHASSIS, EARTH, SHIELD,
and the ``_GND``/``_VSS``/``_AGND``/``_DGND`` suffixes). Every other via
-- including net-less vias -- is a *track via*: copper carrying a signal
or supply to another layer, where a high count means the routing ran out
of room.

Pair skew
---------

Per differential pair, skew comes from the existing
:class:`~kicad_tools.analysis.trace_length.TraceLengthAnalyzer`
(``find_differential_pairs`` + ``analyze_diff_pair``); it is exposed
here, not reimplemented. Match-group skew needs the router's
net-class map (``validate.match_group_skew.derive_group_skew_data``),
which neither consumer selects, so it is not part of this block.

Not a check
-----------

These are metrics, not findings: nothing here is, or feeds, a ``kct
check`` rule. ``sharp_trace_corner`` already warns on acute corners and
turn-backs and its 90 deg threshold is unverified against fab guidance
(see its docstring); no new rule and no new threshold are introduced
here (recorded decision on issue #9594).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "TURN_BACK_MAX_ANGLE_DEG",
    "LayoutQualityReport",
    "analyze_layout_quality",
]

#: A same-net join whose interior angle is below this is a turn-back
#: (overlapping copper / dead-end stub), not merely an acute corner.
#: Mirrors the PCBJam study's turn-back definition; not a fab-derived
#: threshold and deliberately not a ``kct check`` limit.
TURN_BACK_MAX_ANGLE_DEG = 10.0

_CORNER_CLASSES = ("right_angle", "acute", "turn_back")

# Vertex keys round coordinates to 2 decimals, the same granularity the
# sharp-trace-corner rule uses to group findings at one vertex.
_VERTEX_DECIMALS = 2


def _classify_angle(angle: float | None) -> str | None:
    """Bucket one interior angle into a corner class (or ``None``).

    The acute/right-angle window is the shared acid-trap logic's own
    (``MIN_TRACE_ANGLE_DEG``/``ANGLE_TOLERANCE_DEG``), imported rather
    than copied. Imports stay function-local: ``kicad_tools.explain``
    pulls in the whole checks package, which must not import this
    package back at module load.
    """
    from kicad_tools.explain.checks.acid_trap import ANGLE_TOLERANCE_DEG, MIN_TRACE_ANGLE_DEG

    if angle is None:
        return None
    if angle < TURN_BACK_MAX_ANGLE_DEG:
        return "turn_back"
    if angle < MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG:
        return "acute"
    if angle <= MIN_TRACE_ANGLE_DEG + ANGLE_TOLERANCE_DEG:
        return "right_angle"
    return None  # obtuse bend or straight pass-through: not a corner


def _net_raw_name(pcb: PCB, net_number: int, seg_net_name: str) -> str:
    """The net name as pad/via copper records it (burial matching)."""
    if seg_net_name:
        return seg_net_name
    net = pcb.get_net(net_number)
    return net.name if net else ""


def _net_display_name(pcb: PCB, net_number: int, raw_name: str) -> str:
    if raw_name:
        return raw_name
    return f"Net {net_number}"


def _count_corners(pcb: PCB) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Count same-net joins by corner class; totals and per-net tallies.

    Groups segments by (net, layer), collects the legs meeting at each
    junction vertex (``AcidTrapCheck._find_junction``), skips vertices
    buried under same-net pad/via copper, dedupes stacked duplicate
    segments, and classifies every distinct-leg pair angle.
    """
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck
    from kicad_tools.validate.rules.sharp_trace_corner import _copper_discs, _is_buried

    check = AcidTrapCheck()
    discs = _copper_discs(pcb)

    # KiCad-10 name-only boards give every segment net_number 0, so the
    # net name is part of the group key: grouping by number alone would
    # merge distinct nets into one population.
    groups: dict[tuple[int, str, str], list[Any]] = {}
    for seg in pcb.segments:
        groups.setdefault((seg.net_number, seg.net_name, seg.layer), []).append(seg)

    # One vertex per (group, rounded junction): every same-group segment
    # pair sharing an endpoint contributes both legs to it.
    vertices: dict[tuple, tuple[tuple[float, float], list[Any]]] = {}
    for key, segments in groups.items():
        for i, seg1 in enumerate(segments):
            for seg2 in segments[i + 1 :]:
                junction = check._find_junction(seg1, seg2)
                if junction is None:
                    continue
                vkey = (
                    key,
                    round(junction[0], _VERTEX_DECIMALS),
                    round(junction[1], _VERTEX_DECIMALS),
                )
                entry = vertices.setdefault(vkey, (junction, []))
                legs = entry[1]
                if seg1 not in legs:
                    legs.append(seg1)
                if seg2 not in legs:
                    legs.append(seg2)

    totals: dict[str, int] = dict.fromkeys(_CORNER_CLASSES, 0)
    per_net: dict[str, dict[str, int]] = {}
    for vkey, (junction, legs) in vertices.items():
        net_number, seg_net_name, layer = vkey[0]
        raw_name = _net_raw_name(pcb, net_number, seg_net_name)
        # A vertex under same-net pad/via copper is a joint (pad-centre
        # fan-in), not a bend -- same exemption as sharp_trace_corner.
        if _is_buried(discs, raw_name, layer, junction):
            continue
        # Stacked duplicate segments (an art artifact) count once, so a
        # duplicated bend is not double-counted (issue #9037 handling).
        distinct: list[Any] = []
        seen_edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
        for seg in legs:
            pa = (round(seg.start[0], _VERTEX_DECIMALS), round(seg.start[1], _VERTEX_DECIMALS))
            pb = (round(seg.end[0], _VERTEX_DECIMALS), round(seg.end[1], _VERTEX_DECIMALS))
            edge = (pa, pb) if pa <= pb else (pb, pa)
            if edge not in seen_edges:
                seen_edges.add(edge)
                distinct.append(seg)
        if len(distinct) < 2:
            continue
        display = _net_display_name(pcb, net_number, raw_name)
        for i, seg1 in enumerate(distinct):
            for seg2 in distinct[i + 1 :]:
                cls = _classify_angle(check._calculate_angle(seg1, seg2, junction))
                if cls is None:
                    continue
                totals[cls] += 1
                per_net.setdefault(display, dict.fromkeys(_CORNER_CLASSES, 0))[cls] += 1
    return totals, per_net


def _via_split(pcb: PCB) -> tuple[int, int]:
    """``(track, ground)`` via counts under the documented net-name rule."""
    from kicad_tools.router.net_class import NetClass, classify_from_name

    track = 0
    ground = 0
    for via in pcb.vias:
        if classify_from_name(via.net_name or "") is NetClass.GROUND:
            ground += 1
        else:
            track += 1
    return track, ground


def _total_track_length_mm(pcb: PCB) -> float:
    """Summed straight-segment centreline length plus analytic arc length."""
    total = 0.0
    for seg in pcb.segments:
        total += math.dist(seg.start, seg.end)
    for arc in pcb.arcs:
        total += arc.length
    return total


def _outline_area_mm2(pcb: PCB) -> float | None:
    """Area enclosed by the Edge.Cuts outline, or ``None`` when unsupported.

    Polygonizes ``PCB.get_board_outline_segments()`` (straight lines,
    two-chord arc approximations, rects). Cutouts subtract: polygon depth
    parity decides add vs subtract, so outer - cutout + nested island
    comes out right. Open, crossing, malformed or absent outlines (and
    outline shapes the accessor does not model, e.g. ``gr_circle``)
    return ``None`` rather than an inaccurate area -- the same policy as
    the development board-size metadata.
    """
    segments = pcb.get_board_outline_segments()
    if not segments:
        return None
    try:
        from shapely.errors import GEOSException  # type: ignore[import-untyped]
        from shapely.geometry import MultiLineString  # type: ignore[import-untyped]
        from shapely.ops import polygonize_full  # type: ignore[import-untyped]

        lines = MultiLineString(segments)
        if not lines.is_simple:  # crossings and overlapping edges
            return None
        polygons, cuts, dangles, invalid = polygonize_full(lines)
        if polygons.is_empty or not all(g.is_empty for g in (cuts, dangles, invalid)):
            return None
        rings = list(polygons.geoms)
        area = 0.0
        for ring in rings:
            depth = sum(1 for other in rings if other is not ring and other.covers(ring))
            area += ring.area if depth % 2 == 0 else -ring.area
        return area if math.isfinite(area) and area > 0 else None
    except (ValueError, TypeError, OverflowError, GEOSException):
        return None


def _diff_pair_skews(pcb: PCB) -> list[dict[str, Any]]:
    """Per differential pair, the existing trace-length skew analysis."""
    from kicad_tools.analysis.trace_length import TraceLengthAnalyzer

    analyzer = TraceLengthAnalyzer()
    out: list[dict[str, Any]] = []
    for net_p, net_n in analyzer.find_differential_pairs(pcb):
        report = analyzer.analyze_diff_pair(pcb, net_p, net_n)
        out.append(
            {
                "net_positive": net_p,
                "net_negative": net_n,
                "length_positive_mm": round(report.report_p.total_length_mm, 3),
                "length_negative_mm": round(report.report_n.total_length_mm, 3),
                "skew_mm": round(report.skew_mm, 3),
            }
        )
    return out


@dataclass
class LayoutQualityReport:
    """Layout-quality metrics for one parsed PCB.

    Attributes:
        track_vias: Vias on nets that do not classify as ground (the
            routing-exhaustion signal; includes net-less vias).
        ground_vias: Vias on ground-classified nets (stitching/plane ties).
        right_angle_corners: Same-net joins with an interior angle of
            89-91 deg, board-wide total.
        acute_corners: Same-net joins with an interior angle of 10..89 deg
            (the acid-trap population ``sharp_trace_corner`` warns on).
        turn_back_corners: Same-net joins with an interior angle under
            10 deg (overlaps / dead-end stubs).
        corners_per_net: Per-net breakdown of the three corner classes.
        total_track_length_mm: Straight segments plus analytic arcs.
        outline_area_mm2: Edge.Cuts polygon area; ``None`` when the
            outline is absent or unsupported.
        diff_pair_skews: One entry per detected differential pair.
    """

    track_vias: int = 0
    ground_vias: int = 0
    right_angle_corners: int = 0
    acute_corners: int = 0
    turn_back_corners: int = 0
    corners_per_net: dict[str, dict[str, int]] = field(default_factory=dict)
    total_track_length_mm: float = 0.0
    outline_area_mm2: float | None = None
    diff_pair_skews: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Dictionary for JSON serialization (additive-board.json block)."""
        result: dict[str, Any] = {
            "via": {
                "track": self.track_vias,
                "ground": self.ground_vias,
                "total": self.track_vias + self.ground_vias,
            },
            "corners": {
                "right_angle": self.right_angle_corners,
                "acute": self.acute_corners,
                "turn_back": self.turn_back_corners,
            },
            "total_track_length_mm": round(self.total_track_length_mm, 3),
        }
        if self.corners_per_net:
            result["corners"]["per_net"] = {
                net: dict(self.corners_per_net[net]) for net in sorted(self.corners_per_net)
            }
        if self.outline_area_mm2 is not None:
            result["outline_area_mm2"] = round(self.outline_area_mm2, 3)
        if self.diff_pair_skews:
            result["diff_pair_skew"] = self.diff_pair_skews
        return result


def analyze_layout_quality(pcb: PCB) -> LayoutQualityReport:
    """Compute the layout-quality metric set for one parsed PCB.

    Pure over the board: same input, same output (all floats are rounded
    in :meth:`LayoutQualityReport.to_dict`), which is what the
    benchmark's rerun-stability assertion relies on.

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.

    Returns:
        The populated :class:`LayoutQualityReport`.
    """
    totals, per_net = _count_corners(pcb)
    track_vias, ground_vias = _via_split(pcb)
    return LayoutQualityReport(
        track_vias=track_vias,
        ground_vias=ground_vias,
        right_angle_corners=totals["right_angle"],
        acute_corners=totals["acute"],
        turn_back_corners=totals["turn_back"],
        corners_per_net=per_net,
        total_track_length_mm=_total_track_length_mm(pcb),
        outline_area_mm2=_outline_area_mm2(pcb),
        diff_pair_skews=_diff_pair_skews(pcb),
    )
