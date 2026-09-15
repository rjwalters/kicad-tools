"""A blocked coordinated loop tries the other side without weakening its gates."""

import math

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import MatchGroup
from kicad_tools.router.match_group_tuning import tune_match_group_v2
from kicad_tools.router.primitives import Route, Segment


def fixture(*, both_sides=False, angle=0):
    def segment(net, x1, y1, x2, y2):
        def point(x, y):
            return (
                50.089 + x * math.cos(angle) - y * math.sin(angle),
                60.051 + x * math.sin(angle) + y * math.cos(angle),
            )

        return Segment(*point(x1, y1), *point(x2, y2), 0.15, Layer.B_CU, net=net, net_name=str(net))

    routes = {
        net: Route(net=net, net_name=str(net), segments=[segment(net, 0, y, length, y)])
        for net, y, length in [(1, 0, 10), (2, 0.5, 10), (3, 8, 14), (4, 8.5, 14)]
    }
    routes[99] = Route(net=99, net_name="wall", segments=[segment(99, 0, -1, 10, -1)])
    if both_sides:
        # Block even the bounded shallow-loop alternatives, with legal baseline clearance.
        routes[99].segments = [segment(99, 0, -0.5, 10, -0.5)]
        routes[98] = Route(net=98, net_name="other wall", segments=[segment(98, 0, 1, 10, 1)])
    group = MatchGroup("lanes", [], pair_ids=[(1, 2), (3, 4)], tolerance=0.05)
    return group, routes


def tune(group, routes, **kwargs):
    return tune_match_group_v2(
        group,
        routes,
        tolerance_mm=0.05,
        intra_group_clearance_mm=0.15,
        intra_pair_clearance_mm=0.1,
        preserve_pair_spacing=True,
        max_inserts_per_member=1,
        **kwargs,
    )


@pytest.mark.parametrize("angle", [0, math.pi / 2])
def test_opposite_clear_side_matches_both_halves_with_one_insertion(angle):
    group, routes = fixture(angle=angle)
    originals = dict(routes)
    original_segments = {net: list(route.segments) for net, route in routes.items()}
    result = tune(group, routes)
    for net in (1, 2):
        route, report = result[net]
        assert report.success and report.inserts_applied == 1 and report.attempts == 1
        assert "opposite side" in report.serpentine_results[-1].message
        assert LengthTracker.calculate_route_length(route) == pytest.approx(14)
        assert route.segments[0].start == originals[net].segments[0].start
        assert route.segments[-1].end == originals[net].segments[-1].end
        assert all(
            a.end == b.start for a, b in zip(route.segments[:-1], route.segments[1:], strict=True)
        )
        assert originals[net].segments == original_segments[net]
    assert result[3][0] is originals[3] and result[4][0] is originals[4]
    assert routes[99] is originals[99]


def test_second_collision_rolls_back_both_original_route_objects():
    group, routes = fixture(both_sides=True)
    originals = dict(routes)
    segments = {net: route.segments for net, route in routes.items()}
    result = tune(group, routes)
    for net in (1, 2):
        route, report = result[net]
        assert not report.success and report.reason == "post_insertion_drc_violation"
        assert report.inserts_applied == 0
        assert route is originals[net] and route.segments is segments[net]
        assert routes[net] is originals[net]


def test_fixed_host_is_not_modified_to_obtain_an_opposite_loop():
    group, routes = fixture()
    originals = dict(routes)
    result = tune(group, routes, fixed_segment_ids={id(routes[1].segments[0])})
    for net in (1, 2):
        assert result[net][0] is originals[net]
        assert result[net][1].inserts_applied == 0
