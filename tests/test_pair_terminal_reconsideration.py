"""A locally preferred first tail can lose once its partner is complete."""

import copy
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from kicad_tools.router.body_planning import PairBody
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.match_group_length import MatchGroupTracker
from kicad_tools.router.pair_completion import complete_pair_body
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting


def terminal_case(loop_depth=6):
    # Reduced captured geometry: two explicit tails on an otherwise empty board.
    # No board/net special cases enter the generator or the acceptance gates.
    rules = DesignRules(
        grid_resolution=0.127,
        trace_width=0.225,
        trace_clearance=0.15,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
    )
    nc = NetClassRouting(
        name="pair",
        trace_width=0.225,
        clearance=0.1,
        intra_pair_clearance=0.1,
        skew_tolerance_mm=0.075,
        coupled_continuity_threshold=0.85,
    )
    auto = Autorouter(
        110,
        95,
        rules=rules,
        origin_x=93.538,
        origin_y=40,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
        force_python=True,
    )
    finder = CoupledPathfinder(
        auto.grid,
        rules,
        target_spacing_cells=3,
        min_spacing_cells=3,
        net_class_map={"P": nc, "N": nc},
    )
    auto.net_class_map = finder.net_class_map
    pads = tuple(
        Pad(x, y, w, h, net, name, ref=ref, pin="1")
        for net, name, x, y, w, h, ref in [
            (1, "P", 153, 95, 0.4, 1, "P1"),
            (1, "P", 177.23, 92.46, 0.45, 0.45, "P2"),
            (2, "N", 154, 95, 0.4, 1, "N1"),
            (2, "N", 178.5, 92.46, 0.45, 0.45, "N2"),
        ]
    )
    for pad in pads:
        auto.grid.add_pad(pad)
    routes = []
    tails = []
    for net, name, front, body, tail, landing in [
        (
            1,
            "P",
            [(153, 95), (152.991, 94.991), (152.974, 94.991), (152.974, 95.88)],
            [
                (152.974, 95.88),
                (152.974, 96.896),
                (156.974, 96.896),
                (156.974, 102.896),
                (160.974, 102.896),
                (160.974, 96.896),
                (175.707, 96.896),
            ],
            [(175.707, 96.896), (178.907, 96.896), (178.907, 91.816), (177.866, 91.816)],
            [(177.866, 91.816), (177.866, 92.46), (177.23, 92.46)],
        ),
        (
            2,
            "N",
            [(154, 95), (153.991, 94.991), (153.99, 94.991), (153.99, 95.88)],
            [
                (153.99, 95.88),
                (153.99, 96.515),
                (157.355, 96.515),
                (157.355, 102.515),
                (160.593, 102.515),
                (160.593, 96.515),
                (175.707, 96.515),
            ],
            [(175.707, 96.515), (177.866, 94.356), (177.866, 93.086)],
            [(177.866, 93.086), (178.492, 92.46), (178.5, 92.46)],
        ),
    ]:

        def segments(points, layer):
            return [
                Segment(*a, *b, 0.225, layer, net, net_name=name)
                for a, b in zip(points[:-1], points[1:], strict=True)
            ]

        def via(point):
            return Via(*point, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net, name)

        routes.append(
            Route(
                net, name, segments(front, Layer.F_CU) + segments(body, Layer.B_CU), [via(body[0])]
            )
        )
        tails.append(
            Route(
                net,
                name,
                segments(tail, Layer.B_CU) + segments(landing, Layer.F_CU),
                [via(landing[0])],
            )
        )
    for route, loop_top in ((routes[0], 102.896), (routes[1], 102.515)):
        for segment in route.segments:
            if abs(segment.y1 - loop_top) < 1e-8:
                segment.y1 += loop_depth - 6
            if abs(segment.y2 - loop_top) < 1e-8:
                segment.y2 += loop_depth - 6
    body = PairBody(*routes, *(auto.grid.world_to_grid(*r.segments[-1].end) for r in routes))
    pair = SimpleNamespace(positive=SimpleNamespace(net_id=1), negative=SimpleNamespace(net_id=2))
    return auto, finder, pair, pads, body, tails


