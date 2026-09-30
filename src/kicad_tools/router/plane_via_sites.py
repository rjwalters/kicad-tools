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
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from kicad_tools.acceleration import to_numpy

from .primitives import Pad, pad_half_extents

if TYPE_CHECKING:
    from .grid import RoutingGrid

__all__ = [
    "PlaneViaSite",
    "PlaneViaSiteRules",
    "PlaneViaReservation",
    "reserve_plane_via_sites",
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
    """

    via_diameter: float = 0.6
    via_drill: float = 0.3
    clearance: float = 0.2
    hole_to_hole: float = 0.5
    stub_width: float = 0.2
    max_reach: float = 2.0


@dataclass(frozen=True)
class PlaneViaSite:
    """One reserved via site and the plane-net pads it serves."""

    net_name: str
    x: float
    y: float
    pads: tuple[str, ...]


@dataclass
class PlaneViaReservation:
    """Result of :func:`reserve_plane_via_sites`.

    Attributes:
        sites: Reserved sites, in deterministic pad order.
        unreserved: ``REF.PIN`` of pads for which no legal site exists on
            the pre-route board (they fall back to the post-route ladders,
            exactly as before this reservation existed).
        cells_blocked: Grid cells newly marked blocked, summed over layers.
    """

    sites: list[PlaneViaSite] = field(default_factory=list)
    unreserved: list[str] = field(default_factory=list)
    cells_blocked: int = 0


def _pad_name(pad: Pad) -> str:
    return f"{pad.ref}.{pad.pin}"


def _circumscribed_radius(pad: Pad) -> float:
    """Conservative round envelope of a pad (covers rectangle corners)."""
    half_w, half_h = pad_half_extents(pad)
    return math.hypot(half_w, half_h)


def _segment_point_distance(
    ax: float, ay: float, bx: float, by: float, px: float, py: float
) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


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

    def __init__(self, grid: RoutingGrid, rules: PlaneViaSiteRules) -> None:
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
        self.all_layers = list(range(grid.num_layers))
        self.sites: list[PlaneViaSite] = []

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

    def _via_ok(self, pad: Pad, x: float, y: float) -> bool:
        rules = self.rules
        for other in self.pads:
            gap = math.hypot(other.x - x, other.y - y) - _circumscribed_radius(other)
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
        disc_cells = [(gx + dx, gy + dy) for dx, dy in self.disc]
        return all(self._cells_free(layer, disc_cells) for layer in self.all_layers)

    def _stub_ok(self, pad: Pad, x: float, y: float) -> bool:
        rules = self.rules
        half = rules.stub_width / 2.0
        for other in self.pads:
            if other.net_name == pad.net_name:
                continue
            if not (other.through_hole or other.layer == pad.layer):
                continue
            dist = _segment_point_distance(pad.x, pad.y, x, y, other.x, other.y)
            if dist - _circumscribed_radius(other) < half + rules.clearance:
                return False
        layer = self.grid.layer_to_index(pad.layer.value)
        cells = _capsule_cells(self.grid, pad.x, pad.y, x, y, half + self.halo)
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
        # one: two pads sharing a via block half the board area.
        for idx, site in enumerate(self.sites):
            if site.net_name != pad.net_name:
                continue
            if math.hypot(site.x - pad.x, site.y - pad.y) > rules.max_reach:
                continue
            if self._stub_ok(pad, site.x, site.y):
                shared = PlaneViaSite(site.net_name, site.x, site.y, site.pads + (_pad_name(pad),))
                self.sites[idx] = shared
                return shared

        preferred = self._preferred_angle(pad)
        angles = sorted(
            (2 * math.pi * k / 16 for k in range(16)),
            key=lambda a: (abs(math.remainder(a - preferred, 2 * math.pi)), a),
        )
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


def _plane_pads(grid: RoutingGrid, nets: Sequence[str]) -> list[Pad]:
    wanted = set(nets)
    pads = [
        p
        for p in grid._pads
        if p.net_name in wanted and not p.through_hole and not p.steiner_point and p.ref
    ]
    return sorted(pads, key=lambda p: (p.net_name, p.ref, p.pin, p.x, p.y))


def reserve_plane_via_sites(
    grid: RoutingGrid,
    nets: Sequence[str],
    rules: PlaneViaSiteRules | None = None,
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

    Returns:
        A :class:`PlaneViaReservation`.
    """
    rules = rules or PlaneViaSiteRules()
    planner = _SitePlanner(grid, rules)
    result = PlaneViaReservation()
    pads = _plane_pads(grid, nets)
    served: list[tuple[Pad, PlaneViaSite]] = []
    for pad in pads:
        site = planner.find(pad)
        if site is None:
            result.unreserved.append(_pad_name(pad))
        else:
            served.append((pad, site))
    result.sites = list(planner.sites)

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
        layer = grid.layer_to_index(pad.layer.value)
        for gx, gy in _capsule_cells(
            grid, pad.x, pad.y, site.x, site.y, rules.stub_width / 2.0 + planner.halo
        ):
            to_block.add((layer, gx, gy))

    result.cells_blocked = grid.mark_static_keepout_cells(sorted(to_block))
    return result
