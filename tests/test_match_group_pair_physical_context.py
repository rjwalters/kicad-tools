"""Real pair tuning uses physical lengths and complete clearance context."""

import copy

import pytest

from kicad_tools.router.cache import CacheKey, RoutingCache
from kicad_tools.router.connectivity_invariant import (
    enforce_connectivity_invariant,
    snapshot_connectivity,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource, MatchGroupTracker
from kicad_tools.router.match_group_tuning import _post_insertion_clearance_detail_pair_group
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def route(net, start, end, y, *, escape=False):
    return Route(
        net=net,
        net_name=f"N{net}",
        is_escape=escape,
        segments=[Segment(start, y, end, y, 0.2, Layer.F_CU, net=net)],
    )


def via(net, x, y):
    return Via(x=x, y=y, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=net)


def setup_pair(*, reference_length=10, via_counts=(0, 0, 1, 1), force_python=True):
    ar = Autorouter(
        width=45, height=45, force_python=force_python, rules=DesignRules(grid_resolution=0.1)
    )
    if not force_python and ar.grid._cpp_grid is None:
        pytest.skip("C++ backend unavailable")
    ar.net_names = {n: f"N{n}" for n in range(1, 7)}
    for net, y in [(1, 10), (2, 11), (3, 25), (4, 26)]:
        escape = route(net, 5, 6, y, escape=True)
        escape.vias = [via(net, 5 + i, y) for i in range(via_counts[net - 1])]
        ar.routes.extend([escape, route(net, 6, 5 + (10 if net < 3 else reference_length), y)])
    ar.restore_route_snapshot(ar.routes)
    group = MatchGroup(
        name="P",
        net_ids=[],
        pair_ids=[(1, 2), (3, 4)],
        reference_net_id=3,
        tolerance=0.1,
        source=MatchGroupSource.LEGACY_API,
    )
    return ar, group


def physical_length(ar, net):
    return sum(
        MatchGroupTracker._measure_route_total(r, 1.6, 2, blind_buried_supported=False)
        for r in ar.routes
        if r.net == net
    )


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("via_counts", [(0, 0, 1, 1), (1, 0, 1, 1), (1, 1, 2, 2)])
def test_pair_matches_physical_lane_average_without_double_counting_vias(via_counts, force_python):
    ar, group = setup_pair(via_counts=via_counts, force_python=force_python)
    ar.nets = {net: [(f"U{net}", "1"), (f"U{net}", "2")] for net in range(1, 5)}
    for net, y in [(1, 10), (2, 11), (3, 25), (4, 26)]:
        for key, x in zip(ar.nets[net], (5, 15), strict=True):
            ar.pads[key] = Pad(
                x=x,
                y=y,
                width=0.4,
                height=0.4,
                net=net,
                net_name=f"N{net}",
                layer=Layer.F_CU,
                ref=key[0],
                pin=key[1],
            )
    connectivity = snapshot_connectivity(ar)
    before = {n: physical_length(ar, n) for n in range(1, 5)}
    escapes = [r for r in ar.routes if r.is_escape]
    results = ar.apply_match_group_tuning([group], verbose=False)["P"]
    enforce_connectivity_invariant(
        ar, connectivity, phase="physical pair tuning", strict=True, quiet=True
    )
    target = (before[3] + before[4]) / 2
    assert results[1][1].reason == results[2][1].reason == "tuned"
    assert (physical_length(ar, 1) + physical_length(ar, 2)) / 2 == pytest.approx(target, abs=0.02)
    assert physical_length(ar, 1) - physical_length(ar, 2) == pytest.approx(
        before[1] - before[2], abs=0.02
    )
    for net in (1, 2):
        assert results[net][1].length_before_mm == pytest.approx(before[net])
        assert results[net][1].length_after_mm == pytest.approx(physical_length(ar, net))
    assert all(any(r is e for r in ar.routes) for e in escapes)
    committed = list(ar.routes)
    ar.apply_match_group_tuning([group], verbose=False)
    assert [id(r) for r in ar.routes] == [id(r) for r in committed]


def test_equal_physical_pairs_remain_unchanged():
    ar, group = setup_pair(via_counts=(1, 1, 1, 1))
    before = list(ar.routes)
    results = ar.apply_match_group_tuning([group], verbose=False)["P"]
    assert results[1][1].reason == "already_within_tolerance"
    assert results[1][1].length_after_mm == pytest.approx(11.6)
    assert [id(r) for r in ar.routes] == [id(r) for r in before]


def insert_location(net):
    ar, group = setup_pair(reference_length=12, via_counts=(0, 0, 0, 0))
    result = ar.apply_match_group_tuning([group], verbose=False)["P"]
    assert result[net][1].reason == "tuned"
    baseline_y = 10 if net == 1 else 11
    seg = next(
        s
        for r in ar.routes
        if r.net == net
        for s in r.segments
        if abs(s.y1 - baseline_y) > 0.7 and abs(s.y2 - baseline_y) > 0.7
    )
    return (seg.x1 + seg.x2) / 2, (seg.y1 + seg.y2) / 2


@pytest.mark.parametrize("net", [1, 2])
@pytest.mark.parametrize("obstacle", ["via", "smd", "pth", "other_layer_smd"])
def test_foreign_obstacles_reject_both_halves_with_layer_aware_pads(net, obstacle):
    x, y = insert_location(net)
    ar, group = setup_pair(reference_length=12, via_counts=(0, 0, 0, 0))
    if obstacle == "via":
        # The obstacle is in an earlier fragment of the foreign net.
        ar.routes.extend(
            [Route(net=9, net_name="N9", vias=[via(9, x, y)], is_escape=True), route(9, 35, 36, 35)]
        )
        ar.restore_route_snapshot(ar.routes)
    else:
        ar.pads[("X", "1")] = Pad(
            x=x,
            y=y,
            width=0.4,
            height=0.4,
            net=9,
            net_name="N9",
            layer=Layer.F_CU if obstacle == "smd" else Layer.B_CU,
            through_hole=obstacle == "pth",
            ref="X",
            pin="1",
        )
    before = list(ar.routes)
    geometry = copy.deepcopy(before)
    results = ar.apply_match_group_tuning([group], verbose=False)["P"]
    if obstacle == "other_layer_smd":
        assert results[1][1].reason == results[2][1].reason == "tuned"
    else:
        assert results[1][1].reason == results[2][1].reason == "post_insertion_drc_violation"
        assert [id(r) for r in ar.routes] == [id(r) for r in before]
        assert ar.routes == geometry


def test_retained_partner_copper_uses_pair_clearance_floor():
    p = Segment(5, 10, 8, 10, 0.2, Layer.F_CU, net=1)
    n = Segment(5, 11, 8, 11, 0.2, Layer.F_CU, net=2)
    retained = Segment(5, 10.4, 8, 10.4, 0.2, Layer.F_CU, net=2)
    kwargs = {
        "new_p_segments": [p],
        "new_n_segments": [n],
        "candidate_p_id": 1,
        "candidate_n_id": 2,
        "group_net_ids": {1, 2},
        "routes_by_net": {},
        "intra_group_clearance_mm": 0.5,
        "intra_pair_clearance_mm": 0.1,
        "candidate_p_route": Route(net=1, net_name="N1", segments=[p]),
        "candidate_n_route": Route(net=2, net_name="N2", segments=[n, retained]),
    }
    assert _post_insertion_clearance_detail_pair_group(**kwargs) is None
    retained.y1 = retained.y2 = 10.2
    assert "retained partner" in _post_insertion_clearance_detail_pair_group(**kwargs)


def test_trailing_scalars_share_physical_pair_reference_and_via_context():
    ar, group = setup_pair()
    group.net_ids = [5, 6]
    ar.routes.extend([route(5, 5, 15, 35), route(6, 5, 15, 40)])
    ar.routes[-1].vias.append(via(6, 5, 40))
    ar.restore_route_snapshot(ar.routes)
    results = ar.apply_match_group_tuning([group], verbose=False)["P"]
    assert results[5][1].reason == "tuned"
    assert results[6][1].reason == "already_within_tolerance"
    assert physical_length(ar, 5) == pytest.approx(11.6, abs=0.02)
    assert results[6][1].length_before_mm == pytest.approx(11.6)
    assert sum(LengthTracker.calculate_route_length(r) for r in ar.routes if r.net == 6) == 10


def test_sqlite_cache_hit_preserves_pre_tuning_fragments_and_physical_result(tmp_path):
    cold, group = setup_pair(via_counts=(1, 0, 1, 1))
    key = CacheKey.compute("synthetic split pair input", cold.rules, cold.rules.grid_resolution)
    cache = RoutingCache(cache_dir=tmp_path)
    # The CLI writes this cache boundary before optimization and group tuning.
    cache.put(key, cold.routes, cold.get_statistics(), route_usage=cold.grid.export_route_usage())
    cold.apply_match_group_tuning([group], verbose=False)

    reopened = RoutingCache(cache_dir=tmp_path)
    hit = reopened.get(key)
    assert hit is not None
    restored = reopened.deserialize_routes(hit.routes_data)
    assert len(restored) == 8
    assert sum(r.is_escape for r in restored) == 4
    assert sum(len(r.vias) for r in restored) == 3
    warm, group = setup_pair(via_counts=(1, 0, 1, 1))
    warm.restore_route_snapshot(restored)
    warm.apply_match_group_tuning([group], verbose=False)
    assert warm.routes == cold.routes
    assert (physical_length(warm, 1) + physical_length(warm, 2)) / 2 == pytest.approx(
        11.6, abs=0.02
    )
