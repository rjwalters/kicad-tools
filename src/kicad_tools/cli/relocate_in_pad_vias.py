#!/usr/bin/env python3
"""Relocate via-in-pad vias off-pad (connectivity-preserving).

Issue #4359 -- Phase 1 (signal-via slide-out).
Issue #4367 -- Phase 2 (plane-stitch off-pad placement).
Issue #4377 -- Phase 3 (multi-branch fallback + THT hole-to-hole clearance).

When a board is routed for a manufacturer that supports via-in-pad
(``jlcpcb-tier1``, ``pcbway``) and then re-targeted to a profile that does
NOT (``jlcpcb``, ``oshpark``, ``seeed``, ``flashpcb``), every via whose drill
sits inside an SMD pad becomes an unmanufacturable via-in-pad violation.

This module implements the **signal-via** relocation pass: for each in-pad via
that has a connected routed track (an "escape track"), the via is slid just
outside the pad boundary along the track's direction, with drill-edge clearance
>= the profile's ``min_clearance_mm``.  Connectivity is preserved by adding
short **stub** segments from the via's original (in-pad) location to its new
location on every layer that had connected copper (the pad's copper layer plus
each connected segment's layer).  No existing copper is mutated -- only the
via's ``(at ...)`` position moves and new stub segments are appended -- so an
already-routed board is never regressed.

**Phase 2** (issue #4367) extends this to **plane-stitch** vias -- a via tying
a power/ground SMD pad to a same-net copper pour, with no routed escape track.
These have no slide direction, so instead the pass walks the
``stitch --avoid-pad-overlap`` candidate ladder (8 directions x 3 escape
offsets from the pad edge) and picks the first off-pad location that (a) clears
the pad by ``min_clearance_mm``, (b) clears all other-net copper + the
hole-to-hole floor, and (c) lands **inside a same-net zone boundary polygon**
on a layer the via spans. Clearance-checked stubs preserve the pad and original
plane attachment unless actual filled copper proves continuity on that layer.
Refill the zones after saving to validate the final pour connection. When no
plane-legal, clearance-safe candidate exists (no same-net zone, or every
candidate is boxed in) the via is reported *unresolvable* and left in place --
never mis-placed into a short.

**Phase 3** (issue #4377) closes the remaining edge cases:

* **Multi-branch / internal escape** -- a via whose every connected branch
  terminates *inside* the pad (no single reliable slide direction).  Instead of
  reporting it *unresolvable* immediately, the pass now walks the same
  8-direction x 3-offset ladder used by the plane path and stubs on every
  connected layer; it is only *unresolvable* when boxed in with no clearing
  location.
* **THT / NP-THT hole-to-hole clearance** -- the shared clearance gate now
  rejects a candidate whose drill would crowd a plated through-hole's drill
  (hole-to-hole, any net) or violate copper clearance to a different-net plated
  pad.  This is the gap the #4376 judge flagged: ``kicad-cli pcb drc
  --refill-zones`` covers foreign-pour copper but not drill-to-drill spacing.
* **Rotated / dense neighborhoods** -- ``pad_absolute_bbox`` over-approximates
  non-cardinal rotations, so a via may be reported *skipped* / *unresolvable*
  where a tighter geometry engine could relocate it; the safety invariant
  (never mis-placed into a violation) always holds.

Any via this pass cannot safely handle is counted in the report (skipped or
unresolvable) -- it is never left in-pad without being surfaced.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]

from kicad_tools.cli.stitch_cmd import point_in_polygon
from kicad_tools.validate.rules.via_pad_geometry import (
    is_smd_pad,
    pad_absolute_bbox,
    via_inside_pad,
)

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Segment, Via, Zone

# Coincidence tolerance for "a track endpoint lands on the via center", mirroring
# the 0.001 mm test used by fix_vias_cmd.find_nearby_items.
_COINCIDENT_TOL = 1e-3


@dataclass
class ViaRelocation:
    """Record of an in-pad via that was moved off-pad.

    ``kind`` is ``"signal"`` for a Phase-1 slide-along-track move (which appends
    connectivity stubs -- see :attr:`stub_layers`) or ``"plane-stitch"`` for a
    Phase-2 pour move. Both kinds report any added copper in :attr:`stub_layers`;
    a plane stub is omitted only with actual filled-copper continuity evidence.
    """

    old_x: float
    old_y: float
    new_x: float
    new_y: float
    net: int
    net_name: str
    pad_ref: str
    uuid: str
    stub_layers: list[str] = field(default_factory=list)
    kind: str = "signal"


@dataclass
class ViaRelocationSkip:
    """Record of an in-pad via that could not be relocated in Phase 1.

    ``category`` is ``"skipped"`` (a valid off-pad slide exists in principle but
    would introduce a new clearance / hole-to-hole violation) or
    ``"unresolvable"`` (no Phase-1 escape geometry -- plane-stitch, multi-branch,
    or no clearing location).
    """

    x: float
    y: float
    net: int
    net_name: str
    pad_ref: str
    reason: str
    uuid: str
    category: str = "unresolvable"


@dataclass
class RelocationResult:
    """Aggregate outcome of a relocation pass."""

    moved: list[ViaRelocation] = field(default_factory=list)
    skipped: list[ViaRelocationSkip] = field(default_factory=list)
    unresolvable: list[ViaRelocationSkip] = field(default_factory=list)
    supported_noop: bool = False

    @property
    def changed(self) -> bool:
        """True when at least one via was (or would be) moved."""
        return bool(self.moved)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _ray_aabb_exit_distance(
    px: float, py: float, dx: float, dy: float, bbox: tuple[float, float, float, float]
) -> float:
    """Distance from interior point ``(px, py)`` along unit dir ``(dx, dy)`` to
    the boundary of axis-aligned ``bbox``.

    Assumes ``(px, py)`` lies inside ``bbox`` and ``(dx, dy)`` is a unit vector.
    Returns the smallest positive ``t`` at which the ray exits the box.
    """
    min_x, min_y, max_x, max_y = bbox
    t_candidates: list[float] = []
    if dx > 1e-12:
        t_candidates.append((max_x - px) / dx)
    elif dx < -1e-12:
        t_candidates.append((min_x - px) / dx)
    if dy > 1e-12:
        t_candidates.append((max_y - py) / dy)
    elif dy < -1e-12:
        t_candidates.append((min_y - py) / dy)
    positive = [t for t in t_candidates if t > 0]
    if not positive:
        return 0.0
    return min(positive)


def _dist_point_to_aabb(px: float, py: float, bbox: tuple[float, float, float, float]) -> float:
    """Shortest distance from ``(px, py)`` to axis-aligned ``bbox``.

    Returns 0.0 when the point is inside the box.
    """
    min_x, min_y, max_x, max_y = bbox
    ddx = max(min_x - px, 0.0, px - max_x)
    ddy = max(min_y - py, 0.0, py - max_y)
    return math.hypot(ddx, ddy)


def _dist_point_to_segment(px: float, py: float, seg: Segment) -> float:
    """Shortest distance from ``(px, py)`` to the line segment ``seg``."""
    x1, y1 = seg.start
    x2, y2 = seg.end
    dx = x2 - x1
    dy = y2 - y1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx = x1 + t * dx
    cy = y1 + t * dy
    return math.hypot(px - cx, py - cy)


def _endpoint_at(seg: Segment, x: float, y: float) -> tuple[float, float] | None:
    """Return the *other* endpoint of ``seg`` if one endpoint is at ``(x, y)``.

    Returns ``None`` when neither endpoint coincides with ``(x, y)``.
    """
    if abs(seg.start[0] - x) < _COINCIDENT_TOL and abs(seg.start[1] - y) < _COINCIDENT_TOL:
        return seg.end
    if abs(seg.end[0] - x) < _COINCIDENT_TOL and abs(seg.end[1] - y) < _COINCIDENT_TOL:
        return seg.start
    return None


def _pad_copper_layer(pad: Pad) -> str:
    """Return the pad's copper layer name (first ``*.Cu``), defaulting F.Cu."""
    for layer in pad.layers:
        if layer.endswith(".Cu") and not layer.startswith("*"):
            return layer
    return "F.Cu"


