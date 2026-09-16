"""Use the legal host capacity available within the existing insertion budget."""

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroup
from kicad_tools.router.match_group_tuning import tune_match_group_v2
from kicad_tools.router.optimizer.serpentine import SerpentineConfig
from kicad_tools.router.primitives import Route, Segment


def _route(net, points):
    return Route(
        net=net,
        net_name=str(net),
        segments=[
            Segment(*a, *b, 0.2, Layer.F_CU, net, str(net))
            for a, b in zip(points, points[1:], strict=False)
        ],
    )


def _fixture():
    return {
        1: _route(1, [(0, 0), (10, 0), (10, 6)]),
        2: _route(2, [(0, 10), (23, 10)]),
        3: _route(3, [(0, 14), (23, 14)]),
        99: _route(99, [(-1, -0.5), (8, -0.5)]),
        98: _route(98, [(-1, 0.5), (8, 0.5)]),
    }


def _tune(routes, fixed=()):
    return tune_match_group_v2(
        MatchGroup(name="independent_capacity", net_ids=[1, 2, 3], tolerance=0.1),
        routes,
        intra_group_clearance_mm=0.15,
        config=SerpentineConfig(amplitude=1, min_spacing=0.4, gap_factor=1.5, min_segment_length=2),
        max_inserts_per_member=1,
        fixed_segment_ids=set(fixed),
    )[1]


def test_later_legal_host_can_satisfy_target_with_one_insertion():
    routes = _fixture()
    original = routes[1]
    first_host = original.segments[0]
    original_geometry = [
        (segment.x1, segment.y1, segment.x2, segment.y2) for segment in original.segments
    ]
    tuned, result = _tune(routes)
    assert result.success
    assert result.length_after_mm == pytest.approx(23, abs=0.1)
    assert result.attempts == result.inserts_applied == 1
    assert first_host in tuned.segments
    assert [
        (segment.x1, segment.y1, segment.x2, segment.y2) for segment in original.segments
    ] == original_geometry


def test_fixed_full_capacity_host_preserves_legal_partial_progress():
    routes = _fixture()
    fixed = routes[1].segments[1]
    tuned, result = _tune(routes, [id(fixed)])
    assert not result.success
    assert result.reason == "exceeded_max_inserts"
    assert result.inserts_applied == result.attempts == 1
    assert 16 < result.length_after_mm < 23
    assert any(segment is fixed for segment in tuned.segments)


def test_all_fixed_hosts_preserve_original_route_without_progress():
    routes = _fixture()
    original = routes[1]
    tuned, result = _tune(routes, [id(segment) for segment in original.segments])
    assert tuned is original
    assert result.inserts_applied == 0
    assert result.length_after_mm == 16
