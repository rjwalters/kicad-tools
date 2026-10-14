"""Equivalence pins for the ``_trace_halo_clear`` geometry memo (Issue #5617).

The pure-Python A* fallback re-probes the same ``from_cell -> cell`` steps on
an unchanged grid (exhausted-resume and negotiated re-searches).  The memo may
only ever return what a fresh ``RouteHaloGeometry.clear`` would: it is keyed on
the step, the net and its class, and dropped when the halo snapshot
(``state_version``) or the router rule configuration changes.
"""

from __future__ import annotations

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Segment
from kicad_tools.router.rules import DesignRules


def _context() -> tuple[RoutingGrid, Router]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    _add_segment(grid, 50, 60, 80, 60)
    return grid, router


def _add_segment(grid: RoutingGrid, ax: int, ay: int, bx: int, by: int) -> None:
    x1, y1 = grid.grid_to_world(ax, ay)
    x2, y2 = grid.grid_to_world(bx, by)
    route = Route(net=2, net_name="N2")
    route.segments.append(Segment(x1, y1, x2, y2, 0.2, Layer.F_CU, 2, "N2"))
    grid.mark_route(route)


def _fresh(router: Router, layer: int, gx: int, gy: int, net: int, from_cell) -> bool:
    router._trace_halo_cache.clear()
    return router._trace_halo_clear([], layer, gx, gy, net, from_cell)


_STEPS = [
    (gx, gy, (gx - dx, gy - dy))
    for gx in range(58, 72, 2)
    for gy in range(54, 68)
    for dx, dy in ((0, 0), (1, 0), (1, 1))
]


def test_sweep_sees_both_verdicts():
    _, router = _context()
    verdicts = {_fresh(router, 0, gx, gy, 1, fc) for gx, gy, fc in _STEPS}
    assert verdicts == {True, False}


def test_memo_matches_fresh_computation():
    _, router = _context()
    router._trace_halo_cache.clear()
    first = [router._trace_halo_clear([], 0, gx, gy, 1, fc) for gx, gy, fc in _STEPS]
    assert router._trace_halo_cache
    again = [router._trace_halo_clear([], 0, gx, gy, 1, fc) for gx, gy, fc in _STEPS]
    fresh = [_fresh(router, 0, gx, gy, 1, fc) for gx, gy, fc in _STEPS]
    assert first == again == fresh


def test_memo_hits_do_not_recompute():
    _, router = _context()
    halo = router.grid._route_halo
    calls = []
    original = halo.clear
    halo.clear = lambda *a, **k: calls.append(1) or original(*a, **k)  # type: ignore[method-assign]
    for _ in range(3):
        for gx, gy, fc in _STEPS:
            router._trace_halo_clear([], 0, gx, gy, 1, fc)
    assert len(calls) == len({(0, gx, gy, *fc) for gx, gy, fc in _STEPS})


def test_geometry_change_invalidates_memo():
    grid, router = _context()
    for gx, gy, fc in _STEPS:
        router._trace_halo_clear([], 0, gx, gy, 1, fc)
    _add_segment(grid, 50, 56, 80, 56)  # new copper after the memo filled
    after = [router._trace_halo_clear([], 0, gx, gy, 1, fc) for gx, gy, fc in _STEPS]
    assert after == [_fresh(router, 0, gx, gy, 1, fc) for gx, gy, fc in _STEPS]


def test_rule_reset_drops_memo():
    _, router = _context()
    router._trace_halo_clear([], 0, 62, 58, 1, (61, 58))
    assert router._trace_halo_cache
    router.clear_via_cache()
    assert not router._trace_halo_cache
    router._trace_halo_clear([], 0, 62, 58, 1, (61, 58))
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    assert not router._trace_halo_cache


def test_in_place_rule_mutation_is_not_served_stale():
    grid, router = _context()
    steps = _STEPS
    before = [router._trace_halo_clear([], 0, gx, gy, 1, fc) for gx, gy, fc in steps]
    router.rules.trace_clearance = 0.5
    after = [router._trace_halo_clear([], 0, gx, gy, 1, fc) for gx, gy, fc in steps]
    assert after == [_fresh(router, 0, gx, gy, 1, fc) for gx, gy, fc in steps]
    assert after != before
