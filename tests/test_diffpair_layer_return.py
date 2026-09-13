"""One-via landings retain pad, partner-barrel and hole clearances."""

import time
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

from kicad_tools.router.diffpair_routing import CoupledPathfinder, DiffPairRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _case():
    rules = DesignRules(grid_resolution=0.1, manufacturer="jlcpcb")
    grid = RoutingGrid(
        width=8, height=6, rules=rules, layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    head = Pad(x=2, y=3, width=0.3, height=0.3, net=1, net_name="P", layer=Layer.IN1_CU)
    goal = Pad(x=5, y=3, width=0.3, height=0.3, net=1, net_name="P", layer=Layer.F_CU)
    grid.add_pad(head)
    grid.add_pad(goal)
    router = DiffPairRouter.__new__(DiffPairRouter)
    router.autorouter = SimpleNamespace(
        grid=grid, rules=rules, pads={}, routes=[], net_class_map={}
    )
    router._shadow_foreign_universe = None
    router._shadow_via_gate_rejections = 0
    finder = CoupledPathfinder(grid, rules, target_spacing_cells=3, min_spacing_cells=2)
    return router, finder, head, goal, Route(net=2, net_name="N"), Route(net=1, net_name="P")


def test_one_via_landing_connects_both_layers_with_a_through_barrel():
    router, finder, head, goal, partner, body = _case()
    tail = next(router._layer_return_tails(finder, head, goal, partner, body))
    assert len(tail.vias) == 1
    assert set(tail.vias[0].layers) == {Layer.F_CU, Layer.B_CU}
    assert (tail.segments[0].x1, tail.segments[0].y1) == pytest.approx((head.x, head.y))
    assert (tail.segments[-1].x2, tail.segments[-1].y2) == pytest.approx((goal.x, goal.y))
    assert {s.layer for s in tail.segments} == {head.layer, goal.layer}
    assert router.autorouter.routes == []


def test_uncommitted_partner_barrel_at_goal_prevents_landing():
    router, finder, head, goal, partner, body = _case()
    assert next(router._layer_return_tails(finder, head, goal, partner, body), None) is not None
    partner.vias.append(
        Via(
            x=goal.x,
            y=goal.y,
            diameter=0.6,
            drill=0.3,
            layers=(Layer.F_CU, Layer.B_CU),
            net=2,
            net_name="N",
        )
    )
    assert list(router._layer_return_tails(finder, head, goal, partner, body)) == []


def test_expired_deadline_produces_no_candidate():
    router, finder, head, goal, partner, body = _case()
    assert (
        list(
            router._layer_return_tails(
                finder, head, goal, partner, body, deadline=time.monotonic() - 1
            )
        )
        == []
    )


def test_uncommitted_own_body_drill_is_not_reused():
    router, finder, head, goal, partner, body = _case()
    first = next(router._layer_return_tails(finder, head, goal, partner, body)).vias[0]
    body.vias.append(first)
    candidates = list(router._layer_return_tails(finder, head, goal, partner, body))
    assert candidates
    for candidate in candidates:
        via = candidate.vias[0]
        gap = ((via.x - first.x) ** 2 + (via.y - first.y) ** 2) ** 0.5 - (
            via.drill + first.drill
        ) / 2
        assert gap >= finder.rules.min_hole_to_hole - 1e-9


def test_return_barrel_clears_foreign_copper_on_unused_bottom_layer():
    router, finder, head, goal, partner, body = _case()
    first = next(router._layer_return_tails(finder, head, goal, partner, body)).vias[0]
    obstacle = Segment(
        x1=first.x - 0.1,
        y1=first.y,
        x2=first.x + 0.1,
        y2=first.y,
        width=0.2,
        layer=Layer.B_CU,
        net=3,
        net_name="foreign",
    )
    router.autorouter.routes.append(Route(net=3, net_name="foreign", segments=[obstacle]))
    candidates = list(router._layer_return_tails(finder, head, goal, partner, body))
    assert candidates
    for candidate in candidates:
        via = candidate.vias[0]
        clearance = (
            router._point_segment_distance(via.x, via.y, obstacle)
            - (via.diameter + obstacle.width) / 2
        )
        assert clearance >= finder.rules.via_clearance - 1e-9


@pytest.mark.parametrize("pad_gap,allowed", [(0.175, False), (0.225, True)])
def test_return_barrel_uses_via_clearance_for_foreign_pad(monkeypatch, pad_gap, allowed):
    router, finder, head, goal, partner, body = _case()
    finder.rules.trace_clearance = 0.15
    finder.rules.via_clearance = 0.2
    grid = router.autorouter.grid
    first = next(router._layer_return_tails(finder, head, goal, partner, body)).vias[0]
    site = grid.world_to_grid(first.x, first.y)
    foreign_width = 0.2
    grid.add_pad(
        Pad(
            x=first.x + first.diameter / 2 + pad_gap + foreign_width / 2,
            y=first.y,
            width=foreign_width,
            height=0.2,
            net=3,
            net_name="foreign",
            layer=Layer.B_CU,
        )
    )
    # Isolate the exact numerical gate from raster rounding/carve-outs.
    # Only this known off-pad via site is offered; the foreign pad occupies
    # the unused bottom layer, so it affects the barrel, not either tail.
    monkeypatch.setattr(finder, "_is_via_blocked", lambda x, y, net: (x, y) != site)
    monkeypatch.setattr(router, "_via_has_only_pad_blockers", lambda *args: False)

    candidates = list(router._layer_return_tails(finder, head, goal, partner, body))

    assert bool(candidates) is allowed
    for candidate in candidates:
        assert (candidate.vias[0].x, candidate.vias[0].y) == pytest.approx((first.x, first.y))


def test_return_tail_checks_exact_clearance_between_sampling_points():
    router, finder, head, goal, partner, body = _case()
    required = router._pair_seg_clearance(finder, head.net_name)
    assert required == pytest.approx(0.4)
    stub = Segment(
        x1=2.025,
        y1=3.3999,
        x2=2.025,
        y2=3.4009,
        width=0.2,
        layer=head.layer,
        net=partner.net,
        net_name=partner.net_name,
    )
    partner.segments.append(stub)
    # A 0.05mm sampler measures 0.400532mm against the old candidate's
    # (2,3)->(2.475,3) segment, but its true distance is only 0.3999mm.
    obstacle = LineString([(stub.x1, stub.y1), (stub.x2, stub.y2)])

    candidates = list(router._layer_return_tails(finder, head, goal, partner, body))

    assert candidates
    for candidate in candidates:
        for segment in candidate.segments:
            if segment.layer == stub.layer:
                copper = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)])
                assert copper.distance(obstacle) >= required - 1e-9


