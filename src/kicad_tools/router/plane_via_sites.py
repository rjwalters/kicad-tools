"""Pre-route via-site reservation for plane-net SMD pads (Issue #5700).

A plane-net SMD pad -- a pad whose net is carried by a copper pour on an
inner layer and is therefore *skipped* by the trace router -- has no
connectivity of its own after routing.  It is joined to its plane later, by
a post-route stitch via (``kct stitch``) or a pour-repair escape.  Nothing
in the router knew that such a pad still *owes* a via site, so ordinary
signal routing was free to consume every legal via position around it.

The exact-disc halo (#5660) made that concrete on board 06: without the
square halo's diagonal over-blocking, a signal via (net 9) landed 0.806 mm
from ``J1.A4`` (``VBUS_USB``) and, together with J1's own neighbouring
pads and two passing traces, sealed the pad in a 0.8 x 0.75 mm pocket with
no legal via site.  Every post-route strategy (three stitcher ladders,
four pour-repair sub-stages, a bounded grid escape) then failed, correctly,
because there was no escape to find.

This module moves the constraint to where it can be kept: **before**
routing, find one legal via site (plus the stub from the pad to it) for
each plane-net SMD pad of the requested nets and mark those cells as
static obstacles on the routing grid.  The grid is the router's single
source of occupancy truth -- both the Python pathfinder and the C++
backend (mirrored via ``Grid3D::mark_blocked``) read it -- so signal
routes and signal vias keep their normal clearance to the reserved site
exactly as they would to a pre-placed via.  Nothing is emitted: the post-
route stitch/repair passes still choose where the real via goes; this only
guarantees such a place still exists when they run.

Scope is opt-in per net.  Plane nets whose pads sit inside dense arrays
(e.g. a BGA's GND field) are connected by escape traces, not an adjacent
via, and reserving a site beside every one of them would block the very
escape channels those pads need -- so a board lists the nets it wants.

Issue #6401 wires the same reservation into ``kct route`` for the nets it
auto-skips as pours, on both engines:

* :func:`select_plane_via_pads` applies the #5700 caveat as a geometric
  rule instead of a per-board net list: only *perimeter* pads of
  multi-pad footprints are reserved.  A footprint with an area array (more
  than one pad NUMBER strictly inside its pad bounding box, i.e. a BGA field) is
  skipped whole, and a lone interior pad (a QFN/QFP exposed pad) is skipped
  too -- both escape by trace or carry their own via array.  Two-terminal
  parts are skipped: their pads are open on three sides, and the boxed-in
  failure (boards 03/04/05, issue #6388) is always an IC pin in a pin row.
* The grid engine gets the reservation as static grid cells, exactly as
  board 06 does.
* The lattice engine has its own obstacle model and never reads the grid's
  occupancy, so :func:`plane_via_keepout_areas` turns each reserved site
  and stub into a static :class:`~.lattice.obstacles.KeepoutArea` that
  blocks tracks and vias.  The lattice keep-out test has zero clearance
  (copper edge against the area), so each area is the reserved copper
  grown by the clearance a signal net must keep from it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from kicad_tools.acceleration import to_numpy

from .primitives import Pad, pad_half_extents

if TYPE_CHECKING:
    from .grid import RoutingGrid

__all__ = [
    "PlaneViaSite",
    "PlaneViaSiteRules",
    "PlaneViaReservation",
    "PlaneViaStub",
    "plane_via_keepout_areas",
    "reserve_plane_via_sites",
    "select_plane_via_pads",
]


@dataclass(frozen=True)
class PlaneViaSiteRules:
    """Geometry a reserved site must be able to host.

    The defaults are the most demanding via the board-06 post-route
    passes may place: ``kct stitch --mfr jlcpcb`` emits 0.6 / 0.3 mm vias,
    and the recipe's pour-repair escape (``pour_escape.EscapeRules``)
    checks against a 0.2 mm copper clearance, 0.2 mm stubs, and a 0.5 mm
    drill-to-drill floor.  A site that clears the strongest of these clears
    all of them.

    Attributes:
        via_diameter: Copper diameter of the via the site must host (mm).
        via_drill: Drill diameter of that via (mm).
        clearance: Copper-to-copper clearance the via and stub need (mm).
        hole_to_hole: Drill edge-to-edge floor against other holes (mm).
        stub_width: Width of the pad-to-via stub (mm).
        max_reach: Largest pad-centre-to-site distance tried (mm).
        exact_pads: Issue #6401 -- measure clearance to pads against each
            pad's axis-aligned box (``pad_half_extents``, which already
            covers a residual rotation) instead of its circumscribed circle,
            and test the via/stub copper -- not copper plus halo -- against
            the grid's blocked cells, which already carry the halo.  The
            #5700 default (``False``) counts the clearance twice and treats a
            1.5 x 0.3 mm QFP pin as a 0.76 mm-radius disc, which leaves no
            site at all beside a 0.5 mm-pitch pin row (board 04's LQFP-48).
            The marked keep-out is the same in both modes.
        escape_len: Issue #6413 -- length (mm) of the straight outward
            escape corridor each same-layer signal SMD pin keeps in front of
            its tip, from that tip along its long axis away from its
            footprint's centroid.  ``None`` means ``via_diameter +
            clearance``: room for a fan-out via or a 45-degree jog.  Only
            used in ``exact_pads`` mode (the escape-corridor guard).
    """

    via_diameter: float = 0.6
    via_drill: float = 0.3
    clearance: float = 0.2
    hole_to_hole: float = 0.5
    stub_width: float = 0.2
    max_reach: float = 2.0
    exact_pads: bool = False
    escape_len: float | None = None


@dataclass(frozen=True)
class PlaneViaSite:
    """One reserved via site and the plane-net pads it serves."""

    net_name: str
    x: float
    y: float
    pads: tuple[str, ...]


@dataclass(frozen=True)
class PlaneViaStub:
    """The pad-to-site stub one served pad needs (Issue #6401).

    Attributes:
        pad: ``REF.PIN`` of the served pad.
        net_name: The pad's (plane) net.
        layer: The pad's copper layer (:class:`~.layers.Layer`).
        pad_x, pad_y: Pad centre (mm).
        site_x, site_y: The pad's final reserved site (mm).
    """

    pad: str
    net_name: str
    layer: object
    pad_x: float
    pad_y: float
    site_x: float
    site_y: float


@dataclass
class PlaneViaReservation:
    """Result of :func:`reserve_plane_via_sites`.

    Attributes:
        sites: Reserved sites, in deterministic pad order.
        unreserved: ``REF.PIN`` of pads for which no legal site exists on
            the pre-route board (they fall back to the post-route ladders,
            exactly as before this reservation existed).
        cells_blocked: Grid cells newly marked blocked, summed over layers.
        stubs: One stub per served pad, resolved to its final site
            (Issue #6401: the lattice keep-outs are built from these).
        rules: The site geometry the reservation was planned with.
        blocked_escapes: Issue #6413 -- ``(signal pin, served pad)`` pairs
            where the served pad's site or stub blocks the signal pin's
            straight outward escape from ONE side (the pin can still jog away
            from it).  A site that would block a pin from both sides is never
            taken: that pad goes to ``unreserved``.
    """

    sites: list[PlaneViaSite] = field(default_factory=list)
    unreserved: list[str] = field(default_factory=list)
    cells_blocked: int = 0
    stubs: list[PlaneViaStub] = field(default_factory=list)
    rules: PlaneViaSiteRules = field(default_factory=PlaneViaSiteRules)
    blocked_escapes: list[tuple[str, str]] = field(default_factory=list)


def _pad_name(pad: Pad) -> str:
    return f"{pad.ref}.{pad.pin}"


def _circumscribed_radius(pad: Pad) -> float:
    """Conservative round envelope of a pad (covers rectangle corners)."""
    half_w, half_h = pad_half_extents(pad)
    return math.hypot(half_w, half_h)


def _point_box_distance(pad: Pad, x: float, y: float) -> float:
    """Distance from ``(x, y)`` to the pad's axis-aligned copper box."""
    half_w, half_h = pad_half_extents(pad)
    dx = max(abs(x - pad.x) - half_w, 0.0)
    dy = max(abs(y - pad.y) - half_h, 0.0)
    return math.hypot(dx, dy)


def _segment_box_distance(ax: float, ay: float, bx: float, by: float, pad: Pad) -> float:
    """Distance from segment ``a-b`` to the pad's axis-aligned copper box."""
    half_w, half_h = pad_half_extents(pad)
    x0, x1 = pad.x - half_w, pad.x + half_w
    y0, y1 = pad.y - half_h, pad.y + half_h
    # Liang-Barsky clip: zero when the segment touches the box.
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    inside = True
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0.0:
            if q < 0.0:
                inside = False
                break
        else:
            t = q / p
            if p < 0.0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                inside = False
                break
    if inside:
        return 0.0
    corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    return min(
        _point_box_distance(pad, ax, ay),
        _point_box_distance(pad, bx, by),
        *(_segment_point_distance(ax, ay, bx, by, cx, cy) for cx, cy in corners),
    )


def _segment_point_distance(
    ax: float, ay: float, bx: float, by: float, px: float, py: float
) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def _segments_cross(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float
) -> bool:
    def orient(px: float, py: float, qx: float, qy: float, rx: float, ry: float) -> float:
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    d1 = orient(cx, cy, dx, dy, ax, ay)
    d2 = orient(cx, cy, dx, dy, bx, by)
    d3 = orient(ax, ay, bx, by, cx, cy)
    d4 = orient(ax, ay, bx, by, dx, dy)
    return d1 * d2 < 0 and d3 * d4 < 0


def _segment_segment_distance(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float
) -> float:
    """Distance between segments ``a-b`` and ``c-d`` (zero when they cross)."""
    if _segments_cross(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0
    return min(
        _segment_point_distance(ax, ay, bx, by, cx, cy),
        _segment_point_distance(ax, ay, bx, by, dx, dy),
        _segment_point_distance(cx, cy, dx, dy, ax, ay),
        _segment_point_distance(cx, cy, dx, dy, bx, by),
    )


@dataclass(frozen=True)
class _EscapeCorridor:
    """A signal pin's straight outward escape (Issue #6413).

    The centreline runs from the pin's tip ``a`` to ``b``, ``escape_len``
    further along the pin's long axis, away from its footprint's centroid;
    ``(ux, uy)`` is the unit direction.
    """

    pad: str
    layer: object
    ax: float
    ay: float
    bx: float
    by: float
    ux: float
    uy: float

    def sides(self, points: Sequence[tuple[float, float]]) -> frozenset[int]:
        """Which side(s) of the centreline reserved copper at ``points`` is on.

        ``-1`` / ``+1`` for right / left of the escape direction.  A point on
        the centreline, or a stub whose ends straddle it, blocks both.
        """
        offsets = [self.ux * (py - self.ay) - self.uy * (px - self.ax) for px, py in points]
        if any(abs(o) <= 1e-9 for o in offsets) or (min(offsets) < 0.0 < max(offsets)):
            return frozenset((-1, 1))
        return frozenset((1,) if offsets[0] > 0 else (-1,))


def _disc_offsets(radius_cells: float) -> list[tuple[int, int]]:
    reach = int(math.ceil(radius_cells))
    limit = radius_cells * radius_cells
    return [
        (dx, dy)
        for dy in range(-reach, reach + 1)
        for dx in range(-reach, reach + 1)
        if dx * dx + dy * dy <= limit
    ]


def _capsule_cells(
    grid: RoutingGrid, ax: float, ay: float, bx: float, by: float, radius: float
) -> list[tuple[int, int]]:
    """Grid cells whose centre lies within ``radius`` of segment ``a-b``."""
    res = grid.resolution
    gx0 = int(math.floor((min(ax, bx) - radius - grid.origin_x) / res))
    gx1 = int(math.ceil((max(ax, bx) + radius - grid.origin_x) / res))
    gy0 = int(math.floor((min(ay, by) - radius - grid.origin_y) / res))
    gy1 = int(math.ceil((max(ay, by) + radius - grid.origin_y) / res))
    cells = []
    for gy in range(gy0, gy1 + 1):
        wy = grid.origin_y + gy * res
        for gx in range(gx0, gx1 + 1):
            wx = grid.origin_x + gx * res
            if _segment_point_distance(ax, ay, bx, by, wx, wy) <= radius:
                cells.append((gx, gy))
    return cells


class _SitePlanner:
    """Finds legal sites against the pre-route grid plus pad geometry."""

    def __init__(
        self,
        grid: RoutingGrid,
        rules: PlaneViaSiteRules,
        plane_nets: Iterable[str] = (),
        targets: Sequence[Pad] = (),
    ) -> None:
        self.grid = grid
        self.rules = rules
        self.pads: list[Pad] = list(grid._pads)
        self.blocked = to_numpy(grid._blocked)
        self.net = to_numpy(grid._net)
        # The grid encodes clearance by dilating obstacles by
        # ``trace_clearance + trace_width / 2`` (see ``_add_pad_unsafe``), so
        # a foreign trace CENTRELINE only needs to avoid blocked cells.  A
        # reserved via must therefore be dilated by the same amount, using the
        # stronger of the router's and the site's clearance.
        router_rules = grid.rules
        self.halo = max(router_rules.trace_clearance, rules.clearance) + (
            router_rules.trace_width / 2.0
        )
        self.via_r = rules.via_diameter / 2.0
        self.disc_radius = self.via_r + self.halo
        self.disc = _disc_offsets(self.disc_radius / grid.resolution)
        # Issue #6401: a blocked cell already carries the router's own halo
        # (``trace_clearance + trace_width / 2`` past the obstacle copper), so
        # in exact mode the LEGALITY probe for copper of half-width ``h`` that
        # needs ``rules.clearance`` is a disc of ``h + clearance - halo``
        # around its centre -- only the part of the clearance the halo does
        # not already hold.  Pads are measured exactly in ``_pad_gap``; this
        # probe guards the rest of the grid (edge clearance, keep-outs,
        # preserved copper).  The marked keep-out stays ``disc``.
        grid_halo = router_rules.trace_clearance + router_rules.trace_width / 2.0
        if rules.exact_pads:
            self.probe_disc = _disc_offsets(
                max(self.via_r + rules.clearance - grid_halo, 0.0) / grid.resolution
            )
            # Floor at half a cell so a diagonal stub still probes the
            # cells its centreline crosses.
            self.stub_probe = max(
                rules.stub_width / 2.0 + rules.clearance - grid_halo, grid.resolution / 2.0
            )
        else:
            self.probe_disc = self.disc
            self.stub_probe = rules.stub_width / 2.0 + self.halo
        self.all_layers = list(range(grid.num_layers))
        self.sites: list[PlaneViaSite] = []

        # Issue #6413: escape-corridor guard (exact mode only, the
        # ``kct route`` path).  Reserved copper is clear of a signal pin's
        # corridor when a trace on its centreline keeps the clearance.
        self.plane_nets = frozenset(plane_nets)
        gap = max(router_rules.trace_clearance, rules.clearance)
        half_trace = router_rules.trace_width / 2.0
        self.escape_len = (
            rules.escape_len
            if rules.escape_len is not None
            else rules.via_diameter + rules.clearance
        )
        self.site_reach = self.via_r + gap + half_trace
        self.stub_reach = rules.stub_width / 2.0 + gap + half_trace
        self.corridors: list[_EscapeCorridor] = self._build_corridors() if rules.exact_pads else []
        self.at_risk = self._corridors_at_risk(targets)
        self.corridor_sides: dict[int, frozenset[int]] = {}
        self.blocked_escapes: list[tuple[str, str]] = []
        self._near_cache: dict[str, list[int]] = {}

    # -- escape corridors (Issue #6413) ---------------------------------------
    def _build_corridors(self) -> list[_EscapeCorridor]:
        """One straight outward corridor per signal SMD pin, every footprint.

        Built across footprints, not just the served pad's own: board 03's
        crystal ``Y1.2`` site sat in front of ``U1.19``.
        """
        by_ref: dict[str, list[Pad]] = {}
        for p in self.pads:
            if p.ref and not p.steiner_point:
                by_ref.setdefault(p.ref, []).append(p)
        corridors: list[_EscapeCorridor] = []
        for ref in sorted(by_ref):
            body = by_ref[ref]
            if len(body) < 2:
                continue  # no centroid to escape away from
            cx = sum(p.x for p in body) / len(body)
            cy = sum(p.y for p in body) / len(body)
            for p in body:
                if not p.net_name or p.net_name in self.plane_nets or p.through_hole:
                    continue
                half_w, half_h = pad_half_extents(p)
                if half_w >= half_h:
                    ux, uy = (1.0 if p.x >= cx else -1.0), 0.0
                    ax, ay = p.x + ux * half_w, p.y
                else:
                    ux, uy = 0.0, (1.0 if p.y >= cy else -1.0)
                    ax, ay = p.x, p.y + uy * half_h
                corridors.append(
                    _EscapeCorridor(
                        pad=_pad_name(p),
                        layer=p.layer,
                        ax=ax,
                        ay=ay,
                        bx=ax + ux * self.escape_len,
                        by=ay + uy * self.escape_len,
                        ux=ux,
                        uy=uy,
                    )
                )
        return corridors

    def _corridors_at_risk(self, targets: Sequence[Pad]) -> frozenset[int]:
        """Corridors a served pad could reach from BOTH sides.

        Only such a pin can end up flanked.  A pin with served pads on one
        side at most can be blocked from that side only, and it can still jog
        away; demoting every site in front of it pushed sites out past the
        whole pin row (on board 04, U2.35's site 0.65 mm further out, off the
        B.Cu pour, and the pad lost its weld).
        """
        reach = self.rules.max_reach + self.site_reach
        risky: set[int] = set()
        for idx, c in enumerate(self.corridors):
            sides: set[int] = set()
            for p in targets:
                if _segment_point_distance(c.ax, c.ay, c.bx, c.by, p.x, p.y) <= reach:
                    sides |= c.sides([(p.x, p.y)])
            if len(sides) == 2:
                risky.add(idx)
        return frozenset(risky)

    def _near_corridors(self, pad: Pad) -> list[int]:
        """Corridors any site or stub of ``pad`` within ``max_reach`` can touch."""
        name = _pad_name(pad)
        cached = self._near_cache.get(name)
        if cached is None:
            reach = self.rules.max_reach + max(self.site_reach, self.stub_reach) + 1e-6
            cached = [
                i
                for i, c in enumerate(self.corridors)
                if _segment_point_distance(c.ax, c.ay, c.bx, c.by, pad.x, pad.y) <= reach
            ]
            self._near_cache[name] = cached
        return cached

    def _escape_hits(
        self, pad: Pad, x: float, y: float, *, disc: bool = True
    ) -> dict[int, frozenset[int]]:
        """Corridors a site at ``(x, y)`` (and ``pad``'s stub to it) enters.

        The via disc spans every layer; the stub only blocks corridors on the
        pad's own layer.  ``disc=False`` checks the stub alone (a shared site
        whose disc is already counted).
        """
        hits: dict[int, frozenset[int]] = {}
        for idx in self._near_corridors(pad):
            c = self.corridors[idx]
            sides: frozenset[int] = frozenset()
            if disc and (
                _segment_point_distance(c.ax, c.ay, c.bx, c.by, x, y) < self.site_reach - 1e-9
            ):
                sides |= c.sides([(x, y)])
            if c.layer == pad.layer and (
                _segment_segment_distance(c.ax, c.ay, c.bx, c.by, pad.x, pad.y, x, y)
                < self.stub_reach - 1e-9
            ):
                sides |= c.sides([(pad.x, pad.y), (x, y)])
            if sides:
                hits[idx] = sides
        return hits

    def _flanks(self, hits: dict[int, frozenset[int]]) -> bool:
        """Would these hits leave some signal pin blocked from both sides?"""
        return any(
            len(self.corridor_sides.get(idx, frozenset()) | sides) == 2
            for idx, sides in hits.items()
        )

    def _verdict(self, hits: dict[int, frozenset[int]]) -> str:
        """``"flank"`` (never taken), ``"risky"`` (a last resort: it blocks a
        pin that could still be flanked) or ``"ok"`` (no hit, or one-sided
        blocks on pins that cannot be flanked; recorded)."""
        if self._flanks(hits):
            return "flank"
        if any(idx in self.at_risk for idx in hits):
            return "risky"
        return "ok"

    def _commit_hits(self, pad: Pad, hits: dict[int, frozenset[int]]) -> None:
        served = _pad_name(pad)
        for idx, sides in sorted(hits.items()):
            self.corridor_sides[idx] = self.corridor_sides.get(idx, frozenset()) | sides
            pair = (self.corridors[idx].pad, served)
            if pair not in self.blocked_escapes:
                self.blocked_escapes.append(pair)

    def _place(self, pad: Pad, x: float, y: float, hits: dict[int, frozenset[int]]) -> PlaneViaSite:
        site = PlaneViaSite(pad.net_name, x, y, (_pad_name(pad),))
        self.sites.append(site)
        self._commit_hits(pad, hits)
        return site

    def _share(self, idx: int, pad: Pad, hits: dict[int, frozenset[int]]) -> PlaneViaSite:
        site = self.sites[idx]
        shared = PlaneViaSite(site.net_name, site.x, site.y, site.pads + (_pad_name(pad),))
        self.sites[idx] = shared
        self._commit_hits(pad, hits)
        return shared

    # -- legality ------------------------------------------------------------
    def _cells_free(self, layer: int, cells: Iterable[tuple[int, int]]) -> bool:
        """No cell is out of bounds or owned by a routed (nonzero) net.

        Cells blocked with net 0 are the halos of *plane* pads and keepouts
        (``skip_nets`` rewrites plane pads to net 0); those are checked
        geometrically instead, where the pad's real net name is known.
        """
        grid = self.grid
        xs, ys = [], []
        for gx, gy in cells:
            if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                return False
            xs.append(gx)
            ys.append(gy)
        if not xs:
            return True
        xa, ya = np.asarray(xs), np.asarray(ys)
        blocked = self.blocked[layer, ya, xa]
        owners = self.net[layer, ya, xa]
        return not bool(np.any(blocked & (owners != 0)))

    def _pad_gap(self, other: Pad, x: float, y: float) -> float:
        if self.rules.exact_pads:
            return _point_box_distance(other, x, y)
        return math.hypot(other.x - x, other.y - y) - _circumscribed_radius(other)

    def _via_ok(self, pad: Pad, x: float, y: float) -> bool:
        rules = self.rules
        for other in self.pads:
            gap = self._pad_gap(other, x, y)
            if other.net_name == pad.net_name:
                # Never a via-in-pad (the stitcher runs --avoid-pad-overlap).
                if gap < self.via_r:
                    return False
            elif gap < self.via_r + rules.clearance:
                return False
            if other.through_hole and other.drill > 0:
                centre = math.hypot(other.x - x, other.y - y)
                if centre < rules.via_drill / 2 + other.drill / 2 + rules.hole_to_hole:
                    return False
        for site in self.sites:
            centre = math.hypot(site.x - x, site.y - y)
            if centre < max(rules.via_drill + rules.hole_to_hole, 2 * self.via_r + rules.clearance):
                return False
        gx, gy = self.grid.world_to_grid(x, y)
        disc_cells = [(gx + dx, gy + dy) for dx, dy in self.probe_disc]
        return all(self._cells_free(layer, disc_cells) for layer in self.all_layers)

    def _stub_ok(self, pad: Pad, x: float, y: float) -> bool:
        rules = self.rules
        half = rules.stub_width / 2.0
        for other in self.pads:
            if other.net_name == pad.net_name:
                continue
            if not (other.through_hole or other.layer == pad.layer):
                continue
            if rules.exact_pads:
                gap = _segment_box_distance(pad.x, pad.y, x, y, other)
            else:
                dist = _segment_point_distance(pad.x, pad.y, x, y, other.x, other.y)
                gap = dist - _circumscribed_radius(other)
            if gap < half + rules.clearance:
                return False
        layer = self.grid.layer_to_index(pad.layer.value)
        cells = _capsule_cells(self.grid, pad.x, pad.y, x, y, self.stub_probe)
        return self._cells_free(layer, cells)

    # -- search --------------------------------------------------------------
    def _preferred_angle(self, pad: Pad) -> float:
        """Point away from the pad's own component: the fan-out direction.

        The space *between* a component's pad rows is where its signal pins
        cross and escape, so a site there costs routing the most (measured on
        board 06: sites between J1's rows stranded the intra-connector
        ``B7 -> A7`` hop).  Outward from the component centroid is the
        conventional fan-out side for a stitch via.
        """
        body = [p for p in self.pads if p.ref == pad.ref]
        cx = sum(p.x for p in body) / len(body)
        cy = sum(p.y for p in body) / len(body)
        if math.hypot(pad.x - cx, pad.y - cy) < 1e-9:
            return 0.0
        return math.atan2(pad.y - cy, pad.x - cx)

    def find(self, pad: Pad) -> PlaneViaSite | None:
        rules = self.rules
        res = self.grid.resolution
        # Reuse an existing same-net site within reach before claiming a new
        # one: two pads sharing a via block half the board area.  Issue
        # #6413: a shared site whose new stub would block a signal pin's
        # escape is kept only as a fallback behind a clear new site.
        reuse: tuple[int, dict[int, frozenset[int]]] | None = None
        for idx, site in enumerate(self.sites):
            if site.net_name != pad.net_name:
                continue
            if math.hypot(site.x - pad.x, site.y - pad.y) > rules.max_reach:
                continue
            if self._stub_ok(pad, site.x, site.y):
                hits = self._escape_hits(pad, site.x, site.y, disc=False)
                verdict = self._verdict(hits)
                if verdict == "ok":
                    return self._share(idx, pad, hits)
                if reuse is None and verdict == "risky":
                    reuse = (idx, hits)

        preferred = self._preferred_angle(pad)
        angles = sorted(
            (2 * math.pi * k / 16 for k in range(16)),
            key=lambda a: (abs(math.remainder(a - preferred, 2 * math.pi)), a),
        )
        if rules.exact_pads:
            return self._find_exact(pad, angles, preferred, reuse)
        # Outside exact mode there are no corridors, so ``reuse`` is unset.
        start = _circumscribed_radius(pad) + self.via_r
        seen: set[tuple[int, int]] = set()
        steps = int(math.ceil(max(0.0, rules.max_reach - start) / res))
        for step in range(steps + 1):
            dist = start + step * res
            for angle in angles:
                gx, gy = self.grid.world_to_grid(
                    pad.x + dist * math.cos(angle), pad.y + dist * math.sin(angle)
                )
                if (gx, gy) in seen:
                    continue
                seen.add((gx, gy))
                x, y = self.grid.grid_to_world(gx, gy)
                if math.hypot(x - pad.x, y - pad.y) < start:
                    continue
                if self._via_ok(pad, x, y) and self._stub_ok(pad, x, y):
                    site = PlaneViaSite(pad.net_name, x, y, (_pad_name(pad),))
                    self.sites.append(site)
                    return site
        return None

    def _find_exact(
        self,
        pad: Pad,
        angles: list[float],
        preferred: float,
        reuse: tuple[int, dict[int, frozenset[int]]] | None = None,
    ) -> PlaneViaSite | None:
        """Issue #6401: candidate order for ``exact_pads``.

        Candidates lie on the same 16 rays from the pad centre, out to
        ``max_reach``, but are tried by the gap between the via copper and
        the pad's own copper box first (one grid step per bucket) and by
        deviation from the fan-out direction second.  Walking by centre
        distance instead would always try the pad's short side first -- the
        gap between neighbouring pins, where the site costs routing most --
        before the outward end of the pad at the same clearance.

        Issue #6413 (escape-corridor guard): a candidate that would leave a
        signal pin's straight outward corridor blocked from BOTH sides,
        counting the sites already placed, is never taken -- if nothing else
        is legal the pad stays unreserved and falls back to the post-route
        ladders.  A candidate entering the corridor of a pin that served pads
        could still flank (``at_risk``) is tried only after every other
        candidate (and after sharing an existing site, ``reuse``).  A
        one-sided block on a pin that cannot be flanked keeps its #6401 rank
        and is recorded.  Once the guard has skipped a legal candidate,
        candidates in the inward half-plane (under the body) wait until the
        outward half is exhausted, so the guard never trades a pin's outward
        escape for a via under the part.  When the guard skips nothing the
        order is exactly the #6401 order.
        """
        rules = self.rules
        res = self.grid.resolution
        rank = {angle: i for i, angle in enumerate(angles)}
        candidates: dict[tuple[int, int], tuple[int, int]] = {}
        steps = int(math.ceil(rules.max_reach / res))
        for angle in angles:
            for step in range(1, steps + 1):
                dist = step * res
                gx, gy = self.grid.world_to_grid(
                    pad.x + dist * math.cos(angle), pad.y + dist * math.sin(angle)
                )
                x, y = self.grid.grid_to_world(gx, gy)
                if math.hypot(x - pad.x, y - pad.y) > rules.max_reach + 1e-9:
                    continue
                gap = _point_box_distance(pad, x, y) - self.via_r
                if gap < -1e-9:
                    continue  # the via copper would overlap the pad
                key = (int(round(gap / res)), rank[angle])
                if (gx, gy) not in candidates or key < candidates[(gx, gy)]:
                    candidates[(gx, gy)] = key
        last_resort: list[tuple[float, float, dict[int, frozenset[int]]]] = []
        inward: list[tuple[float, float, dict[int, frozenset[int]]]] = []
        guard_moved = False
        for (gx, gy), key in sorted(candidates.items(), key=lambda kv: (kv[1], kv[0])):
            x, y = self.grid.grid_to_world(gx, gy)
            hits = self._escape_hits(pad, x, y)
            verdict = self._verdict(hits)
            if verdict != "ok":
                # Only a LEGAL candidate the guard skips changes the outcome.
                if self._via_ok(pad, x, y) and self._stub_ok(pad, x, y):
                    guard_moved = True
                    if verdict == "risky":
                        last_resort.append((x, y, hits))
                continue
            if guard_moved and (
                abs(math.remainder(angles[key[1]] - preferred, 2 * math.pi)) > math.pi / 2 + 1e-9
            ):
                # The guard skipped a nearer outward site: do not let that
                # pull the site under the body (the inward half-plane, where
                # pins escape and ``via_under_body`` objects).  Outward sites
                # further out are tried first; these come next.
                inward.append((x, y, hits))
                continue
            if self._via_ok(pad, x, y) and self._stub_ok(pad, x, y):
                return self._place(pad, x, y, hits)
        for x, y, hits in inward:
            if self._via_ok(pad, x, y) and self._stub_ok(pad, x, y):
                return self._place(pad, x, y, hits)
        if reuse is not None:
            return self._share(reuse[0], pad, reuse[1])
        if last_resort:
            x, y, hits = last_resort[0]
            return self._place(pad, x, y, hits)
        return None


def _plane_pads(grid: RoutingGrid, nets: Sequence[str]) -> list[Pad]:
    wanted = set(nets)
    pads = [
        p
        for p in grid._pads
        if p.net_name in wanted and not p.through_hole and not p.steiner_point and p.ref
    ]
    return sorted(pads, key=lambda p: (p.net_name, p.ref, p.pin, p.x, p.y))


def select_plane_via_pads(
    pads: Sequence[Pad],
    nets: Iterable[str],
    *,
    min_footprint_pads: int = 3,
    tolerance: float = 0.05,
) -> tuple[list[Pad], dict[str, str]]:
    """Pick the plane-net SMD pads a pre-route reservation should serve (#6401).

    The #5700 caveat as a geometric rule (see the module docstring): a pad
    is served only when it is an SMD pad of one of ``nets`` on the
    *perimeter* of a footprint with at least ``min_footprint_pads`` pads,
    and that footprint is not an area array.

    A pad is on the perimeter when its centre lies within ``tolerance`` of
    an edge of the bounding box of its footprint's pad centres.  A footprint
    is an area array when more than one of its pads is strictly inside that
    box -- counted by pad number, so an exposed pad split into same-numbered
    sub-pads is still one pad; a single interior pad is an exposed/thermal
    pad and only that pad is skipped.

    Args:
        pads: Every pad on the board (all nets: the footprint geometry needs
            the signal pins too).
        nets: Plane-net names whose pads may be served.
        min_footprint_pads: Smallest footprint (pad count) considered.
        tolerance: Perimeter tolerance in mm.

    Returns:
        ``(selected, skipped)``: the served pads in deterministic order, and
        ``REF.PIN -> reason`` for each candidate pad left out.
    """
    wanted = set(nets)
    by_ref: dict[str, list[Pad]] = {}
    for pad in pads:
        if pad.ref and not pad.steiner_point:
            by_ref.setdefault(pad.ref, []).append(pad)

    selected: list[Pad] = []
    skipped: dict[str, str] = {}
    for ref in sorted(by_ref):
        body = by_ref[ref]
        candidates = [p for p in body if p.net_name in wanted and not p.through_hole]
        if not candidates:
            continue
        # Distinct pad positions: a footprint may repeat a pad number.
        positions = {(round(p.x, 4), round(p.y, 4)) for p in body}
        if len(positions) < min_footprint_pads:
            for p in candidates:
                skipped[_pad_name(p)] = f"footprint has {len(positions)} pad(s)"
            continue
        x0 = min(x for x, _ in positions)
        x1 = max(x for x, _ in positions)
        y0 = min(y for _, y in positions)
        y1 = max(y for _, y in positions)

        def interior(x: float, y: float) -> bool:
            return x0 + tolerance < x < x1 - tolerance and y0 + tolerance < y < y1 - tolerance

        # Count interior pad NUMBERS, not copper pieces: an exposed pad split
        # into thermal sub-pads repeats one number and its paste apertures
        # carry none (board 05's U2/U3), while a BGA field numbers every ball.
        interior_numbers = {p.pin for p in body if p.pin and interior(p.x, p.y)}
        if len(interior_numbers) > 1:
            for p in candidates:
                skipped[_pad_name(p)] = (
                    f"area-array footprint ({len(interior_numbers)} interior pads)"
                )
            continue
        for p in candidates:
            if interior(p.x, p.y):
                skipped[_pad_name(p)] = "interior (exposed) pad"
            else:
                selected.append(p)
    selected.sort(key=lambda p: (p.net_name, p.ref, p.pin, p.x, p.y))
    return selected, skipped


def _regular_polygon(
    cx: float, cy: float, radius: float, sides: int = 24
) -> tuple[tuple[float, float], ...]:
    """A polygon that CONTAINS the circle of ``radius`` (circumscribed)."""
    r = radius / math.cos(math.pi / sides)
    return tuple(
        (cx + r * math.cos(2 * math.pi * k / sides), cy + r * math.sin(2 * math.pi * k / sides))
        for k in range(sides)
    )


def _capsule_polygon(
    ax: float, ay: float, bx: float, by: float, radius: float, arc_sides: int = 8
) -> tuple[tuple[float, float], ...]:
    """A polygon that CONTAINS the capsule of ``radius`` around ``a-b``."""
    length = math.hypot(bx - ax, by - ay)
    if length < 1e-9:
        return _regular_polygon(ax, ay, radius)
    theta = math.atan2(by - ay, bx - ax)
    # Circumscribe each half-disc cap: vertices at radius / cos(step / 2).
    step = math.pi / arc_sides
    r = radius / math.cos(step / 2)
    points: list[tuple[float, float]] = []
    # Cap around b: from theta - 90deg to theta + 90deg.
    points.append(
        (bx + radius * math.cos(theta - math.pi / 2), by + radius * math.sin(theta - math.pi / 2))
    )
    for k in range(arc_sides):
        ang = theta - math.pi / 2 + step * (k + 0.5)
        points.append((bx + r * math.cos(ang), by + r * math.sin(ang)))
    points.append(
        (bx + radius * math.cos(theta + math.pi / 2), by + radius * math.sin(theta + math.pi / 2))
    )
    # Cap around a: from theta + 90deg to theta + 270deg.
    points.append(
        (ax + radius * math.cos(theta + math.pi / 2), ay + radius * math.sin(theta + math.pi / 2))
    )
    for k in range(arc_sides):
        ang = theta + math.pi / 2 + step * (k + 0.5)
        points.append((ax + r * math.cos(ang), ay + r * math.sin(ang)))
    points.append(
        (
            ax + radius * math.cos(theta + 3 * math.pi / 2),
            ay + radius * math.sin(theta + 3 * math.pi / 2),
        )
    )
    return tuple(points)


def plane_via_keepout_areas(
    reservation: PlaneViaReservation,
    *,
    clearance: float,
    layer_index: Callable[[object], int],
    num_layers: int,
) -> list[Any]:
    """Project a reservation onto the lattice engine's keep-out model (#6401).

    Each reserved site becomes a disc that blocks tracks and vias on every
    layer (the via barrel spans the stack), and each stub a capsule that
    blocks tracks and vias on the pad's layer.  The lattice keep-out test is
    a zero-clearance copper-edge test, so both shapes are the reserved
    copper grown by ``clearance`` -- signal copper then keeps that clearance
    from the via and the stub, as it would from placed copper.

    Args:
        reservation: The planned reservation (sites and stubs).
        clearance: Copper clearance a signal net keeps from the reserved
            via and stub (mm).
        layer_index: Maps a pad's :class:`~.layers.Layer` to the lattice
            layer index.
        num_layers: Copper layers in the stack.

    Returns:
        ``KeepoutArea`` instances, sites first, in reservation order.
    """
    from .lattice.obstacles import KeepoutArea

    rules = reservation.rules
    all_layers = frozenset(range(num_layers))
    areas: list[Any] = []
    for site in reservation.sites:
        areas.append(
            KeepoutArea(
                polygon=_regular_polygon(site.x, site.y, rules.via_diameter / 2.0 + clearance),
                layers=all_layers,
                blocks_tracks=True,
                blocks_vias=True,
                name=f"plane-via-site:{site.net_name}:{'/'.join(site.pads)}",
            )
        )
    for stub in reservation.stubs:
        try:
            layer = int(layer_index(stub.layer))
        except Exception:
            layers = all_layers
        else:
            layers = frozenset({layer})
        areas.append(
            KeepoutArea(
                polygon=_capsule_polygon(
                    stub.pad_x,
                    stub.pad_y,
                    stub.site_x,
                    stub.site_y,
                    rules.stub_width / 2.0 + clearance,
                ),
                layers=layers,
                blocks_tracks=True,
                blocks_vias=True,
                name=f"plane-via-stub:{stub.net_name}:{stub.pad}",
            )
        )
    return areas


def reserve_plane_via_sites(
    grid: RoutingGrid,
    nets: Sequence[str],
    rules: PlaneViaSiteRules | None = None,
    *,
    pads: Sequence[Pad] | None = None,
) -> PlaneViaReservation:
    """Reserve one legal via site (and stub) per plane-net SMD pad.

    Must run on the pre-route grid (after pads are loaded, before any
    routing), so the reservation is part of the static obstacle snapshot
    that rip-up restores.  The marked cells are ``blocked`` with net ``0`` --
    the same encoding ``skip_nets`` gives the plane pads themselves -- so
    every routed net treats the site like plane copper, in both the Python
    pathfinder and the attached C++ grid.

    Args:
        grid: The routing grid, pads already added.
        nets: Plane-net names whose SMD pads need a reserved site.
        rules: Site geometry; defaults to :class:`PlaneViaSiteRules`.
        pads: Issue #6401 -- serve exactly these pads (e.g. the output of
            :func:`select_plane_via_pads`) instead of every SMD pad of
            ``nets``.

    Returns:
        A :class:`PlaneViaReservation`.
    """
    rules = rules or PlaneViaSiteRules()
    targets = list(pads) if pads is not None else _plane_pads(grid, nets)
    planner = _SitePlanner(grid, rules, set(nets) | {p.net_name for p in targets}, targets)
    result = PlaneViaReservation(rules=rules)
    served: list[tuple[Pad, PlaneViaSite]] = []
    for pad in targets:
        site = planner.find(pad)
        if site is None:
            result.unreserved.append(_pad_name(pad))
        else:
            served.append((pad, site))
    result.sites = list(planner.sites)
    result.blocked_escapes = list(planner.blocked_escapes)

    # Resolve each served pad to its FINAL site (a later pad may have been
    # appended to a shared site after this pad was served).
    by_pad = {name: site for site in result.sites for name in site.pads}

    to_block: set[tuple[int, int, int]] = set()
    for site in result.sites:
        gx, gy = grid.world_to_grid(site.x, site.y)
        for layer in planner.all_layers:
            for dx, dy in planner.disc:
                to_block.add((layer, gx + dx, gy + dy))
    for pad, _site in served:
        site = by_pad[_pad_name(pad)]
        result.stubs.append(
            PlaneViaStub(
                pad=_pad_name(pad),
                net_name=pad.net_name,
                layer=pad.layer,
                pad_x=pad.x,
                pad_y=pad.y,
                site_x=site.x,
                site_y=site.y,
            )
        )
        layer = grid.layer_to_index(pad.layer.value)
        for gx, gy in _capsule_cells(
            grid, pad.x, pad.y, site.x, site.y, rules.stub_width / 2.0 + planner.halo
        ):
            to_block.add((layer, gx, gy))

    result.cells_blocked = grid.mark_static_keepout_cells(sorted(to_block))
    return result


def reserve_router_plane_via_sites(
    router: Any,
    nets: Iterable[str],
    *,
    via_diameter: float | None = None,
    via_drill: float | None = None,
    clearance: float | None = None,
    stub_width: float | None = None,
) -> PlaneViaReservation:
    """Reserve pour-pad via sites on a loaded ``Autorouter`` (Issue #6401).

    The ``kct route`` entry point, for both engines.  Runs on the pre-route
    board (pads loaded, nothing routed):

    1. picks the pads with :func:`select_plane_via_pads`;
    2. plans one legal site plus stub per pad with the #5700 planner on
       ``router.grid`` and marks those cells as static grid keep-outs (what
       the grid engine and every grid-based pass reads);
    3. stores the same sites as lattice keep-out areas on
       ``router._plane_via_keepout_areas``, which
       ``Autorouter._negotiate_lattice_netset`` installs next to the board's
       rule areas (the lattice engine never reads the grid's occupancy).

    The site geometry is the via the post-route oracle places
    (``via_diameter`` / ``via_drill``, defaulting to the router's rules) at
    the strongest clearance any reserved net needs: the router clearance,
    ``clearance`` and each reserved net's class clearance (#6374: pour
    copper keeps its class clearance).

    Args:
        router: The loaded :class:`~kicad_tools.router.core.Autorouter`.
        nets: Pour-net names the router skipped.
        via_diameter: Via copper diameter the post-route pass places (mm).
        via_drill: That via's drill (mm).
        clearance: Copper clearance floor (mm), e.g. ``--clearance``.
        stub_width: Pad-to-via stub width (mm).

    Returns:
        The reservation (empty when there are no eligible pads).
    """
    grid = router.grid
    nets = sorted(set(nets))
    pads, _skipped = select_plane_via_pads(list(grid._pads), nets)
    if not pads:
        router._plane_via_keepout_areas = ()
        return PlaneViaReservation()

    drules = router.rules
    gap = max(float(drules.trace_clearance), float(clearance or 0.0))
    class_map = getattr(router, "net_class_map", None) or {}
    for name in nets:
        net_class = class_map.get(name)
        class_clearance = getattr(net_class, "clearance", None) if net_class else None
        if class_clearance:
            gap = max(gap, float(class_clearance))
    defaults = PlaneViaSiteRules()
    rules = PlaneViaSiteRules(
        via_diameter=max(float(via_diameter or 0.0), float(drules.via_diameter)),
        via_drill=max(float(via_drill or 0.0), float(drules.via_drill)),
        clearance=gap,
        hole_to_hole=defaults.hole_to_hole,
        stub_width=float(stub_width or drules.trace_width),
        max_reach=defaults.max_reach,
        exact_pads=True,
    )
    reservation = reserve_plane_via_sites(grid, nets, rules, pads=pads)

    stack = getattr(router, "layer_stack", None)

    def layer_index(layer: object) -> int:
        if stack is not None:
            return int(stack.layer_enum_to_index(layer))
        return int(grid.layer_to_index(layer.value))  # type: ignore[attr-defined]

    router._plane_via_keepout_areas = tuple(
        plane_via_keepout_areas(
            reservation,
            clearance=gap,
            layer_index=layer_index,
            num_layers=int(grid.num_layers),
        )
    )
    return reservation
