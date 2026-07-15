"""Buttress-wire reinforcement: anchor-PTH row along a named net.

Unit A (MVP) of the #4218 buttress-wire reinforcement design (Part 2 of
the #4215 ampacity feature). This is a **post-route** pass that reads an
already-routed board, walks the routed copper of one named net into
ordered polylines, and emits a spaced row of **same-net** plated
through-hole (PTH) anchor vias along the longest run. A solid-core copper
wire ("buttress wire") is later soldered through the anchor row by hand to
carry additional current.

Structural template: ``kct stitch`` (``cli/stitch_cmd.py``) -- a
load -> add-geometry-to-a-named-net -> save pass. Anchors are emitted via
:meth:`kicad_tools.schema.pcb.PCB.add_via` (a large plated via IS a PTH).

Key properties:

* **Same-net, no shorts.** Anchors carry the target net, so they never
  short the trace to itself.
* **Clearance-refuse, not silent short.** Before committing each anchor,
  the pass runs
  :func:`kicad_tools.router.via_clearance.point_clear_of_copper` against
  all *other*-net copper (tracks / vias / pads / filled zone polygons) and
  the manufacturer hole-to-hole floor. A colliding anchor is REFUSED and
  reported -- never silently placed (a short) and never silently dropped.
* **Junction-aware chaining.** Segment chaining reuses the hardened
  junction-splitting chainer
  (:func:`kicad_tools.router.optimizer.chain.sort_into_chains`) via a thin
  adapter from :class:`kicad_tools.schema.pcb.Segment` to
  :class:`kicad_tools.router.primitives.Segment`. For the MVP the longest
  run is anchored and any additional branches are *reported* as unhandled.

Out of scope (later units, tracked on #4218): mask-opening channel
(Unit B), net-class ``reinforced`` wiring (Unit C), HV keep-away
(Unit D), ampacity-credit coupling in #4217 (Unit E), first-class
HV/creepage model (Unit F).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_tools.core.types import CopperLayer
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.physics.wire_gauge import (
    DEFAULT_WIRE_GAUGE_AWG,
    anchor_drill_for_awg,
    anchor_pad_for_drill,
)
from kicad_tools.router.optimizer.chain import sort_into_chains
from kicad_tools.router.primitives import Segment as RouterSegment
from kicad_tools.router.via_clearance import (
    FilledPolygonLike,
    ForeignPadTuple,
    TrackSegmentLike,
    point_clear_of_copper,
)
from kicad_tools.schema.pcb import PCB, Segment

# Default spacing between mid-run anchors (mm). Matches the architect's
# ``ReinforcementSpec.anchor_spacing_mm`` default.
DEFAULT_ANCHOR_SPACING_MM = 15.0

# Fallback design rules when no manufacturer profile is supplied. These are
# JLCPCB-tier-typical values; the annular-ring floor is what actually sizes
# the anchor pad. The reinforce pass NEVER hardcodes the annular ring at a
# call site -- it always reads it from these rules (real or fallback).
_FALLBACK_DESIGN_RULES = DesignRules(  # noqa: MFR001 reason="intentional manufacturer-agnostic fallback: reinforce is a post-route pass that runs when no manufacturer profile is supplied; a real profile overrides these JLCPCB-tier-typical defaults when threaded in"
    min_trace_width_mm=0.127,
    min_clearance_mm=0.2,
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.25,
    min_hole_to_hole_mm=0.5,
)


@dataclass
class RefusedAnchor:
    """An anchor position that was refused for clearance reasons."""

    x: float
    y: float
    reason: str


@dataclass
class PlacedAnchor:
    """An anchor via that was placed."""

    x: float
    y: float


@dataclass
class ReinforceResult:
    """Outcome of a :func:`reinforce_net` pass."""

    net_name: str
    wire_gauge_awg: int
    anchor_drill_mm: float
    anchor_pad_mm: float
    spacing_mm: float
    layer: str = ""
    #: Segments chained into the anchored (longest) run.
    run_segment_count: int = 0
    #: Cumulative arc length of the anchored run, in mm.
    run_length_mm: float = 0.0
    #: Number of additional linear runs on this net that were NOT anchored
    #: (branches / disconnected runs -- reported, not silently dropped).
    unhandled_runs: int = 0
    placed: list[PlacedAnchor] = field(default_factory=list)
    refused: list[RefusedAnchor] = field(default_factory=list)

    @property
    def placed_count(self) -> int:
        return len(self.placed)

    @property
    def refused_count(self) -> int:
        return len(self.refused)


class ReinforceError(ValueError):
    """Raised when the reinforce pass cannot proceed (e.g. unknown net)."""


def _seg_length(seg: Segment) -> float:
    (x1, y1), (x2, y2) = seg.start, seg.end
    return math.hypot(x2 - x1, y2 - y1)


def _to_router_segment(seg: Segment) -> RouterSegment:
    """Adapt a schema ``Segment`` to the chainer's ``router`` ``Segment``.

    The junction-aware chainer (:func:`sort_into_chains`) operates on
    :class:`kicad_tools.router.primitives.Segment` (``x1/y1/x2/y2``), while
    a parsed board yields :class:`kicad_tools.schema.pcb.Segment`
    (``start``/``end`` tuples). This trivial field mapping lets the pass
    reuse the single hardened chaining implementation rather than fork it.
    """
    (x1, y1), (x2, y2) = seg.start, seg.end
    # ``sort_into_chains`` only reads the ``x1/y1/x2/y2`` endpoints; the
    # ``layer`` field (a ``CopperLayer`` enum on the router type) is carried
    # along but never consulted, so a placeholder is fine. Callers pre-filter
    # segments to a single layer before chaining.
    return RouterSegment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=seg.width,
        layer=CopperLayer.F_CU,
        net=seg.net_number,
        net_name=seg.net_name,
    )


def _chain_polylines(segments: list[Segment]) -> list[list[Segment]]:
    """Chain schema segments into ordered, junction-split polylines.

    Returns a list of runs, each an ordered list of the *original* schema
    ``Segment`` objects walked endpoint-to-endpoint. Junctions (degree>=3)
    split runs so branched nets are not silently merged.
    """
    if not segments:
        return []

    router_segs = [_to_router_segment(s) for s in segments]
    chains = sort_into_chains(router_segs)

    # Map each returned (possibly flipped) router segment back to the
    # original schema segment by index. ``sort_into_chains`` preserves the
    # ``width``/``layer``/``net`` fields and only ever flips endpoints, so we
    # match on the unordered endpoint pair + width to recover the index.
    remaining = list(range(len(segments)))

    def _match(rseg: RouterSegment) -> int:
        target_pts = frozenset(
            {
                (round(rseg.x1, 6), round(rseg.y1, 6)),
                (round(rseg.x2, 6), round(rseg.y2, 6)),
            }
        )
        for idx in remaining:
            s = segments[idx]
            pts = frozenset(
                {
                    (round(s.start[0], 6), round(s.start[1], 6)),
                    (round(s.end[0], 6), round(s.end[1], 6)),
                }
            )
            if pts == target_pts and abs(s.width - rseg.width) < 1e-9:
                return idx
        # Fallback: endpoint-only match (width drift tolerance).
        for idx in remaining:
            s = segments[idx]
            pts = frozenset(
                {
                    (round(s.start[0], 6), round(s.start[1], 6)),
                    (round(s.end[0], 6), round(s.end[1], 6)),
                }
            )
            if pts == target_pts:
                return idx
        return -1

    result: list[list[Segment]] = []
    for chain in chains:
        run: list[Segment] = []
        for rseg in chain:
            idx = _match(rseg)
            if idx < 0:
                continue
            remaining.remove(idx)
            # Re-orient the original schema segment to match the chained
            # walk direction so the arc-length walk is continuous.
            orig = segments[idx]
            if abs(orig.start[0] - rseg.x1) < 1e-6 and abs(orig.start[1] - rseg.y1) < 1e-6:
                run.append(orig)
            else:
                run.append(
                    Segment(
                        start=orig.end,
                        end=orig.start,
                        width=orig.width,
                        layer=orig.layer,
                        net_number=orig.net_number,
                        net_name=orig.net_name,
                        uuid=orig.uuid,
                    )
                )
        if run:
            result.append(run)
    return result


def _run_length(run: list[Segment]) -> float:
    return sum(_seg_length(s) for s in run)


def _ordered_points(run: list[Segment]) -> list[tuple[float, float]]:
    """Return the ordered vertex list of an oriented run polyline."""
    if not run:
        return []
    pts: list[tuple[float, float]] = [run[0].start]
    for seg in run:
        pts.append(seg.end)
    return pts


def _anchor_positions(
    points: list[tuple[float, float]], spacing: float
) -> list[tuple[float, float]]:
    """Sample anchor positions along a polyline by arc length.

    Places anchors at arc-length 0, ``spacing``, ``2*spacing``, ... and
    always at the final endpoint (so both true endpoints are anchored even
    if the last interval is shorter than ``spacing``).
    """
    if len(points) < 2:
        return list(points)
    if spacing <= 0:
        raise ReinforceError(f"spacing must be positive, got {spacing}")

    # Cumulative arc length at each vertex.
    cum: list[float] = [0.0]
    for i in range(1, len(points)):
        (x0, y0), (x1, y1) = points[i - 1], points[i]
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    total = cum[-1]

    targets: list[float] = []
    d = 0.0
    while d < total - 1e-9:
        targets.append(d)
        d += spacing
    targets.append(total)  # always the final endpoint

    positions: list[tuple[float, float]] = []
    seg_i = 0
    for t in targets:
        # Advance to the polyline segment containing arc length ``t``.
        while seg_i < len(cum) - 2 and cum[seg_i + 1] < t:
            seg_i += 1
        seg_len = cum[seg_i + 1] - cum[seg_i]
        if seg_len <= 1e-12:
            positions.append(points[seg_i])
            continue
        frac = (t - cum[seg_i]) / seg_len
        frac = max(0.0, min(1.0, frac))
        (x0, y0), (x1, y1) = points[seg_i], points[seg_i + 1]
        positions.append((x0 + frac * (x1 - x0), y0 + frac * (y1 - y0)))
    return positions


class _TrackObstacle:
    """Minimal duck-typed track for ``point_clear_of_copper``."""

    __slots__ = ("start_x", "start_y", "end_x", "end_y", "width")

    def __init__(self, sx: float, sy: float, ex: float, ey: float, width: float) -> None:
        self.start_x = sx
        self.start_y = sy
        self.end_x = ex
        self.end_y = ey
        self.width = width


class _FilledPolyObstacle:
    """Minimal duck-typed filled polygon for ``point_clear_of_copper``."""

    __slots__ = ("points", "min_x", "min_y", "max_x", "max_y")

    def __init__(self, points: list[tuple[float, float]]) -> None:
        self.points = points
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.min_x = min(xs)
        self.min_y = min(ys)
        self.max_x = max(xs)
        self.max_y = max(ys)


@dataclass
class _OtherNetCopper:
    """Collected other-net copper for the clearance re-check."""

    tracks: list[TrackSegmentLike] = field(default_factory=list)
    vias: list[tuple[float, float, float, int]] = field(default_factory=list)
    pads: list[ForeignPadTuple] = field(default_factory=list)
    filled_polygons: list[FilledPolygonLike] = field(default_factory=list)
    drills: list[tuple[float, float, float, int]] = field(default_factory=list)


def _collect_other_net_copper(pcb: PCB, target_net: int) -> _OtherNetCopper:
    """Build the other-net obstacle set (everything NOT on ``target_net``)."""
    other = _OtherNetCopper()

    for seg in pcb.segments:
        if seg.net_number == target_net:
            continue
        (sx, sy), (ex, ey) = seg.start, seg.end
        other.tracks.append(_TrackObstacle(sx, sy, ex, ey, seg.width))

    for via in pcb.vias:
        if via.net_number == target_net:
            continue
        vx, vy = via.position
        other.vias.append((vx, vy, via.size, via.net_number))
        if via.drill > 0:
            other.drills.append((vx, vy, via.drill, via.net_number))

    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number == target_net:
                continue
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is None:
                continue
            px, py = pos
            # Effective bounding radius of the pad (disc model).
            radius = max(pad.size) / 2.0 if pad.size else 0.0
            other.pads.append((px, py, radius, pad.net_number))
            if pad.drill > 0:
                other.drills.append((px, py, pad.drill, pad.net_number))

    for zone in pcb.zones:
        if zone.net_number == target_net:
            continue
        for poly in zone.filled_polygons:
            if len(poly) >= 3:
                other.filled_polygons.append(_FilledPolyObstacle(list(poly)))

    return other


def reinforce_net(
    pcb: PCB,
    net_name: str,
    *,
    wire_gauge_awg: int = DEFAULT_WIRE_GAUGE_AWG,
    spacing_mm: float = DEFAULT_ANCHOR_SPACING_MM,
    design_rules: DesignRules | None = None,
    layer: str | None = None,
    dry_run: bool = False,
) -> ReinforceResult:
    """Emit a spaced same-net PTH anchor row along a routed net's trace.

    Post-route pass: chains the target net's routed segments into ordered
    polylines, selects the longest linear run, and places gauge-sized
    plated-PTH anchor vias at both endpoints and every ``spacing_mm`` along
    it. Each candidate anchor is clearance-checked against all other-net
    copper; colliding anchors are refused (reported, not placed).

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        net_name: Target net name (must have routed copper).
        wire_gauge_awg: Buttress-wire gauge (default 16 AWG). Sizes the
            anchor drill/pad.
        spacing_mm: Arc-length spacing between mid-run anchors (mm).
        design_rules: Manufacturer :class:`DesignRules` supplying
            ``min_annular_ring_mm`` (anchor pad sizing), ``min_clearance_mm``
            (other-net clearance), and ``min_hole_to_hole_mm``. Falls back to
            JLCPCB-tier-typical values when ``None``.
        layer: Restrict to this copper layer. When ``None``, the layer with
            the most cumulative segment length for the net is chosen.
        dry_run: When True, compute the placed/refused plan without
            mutating the board (no vias added).

    Returns:
        A :class:`ReinforceResult` summarising placed/refused anchors and
        the anchored run.

    Raises:
        ReinforceError: If the net does not exist, has no routed copper, or
            ``spacing_mm`` is non-positive.
    """
    if spacing_mm <= 0:
        raise ReinforceError(f"spacing_mm must be positive, got {spacing_mm}")

    rules = design_rules or _FALLBACK_DESIGN_RULES

    # Resolve the net. Accept a net that exists in the net table OR that has
    # segments carrying its name (some boards carry name-only segments).
    net_obj = pcb.get_net_by_name(net_name)
    net_segments = [
        s
        for s in pcb.segments
        if s.net_name == net_name or (net_obj is not None and s.net_number == net_obj.number)
    ]
    if net_obj is None and not net_segments:
        raise ReinforceError(f"net {net_name!r} not found on board")

    target_net_number = (
        net_obj.number
        if net_obj is not None
        else (net_segments[0].net_number if net_segments else 0)
    )

    anchor_drill = anchor_drill_for_awg(wire_gauge_awg)
    anchor_pad = anchor_pad_for_drill(anchor_drill, rules.min_annular_ring_mm)

    result = ReinforceResult(
        net_name=net_name,
        wire_gauge_awg=wire_gauge_awg,
        anchor_drill_mm=anchor_drill,
        anchor_pad_mm=anchor_pad,
        spacing_mm=spacing_mm,
    )

    if not net_segments:
        raise ReinforceError(f"net {net_name!r} has no routed copper to reinforce")

    # Group by layer; pick the layer with the most cumulative length (MVP).
    by_layer: dict[str, list[Segment]] = {}
    for s in net_segments:
        by_layer.setdefault(s.layer, []).append(s)

    if layer is not None:
        if layer not in by_layer:
            raise ReinforceError(f"net {net_name!r} has no routed copper on layer {layer!r}")
        target_layer = layer
    else:
        target_layer = max(by_layer, key=lambda lay: sum(_seg_length(s) for s in by_layer[lay]))
    result.layer = target_layer

    runs = _chain_polylines(by_layer[target_layer])
    if not runs:
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    runs.sort(key=_run_length, reverse=True)
    anchored_run = runs[0]
    result.run_segment_count = len(anchored_run)
    result.run_length_mm = _run_length(anchored_run)
    result.unhandled_runs = len(runs) - 1

    points = _ordered_points(anchored_run)
    positions = _anchor_positions(points, spacing_mm)

    other = _collect_other_net_copper(pcb, target_net_number)

    for x, y in positions:
        clear = point_clear_of_copper(
            x,
            y,
            via_size=anchor_pad,
            clearance=rules.min_clearance_mm,
            other_net_tracks=other.tracks,
            other_net_vias=other.vias,
            other_net_pads=other.pads,
            other_net_filled_polygons=other.filled_polygons,
            via_drill=anchor_drill,
            other_net_drills=other.drills,
            min_hole_to_hole=rules.min_hole_to_hole_mm,
        )
        if not clear:
            result.refused.append(
                RefusedAnchor(
                    x=x,
                    y=y,
                    reason="other-net clearance/hole-to-hole violation",
                )
            )
            continue

        if not dry_run:
            pcb.add_via(
                x,
                y,
                size=anchor_pad,
                drill=anchor_drill,
                layers=("F.Cu", "B.Cu"),
                net=net_name,
                dedupe=True,
            )
        result.placed.append(PlacedAnchor(x=x, y=y))

    return result
