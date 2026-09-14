import copy
import math
import time
from collections import Counter
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


def test_future_landing_barrel_is_an_obstacle_without_marking_occupancy():
    """A future landing reservation blocks its own site without being committed.

    ``sites`` plans the P leg's ONLY candidate exactly where ``reserved``
    places a future via, so the single-site search this plan offers cannot
    land there. Issue #5333's widen-fallback (:func:`_tails_with_widen_fallback`
    in ``pair_completion.py``) then retries the P leg unrestricted and finds
    a different legal via nearby on this otherwise empty board -- so
    completion now succeeds instead of failing outright. The regression this
    test guards stays intact either way: the reservation is never silently
    crossed, and it is never committed as real occupancy.
    """
    auto, finder, pair, pads, body, sites = case()
    reserved = Route(
        net=3,
        net_name="future",
        vias=[
            Via(
                x=76.2,
                y=2,
                diameter=0.6,
                drill=0.3,
                layers=(Layer.F_CU, Layer.B_CU),
                net=3,
            )
        ],
    )
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
        reserved_routes=(reserved,),
    )
    assert result is not None
    rules = finder.rules
    min_center_distance = (
        reserved.vias[0].diameter / 2 + rules.via_diameter / 2 + rules.via_clearance
    )
    for route in result:
        for via in route.vias:
            distance = math.hypot(via.x - reserved.vias[0].x, via.y - reserved.vias[0].y)
            assert distance >= min_center_distance - 1e-9
    assert not auto.routes and not auto.grid.routes


def test_tuning_sees_all_reservations_and_existing_copper_for_a_future_net(monkeypatch):
    from dataclasses import replace

    from kicad_tools.router import pair_completion

    auto, finder, pair, pads, body, sites = case()
    segment = replace(body.p_route.segments[0], x1=40, x2=41, y1=6, y2=6, net=3)
    existing = Route(net=3, net_name="future", segments=[segment])
    auto.routes.append(existing)
    reservations = (
        Route(net=3, net_name="future", segments=[replace(segment, x1=42, x2=43)]),
        Route(
            net=3,
            net_name="future",
            vias=[Via(x=45, y=6, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=3)],
        ),
    )
    tune = pair_completion.tune_diff_pair_skew
    calls = []

    def inspect(pair, corpus, **kwargs):
        assert len(corpus[3].segments) == 2 and len(corpus[3].vias) == 1
        calls.append(True)
        return tune(pair, corpus, **kwargs)

    monkeypatch.setattr(pair_completion, "tune_diff_pair_skew", inspect)
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
            reserved_routes=reservations,
        )
        is not None
    )
    assert calls and len(existing.segments) == 1 and not existing.vias
    assert auto.routes == [existing] and not auto.grid.routes


def test_no_via_sites_are_tallied_as_no_tail_not_silence():
    """An empty site plan is a distinct, nameable defect (#5333).

    ``complete_pair_body`` returning ``None`` cannot say whether the layer
    return search never offered a candidate or every candidate it offered
    was rejected later -- the ``reasons`` tally is what makes that legible.
    """
    auto, finder, pair, pads, body, _sites = case()
    reasons: Counter[str] = Counter()
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
            allowed_via_sites=(frozenset(), frozenset()),
            reasons=reasons,
        )
        is None
    )
    # Both approach orderings ((0, 1) and (1, 0)) find nothing to offer.
    assert reasons == {"no_tail": 2}


def test_successful_completion_leaves_the_reasons_tally_empty():
    auto, finder, pair, pads, body, sites = case()
    reasons: Counter[str] = Counter()
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
        reasons=reasons,
    )
    assert result is not None
    assert reasons == {}


def test_default_reasons_counter_is_fresh_per_call():
    """The optional counter must never default to a shared mutable object."""
    auto, finder, pair, pads, body, _sites = case()
    # Two independent calls with no explicit ``reasons=`` must not leak state
    # into each other via a shared default argument.
    complete_pair_body(
        auto._diffpair,
        finder,
        pair,
        pads,
        body,
        deadline=time.monotonic() + 5,
        board_thickness_mm=1.6,
        num_copper_layers=2,
        allowed_via_sites=(frozenset(), frozenset()),
    )
    reasons: Counter[str] = Counter()
    complete_pair_body(
        auto._diffpair,
        finder,
        pair,
        pads,
        body,
        deadline=time.monotonic() + 5,
        board_thickness_mm=1.6,
        num_copper_layers=2,
        allowed_via_sites=(frozenset(), frozenset()),
        reasons=reasons,
    )
    assert reasons == {"no_tail": 2}