# ---------------------------------------------------------------------------
# Phase 2: plane-stitch off-pad placement helpers
# ---------------------------------------------------------------------------

# Candidate directions from the pad centre (unit vectors) -- the 8-direction
# ladder mirrored from stitch_cmd.calculate_via_position.
_PLANE_DIRECTIONS: tuple[tuple[float, float], ...] = (
    (1.0, 0.0),
    (0.0, 1.0),
    (-1.0, 0.0),
    (0.0, -1.0),
    (0.707, 0.707),
    (-0.707, 0.707),
    (-0.707, -0.707),
    (0.707, -0.707),
)


def _via_spans_zone_layer(via: Via, zone: Zone) -> bool:
    """True when ``zone.layer`` is a copper layer the ``via`` connects on.

    A candidate is only plane-legal on layers the via actually touches.  A via
    whose ``layers`` list names the zone layer clearly spans it; a full
    through-hole via (both ``F.Cu`` and ``B.Cu`` present) passes through every
    copper layer, so any copper zone layer counts.
    """
    via_layers = set(via.layers)
    if zone.layer in via_layers:
        return True
    if "F.Cu" in via_layers and "B.Cu" in via_layers and zone.layer.endswith(".Cu"):
        return True
    return False


def _same_net_zone_boundaries(pcb: PCB, via: Via, net_name: str) -> list[list[tuple[float, float]]]:
    """Return boundary polygons of same-net zones on a layer the via spans.

    Boundaries restrict the candidate search; they do not prove connectivity.
    Actual filled copper or explicit stubs preserve the original attachment.
    Net matching is by number when both are non-zero, falling back to name
    (KiCad 10 may emit ``(net "GND")`` with no numeric id).
    """
    polygons: list[list[tuple[float, float]]] = []
    for zone in pcb.zones:
        net_matches = (via.net_number != 0 and zone.net_number == via.net_number) or (
            bool(net_name) and zone.net_name == net_name
        )
        if not net_matches:
            continue
        if not _via_spans_zone_layer(via, zone):
            continue
        if zone.polygon:
            polygons.append(zone.polygon)
    return polygons


