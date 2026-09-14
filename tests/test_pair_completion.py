import copy
import time
from types import SimpleNamespace

import pytest

from kicad_tools.router.body_planning import PairBody
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer
from kicad_tools.router.pair_completion import complete_pair_body
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting


def case():
    rules = DesignRules(
        grid_resolution=0.1, trace_width=0.15, trace_clearance=0.15, via_clearance=0.2
    )
    nc = NetClassRouting(
        name="pair",
        trace_width=0.15,
        clearance=0.15,
        skew_tolerance_mm=0.05,
        coupled_continuity_threshold=0.85,
    )
    auto = Autorouter(width=80, height=8, rules=rules)
    finder = CoupledPathfinder(
        auto.grid,
        rules,
        target_spacing_cells=4,
        min_spacing_cells=3,
        net_class_map={"P": nc, "N": nc},
    )
    auto.net_class_map = finder.net_class_map
    pads = tuple(
        Pad(x=x, y=y, width=0.3, height=0.3, layer=Layer.F_CU, net=net, net_name=name)
        for net, name, y in ((1, "P", 2), (2, "N", 3))
        for x in (2, 78)
    )
    for pad in pads:
        auto.grid.add_pad(pad)
    routes = []
    for net, name, y, points in (
        (1, "P", 2, [(3, 2), (74, 2)]),
        (2, "N", 3, [(3, 3), (4, 3), (4, 2.4), (74, 2.4)]),
    ):
        segments = [
            Segment(x1=2, y1=y, x2=3, y2=y, width=0.15, layer=Layer.F_CU, net=net, net_name=name)
        ]
        segments += [
            Segment(
                x1=a[0],
                y1=a[1],
                x2=b[0],
                y2=b[1],
                width=0.15,
                layer=Layer.B_CU,
                net=net,
                net_name=name,
            )
            for a, b in zip(points[:-1], points[1:], strict=True)
        ]
        routes.append(
            Route(
                net=net,
                net_name=name,
                segments=segments,
                vias=[
                    Via(
                        x=3,
                        y=y,
                        diameter=0.6,
                        drill=0.3,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=net,
                        net_name=name,
                    )
                ],
            )
        )
    body = PairBody(*routes, auto.grid.world_to_grid(74, 2), auto.grid.world_to_grid(74, 2.4))
    pair = SimpleNamespace(positive=SimpleNamespace(net_id=1), negative=SimpleNamespace(net_id=2))
    sites = tuple(frozenset({auto.grid.world_to_grid(76.2, y)}) for y in (2, 3))
    return auto, finder, pair, pads, body, sites


def test_completes_and_tunes_without_mutating_body_or_committing_occupancy():
    auto, finder, pair, pads, body, sites = case()
    original = copy.deepcopy(body)
    result = complete_pair_body(
        auto._diffpair,
        finder,
        pair,
        pads,
        body,
        deadline=time.monotonic() + 5,
        board_thickness_mm=1.6,
        num_copper_layers=2,
        allowed_via_sites=sites,
    )
    assert result is not None
    assert body == original and not auto.routes and not auto.grid.routes
    assert auto._diffpair._shadow_foreign_universe is None
    assert all(len(r.vias) == 2 for r in result)


@pytest.mark.parametrize("expired", [False, True])
def test_no_search_after_deadline_or_empty_site_plan(monkeypatch, expired):
    auto, finder, pair, pads, body, sites = case()
    if expired:

        def unexpected(*args, **kwargs):
            raise AssertionError("terminal synthesis after deadline")

        monkeypatch.setattr(auto._diffpair, "_layer_return_tails", unexpected)
    else:
        sites = (frozenset(), frozenset())
    assert (
        complete_pair_body(
            auto._diffpair,
            finder,
            pair,
            pads,
            body,
            deadline=0 if expired else time.monotonic() + 5,
            board_thickness_mm=1.6,
            num_copper_layers=2,
            allowed_via_sites=sites,
        )
        is None
    )
    assert auto._diffpair._shadow_foreign_universe is None


def test_rejects_invalid_geometry_introduced_during_tuning(monkeypatch):
    from dataclasses import replace

    from kicad_tools.router import pair_completion

    auto, finder, pair, pads, body, sites = case()
    original = copy.deepcopy(body)
    tune = pair_completion.tune_diff_pair_skew
    calls = []

    def corrupt(*args, **kwargs):
        p, n, result = tune(*args, **kwargs)
        p = copy.deepcopy(p)
        p.segments.append(replace(p.segments[0], x1=40, x2=41, y1=6, y2=6))
        calls.append(True)
        return p, n, result

    monkeypatch.setattr(pair_completion, "tune_diff_pair_skew", corrupt)
    assert (
        complete_pair_body(
            auto._diffpair,
            finder,
            pair,
            pads,
            body,
            deadline=time.monotonic() + 5,
            board_thickness_mm=1.6,
            num_copper_layers=2,
            allowed_via_sites=sites,
        )
        is None
    )
    assert calls and body == original
    assert not auto.routes and not auto.grid.routes


def test_expiry_during_tuning_cannot_accept_candidate(monkeypatch):
    from kicad_tools.router import pair_completion

    auto, finder, pair, pads, body, sites = case()
    tune = pair_completion.tune_diff_pair_skew
    now = time.monotonic()
    deadline = now + 5
    clock = [now]
    monkeypatch.setattr(pair_completion, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def expire(*args, **kwargs):
        result = tune(*args, **kwargs)
        clock[0] = deadline
        return result

    monkeypatch.setattr(pair_completion, "tune_diff_pair_skew", expire)
    assert (
        complete_pair_body(
            auto._diffpair,
            finder,
            pair,
            pads,
            body,
            deadline=deadline,
            board_thickness_mm=1.6,
            num_copper_layers=2,
            allowed_via_sites=sites,
        )
        is None
    )
    assert clock[0] == deadline
    assert auto._diffpair._shadow_foreign_universe is None