@pytest.mark.parametrize("shortest", [False, True])
@pytest.mark.parametrize("threshold, accepted", [(0.85, True), (0.99, False)])
def test_reconsider_first_terminal_against_completed_partner(
    monkeypatch, shortest, threshold, accepted
):
    import kicad_tools.router.pair_completion as pc

    auto, finder, pair, pads, body, tails = terminal_case()
    for nc in finder.net_class_map.values():
        nc.coupled_continuity_threshold = threshold
    before = copy.deepcopy(body)
    # Reproduce the initial local choices, including their geometry/quality
    # rejection. The production reconsideration and all physical gates are real.
    monkeypatch.setattr(
        pc,
        "_tails_with_widen_fallback",
        lambda _r, _f, head, *args, **kwargs: iter([copy.deepcopy(tails[head.net - 1])]),
    )
    reasons = Counter()
    result = complete_pair_body(
        auto._diffpair,
        finder,
        pair,
        pads,
        body,
        deadline=time.monotonic() + 5,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        prefer_shortest_approach=shortest,
        reasons=reasons,
    )
    assert (result is not None) == accepted, reasons
    assert reasons["coupling_threshold"] > 0
    assert body == before and not auto.routes and not auto.grid.routes
    if result is None:
        return
    lengths = [
        MatchGroupTracker._measure_route_total(r, 1.6, 4, blind_buried_supported=False)
        for r in result
    ]
    assert abs(lengths[0] - lengths[1]) <= 0.075
    assert (
        min(
            auto._diffpair._tail_coupled_fraction(result[0], result[1].segments),
            auto._diffpair._tail_coupled_fraction(result[1], result[0].segments),
        )
        >= 0.85
    )


def test_reconsideration_shares_original_candidate_ceiling(monkeypatch):
    import itertools

    import kicad_tools.router.pair_completion as pc

    auto, finder, pair, pads, body, tails = terminal_case()

    def repeated(_r, _f, head, *args, **kwargs):
        return itertools.repeat(tails[head.net - 1])

    monkeypatch.setattr(pc, "_tails_with_widen_fallback", repeated)
    monkeypatch.setattr(
        auto._diffpair,
        "_layer_return_tails",
        lambda _f, head, *args, **kwargs: iter([tails[head.net - 1]]),
    )
    calls = []

    def rejected(*args, reasons, **kwargs):
        calls.append(1)
        reasons["coupling_threshold"] += 1

    monkeypatch.setattr(pc, "qualify_constructed_pair", rejected)
    assert (
        complete_pair_body(
            auto._diffpair,
            finder,
            pair,
            pads,
            body,
            deadline=time.monotonic() + 5,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )
        is None
    )
    assert len(calls) == 2 * 10 * 10
    assert not auto.routes and not auto.grid.routes


@pytest.mark.parametrize("threshold, accepted", [(0.85, True), (0.99, False)])
def test_original_two_mm_body_with_real_shortest_tail_generation(threshold, accepted):
    # Original captured body depth and preference, with real initial generation
    # on a reduced empty board. No tail or quality function is substituted.
    auto, finder, pair, pads, body, _ = terminal_case(loop_depth=2)
    for nc in finder.net_class_map.values():
        nc.coupled_continuity_threshold = threshold
    before = copy.deepcopy(body)
    reasons = Counter()
    result = complete_pair_body(
        auto._diffpair,
        finder,
        pair,
        pads,
        body,
        deadline=time.monotonic() + 20,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        prefer_shortest_approach=True,
        reasons=reasons,
    )
    assert (result is not None) == accepted, reasons
    assert reasons["coupling_threshold"] > 0
    assert body == before and not auto.routes and not auto.grid.routes
    if result is not None:
        lengths = [
            MatchGroupTracker._measure_route_total(r, 1.6, 4, blind_buried_supported=False)
            for r in result
        ]
        assert abs(lengths[0] - lengths[1]) <= 0.075
        assert (
            min(
                auto._diffpair._tail_coupled_fraction(result[0], result[1].segments),
                auto._diffpair._tail_coupled_fraction(result[1], result[0].segments),
            )
            >= threshold
        )