def _plane_stub_layers(
    pcb: PCB, via: Via, pad: Pad, target: tuple[float, float], net_name: str
) -> list[str]:
    """Preserve surface and original plane connections with conservative stubs.

    Only one actual solid fill component covering both complete via lands is
    sufficient evidence to omit a stub. Boundary membership, holes, and separate
    islands cannot establish continuity. Missing fills retain explicit copper.
    Stubs use the via diameter to preserve its entire original copper land,
    including edge-only pad contacts and multiple pads touching the annulus.
    """
    from kicad_tools.validate.connectivity import ConnectivityValidator

    old_land = Point(via.position).buffer(via.size / 2)
    new_land = Point(target).buffer(via.size / 2)
    layers = [_pad_copper_layer(pad)]
    connected_layers: set[str] = set()
    for zone in pcb.zones:
        if zone.keepout is not None or not _via_spans_zone_layer(via, zone):
            continue
        if not (
            (via.net_number and zone.net_number == via.net_number)
            or (net_name and zone.net_name == net_name)
        ):
            continue
        # Preserve potential original attachments when no fill is available.
        if not zone.filled_polygons and point_in_polygon(*via.position, zone.polygon):
            layers.append(zone.layer)
        for index, points in enumerate(zone.filled_polygons):
            layer = zone.filled_polygon_layer(index)
            solid = ConnectivityValidator._fill_solid_region(points)
            if solid is None:
                continue
            components = list(solid.geoms) if hasattr(solid, "geoms") else [solid]
            for component in components:
                if component.intersects(old_land):
                    layers.append(layer)
                if component.covers(old_land) and component.covers(new_land):
                    connected_layers.add(layer)
    return list(dict.fromkeys(layer for layer in layers if layer and layer not in connected_layers))


def _first_offpad_plane_candidate(
    pcb: PCB,
    via: Via,
    bbox: tuple[float, float, float, float],
    pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]],
    tht_pads: list[ThtPad],
    min_clearance: float,
    min_hole_to_hole: float,
    zone_polygons: list[list[tuple[float, float]]],
    pad: Pad,
    net_name: str,
) -> tuple[float, float] | None:
    """Find the first plane-legal, clearance-safe off-pad location for a via.

    Walks the 8-direction x 3-offset ladder from the pad centre.  A candidate is
    accepted only when it (a) clears the pad bbox edge by ``min_clearance`` (via
    copper radius), (b) lands inside a same-net zone boundary polygon, and (c)
    introduces no other-net clearance or hole-to-hole violation
    (:func:`_check_clearance`), including every required connection stub.
    Returns ``None`` when every candidate fails --
    the caller then reports the via as ``unresolvable`` and leaves it in place.
    """
    pad_cx = (bbox[0] + bbox[2]) / 2.0
    pad_cy = (bbox[1] + bbox[3]) / 2.0
    via_r = via.size / 2.0
    # Extra push-out beyond the guaranteed pad-clearing distance, to dodge
    # obstacles farther from the pad edge (mirrors stitch's offset*{1,1.5,2}).
    step = via.size + min_clearance
    extra_offsets = (0.0, step * 0.5, step)

    for extra in extra_offsets:
        for dx, dy in _PLANE_DIRECTIONS:
            t_exit = _ray_aabb_exit_distance(pad_cx, pad_cy, dx, dy, bbox)
            slide = t_exit + via_r + min_clearance + extra
            nx = pad_cx + dx * slide
            ny = pad_cy + dy * slide

            # (a) via copper must clear the pad bbox edge by min_clearance.
            if _dist_point_to_aabb(nx, ny, bbox) - via_r < min_clearance - 1e-6:
                continue
            # (b) must land inside a same-net zone boundary (pour overlap).
            if not any(point_in_polygon(nx, ny, poly) for poly in zone_polygons):
                continue
            # (c) must not violate other-net copper / hole-to-hole.
            if (
                _check_clearance(
                    pcb, via, nx, ny, pads_by_net, tht_pads, min_clearance, min_hole_to_hole
                )
                is not None
            ):
                continue
            stub_layers = _plane_stub_layers(pcb, via, pad, (nx, ny), net_name)
            if _check_stub_clearance(pcb, via, (nx, ny), stub_layers, via.size, min_clearance):
                continue
            return (nx, ny)

    return None


