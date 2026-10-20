"""Area-array pour clusters stay whole on the lattice engine (Issue #6673).

Board 07rf pours ``+1V8`` on ``F.Cu`` (the third power net of a 4-layer
board), so U4's 13-ball ``+1V8`` field reaches the pour only through the
fill between its own balls.  The #6401 reservation skips area arrays, so a
lattice ``TMDS_D2_P`` leg ran between rows C and D and a TMDS via took the
B/C interstitial, leaving C3/C4 on a 0.53 mm2 fill island.

:func:`area_array_pour_links` marks every row/column-adjacent pair of
same-pour balls on a layer that pour floods, and
:func:`area_array_link_keepout_areas` keeps foreign tracks from crossing
between them and foreign vias off the interstitials beside them.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.lattice.geometry import seg_near_polygon
from kicad_tools.router.lattice.obstacles import LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.plane_via_sites import (
    AreaArrayLink,
    PlaneFill,
    area_array_link_keepout_areas,
    area_array_pour_links,
    plane_fill_pullback,
    reserve_router_plane_via_sites,
)
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules

PITCH = 1.27
CX, CY = 15.0, 10.0
POUR = "+1V8"
POUR_NET = 5
SIG = 1
GND = 3
# The auto-pour zone on 07rf: clearance 0.3 mm, min_thickness 0.25 mm.
FILL = PlaneFill(layer="F.Cu", clearance=0.3, min_thickness=0.25)
PULLBACK = plane_fill_pullback(FILL, 0.2)
STACK = LayerStack.two_layer()
F = STACK.layer_enum_to_index(Layer.F_CU)


def _ball(row: int, col: int, net: int, name: str, *, n: int = 5, ref: str = "U4") -> Pad:
    half = (n - 1) / 2.0
    return Pad(
        x=CX + (col - half) * PITCH,
        y=CY + (row - half) * PITCH,
        width=0.45,
        height=0.45,
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin=f"{'ABCDEFGHJ'[row]}{col + 1}",
    )


def _bga(cluster: set[tuple[int, int]], signal: set[tuple[int, int]] = frozenset()) -> list[Pad]:
    """5x5 BGA: ``cluster`` cells are the pour, ``signal`` cells net SIG, rest GND."""
    pads = []
    for r in range(5):
        for c in range(5):
            if (r, c) in cluster:
                pads.append(_ball(r, c, POUR_NET, POUR))
            elif (r, c) in signal:
                pads.append(_ball(r, c, SIG, "SIG"))
            else:
                pads.append(_ball(r, c, GND, "GND"))
    return pads


INTERIOR = {(r, c) for r in (1, 2, 3) for c in (1, 2, 3)}


def _links(pads: list[Pad], layers: dict[str, list[int]] | None = None):
    return area_array_pour_links(
        pads, {POUR: [F]} if layers is None else layers, STACK.layer_enum_to_index
    )


def test_interior_cluster_links_every_adjacent_pair() -> None:
    links, released = _links(_bga(INTERIOR))
    # 3x3 cluster: 6 row links + 6 column links.
    assert len(links) == 12
    assert released == []
    assert {link.net_name for link in links} == {POUR}
    assert {link.layer for link in links} == {F}
    for link in links:
        assert math.hypot(link.bx - link.ax, link.by - link.ay) == pytest.approx(PITCH)


def test_no_links_when_the_pour_is_on_another_layer() -> None:
    b = STACK.layer_enum_to_index(Layer.B_CU)
    links, _released = _links(_bga(INTERIOR), {POUR: [b]})
    assert links == []


def test_no_links_for_a_perimeter_only_footprint() -> None:
    # A QFP-like ring: no interior pads, so not an area array.
    pads = [p for p in _bga({(0, 1), (0, 2), (0, 3)}) if p.pin[0] in "AE" or p.pin[1] in "15"]
    links, _released = _links(pads)
    assert links == []


def test_rotated_array_indexes_on_its_own_axes() -> None:
    pads = _bga(INTERIOR)
    ang = math.radians(30.0)
    for p in pads:
        dx, dy = p.x - CX, p.y - CY
        p.x = CX + dx * math.cos(ang) - dy * math.sin(ang)
        p.y = CY + dx * math.sin(ang) + dy * math.cos(ang)
    links, _released = _links(pads)
    assert len(links) == 12


def test_a_foreign_ball_sealed_by_the_cluster_keeps_an_exit() -> None:
    # A ring of pour balls around one signal ball in the middle.
    ring = INTERIOR - {(2, 2)}
    links, released = _links(_bga(ring, {(2, 2)}))
    assert released == ["U4.C3"]
    # The links on the sides of C3's four corner interstitials are dropped
    # (all 8 ring links), so it can drop a via beside itself.
    assert links == []


def test_a_foreign_ball_with_an_outside_exit_is_left_alone() -> None:
    # Signal ball on the ring (row A): always free outward.
    links, released = _links(_bga(INTERIOR, {(0, 2)}))
    assert released == []
    assert len(links) == 12


# ---------------------------------------------------------------------------
# Lattice route: a foreign net whose shortest path crosses the cluster.
# ---------------------------------------------------------------------------

OUTLINE = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]


def _route(mask: LatticeKeepoutMask | None):
    # SIG balls A3 and E3, above and below the cluster: the straight path
    # between them runs down a channel through the middle of the cluster.
    pads = _bga(INTERIOR, {(0, 2), (4, 2)})
    pf = LatticePathfinder(OUTLINE, pads, DesignRules(), STACK)
    if mask is not None:
        pf.set_keepouts(mask)
    a, b = (p for p in pf.pads if p.net == SIG)
    conns = [((SIG, 0), a, b, None)]
    routes, stats = pf.route_netset(conns, max_iterations=4)
    return routes, stats


def _crosses_cluster(routes, links: list[AreaArrayLink]) -> bool:
    for route in routes.values():
        assert isinstance(route, Route)
        for seg in route.segments:
            if seg.layer != Layer.F_CU:
                continue
            for link in links:
                if seg_near_polygon(
                    (seg.x1, seg.y1),
                    (seg.x2, seg.y2),
                    ((link.ax, link.ay), (link.bx, link.by), (link.ax, link.ay)),
                    seg.width / 2.0,
                ):
                    return True
    return False


def test_lattice_route_does_not_split_the_cluster() -> None:
    links, _released = _links(_bga(INTERIOR, {(0, 2), (4, 2)}))
    baseline, stats = _route(None)
    assert stats.routed == 1
    # Without the guard the shortest F.Cu path does cut the cluster.
    assert _crosses_cluster(baseline, links)

    areas = area_array_link_keepout_areas(links, pullback={(POUR, F): PULLBACK})
    assert len(areas) == 12
    routes, stats = _route(LatticeKeepoutMask(areas))
    assert stats.routed == 1
    assert not _crosses_cluster(routes, links)
    # And no copper (track edge or via) lands inside any link keep-out.
    for route in routes.values():
        for seg in route.segments:
            if seg.layer != Layer.F_CU:
                continue
            for area in areas:
                assert not seg_near_polygon(
                    (seg.x1, seg.y1), (seg.x2, seg.y2), area.polygon, seg.width / 2.0
                )
        for via in route.vias:
            for area in areas:
                assert not seg_near_polygon(
                    (via.x, via.y), (via.x, via.y), area.polygon, via.diameter / 2.0
                )


def test_router_entry_point_adds_links_without_any_site_pad() -> None:
    rules = DesignRules(
        grid_resolution=0.05, trace_width=0.2, trace_clearance=0.15, via_diameter=0.6
    )
    grid = RoutingGrid(width=30, height=20, rules=rules, layer_stack=STACK)
    for pad in _bga(INTERIOR):
        grid.add_pad(pad)
    router = SimpleNamespace(grid=grid, rules=rules, net_class_map={}, layer_stack=STACK)
    reservation = reserve_router_plane_via_sites(router, [POUR], plane_fills={POUR: [FILL]})
    assert reservation.sites == []
    assert len(reservation.array_links) == 12
    names = {a.name.split(":")[0] for a in router._plane_via_keepout_areas}
    assert names == {"pour-array-link"}
    # Without fill data the pour layer is unknown: nothing is added.
    reservation = reserve_router_plane_via_sites(router, [POUR])
    assert reservation.array_links == []
    assert router._plane_via_keepout_areas == ()
