"""A connection never treats its own net's committed copper as an obstacle (#6566).

A net with N>=3 pads is routed as N-1 star connections sharing one anchor pad.
The mesh negotiator used to hand each later leg the earlier legs' copper as an
obstacle, so its start (inside that capsule) was declined every pass.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytest.importorskip("kicad_tools.router.router_cpp")

_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _pad(x: float, y: float, net: int, ref: str, pin: str = "1") -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _star(pads: list[Pad], net: int):
    return [((net, i), pads[0], p, None) for i, p in enumerate(pads[1:])]


@pytest.mark.parametrize(
    "coords",
    [
        [(2.0, 10.0), (10.0, 10.0), (18.0, 10.0)],  # collinear
        [(2.0, 10.0), (10.0, 10.0), (2.0, 18.0)],  # L-shape
    ],
)
def test_three_pad_net_routes_all_connections(coords) -> None:
    rules = DesignRules()
    pads = [_pad(x, y, 1, f"P{i}") for i, (x, y) in enumerate(coords)]
    pf = MeshPathfinder(_OUTLINE, pads, rules, layer_stack=LayerStack.two_layer())
    routes, stats = pf.route_netset(_star(pads, 1), max_iterations=6)
    assert stats.routed == 2
    assert len(routes) == 2


def test_foreign_net_still_kept_clear() -> None:
    rules = DesignRules()
    net1 = [_pad(2.0, 10.0, 1, "A"), _pad(10.0, 10.0, 1, "B"), _pad(2.0, 18.0, 1, "C")]
    net2 = [_pad(5.0, 14.0, 2, "D"), _pad(12.0, 14.0, 2, "E")]
    pf = MeshPathfinder(_OUTLINE, [*net1, *net2], rules, layer_stack=LayerStack.two_layer())
    conns = [*_star(net1, 1), ((2, 0), net2[0], net2[1], None)]
    routes, stats = pf.route_netset(conns, max_iterations=6)
    assert stats.routed == 3
    gap = rules.trace_width + rules.trace_clearance
    segs1 = [s for r in routes.values() for s in r.segments if s.net == 1]
    segs2 = [s for r in routes.values() for s in r.segments if s.net == 2]
    assert segs1 and segs2
    for a in segs1:
        for b in segs2:
            if a.layer != b.layer:
                continue
            d = seg_seg_dist((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2))
            assert d >= gap - 1e-6


def test_autorouter_mesh_connects_all_pads_of_three_pad_net() -> None:
    router = Autorouter(20, 20, strategy="mesh")
    pads = [_pad(2.0, 10.0, 1, "A"), _pad(10.0, 10.0, 1, "B"), _pad(2.0, 18.0, 1, "C")]
    for p in pads:
        router.pads[(p.ref, p.pin)] = p
        router.nets.setdefault(1, []).append((p.ref, p.pin))
    routes = router.route_all()

    # Union-find over pad positions touched by segment endpoints.
    parent = {i: i for i in range(len(pads))}

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    pts: dict[tuple[float, float], list[int]] = {}
    for r in routes:
        for s in r.segments:
            for pt in ((s.x1, s.y1), (s.x2, s.y2)):
                pts.setdefault((round(pt[0], 3), round(pt[1], 3)), []).append(id(r))
    # Connectivity via shared segment endpoints, with pads attached at endpoints.
    nodes: dict[object, object] = {}

    def f(x):
        nodes.setdefault(x, x)
        while nodes[x] != x:
            x = nodes[x]
        return x

    for r in routes:
        for s in r.segments:
            a, b = ("pt", round(s.x1, 3), round(s.y1, 3)), ("pt", round(s.x2, 3), round(s.y2, 3))
            nodes[f(a)] = f(b)
    for i, p in enumerate(pads):
        near = [k for k in list(nodes) if abs(k[1] - p.x) < 0.6 and abs(k[2] - p.y) < 0.6]
        for k in near:
            nodes[f(("pad", i))] = f(k)
    assert len({f(("pad", i)) for i in range(len(pads))}) == 1


def test_via_keepout_and_standoff_helpers_6566():
    """Placed vias are keep-outs and a via keeps a body standoff from copper."""
    from kicad_tools.router.mesh.pathfinder import (
        _point_polygon_distance,
        _via_keepout,
    )

    octagon = _via_keepout((10.0, 10.0), 0.65)
    # The octagon circumscribes the disc: every edge is >= radius from centre.
    assert _point_polygon_distance((10.0, 10.0), octagon) == 0.0
    assert _point_polygon_distance((10.0 + 0.65 + 0.25, 10.0), octagon) == pytest.approx(
        0.25, abs=1e-6
    )