def _first_offpad_signal_candidate(
    pcb: PCB,
    via: Via,
    bbox: tuple[float, float, float, float],
    pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]],
    tht_pads: list[ThtPad],
    min_clearance: float,
    min_hole_to_hole: float,
    stub_layers: list[str] | None = None,
    stub_width: float = 0.2,
) -> tuple[float, float] | None:
    """Find the first clearance-safe off-pad location for a **signal** via.

    Phase-3 (Gap A) fallback for multi-branch / internal-escape vias: when no
    connected track leaves the pad boundary there is no single reliable slide
    direction, so walk the same 8-direction x 3-offset ladder used by the
    plane-stitch path.  Unlike :func:`_first_offpad_plane_candidate` this does
    **not** require zone membership -- signal connectivity is re-realized by the
    connectivity **stubs** the caller appends on every connected layer, not by
    pour overlap.  A candidate is accepted only when it (a) clears the pad bbox
    edge by ``min_clearance`` (via copper radius) and (b) introduces no other-net
    clearance or hole-to-hole violation (:func:`_check_clearance`).  Returns
    ``None`` when every candidate is boxed in -- the caller then reports the via
    as ``unresolvable`` and leaves it in place (safety invariant preserved).
    """
    pad_cx = (bbox[0] + bbox[2]) / 2.0
    pad_cy = (bbox[1] + bbox[3]) / 2.0
    via_r = via.size / 2.0
    step = via.size + min_clearance
    extra_offsets = (0.0, step * 0.5, step)

    for extra in extra_offsets:
        for dx, dy in _PLANE_DIRECTIONS:
            t_exit = _ray_aabb_exit_distance(pad_cx, pad_cy, dx, dy, bbox)
            slide = t_exit + via_r + min_clearance + extra
            nx = pad_cx + dx * slide
            ny = pad_cy + dy * slide

            # (a) via copper must clear the pad bbox edge by min_clearance.
            if _dist_point_to_aabb(nx, ny, bbox) - via_r < min_clearance - 1e-6:
                continue
            # (b) must not violate other-net copper / hole-to-hole.
            if (
                _check_clearance(
                    pcb, via, nx, ny, pads_by_net, tht_pads, min_clearance, min_hole_to_hole
                )
                is not None
            ):
                continue
            if (
                _check_stub_clearance(
                    pcb, via, (nx, ny), stub_layers or [], stub_width, min_clearance
                )
                is not None
            ):
                continue
            return (nx, ny)

    return None


# ---------------------------------------------------------------------------
# Core relocation pass
# ---------------------------------------------------------------------------


def _collect_smd_pads_by_net(
    pcb: PCB,
) -> dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]]:
    """Group all SMD pads by net number with precomputed obstacle AABBs.

    Net-zero pads still carry physical copper.  Retain them for clearance
    checks; relocation callers exclude net-zero vias as electrical sources.
    """
    pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]] = {}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if not is_smd_pad(pad):
                continue
            bbox = pad_absolute_bbox(pad, fp)
            pads_by_net.setdefault(pad.net_number, []).append((fp, pad, bbox))
    return pads_by_net


# One entry per plated through-hole pad: (footprint, pad, copper AABB, hole center).
ThtPad = tuple["Footprint", "Pad", tuple[float, float, float, float], tuple[float, float]]


def _collect_tht_pads(pcb: PCB) -> list[ThtPad]:
    """Collect plated through-hole pads with their copper AABB and hole center.

    Returns every ``thru_hole`` / ``np_thru_hole`` pad with a positive drill,
    **regardless of net** -- hole-to-hole spacing is net-agnostic (a relocated
    via drill may not crowd any plated hole, even one on its own net), mirroring
    the existing other-via hole-to-hole logic in :func:`_check_clearance`.
    Copper-clearance callers additionally filter on net number.  The hole center
    is the pad's absolute position (the bbox midpoint); the drill diameter is
    ``pad.drill``.  This is the coverage gap the #4376 judge flagged: the
    ``kicad-cli pcb drc --refill-zones`` cross-gate catches foreign-pour shorts
    but does **not** cover hole-to-hole, so it must be checked here.
    """
    tht: list[ThtPad] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.type not in ("thru_hole", "np_thru_hole"):
                continue
            if pad.drill <= 0:
                continue
            bbox = pad_absolute_bbox(pad, fp)
            hole_center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            tht.append((fp, pad, bbox, hole_center))
    return tht


