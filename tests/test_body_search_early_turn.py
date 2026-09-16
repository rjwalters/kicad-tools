"""Earlier turns must reach the real physical gate, without extra allowance."""

import time
from types import SimpleNamespace

import pytest

from kicad_tools.router import body_search
from kicad_tools.router.body_planning import construct_pair_body
from kicad_tools.router.construction_validation import constructed_pair_geometry_issue
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting
from tests.test_body_planning import case


@pytest.mark.parametrize("obstacle_y,expected", [(8.0, True), (8.65, False)])
def test_early_turn_before_foreign_lane_keeps_physical_checks(monkeypatch, obstacle_y, expected):
    finder, pads, departure = case()
    auto = Autorouter(width=20, height=20, rules=finder.rules)
    finder = CoupledPathfinder(auto.grid, finder.rules, target_spacing_cells=3, min_spacing_cells=2)
    finder.net_class_map = {"1": NetClassRouting(name="pair", trace_width=0.15, clearance=0.15)}
    auto.routes.append(
        Route(
            net=9,
            net_name="OTHER",
            segments=[
                Segment(
                    x1=4,
                    y1=obstacle_y,
                    x2=14,
                    y2=obstacle_y,
                    width=0.15,
                    layer=Layer.B_CU,
                    net=9,
                )
            ],
        )
    )
    router = auto._diffpair
    deadline = time.monotonic() + 10
    # Every original depth hits this foreign lane. These are actual bodies
    # and the production geometry validator, not a mocked success oracle.
    if expected:
        for depth in range(6, 13):
            body = construct_pair_body(finder, departure, pads, depth_cells=depth, retreat_cells=15)
            heads = tuple(
                router._virtual_pad_at(pad, *auto.grid.grid_to_world(*head), 1)
                for pad, head in zip((pads[1], pads[3]), (body.p_head, body.n_head), strict=True)
            )
            assert (
                constructed_pair_geometry_issue(
                    router,
                    finder,
                    body.p_route,
                    body.n_route,
                    (pads[0], heads[0], pads[2], heads[1]),
                    intra_pair_clearance=0.15,
                    deadline=deadline,
                )
                is not None
            )
    completed = []

    def observe_terminal(router, finder, pair, pads, body, **kwargs):
        # Isolate body discovery. Terminal connectivity/skew are intentionally
        # not represented as qualified by this geometry-stage control.
        completed.append(body)
        return body.p_route, body.n_route

    monkeypatch.setattr(body_search, "complete_pair_body", observe_terminal)
    budget = body_search.BodySearchBudget(deadline, 32)
    result = body_search.complete_departure(
        router,
        finder,
        None,
        pads,
        departure,
        SimpleNamespace(allowed_sites=()),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=2,
    )
    assert (result is not None) is expected
    assert bool(completed) is expected
    assert budget.bodies_used + budget.bodies_remaining == 32
    assert budget.bodies_geometry_rejected > 0


@pytest.mark.parametrize(
    "obstacle_bottom,expected", [(8.4, True), (8.45, True), (8.5, True), (8.55, True), (8.6, True)]
)
def test_deeper_turn_remains_reachable_with_early_turns(monkeypatch, obstacle_bottom, expected):
    finder, pads, departure = case()
    auto = Autorouter(width=20, height=20, rules=finder.rules)
    finder = CoupledPathfinder(auto.grid, finder.rules, target_spacing_cells=3, min_spacing_cells=2)
    finder.net_class_map = {"1": NetClassRouting(name="pair", trace_width=0.15, clearance=0.15)}
    auto.routes.append(
        Route(9, "OTHER", segments=[Segment(9, obstacle_bottom, 9, 10, 0.15, Layer.B_CU, 9)])
    )
    completed = []

    def observe_terminal(router, finder, pair, pads, body, **kwargs):
        # The constructor and physical validation are real; this records only
        # body discovery, without claiming terminal or full-board completion.
        completed.append(body)
        return body.p_route, body.n_route

    monkeypatch.setattr(body_search, "complete_pair_body", observe_terminal)
    budget = body_search.BodySearchBudget(time.monotonic() + 10, 32)
    result = body_search.complete_departure(
        auto._diffpair,
        finder,
        None,
        pads,
        departure,
        SimpleNamespace(allowed_sites=()),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=2,
    )
    assert (result is not None) is expected
    assert bool(completed) is expected
    assert budget.bodies_used + budget.bodies_remaining == 32
    assert budget.bodies_geometry_rejected > 0
