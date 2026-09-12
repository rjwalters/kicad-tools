"""Tuning must preserve the complete copper of nets stored as fragments."""

import copy

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.router.connectivity_invariant import (
    enforce_connectivity_invariant,
    snapshot_connectivity,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.router.optimizer import make_collision_checker
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def fragment(net, x1, x2, y, *, escape=False):
    return Route(
        net=net,
        net_name=f"D{net}",
        segments=[Segment(x1, y, x2, y, 0.2, Layer.F_CU, net=net)],
        is_escape=escape,
    )


def setup_routes(*, force_python=True, escape=True, reference_length=12):
    ar = Autorouter(
        width=40,
        height=40,
        force_python=force_python,
        rules=DesignRules(grid_resolution=0.1),
    )
    if not force_python and ar.grid._cpp_grid is None:
        pytest.skip("C++ backend unavailable")
    ar.net_names = {1: "D1", 2: "D2"}
    ar.routes = [
        fragment(1, 5, 6, 10, escape=escape),
        fragment(1, 6, 15, 10),
        fragment(2, 5, 6, 30, escape=escape),
        fragment(2, 6, 5 + reference_length, 30),
    ]
    for route in ar.routes:
        ar._mark_route(route)
    group = MatchGroup(
        name="G",
        net_ids=[1, 2],
        tolerance=0.1,
        reference_net_id=2,
        source=MatchGroupSource.LEGACY_API,
    )
    return ar, group


def length(ar, net):
    return sum(LengthTracker.calculate_route_length(r) for r in ar.routes if r.net == net)


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("escape", [True, False])
def test_complete_net_tuning_preserves_fragments_and_refreshes_collision_views(
    force_python, escape
):
    ar, group = setup_routes(force_python=force_python, escape=escape)
    original = list(ar.routes)
    independent = fragment(9, 2, 3, 35)
    ar.grid.mark_route(independent)
    checker = make_collision_checker(ar.grid, ignore_overflow=True)
    ar.nets = {1: [("U1", "1"), ("U2", "1")]}
    ar.pads = {
        key: Pad(
            x=x,
            y=10,
            width=0.4,
            height=0.4,
            layer=Layer.F_CU,
            net=1,
            net_name="D1",
            ref=key[0],
            pin=key[1],
        )
        for key, x in zip(ar.nets[1], (5, 15), strict=True)
    }
    connectivity = snapshot_connectivity(ar)

    result = ar.apply_match_group_tuning([group], verbose=False)["G"][1][1]

    assert result.reason == "tuned"
    enforce_connectivity_invariant(
        ar, connectivity, phase="split-route tuning", strict=True, quiet=True
    )
    assert result.length_before_mm == pytest.approx(10)
    assert length(ar, 1) == pytest.approx(12, abs=0.1)
    assert result.length_after_mm == pytest.approx(length(ar, 1))
    assert any(r is original[2] for r in ar.routes)
    assert any(r is original[3] for r in ar.routes)
    assert not any(r is original[1] for r in ar.routes)
    assert sum(r.net == 1 for r in ar.routes) == (2 if escape else 1)
    if escape:
        assert any(r is original[0] and r.is_escape for r in ar.routes)
    assert {id(r) for r in ar.grid.routes} == {id(r) for r in ar.routes} | {id(independent)}

    # Query a new meander segment through a checker created before tuning.
    segment = next(
        s
        for r in ar.routes
        if r.net == 1
        for s in r.segments
        if abs(s.y1 - 10) > 0.3 and abs(s.y2 - 10) > 0.3
    )
    assert not checker.path_is_clear(
        segment.x1, segment.y1, segment.x2, segment.y2, Layer.F_CU, 0.2, 99
    )
    gx, gy = ar.grid.world_to_grid((segment.x1 + segment.x2) / 2, (segment.y1 + segment.y2) / 2)
    assert ar.grid.grid[0][gy][gx].net == 1
    if not force_python:
        assert ar.grid._cpp_grid.is_blocked_for_net(gx, gy, 0, 99)
    if ar.grid._rtree_available:
        indexed = [s for items in ar.grid._seg_rtree_items.values() for s in items.values()]
        assert {id(s) for s in indexed} == {id(s) for r in ar.grid.routes for s in r.segments}

    committed = list(ar.routes)
    ar.apply_match_group_tuning([group], verbose=False)
    assert [id(r) for r in ar.routes] == [id(r) for r in committed]
    assert length(ar, 1) == pytest.approx(12, abs=0.1)


def test_already_matched_split_routes_are_unchanged():
    ar, group = setup_routes(reference_length=10)
    original = list(ar.routes)
    ar.apply_match_group_tuning([group], verbose=False)
    assert [id(r) for r in ar.routes] == [id(r) for r in original]


def test_escape_only_net_cannot_become_a_completed_route():
    ar, group = setup_routes()
    ar.routes[1].is_escape = True
    original = copy.deepcopy(ar.routes)
    result = ar.apply_match_group_tuning([group], verbose=False)["G"][1][1]
    assert result.reason == "no_suitable_segment"
    assert ar.routes == original
    assert all(r.is_escape for r in ar.routes if r.net == 1)


def test_reference_via_in_first_fragment_is_counted():
    ar, group = setup_routes(reference_length=10)
    via = Via(x=6, y=30, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=2)
    ar.routes[2].vias.append(via)
    ar.restore_route_snapshot(ar.routes)
    expected = 10 + ar._build_manufacturer_design_rules().board_thickness_mm
    ar.apply_match_group_tuning([group], verbose=False)
    assert length(ar, 1) == pytest.approx(expected, abs=0.1)
    assert sum(v is via for r in ar.routes for v in r.vias) == 1


def test_foreign_via_in_earlier_fragment_blocks_candidate():
    control, group = setup_routes()
    control.apply_match_group_tuning([group], verbose=False)
    host = next(
        s
        for r in control.routes
        if r.net == 1
        for s in r.segments
        if abs(s.y1 - 10) > 0.3 and abs(s.y2 - 10) > 0.3
    )
    via = Via(
        x=(host.x1 + host.x2) / 2,
        y=(host.y1 + host.y2) / 2,
        diameter=0.6,
        drill=0.3,
        layers=(Layer.F_CU, Layer.B_CU),
        net=3,
    )
    ar, group = setup_routes()
    foreign = [Route(net=3, net_name="D3", vias=[via], is_escape=True), fragment(3, 25, 28, 25)]
    ar.routes.extend(foreign)
    ar.restore_route_snapshot(ar.routes)
    ar.apply_match_group_tuning([group], verbose=False)
    for route in ar.routes:
        if route.net == 1:
            for seg in route.segments:
                distance = Point(via.x, via.y).distance(
                    LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
                )
                assert distance + 1e-8 >= via.diameter / 2 + seg.width / 2 + ar.rules.via_clearance
    assert all(any(r is f for r in ar.routes) for f in foreign)


def test_failed_insertion_preserves_original_fragment_objects():
    ar, group = setup_routes(reference_length=12)
    ar.routes.extend([fragment(3, 0, 25, 9.5), fragment(4, 0, 25, 10.5)])
    ar.restore_route_snapshot(ar.routes)
    original = list(ar.routes)
    geometry = copy.deepcopy(ar.routes)
    result = ar.apply_match_group_tuning([group], verbose=False)["G"][1][1]
    assert result.reason == "post_insertion_drc_violation"
    assert result.attempts > 0
    assert ar.routes == geometry
    assert [id(r) for r in ar.routes] == [id(r) for r in original]
    assert {id(r) for r in ar.grid.routes} == {id(r) for r in original}


def test_pair_tuning_preserves_both_escape_fragments():
    ar, _ = setup_routes()
    ar.routes = [
        fragment(net, 5, 6, y, escape=True) for net, y in [(1, 10), (2, 11), (3, 30), (4, 31)]
    ] + [
        fragment(net, 6, end, y)
        for net, end, y in [(1, 15, 10), (2, 15, 11), (3, 17, 30), (4, 17, 31)]
    ]
    ar.restore_route_snapshot(ar.routes)
    escapes = ar.routes[:4]
    group = MatchGroup(
        name="P",
        net_ids=[],
        pair_ids=[(1, 2), (3, 4)],
        tolerance=0.1,
        reference_net_id=3,
        source=MatchGroupSource.LEGACY_API,
    )
    results = ar.apply_match_group_tuning([group], verbose=False)["P"]
    assert results[1][1].reason == "tuned"
    assert results[2][1].reason == "tuned"
    for net in (1, 2):
        assert length(ar, net) == pytest.approx(12, abs=0.1)
        assert sum(r.net == net for r in ar.routes) == 2
    assert all(any(r is escape for r in ar.routes) for escape in escapes)
