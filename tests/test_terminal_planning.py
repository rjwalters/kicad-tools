import time
from dataclasses import replace

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.terminal_planning import PairLanding, landing_proposals, select_landing_plan


def case(turns=0):
    rules = DesignRules(
        grid_resolution=0.1, trace_width=0.15, trace_clearance=0.15, via_clearance=0.2
    )
    auto = Autorouter(width=30, height=30, rules=rules)
    finder = CoupledPathfinder(auto.grid, rules, target_spacing_cells=3, min_spacing_cells=2)
    pads = tuple(
        Pad(x=x, y=y, width=0.4, height=0.4, layer=Layer.F_CU, net=net, net_name=str(net))
        for net, points in ((1, ((5, 20), (20, 18))), (2, ((6, 20), (20, 18.8))))
        for x, y in points
    )

    def rotate(point, center):
        x, y = point[0] - center, point[1] - center
        for _ in range(turns):
            x, y = -y, x
        return x + center, y + center

    pads = tuple(replace(p, x=rotate((p.x, p.y), 15)[0], y=rotate((p.x, p.y), 15)[1]) for p in pads)
    for p in pads:
        auto.grid.add_pad(p)
    return auto, finder, pads, rotate((0, -1), 0), rotate


def proposals(auto, finder, pads, outward):
    return list(
        landing_proposals(
            auto._diffpair, finder, pads, outward=outward, deadline=time.monotonic() + 5
        )
    )


def test_landing_proposals_keep_all_pad_and_partner_barrel_clearances():
    auto, finder, pads, outward, _ = case()
    options = proposals(auto, finder, pads, outward)
    assert options
    for option in options:
        vias = [option.p_reservation.vias[0], option.n_reservation.vias[0]]
        for v in vias:
            assert (
                auto.grid.worst_via_pad_deficit(v, exclude_net=-1, clearance_floor=0.2)[0] <= 1e-9
            )
        assert ((vias[0].x - vias[1].x) ** 2 + (vias[0].y - vias[1].y) ** 2) ** 0.5 >= 0.8 - 1e-9
    assert not auto.routes and not auto.grid.routes
    first = options[0]
    x, y = first.p_site
    # A raw unknown keepout cannot be excused by exact pad geometry.
    auto.grid.cell_at(0, y, x).blocked = True
    assert all(p.p_site != first.p_site for p in proposals(auto, finder, pads, outward))


@pytest.mark.parametrize("turns", [1, 2, 3])
def test_landing_plan_rotates_with_source_frame(turns):
    auto, finder, pads, outward, _ = case()
    original = proposals(auto, finder, pads, outward)
    auto, finder, pads, outward, rotate = case(turns)
    rotated = proposals(auto, finder, pads, outward)
    expected = {(rotate(p.p_site, 150), rotate(p.n_site, 150)) for p in original}
    assert {(p.p_site, p.n_site) for p in rotated} == expected


def landing(net, x):
    routes = [
        Route(
            net=n,
            net_name=str(n),
            vias=[Via(x=x, y=y, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=n)],
        )
        for n, y in ((net, 2), (net + 1, 3))
    ]
    return PairLanding((int(x * 10), 20), (int(x * 10), 30), *routes)


def test_shared_plan_backtracks_and_obeys_exact_choice_cap():
    auto, _, _, _, _ = case()
    first, alternate, second = landing(1, 4), landing(1, 6), landing(3, 4)
    options = [[first, alternate], [second]]
    assert (
        select_landing_plan(auto._diffpair, options, deadline=time.monotonic() + 5, max_choices=2)
        is None
    )
    assert select_landing_plan(
        auto._diffpair, options, deadline=time.monotonic() + 5, max_choices=4
    ) == [alternate, second]
    assert select_landing_plan(
        auto._diffpair, [[alternate], [second]], deadline=time.monotonic() + 5, max_choices=2
    ) == [alternate, second]
    assert not auto.routes and not auto.grid.routes


def test_expired_plan_does_not_enter_geometry_search(monkeypatch):
    auto, finder, pads, outward, _ = case()

    def unexpected(*args, **kwargs):
        raise AssertionError("expired planner checked geometry")

    monkeypatch.setattr(finder, "_is_via_blocked", unexpected)
    assert not list(landing_proposals(auto._diffpair, finder, pads, outward=outward, deadline=0))
    assert (
        select_landing_plan(auto._diffpair, [[landing(1, 4)]], deadline=0, max_choices=10) is None
    )