def _check_clearance(
    pcb: PCB,
    via: Via,
    new_x: float,
    new_y: float,
    pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]],
    tht_pads: list[ThtPad],
    min_clearance: float,
    min_hole_to_hole: float,
) -> str | None:
    """Return a human reason string if placing ``via`` at ``(new_x, new_y)``
    would violate copper clearance or hole-to-hole; ``None`` when clear.

    Same-net copper is exempt (a via may touch its own net).  The following
    obstacles are considered: other vias (hole-to-hole any-net + copper
    clearance different-net), other-net SMD pads, plated through-hole pads
    (hole-to-hole any-net + copper clearance different-net -- the #4376
    judge-flagged gap that the ``--refill-zones`` DRC cross-gate does not cover),
    and other-net routed segments.
    """
    via_r = via.size / 2.0
    hole_r = via.drill / 2.0

    # Other vias: hole-to-hole (any net) + copper clearance (different net).
    for other in pcb.vias:
        if other is via:
            continue
        d = math.hypot(other.position[0] - new_x, other.position[1] - new_y)
        hole_gap = d - hole_r - other.drill / 2.0
        if hole_gap < min_hole_to_hole - 1e-6:
            return (
                f"hole-to-hole {hole_gap:.3f}mm to via at "
                f"({other.position[0]:.2f}, {other.position[1]:.2f})"
            )
        if other.net_number != via.net_number:
            cu_gap = d - via_r - other.size / 2.0
            if cu_gap < min_clearance - 1e-6:
                return (
                    f"clearance {cu_gap:.3f}mm to via at "
                    f"({other.position[0]:.2f}, {other.position[1]:.2f})"
                )

    # Other-net and unassigned SMD pads: copper clearance to pad AABB.
    for net_number, entries in pads_by_net.items():
        if net_number != 0 and net_number == via.net_number:
            continue
        for fp, pad, bbox in entries:
            cu_gap = _dist_point_to_aabb(new_x, new_y, bbox) - via_r
            if cu_gap < min_clearance - 1e-6:
                return f"clearance {cu_gap:.3f}mm to pad {fp.reference}-{pad.number}"

    # THT / NP-THT plated pads: hole-to-hole (any net) + copper clearance
    # (different net only).  Hole-to-hole is the #4376 judge-flagged gap: the
    # ``--refill-zones`` DRC cross-gate covers foreign-pour copper but NOT the
    # drill-to-drill spacing between a relocated via and a plated through-hole.
    for fp, pad, bbox, hole_center in tht_pads:
        d = math.hypot(hole_center[0] - new_x, hole_center[1] - new_y)
        hole_gap = d - hole_r - pad.drill / 2.0
        if hole_gap < min_hole_to_hole - 1e-6:
            return f"hole-to-hole {hole_gap:.3f}mm to THT pad {fp.reference}-{pad.number}"
        # Non-plated holes (net 0) carry no copper, so only plated pads on a
        # different net can violate copper clearance.
        if pad.net_number != 0 and pad.net_number != via.net_number:
            cu_gap = _dist_point_to_aabb(new_x, new_y, bbox) - via_r
            if cu_gap < min_clearance - 1e-6:
                return f"clearance {cu_gap:.3f}mm to THT pad {fp.reference}-{pad.number}"

    # Routed segments on other nets: copper clearance.
    for seg in pcb.segments:
        if seg.net_number == via.net_number:
            continue
        cu_gap = _dist_point_to_segment(new_x, new_y, seg) - via_r - seg.width / 2.0
        if cu_gap < min_clearance - 1e-6:
            return f"clearance {cu_gap:.3f}mm to track on net {seg.net_number}"

    return None


def _check_stub_clearance(
    pcb: PCB,
    via: Via,
    target: tuple[float, float],
    stub_layers: list[str],
    stub_width: float,
    min_clearance: float,
) -> str | None:
    """Validate the entire swept copper of a proposed set of straight stubs.

    Centerline distances include both conductors' radii, avoiding polygonal
    approximations of round caps. Pad AABBs conservatively include rotations.
    Net zero is physical copper, never a same-net exemption. Existing zone
    fills are checked on their actual layers; native DRC with refill remains
    required because an outline cannot predict future poured copper.

    Copper drawings / arcs and custom-pad primitives are not modeled by this
    relocator. Refuse moves on their layers rather than silently ignoring them.
    """
    from kicad_tools.validate.connectivity import ConnectivityValidator

    path = LineString([via.position, target])
    radius = stub_width / 2.0
    layers = set(stub_layers)
    copper_layers = [layer.name for layer in pcb.copper_layers]

    # Subtract the old via's clearance envelope AFTER buffering the complete
    # stub. Clipping its centerline first hides new copper beside the old via.
    # Polygon buffers are inscribed approximations. Inflate candidate/obstacle
    # buffers to circumscribe their true circles, but leave the excluded old
    # envelope inscribed: approximation can only reject, never hide new copper.
    quadrants = 64
    circumscribe = 1.0 / math.cos(math.pi / (4 * quadrants))
    new_keepout = None

    def overlaps(item_layers: list[str]) -> bool:
        return (
            bool(layers.intersection(item_layers))
            or ("*.Cu" in item_layers and bool(layers))
            or ("F&B.Cu" in item_layers and bool(layers & {"F.Cu", "B.Cu"}))
        )

    def foreign(net: int) -> bool:
        return net == 0 or net != via.net_number

    def too_close(shape, other_radius: float = 0.0) -> bool:
        nonlocal new_keepout
        # Exact analytic distance quickly accepts geometry outside the entire
        # candidate envelope, without polygon discretization error.
        if path.distance(shape) - radius - other_radius >= min_clearance - 1e-6:
            return False
        if new_keepout is None:
            candidate = path.buffer((radius + min_clearance) * circumscribe, quad_segs=quadrants)
            old_keepout = Point(via.position).buffer(
                via.size / 2.0 + min_clearance, quad_segs=quadrants
            )
            new_keepout = candidate.difference(old_keepout)
        copper = (
            shape.buffer(other_radius * circumscribe, quad_segs=quadrants)
            if other_radius > 0
            else shape
        )
        return bool(new_keepout.intersects(copper))

    for seg in pcb.segments:
        if seg.layer in layers and foreign(seg.net_number):
            if too_close(LineString([seg.start, seg.end]), seg.width / 2.0):
                return f"stub clearance to track on {seg.layer}, net {seg.net_number}"

    for other in pcb.vias:
        if other is via or not foreign(other.net_number):
            continue
        # KiCad records the endpoints of a blind/buried span, not every layer.
        span = list(other.layers)
        if len(span) >= 2 and all(layer in copper_layers for layer in span):
            indices = [copper_layers.index(layer) for layer in span]
            span = copper_layers[min(indices) : max(indices) + 1]
        if overlaps(span) and too_close(Point(other.position), other.size / 2.0):
            return f"stub clearance to via on net {other.net_number}"

    for fp in pcb.footprints:
        for pad in fp.pads:
            if not foreign(pad.net_number) or not overlaps(pad.layers):
                continue
            if pad.shape == "custom":
                return f"stub clearance unproven for custom pad {fp.reference}-{pad.number}"
            if too_close(box(*pad_absolute_bbox(pad, fp))):
                return f"stub clearance to pad {fp.reference}-{pad.number}"

    for zone in pcb.zones:
        if not foreign(zone.net_number):
            continue
        for index, points in enumerate(zone.filled_polygons):
            if zone.filled_polygon_layer(index) not in layers:
                continue
            solid = ConnectivityValidator._fill_solid_region(points)
            if solid is not None and too_close(solid):
                return f"stub clearance to filled zone on {zone.filled_polygon_layer(index)}"

    # Inspect source nodes too: routed arcs and copper text are not represented
    # by pcb.segments. Unknown copper cannot be treated as empty space.
    for node in pcb._sexp.children:
        if node.is_atom:
            continue
        nodes = node.children if node.name in ("footprint", "module") else [node]
        for item in nodes:
            if item.is_atom or not (
                item.name in ("arc", "property", "dimension")
                or (item.name or "").startswith(("gr_", "fp_"))
            ):
                continue
            layer_node = item.find("layer")
            layer = layer_node.get_string(0) if layer_node is not None else ""
            if layer in layers:
                return f"stub clearance unproven for {item.name} on {layer}"

    return None


