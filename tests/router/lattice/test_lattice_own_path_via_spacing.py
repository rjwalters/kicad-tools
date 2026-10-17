"""Issue #6957: the lattice A* must space a route's OWN vias at the hole floor.

Board 03's VBUS fan-out went F.Cu -> via -> one 0.566 mm B.Cu step -> via ->
F.Cu, leaving two 0.3 mm drills 0.266 mm apart edge-to-edge against a 0.5 mm
``min_hole_to_hole``.  The lattice checked a candidate via only against
COMMITTED copper (``CommittedCopper.via_clear``, memoised per node in
``via_ok``), never against the vias earlier on its own path.  #6464 / #6473
added that check to the grid engines; this is the lattice twin.

The fixture reproduces the hop with a congestion wall: every F.Cu lattice
edge crossing ``x = 5`` carries a huge history penalty, so the cheapest path
dips to B.Cu for the shortest possible step.  Pre-fix that step is a single
0.4 mm lattice edge (0.1 mm hole gap).
"""

from __future__ import annotations

import itertools
import math

from kicad_tools.router.lattice.pathfinder import LatticePathfinder, _RouteResult
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules

_OUTLINE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
_WALL_X = 5.0


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        min_hole_to_hole=0.5,
    )


def _fixture() -> tuple[LatticePathfinder, Pad, Pad, dict]:
    start = Pad(4.0, 5.0, 0.6, 0.6, 1, "VBUS", ref="J1", pin="1", layer=Layer.F_CU)
    end = Pad(6.0, 5.0, 0.6, 0.6, 1, "VBUS", ref="J2", pin="1", layer=Layer.F_CU)
    pf = LatticePathfinder(_OUTLINE, [start, end], _rules(), LayerStack.two_layer())
    lattice = pf.build()
    history: dict = {}
    for key, nbrs in lattice.adj.items():
        for nbr, _len in nbrs:
            ax = lattice.node_point(key)[0]
            bx = lattice.node_point(nbr)[0]
            if min(ax, bx) <= _WALL_X <= max(ax, bx) and ax != bx:
                history[("e", (min(key, nbr), max(key, nbr)), 0)] = 1000.0
    return pf, start, end, history


def _floor(rules: DesignRules) -> float:
    return rules.via_drill + rules.min_hole_to_hole


def _assert_own_vias_spaced(route: Route, rules: DesignRules) -> None:
    sites = [(v.x, v.y) for v in route.vias]
    for a, b in itertools.combinations(sites, 2):
        d = math.dist(a, b)
        assert d >= _floor(rules) - 1e-9, (
            f"own vias {a} and {b} are {d:.3f} mm apart centre-to-centre "
            f"(hole gap {d - rules.via_drill:.3f} mm < {rules.min_hole_to_hole} mm)"
        )


def test_walled_hop_spaces_own_vias_at_hole_floor() -> None:
    pf, start, end, history = _fixture()
    result, reason = pf._route_impl(
        start, end, None, committed=pf._fresh_committed(), history=history, present=1.0
    )
    assert result is not None, reason
    route = result.route
    # The wall forces a layer hop: two vias, both on this one route.
    assert len(route.vias) == 2
    assert {s.layer for s in route.segments} == {Layer.F_CU, Layer.B_CU}
    _assert_own_vias_spaced(route, pf.rules)


def test_own_path_vias_clear_floor_and_stack() -> None:
    pf, *_ = _fixture()
    floor = _floor(pf.rules)
    chain = ((1.0, 1.0), None)
    # Exactly on the floor (float noise included) is legal.
    assert pf._own_path_vias_clear((1.0 + floor, 1.0), chain)
    assert pf._own_path_vias_clear((1.0 + 0.4 + 0.4, 1.0), chain)
    # Board 03's 0.566 mm diagonal hop is not.
    assert not pf._own_path_vias_clear((1.4, 1.4), chain)
    # A via stack at one node is one drill.
    assert pf._own_path_vias_clear((1.0, 1.0), chain)
    # Every earlier via counts, not only the nearest one.
    deep = ((5.0, 5.0), ((1.0, 1.0), None))
    assert not pf._own_path_vias_clear((1.0, 1.5), deep)
    assert pf._own_path_vias_clear((1.0, 3.0), deep)


def test_self_drc_gate_declines_sub_floor_own_vias() -> None:
    pf, start, *_ = _fixture()
    runs = [(0, [(4.0, 5.0), (4.8, 5.0)], [0.2]), (1, [(4.8, 5.0), (5.2, 5.2)], [0.2])]
    runs.append((0, [(5.2, 5.2), (6.0, 5.0)], [0.2]))
    vias = [(4.8, 5.0), (5.2, 5.2)]
    route = pf._emit(start, 1, None, runs, vias)
    assert route is not None
    result = _RouteResult(route, runs, vias, set())
    assert pf._self_drc_reason(result) == "self-drc-own-via-hole-to-hole"
