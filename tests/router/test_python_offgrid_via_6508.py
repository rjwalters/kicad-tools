"""Issue #6508: a rip-up must not hollow out a surviving via's raster halo.

Board 05's PWM_C fallback built 44 candidates the Python A* admitted and
``_validate_route_clearance`` rejected, all on one segment ending 0.473 mm from
the centre of a committed foreign (off-grid) via.  Instrumenting the first
rejection showed the via's 253-cell halo missing 94 cells on every layer: the
cells had been stamped under another route's ownership (a cell has one owner,
and ``_mark_*`` only claims unblocked cells), and ripping that route up freed
them while the via's own route stayed committed.  The search then saw free
space where the validator measured copper.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid, halo_offsets
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

RES = 0.065
MTW = 0.2


def _build(via_offset, trace_dy):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.15,
        min_hole_to_hole=0.25,
        grid_resolution=RES,
    )
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    a = grid.grid_to_world(30, 40)
    b = grid.grid_to_world(90, 80)
    vx, vy = grid.grid_to_world(60, 60)
    vx, vy = vx + via_offset[0], vy + via_offset[1]
    # N3's trace is committed first and owns the cells the N2 via's halo shares.
    first = Route(net=3, net_name="N3")
    first.segments.append(
        Segment(vx - 3, vy + trace_dy, vx + 3, vy + trace_dy, 0.2, Layer.F_CU, 3, "N3")
    )
    via_route = Route(net=2, net_name="N2")
    via_route.vias.append(Via(vx, vy, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(first, max_trace_width=MTW)
    grid.mark_route(via_route, max_trace_width=MTW)
    grid.unmark_route(first, max_trace_width=MTW)
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2, "N3": 3})
    start = Pad(x=a[0], y=a[1], width=0.1, height=0.1, layer=Layer.F_CU, net=1, net_name="N1")
    end = Pad(x=b[0], y=b[1], width=0.1, height=0.1, layer=Layer.F_CU, net=1, net_name="N1")
    rejected: list[Route] = []
    original = router._validate_route_clearance

    def spy(route, net, *args, **kwargs):
        ok = original(route, net, *args, **kwargs)
        if not ok:
            rejected.append(route)
        return ok

    router._validate_route_clearance = spy  # type: ignore[method-assign]
    return grid, router, start, end, rejected, (vx, vy)


@pytest.mark.parametrize("offset", [(0.03, 0.02), (0.0, 0.0), (-0.025, 0.031)])
def test_ripped_neighbour_leaves_via_halo_intact(offset):
    grid, *_, (vx, vy) = _build(offset, -0.3)
    gx, gy = grid.world_to_grid(vx, vy)
    radius = int((0.3 + 0.15 + MTW / 2) / RES) + 1
    for layer in range(grid.num_layers):
        for dx, dy in halo_offsets(radius):
            assert grid._blocked[layer, gy + dy, gx + dx], (layer, dx, dy)
            assert grid._net[layer, gy + dy, gx + dx] == 2


@pytest.mark.parametrize("method", ["route", "route_bidirectional"])
@pytest.mark.parametrize("trace_dy", [-0.3, -0.4])
def test_python_search_never_builds_a_rejected_candidate(method, trace_dy):
    grid, router, start, end, rejected, _ = _build((0.03, 0.02), trace_dy)
    result = getattr(router, method)(start, end)
    assert not rejected
    assert result is not None
    assert all(grid.validate_segment_clearance(s, 1)[0] for s in result.segments)


def test_rip_up_still_frees_the_ripped_routes_own_cells():
    grid, *_, (vx, vy) = _build((0.03, 0.02), -0.3)
    # Far from the via (the trace ran +-3 mm), the trace's halo is gone.
    gx, gy = grid.world_to_grid(vx + 2.5, vy - 0.3)
    assert not grid._blocked[0, gy, gx]
    assert not grid._net[0, gy, gx]
