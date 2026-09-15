"""Pair tuning can try a clear shared host after both sides of the first fail."""

import math

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import MatchGroup
from kicad_tools.router.match_group_tuning import tune_match_group_v2
from kicad_tools.router.primitives import Route, Segment


def fixture(*, angle=0, block_second=False):
    def seg(net, x1, y1, x2, y2):
        def pt(x, y):
            return (
                40.038 + x * math.cos(angle) - y * math.sin(angle),
                50 + x * math.sin(angle) + y * math.cos(angle),
            )

        return Segment(*pt(x1, y1), *pt(x2, y2), 0.15, Layer.F_CU, net=net, net_name=str(net))

    routes = {}
    for net, y in [(1, 0), (2, 0.5)]:
        routes[net] = Route(
            net=net,
            net_name=str(net),
            segments=[
                seg(net, 0, y, 10, y),
                seg(net, 10, y, 12, y + 2),
                seg(net, 12, y + 2, 18, y + 2),
            ],
        )
    target = LengthTracker.calculate_route_length(routes[1]) + 4
    routes[3] = Route(net=3, net_name="3", segments=[seg(3, 0, 20, target, 20)])
    routes[90] = Route(
        net=90, net_name="walls", segments=[seg(90, 0, -0.5, 10, -0.5), seg(90, 0, 1.0, 10, 1.0)]
    )
    if block_second:
        routes[90].segments.extend([seg(90, 12, 1.5, 18, 1.5), seg(90, 12, 3.0, 18, 3.0)])
    return MatchGroup("DDR", [3], pair_ids=[(1, 2)], tolerance=0.05), routes, target


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
def test_second_shared_host_succeeds_without_increasing_insertion_budget(angle):
    group, routes, target = fixture(angle=angle)
    original = dict(routes)
    result = tune(group, routes)
    for net in (1, 2):
        route, report = result[net]
        assert report.success
        assert report.inserts_applied == report.attempts == 1
        assert LengthTracker.calculate_route_length(route) == pytest.approx(target)
        assert route.segments[0] is original[net].segments[0]
        assert all(
            a.end == b.start for a, b in zip(route.segments[:-1], route.segments[1:], strict=True)
        )
    assert routes[90] is original[90]


def test_blocked_and_fixed_alternatives_leave_original_pair_unchanged():
    group, routes, _ = fixture(block_second=True)
    original = dict(routes)
    result = tune(
        group, routes, fixed_segment_ids={id(routes[1].segments[1]), id(routes[2].segments[1])}
    )
    for net in (1, 2):
        assert not result[net][1].success
        assert result[net][0] is original[net]
        assert result[net][1].inserts_applied == 0


def test_clear_alternative_marked_fixed_is_not_used():
    group, routes, _ = fixture()
    original = dict(routes)
    fixed = {id(segment) for net in (1, 2) for segment in routes[net].segments[1:]}
    result = tune(group, routes, fixed_segment_ids=fixed)
    for net in (1, 2):
        assert not result[net][1].success
        assert result[net][0] is original[net]
        assert result[net][1].inserts_applied == 0


@pytest.mark.parametrize("angle", [0, math.pi / 2])
def test_shallow_coordinated_loops_fit_corridor_with_original_insertion_budget(angle):
    group, routes, _ = fixture(angle=angle)
    # Keep one 10mm shared host with enough width for three coordinated bends.
    for net in (1, 2):
        routes[net].segments = routes[net].segments[:1]
    routes[90].segments = []
    from dataclasses import replace

    host = routes[1].segments[0]
    normal = (-math.sin(angle), math.cos(angle))
    n = routes[2].segments[0]
    routes[2].segments[0] = replace(
        n,
        x1=n.x1 - normal[0] * 0.119,
        y1=n.y1 - normal[1] * 0.119,
        x2=n.x2 - normal[0] * 0.119,
        y2=n.y2 - normal[1] * 0.119,
    )
    for distance in (-1.8, 2.3):
        routes[90].segments.append(
            replace(
                host,
                net=90,
                x1=host.x1 + normal[0] * distance,
                y1=host.y1 + normal[1] * distance,
                x2=host.x2 + normal[0] * distance,
                y2=host.y2 + normal[1] * distance,
            )
        )
    # Reference adds 7mm: one/two U-loops cross the walls, three stay clear.
    ref = routes[3].segments[0]
    routes[3].segments = [
        replace(ref, x2=ref.x1 + 17 * math.cos(angle), y2=ref.y1 + 17 * math.sin(angle))
    ]
    result = tune(group, routes)
    for net in (1, 2):
        tuned, report = result[net]
        assert report.success and report.inserts_applied == report.attempts == 1
        assert LengthTracker.calculate_route_length(tuned) == pytest.approx(17)
        assert report.serpentine_results[-1].num_loops == 3