def _interpad_case(keepout_order=None):
    router, finder, head, goal, partner, body = _case()
    grid = router.autorouter.grid
    if keepout_order == "before":
        grid.add_keepout(3.5, 2.7, 3.5, 2.7)
    for x in (3.15, 4.45):
        for y in (2.35, 3.65):
            grid.add_pad(
                Pad(x=x, y=y, width=0.45, height=0.45, net=3, net_name="foreign", layer=Layer.F_CU)
            )
    if keepout_order == "after":
        grid.add_keepout(3.5, 2.7, 3.5, 2.7)
    return router, finder, head, goal, partner, body


def test_exact_pad_landing_recovers_clear_interpad_site():
    router, finder, head, goal, partner, body = _interpad_case()
    grid = router.autorouter.grid
    gx, gy = grid.world_to_grid(3.8, 3)
    assert finder._is_via_blocked(gx, gy, head.net)
    assert router._via_has_only_pad_blockers(finder, gx, gy)
    candidates = list(router._layer_return_tails(finder, head, goal, partner, body))
    assert any((t.vias[0].x, t.vias[0].y) == pytest.approx((3.8, 3)) for t in candidates)


@pytest.mark.parametrize("order", ["before", "after"])
def test_keepout_overlapping_pad_halo_never_acquires_pad_provenance(order):
    router, finder, head, goal, partner, body = _interpad_case(order)
    grid = router.autorouter.grid
    gx, gy = grid.world_to_grid(3.8, 3)
    assert not router._via_has_only_pad_blockers(finder, gx, gy)
    candidates = list(router._layer_return_tails(finder, head, goal, partner, body))
    assert not any((t.vias[0].x, t.vias[0].y) == pytest.approx((3.8, 3)) for t in candidates)


def test_old_grid_without_pad_provenance_cannot_use_exact_exception():
    router, finder, head, goal, partner, body = _interpad_case()
    grid = router.autorouter.grid
    del grid._pad_geometry_cells
    assert not router._via_has_only_pad_blockers(finder, *grid.world_to_grid(3.8, 3))


@pytest.mark.parametrize("blocker", ["route", "region", "obstacle", "unknown"])
def test_other_blocking_writes_revoke_pad_geometry_provenance(blocker):
    from kicad_tools.router.primitives import Obstacle

    router, finder, head, goal, partner, body = _interpad_case()
    grid = router.autorouter.grid
    site = grid.world_to_grid(3.8, 3)
    assert router._via_has_only_pad_blockers(finder, *site)
    if blocker == "route":
        grid.mark_route(
            Route(
                net=4,
                net_name="other",
                segments=[
                    Segment(x1=3.5, y1=2.7, x2=3.6, y2=2.7, width=0.2, layer=Layer.F_CU, net=4)
                ],
            )
        )
    elif blocker == "region":
        grid.mark_region_bound(0, 0, 3.4, 6)
    elif blocker == "obstacle":
        grid.add_obstacle(Obstacle(x=3.5, y=2.7, width=0.1, height=0.1, layer=Layer.F_CU))
    else:
        x, y = grid.world_to_grid(3.5, 2.7)
        grid.cell_at(0, y, x).blocked = True
    assert not router._via_has_only_pad_blockers(finder, *site)


def test_stitch_reservation_does_not_acquire_geometry_only_provenance():
    router, finder, head, goal, partner, body = _case()
    grid = router.autorouter.grid
    grid.rules.stitch_via_halo = False
    pad = Pad(x=4, y=3, width=0.1, height=0.1, net=0, net_name="GND", layer=Layer.F_CU)
    grid.add_pad(pad)
    before = grid._blocked.copy()
    grid.rules.stitch_via_halo = True
    grid.add_pad(pad)
    added = grid._blocked & ~before
    assert added.any()
    assert not any(added[layer, y, x] for layer, y, x in grid._pad_geometry_cells)
