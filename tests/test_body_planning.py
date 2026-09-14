import copy
import math
from dataclasses import replace

import pytest

from kicad_tools.router.body_planning import construct_pair_body
from kicad_tools.router.departure_planning import DepartureProposal, ValidatedDeparture
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.router.rules import DesignRules


def case():
    rules = DesignRules(grid_resolution=0.1, trace_width=0.15, trace_clearance=0.15)
    grid = RoutingGrid(width=20, height=20, rules=rules)
    finder = CoupledPathfinder(grid, rules, target_spacing_cells=3, min_spacing_cells=2)
    pads = tuple(
        Pad(x=x, y=y, width=0.4, height=0.4, layer=Layer.F_CU, net=net, net_name=str(net))
        for net, ends in ((1, ((5, 10), (15, 8))), (2, ((6, 10), (15, 8.8))))
        for x, y in ends
    )
    routes = []
    for pad in (pads[0], pads[2]):
        routes.append(
            Route(
                net=pad.net,
                net_name=pad.net_name,
                segments=[
                    Segment(
                        x1=pad.x,
                        y1=pad.y,
                        x2=pad.x,
                        y2=9,
                        width=0.15,
                        layer=Layer.F_CU,
                        net=pad.net,
                    )
                ],
                vias=[
                    Via(
                        x=pad.x,
                        y=9,
                        diameter=0.6,
                        drill=0.3,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=pad.net,
                    )
                ],
            )
        )
    departure = ValidatedDeparture(
        DepartureProposal(((50, 90, 1, 60, 90, 1),), (1, 0), (0, -1), 1, 0), *routes, 12
    )
    return finder, pads, departure


@pytest.mark.parametrize("goal_offset", [None, 0, -1])
def test_body_keeps_prefix_and_emits_chained_aligned_extensions(goal_offset):
    finder, pads, departure = case()
    original = copy.deepcopy(departure)
    body = construct_pair_body(
        finder, departure, pads, depth_cells=5, retreat_cells=20, goal_offset_cells=goal_offset
    )
    assert body is not None and departure == original
    for route, head in ((body.p_route, body.p_head), (body.n_route, body.n_head)):
        assert route.segments[-1].end == finder.grid.grid_to_world(*head)
        assert all(
            a.end == b.start for a, b in zip(route.segments[:-1], route.segments[1:], strict=True)
        )
        assert all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
        assert len(route.vias) == 1


def test_coordinated_body_loop_adds_equal_length():
    finder, pads, departure = case()
    base = construct_pair_body(finder, departure, pads, depth_cells=5, retreat_cells=20)
    loop = construct_pair_body(
        finder, departure, pads, depth_cells=5, retreat_cells=20, added_coupled_length=4
    )
    assert base is not None and loop is not None
    for a, b in ((base.p_route, loop.p_route), (base.n_route, loop.n_route)):

        def length(route):
            return sum(math.dist(s.start, s.end) for s in route.segments)

        assert length(b) - length(a) == pytest.approx(4)
        assert (
            a.segments[0].start == b.segments[0].start and a.segments[-1].end == b.segments[-1].end
        )


def test_body_rejects_backward_run_and_invalid_frame():
    finder, pads, departure = case()
    assert construct_pair_body(finder, departure, pads, depth_cells=5, retreat_cells=200) is None
    bad = replace(departure, proposal=replace(departure.proposal, outward=(1, 0)))
    assert construct_pair_body(finder, bad, pads, depth_cells=5, retreat_cells=20) is None


@pytest.mark.parametrize("turns", [1, 2, 3])
@pytest.mark.parametrize("goal_offset", [None, -1])
def test_body_rotates_with_departure_frame(turns, goal_offset):
    finder, pads, departure = case()

    def rotate(point, center):
        x, y = point[0] - center, point[1] - center
        for _ in range(turns):
            x, y = -y, x
        return x + center, y + center

    rotated_pads = copy.deepcopy(pads)
    rotated = copy.deepcopy(departure)
    for pad in rotated_pads:
        pad.x, pad.y = rotate((pad.x, pad.y), 10)
    for route in (rotated.p_route, rotated.n_route):
        for segment in route.segments:
            segment.x1, segment.y1 = rotate(segment.start, 10)
            segment.x2, segment.y2 = rotate(segment.end, 10)
        for via in route.vias:
            via.x, via.y = rotate((via.x, via.y), 10)
    prefix = tuple(
        (*rotate(step[:2], 100), step[2], *rotate(step[3:5], 100), step[5])
        for step in departure.proposal.prefix
    )
    rotated = replace(
        rotated,
        proposal=replace(
            departure.proposal,
            prefix=prefix,
            across=rotate(departure.proposal.across, 0),
            outward=rotate(departure.proposal.outward, 0),
        ),
    )
    options = {
        "depth_cells": 5,
        "retreat_cells": 20,
        "goal_offset_cells": goal_offset,
        "added_coupled_length": 4,
    }
    original_body = construct_pair_body(finder, departure, pads, **options)
    rotated_body = construct_pair_body(finder, rotated, rotated_pads, **options)
    assert original_body is not None and rotated_body is not None
    for original, result in (
        (original_body.p_route, rotated_body.p_route),
        (original_body.n_route, rotated_body.n_route),
    ):
        assert len(original.segments) == len(result.segments)
        for a, b in zip(original.segments, result.segments, strict=True):
            assert b.start == pytest.approx(rotate(a.start, 10))
            assert b.end == pytest.approx(rotate(a.end, 10))