def _persist_via_with_stubs(
    pcb: PCB,
    via: Via,
    target: tuple[float, float],
    stub_layers: list[str],
    stub_width: float,
    net_name: str,
) -> bool:
    """Commit a validated move and its copper, rolling back failed writes.

    Preserve object identities used by the caller's remaining worklist and
    footprint bindings. A false relocate_via result still changes Via.position,
    so restore that explicitly. No stubs are emitted until persistence works.
    """
    import copy

    old_position = via.position
    old_children = list(pcb._sexp.children)
    old_segments = list(pcb._segments)
    via_positions = [
        (at, copy.deepcopy(at.children))
        for node in old_children
        if not node.is_atom and node.name == "via"
        if (at := node.find("at")) is not None
    ]
    committed = False
    try:
        if not pcb.relocate_via(via, target):
            return False
        for layer in stub_layers:
            pcb.add_trace(old_position, target, width=stub_width, layer=layer, net=net_name or None)
        committed = True
        return True
    finally:
        if not committed:
            pcb._sexp.children[:] = old_children
            pcb._segments[:] = old_segments
            for at, children in via_positions:
                at.children[:] = children
            via.position = old_position
            pcb._invalidate_dedup_keys()


def relocate_in_pad_vias(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    nets: set[str] | None = None,
    dry_run: bool = False,
) -> RelocationResult:
    """Slide signal in-pad vias off-pad, preserving connectivity (Phase 1).

    Args:
        pcb: The board to operate on (mutated in place unless ``dry_run``).
        design_rules: Active manufacturer rules.  A no-op when
            ``via_in_pad_supported`` is True.  ``min_clearance_mm`` and
            ``min_hole_to_hole_mm`` gate the off-pad placement.
        nets: Optional set of net *names* to restrict the pass to.  ``None``
            means all nets.
        dry_run: When True, compute the report but do not mutate the board.

    Returns:
        A :class:`RelocationResult` with moved / skipped / unresolvable records.
        Relocation is always clearance-safe: any via whose only off-pad slide
        would introduce a new clearance or hole-to-hole violation is recorded
        as *skipped* and left untouched (an already-routed board is never made
        worse).
    """
    if dry_run:
        import copy

        # Later candidates must see earlier planned vias AND their stubs.
        # Simulating the normal mutation path also keeps reports equivalent.
        return relocate_in_pad_vias(copy.deepcopy(pcb), design_rules, nets=nets)

    result = RelocationResult()

    # Capability gate: a no-op on profiles that support via-in-pad.
    if getattr(design_rules, "via_in_pad_supported", False):
        result.supported_noop = True
        return result

    min_clearance = design_rules.min_clearance_mm
    min_hole_to_hole = design_rules.min_hole_to_hole_mm

    pads_by_net = _collect_smd_pads_by_net(pcb)
    tht_pads = _collect_tht_pads(pcb)

    # Iterate a snapshot: relocate_via/add_trace mutate the underlying lists.
    for via in list(pcb.vias):
        if via.net_number == 0:
            continue

        candidates = pads_by_net.get(via.net_number)
        if not candidates:
            continue

        # First same-net pad whose copper overlaps the via drill.
        containing = next(
            ((fp, pad, bbox) for fp, pad, bbox in candidates if via_inside_pad(via, bbox, pad, fp)),
            None,
        )
        if containing is None:
            continue

        fp, pad, bbox = containing
        pad_ref = f"{fp.reference}-{pad.number}"
        net_name = via.net_name or pad.net_name or ""

        # Net-name scoping.
        if nets is not None and net_name not in nets:
            continue

        vx, vy = via.position

        # Classify: enumerate routed segments whose endpoint lands on the via.
        connected: list[tuple[Segment, tuple[float, float]]] = []
        for seg in pcb.segments_in_net(via.net_number):
            far = _endpoint_at(seg, vx, vy)
            if far is not None:
                connected.append((seg, far))

        if not connected:
            # Preserve both the surface land and original plane attachment.
            # A zone outline selects candidates; only filled copper proves a
            # connection that can safely omit its layer's stub.
            zone_polygons = _same_net_zone_boundaries(pcb, via, net_name)
            if not zone_polygons:
                result.unresolvable.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason=(
                            "plane-stitch via has no same-net zone on a via layer to relocate into"
                        ),
                        uuid=via.uuid,
                        category="unresolvable",
                    )
                )
                continue

            target = _first_offpad_plane_candidate(
                pcb,
                via,
                bbox,
                pads_by_net,
                tht_pads,
                min_clearance,
                min_hole_to_hole,
                zone_polygons,
                pad,
                net_name,
            )
            if target is None:
                result.unresolvable.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason=(
                            "plane-stitch via: no clearance-legal off-pad location "
                            "inside the same-net zone boundary (boxed in)"
                        ),
                        uuid=via.uuid,
                        category="unresolvable",
                    )
                )
                continue

            new_x, new_y = target
            plane_stub_layers = _plane_stub_layers(pcb, via, pad, target, net_name)
            if not dry_run:
                moved_ok = _persist_via_with_stubs(
                    pcb, via, target, plane_stub_layers, via.size, net_name
                )
                if not moved_ok:
                    result.unresolvable.append(
                        ViaRelocationSkip(
                            x=vx,
                            y=vy,
                            net=via.net_number,
                            net_name=net_name,
                            pad_ref=pad_ref,
                            reason="could not locate backing (via ...) node to persist move",
                            uuid=via.uuid,
                            category="unresolvable",
                        )
                    )
                    continue

            result.moved.append(
                ViaRelocation(
                    old_x=vx,
                    old_y=vy,
                    new_x=new_x,
                    new_y=new_y,
                    net=via.net_number,
                    net_name=net_name,
                    pad_ref=pad_ref,
                    uuid=via.uuid,
                    stub_layers=plane_stub_layers,
                    kind="plane-stitch",
                )
            )
            continue

        pad_center_x = (bbox[0] + bbox[2]) / 2.0
        pad_center_y = (bbox[1] + bbox[3]) / 2.0

        # Choose the escape track: the connected segment whose far endpoint is
        # farthest from the pad center (the one leaving the pad).
        escape = max(
            connected,
            key=lambda item: math.hypot(item[1][0] - pad_center_x, item[1][1] - pad_center_y),
        )
        _, far = escape

        # Layers needing a connectivity stub: the pad's copper layer (pad->via
        # path) plus every connected segment's layer (route->via path).
        stub_layers: list[str] = []
        seen_layers: set[str] = set()
        for layer in [_pad_copper_layer(pad), *(seg.layer for seg, _ in connected)]:
            if layer and layer not in seen_layers:
                seen_layers.add(layer)
                stub_layers.append(layer)

        # Stub width: the escape track's width (fall back to a sane default).
        stub_width = escape[0].width if escape[0].width > 0 else 0.2

        if _dist_point_to_aabb(far[0], far[1], bbox) <= 1e-6:
            # Gap A (Phase 3): no connected branch leaves the pad boundary, so
            # there is no single reliable slide direction (multi-branch fan-out
            # or a fully-internal escape whose routed exit is a separate segment
            # not sharing the via node).  Fall back to the 8-direction x 3-offset
            # ladder and stub on every connected layer, exactly as the slide path
            # does.  The via is reported unresolvable only when no clearing
            # off-pad location exists (boxed in) -- never mis-placed.
            target = _first_offpad_signal_candidate(
                pcb,
                via,
                bbox,
                pads_by_net,
                tht_pads,
                min_clearance,
                min_hole_to_hole,
                stub_layers,
                stub_width,
            )
            if target is None:
                result.unresolvable.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason=(
                            "multi-branch / internal escape: no clearance-legal "
                            "off-pad location (boxed in)"
                        ),
                        uuid=via.uuid,
                        category="unresolvable",
                    )
                )
                continue
            # The ladder candidate already passed _check_clearance; no re-check.
            new_x, new_y = target
        else:
            # Single escape direction (from via center toward the far endpoint).
            dir_x = far[0] - vx
            dir_y = far[1] - vy
            dir_len = math.hypot(dir_x, dir_y)
            if dir_len < 1e-9:
                result.unresolvable.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason="degenerate escape-track direction",
                        uuid=via.uuid,
                        category="unresolvable",
                    )
                )
                continue
            dir_x /= dir_len
            dir_y /= dir_len

            # Slide along the escape direction until the drill circle clears the
            # pad edge by min_clearance: new = via + dir * (t_exit + drill/2 + clr).
            t_exit = _ray_aabb_exit_distance(vx, vy, dir_x, dir_y, bbox)
            slide = t_exit + via.drill / 2.0 + min_clearance
            new_x = vx + dir_x * slide
            new_y = vy + dir_y * slide

            # Clearance gate: never emit a worse board.
            reason = _check_clearance(
                pcb, via, new_x, new_y, pads_by_net, tht_pads, min_clearance, min_hole_to_hole
            )
            if reason is not None:
                result.skipped.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason=reason,
                        uuid=via.uuid,
                        category="skipped",
                    )
                )
                continue

        reason = _check_stub_clearance(
            pcb, via, (new_x, new_y), stub_layers, stub_width, min_clearance
        )
        if reason is not None:
            result.unresolvable.append(
                ViaRelocationSkip(
                    x=vx,
                    y=vy,
                    net=via.net_number,
                    net_name=net_name,
                    pad_ref=pad_ref,
                    reason=reason,
                    uuid=via.uuid,
                    category="unresolvable",
                )
            )
            continue

        if not dry_run:
            moved_ok = _persist_via_with_stubs(
                pcb, via, (new_x, new_y), stub_layers, stub_width, net_name
            )
            if not moved_ok:
                # Could not find the backing S-expression node -- record as
                # unresolvable rather than claiming a move that will not persist.
                result.unresolvable.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason="could not locate backing (via ...) node to persist move",
                        uuid=via.uuid,
                        category="unresolvable",
                    )
                )
                continue

        result.moved.append(
            ViaRelocation(
                old_x=vx,
                old_y=vy,
                new_x=new_x,
                new_y=new_y,
                net=via.net_number,
                net_name=net_name,
                pad_ref=pad_ref,
                uuid=via.uuid,
                stub_layers=stub_layers,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_relocation_results(
    result: RelocationResult,
    output_format: str = "text",
    dry_run: bool = False,
    mfr: str | None = None,
) -> None:
    """Print a relocation report in ``text``, ``json``, or ``summary`` form."""
    if output_format == "json":
        data = {
            "manufacturer": mfr,
            "dry_run": dry_run,
            "via_in_pad_supported_noop": result.supported_noop,
            "moved": [
                {
                    "old_x": m.old_x,
                    "old_y": m.old_y,
                    "new_x": m.new_x,
                    "new_y": m.new_y,
                    "net": m.net,
                    "net_name": m.net_name,
                    "pad": m.pad_ref,
                    "uuid": m.uuid,
                    "stub_layers": m.stub_layers,
                    "kind": m.kind,
                }
                for m in result.moved
            ],
            "skipped": [
                {
                    "x": s.x,
                    "y": s.y,
                    "net": s.net,
                    "net_name": s.net_name,
                    "pad": s.pad_ref,
                    "reason": s.reason,
                    "uuid": s.uuid,
                }
                for s in result.skipped
            ],
            "unresolvable": [
                {
                    "x": u.x,
                    "y": u.y,
                    "net": u.net,
                    "net_name": u.net_name,
                    "pad": u.pad_ref,
                    "reason": u.reason,
                    "uuid": u.uuid,
                }
                for u in result.unresolvable
            ],
        }
        print(json.dumps(data, indent=2))
        return

    if result.supported_noop:
        msg = (
            f"Manufacturer profile{f' {mfr}' if mfr else ''} supports via-in-pad; "
            "no relocation needed."
        )
        print(msg)
        return

    if output_format == "summary":
        action = "Would move" if dry_run else "Moved"
        print(f"{action} {len(result.moved)} in-pad via(s) off-pad")
        if result.skipped:
            print(f"  {len(result.skipped)} skipped (clearance/hole-to-hole)")
        if result.unresolvable:
            print(f"  {len(result.unresolvable)} unresolvable (Phase 2/3)")
        return

    # Text output.
    if not result.moved and not result.skipped and not result.unresolvable:
        print("No via-in-pad vias found; nothing to relocate.")
        return

    action = "Would move" if dry_run else "Moved"
    print(f"{action} {len(result.moved)} in-pad via(s) off-pad:")
    for m in result.moved[:10]:
        if m.stub_layers:
            tie = f"stubs on {', '.join(m.stub_layers)}"
        else:
            tie = "plane-stitch (no stub; connected filled copper)"
        print(
            f"  Via {m.uuid[:8] or '?'} (net '{m.net_name}') on pad {m.pad_ref}: "
            f"({m.old_x:.3f}, {m.old_y:.3f}) -> ({m.new_x:.3f}, {m.new_y:.3f}); "
            f"{tie}"
        )
    if len(result.moved) > 10:
        print(f"  ... and {len(result.moved) - 10} more")

    if any(m.kind == "plane-stitch" for m in result.moved):
        print(
            "\nNote: run `kicad-cli pcb drc --refill-zones` to validate "
            "plane-stitch pour connections after saving."
        )

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} via(s) (would violate clearance):")
        for s in result.skipped[:10]:
            print(f"  Via at ({s.x:.3f}, {s.y:.3f}) on pad {s.pad_ref}: {s.reason}")
        if len(result.skipped) > 10:
            print(f"  ... and {len(result.skipped) - 10} more")

    if result.unresolvable:
        print(
            f"\nUnresolvable {len(result.unresolvable)} via(s) (deferred to Phase 2/3 follow-ups):"
        )
        for u in result.unresolvable[:10]:
            print(f"  Via at ({u.x:.3f}, {u.y:.3f}) on pad {u.pad_ref}: {u.reason}")
        if len(result.unresolvable) > 10:
            print(f"  ... and {len(result.unresolvable) - 10} more")
