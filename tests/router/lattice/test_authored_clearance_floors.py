"""Authored netclass minima in the lattice engine (Issue #6243).

The lattice already carries per-item clearance (committed copper stores its
own class gap and every predicate takes ``max(own, stored)``) -- the authored
minimum rides that: a connection's own floor raises its clearance, committed
copper stores it, and a floored pad's static keep-out grows by its floor.
The kernel-backed census (``authored_clearance.authored_violations``) is the
referee for everything the engine emits.
"""

from __future__ import annotations

from kicad_tools.router.authored_clearance import authored_violations
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

OUTLINE = [(0.0, 0.0), (14.0, 0.0), (14.0, 10.0), (0.0, 10.0)]


def _pad(x, y, net, ref, *, authored=0.0):
    return Pad(
        x=x,
        y=y,
        width=0.8,
        height=0.8,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
        authored_clearance=authored,
    )


def _route(rules, pads, conns):
    pf = LatticePathfinder(OUTLINE, pads, rules, LayerStack.two_layer(), fine=0.2)
    routes, _stats = pf.route_netset(conns, max_iterations=6)
    return pf, routes


def test_conn_geometry_takes_the_authored_floor():
    rules = DesignRules(trace_clearance=0.15, net_clearance_floors={7: 0.5})
    pf = LatticePathfinder(OUTLINE, [_pad(1, 1, 7, "A")], rules)
    assert pf._conn_geometry(None, 7) == (rules.trace_width / 2.0, 0.5)
    assert pf._conn_geometry(None, 8) == (rules.trace_width / 2.0, 0.15)
    assert pf._conn_geometry(None) == (rules.trace_width / 2.0, 0.15)
    assert pf._fixed_clearance_for(7, None) == 0.5
    assert pf._fixed_clearance_for(8, None, item_floor=0.4) == 0.4


def test_floored_pad_keepout_grows_by_its_floor():
    rules = DesignRules(trace_clearance=0.15, net_clearance_floors={2: 0.6})
    pads = [_pad(5, 5, 2, "S"), _pad(9, 5, 3, "P"), _pad(5, 2, 0, "J", authored=0.45)]
    pf = LatticePathfinder(OUTLINE, pads, rules)
    rects = pf.obstacles.pad_rects
    plain = rects[1][2] - rects[1][0]
    assert abs((rects[0][2] - rects[0][0]) - (plain + 2 * (0.6 - 0.15))) < 1e-9
    assert abs((rects[2][2] - rects[2][0]) - (plain + 2 * (0.45 - 0.15))) < 1e-9
    # No floors at all -> byte-identical model.
    assert LatticePathfinder(OUTLINE, pads[:2], DesignRules())._authored_pad_extra() is None


def test_routed_lattice_copper_keeps_every_authored_minimum():
    rules = DesignRules(trace_clearance=0.15, net_clearance_floors={1: 0.7})
    # The straight A run passes 0.5 mm under B's pads and the straight B run
    # 0.5 mm under J1: both legal at the 0.15 mm base, both illegal here.
    pads = [
        _pad(1.5, 4.0, 1, "A1"),
        _pad(12.5, 4.0, 1, "A2"),
        _pad(4.0, 5.0, 2, "B1"),
        _pad(10.0, 5.0, 2, "B2"),
        # A neutral (skipped-net) pad that keeps its own strict class.
        _pad(7.0, 6.0, 0, "J1", authored=0.6),
    ]
    a1, a2, b1, b2 = pads[:4]
    pf, routes = _route(rules, pads, [((1, 0), a1, a2, None), ((2, 0), b1, b2, None)])
    assert (1, 0) in routes and (2, 0) in routes, pf.failure_reasons
    found = authored_violations(rules.net_clearance_floors, pads, routes.values())
    assert found == [], found


def test_control_without_lattice_floors_would_violate(monkeypatch):
    """The scene above is not vacuous: with the lattice plumbing bypassed the
    same connections ship authored-minimum violations."""
    rules = DesignRules(trace_clearance=0.15, net_clearance_floors={1: 0.7})
    pads = [
        _pad(1.5, 4.0, 1, "A1"),
        _pad(12.5, 4.0, 1, "A2"),
        _pad(4.0, 5.0, 2, "B1"),
        _pad(10.0, 5.0, 2, "B2"),
        _pad(7.0, 6.0, 0, "J1", authored=0.6),
    ]
    a1, a2, b1, b2 = pads[:4]
    monkeypatch.setattr(LatticePathfinder, "_authored_pad_extra", lambda self: None)
    original = LatticePathfinder._conn_geometry
    monkeypatch.setattr(
        LatticePathfinder, "_conn_geometry", lambda self, nc, net=None: original(self, nc)
    )
    _pf, routes = _route(rules, pads, [((1, 0), a1, a2, None), ((2, 0), b1, b2, None)])
    assert authored_violations(rules.net_clearance_floors, pads, routes.values())
