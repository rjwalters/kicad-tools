"""Real skew tuning must retain split-net ownership and refresh collision views."""

import copy

import pytest

from kicad_tools.router.connectivity_invariant import (
    enforce_connectivity_invariant,
    snapshot_connectivity,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_detection import detect_diff_pairs
from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.optimizer import make_collision_checker
from kicad_tools.router.primitives import Pad, Route, Segment, Via


def fragment(net, a, b, y, escape=False):
    return Route(
        net=net,
        net_name={1: "SIG_P", 2: "SIG_N"}.get(net, "OTHER"),
        is_escape=escape,
        segments=[Segment(a, y, b, y, 0.2, Layer.F_CU, net=net)],
    )


def setup(shorter=1, *, native=False, escape_length=1, split=False):
    if native:
        from kicad_tools.router.cpp_backend import is_cpp_available

        if not is_cpp_available():
            pytest.skip("matching C++ backend unavailable")
    ar = Autorouter(width=60, height=40, force_python=not native)
    ar.net_names = {1: "SIG_P", 2: "SIG_N"}
    for net, y in [(1, 10), (2, 20)]:
        end = 5 + escape_length + (12 if net == shorter else 13)
        boundary = 5 + escape_length
        ar.routes.append(fragment(net, 5, boundary, y, True))
        if split:
            ar.routes.extend(
                [fragment(net, boundary, boundary + 3, y), fragment(net, boundary + 3, end, y)]
            )
        else:
            ar.routes.append(fragment(net, boundary, end, y))
        ar.nets[net] = [(f"U{net}", "1"), (f"J{net}", "1")]
        for key, x in zip(ar.nets[net], [5, end], strict=True):
            ar.pads[key] = Pad(
                x=x,
                y=y,
                width=0.4,
                height=0.4,
                layer=Layer.F_CU,
                net=net,
                net_name=ar.net_names[net],
                ref=key[0],
                pin=key[1],
            )
    for route in ar.routes:
        ar._mark_route(route)
    return ar, detect_diff_pairs(net_names=ar.net_names)


def length(ar, net):
    return sum(LengthTracker.calculate_route_length(r) for r in ar.routes if r.net == net)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "shorter,escape_length,split", [(1, 1, False), (2, 1, True), (1, 17, True)]
)
def test_split_pair_publishes_once_and_refreshes_state(native, shorter, escape_length, split):
    ar, pairs = setup(shorter, native=native, escape_length=escape_length, split=split)
    original = list(ar.routes)
    escapes = [r for r in original if r.is_escape]
    fixed_geometry = copy.deepcopy([(r.segments, r.vias) for r in escapes])
    independent = fragment(9, 2, 3, 35)
    ar.grid.mark_route(independent)
    checker = make_collision_checker(ar.grid, ignore_overflow=True)
    connected = snapshot_connectivity(ar)
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("SIG_P", "SIG_N")]
    assert result.reason == "tuned"
    assert abs(length(ar, 1) - length(ar, 2)) <= 0.5
    assert result.skew_after_mm == pytest.approx(abs(length(ar, 1) - length(ar, 2)))
    assert ar.diffpair_length_tracker.lengths[shorter] == pytest.approx(length(ar, shorter))
    # Issue #7891: an escape stub may host the bump when it is the best host
    # (the 17 mm synthetic escape here); the bump is written into that escape
    # fragment, in place of the stub.  Every other escape is untouched.
    hosted = escape_length > 2.0
    for escape, geometry in zip(escapes, fixed_geometry, strict=True):
        if hosted and escape.net == shorter:
            [edited] = [r for r in ar.routes if r.is_escape and r.net == escape.net]
            assert edited is not escape
            assert all(a is b for a, b in zip(edited.vias, escape.vias, strict=True))
            assert any(s.y1 != s.y2 for s in edited.segments)
        else:
            assert any(r is escape for r in ar.routes)
            assert (escape.segments, escape.vias) == geometry
    assert sum(r.net == shorter and not r.is_escape for r in ar.routes) == 1
    assert not any(r in ar.routes for r in original if r.net == shorter and not r.is_escape)
    assert {id(r) for r in ar.grid.routes} == {id(r) for r in ar.routes} | {id(independent)}
    enforce_connectivity_invariant(
        ar, connected, phase="split pair tuning", strict=True, quiet=True
    )
    segment = next(
        s
        for r in ar.routes
        if r.net == shorter and r.is_escape == hosted
        for s in r.segments
        if s.y1 != s.y2
    )
    assert not checker.path_is_clear(
        segment.x1, segment.y1, segment.x2, segment.y2, segment.layer, segment.width, 99
    )
    gx, gy = ar.grid.world_to_grid((segment.x1 + segment.x2) / 2, (segment.y1 + segment.y2) / 2)
    if native:
        assert ar.grid._cpp_grid.is_blocked_for_net(gx, gy, 0, 99)
    if ar.grid._rtree_available:
        indexed = [s for items in ar.grid._seg_rtree_items.values() for s in items.values()]
        assert {id(s) for s in indexed} == {id(s) for r in ar.grid.routes for s in r.segments}
    first = list(ar.routes)
    second = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("SIG_P", "SIG_N")]
    assert second.reason == "already_within_tolerance"
    assert all(a is b for a, b in zip(first, ar.routes, strict=True))


def test_escape_only_net_hosts_its_bump_in_the_escape_and_keeps_via_identity():
    """Issue #7891: escape copper is a host like any other (it used to be
    refused, ``no_suitable_segment``).  The bump lands in the escape fragment
    it replaced, still an escape; no channel fragment is invented, and the
    other fragments and the via keep their identity."""
    ar, pairs = setup()
    for route in ar.routes:
        route.is_escape = True
    via = Via(6, 10, 0.6, 0.3, (Layer.F_CU, Layer.B_CU), net=1)
    ar.routes[0].vias.append(via)
    original = list(ar.routes)
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("SIG_P", "SIG_N")]
    assert result.reason == "tuned"
    assert abs(length(ar, 1) - length(ar, 2)) <= 0.5
    assert len(ar.routes) == len(original) and all(r.is_escape for r in ar.routes)
    assert ar.routes[0] is original[0] and ar.routes[0].vias[0] is via
    assert ar.routes[1] is not original[1]
    assert any(s.y1 != s.y2 for s in ar.routes[1].segments)
    assert all(a is b for a, b in zip(original[2:], ar.routes[2:], strict=True))


def test_escape_bump_never_touches_its_own_pad():
    """Issue #7891: the own-copper rule holds an escape-hosted bump off its
    own pads: no off-line bump copper within the board clearance of one."""
    from kicad_tools.router.grid import pad_segment_edge_clearance

    ar, pairs = setup(escape_length=17, split=True)
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("SIG_P", "SIG_N")]
    assert result.reason == "tuned"
    bump = [s for r in ar.routes if r.net == 1 for s in r.segments if s.y1 != 10 or s.y2 != 10]
    assert bump
    for pad in (p for p in ar.pads.values() if p.net == 1):
        assert (
            min(pad_segment_edge_clearance(pad, s.x1, s.y1, s.x2, s.y2, s.width / 2) for s in bump)
            >= ar.rules.trace_clearance - 1e-9
        )


def test_earlier_foreign_fragment_rejects_and_retains_original_objects():
    ar, pairs = setup()
    # The shorter half bulges away from the partner at y=20, toward y<10.  The
    # placement search (#6788) also tries the reverse side, so block that too.
    # Bumps stay below the pair gap (#7521), so the blockers sit close.
    ar.routes.extend([fragment(9, 5, 20, 9.7), fragment(9, 5, 20, 10.3), fragment(9, 2, 3, 35)])
    for route in ar.routes[-3:]:
        ar._mark_route(route)
    original = list(ar.routes)
    geometry = copy.deepcopy([(r.segments, r.vias) for r in original])
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("SIG_P", "SIG_N")]
    assert result.reason == "post_insertion_drc_violation"
    assert all(a is b for a, b in zip(original, ar.routes, strict=True))
    assert [(r.segments, r.vias) for r in ar.routes] == geometry
    assert result.skew_after_mm == pytest.approx(1)


@pytest.mark.parametrize("reverse", [False, True])
def test_fragment_order_and_vias_are_preserved_once(reverse):
    ar, pairs = setup(split=True)
    vias = [
        Via(6, 10, 0.6, 0.3, (Layer.F_CU, Layer.B_CU), net=1),
        Via(9, 10, 0.6, 0.3, (Layer.F_CU, Layer.B_CU), net=1),
    ]
    ar.routes[0].vias.append(vias[0])
    ar.routes[1].vias.append(vias[1])
    if reverse:
        ar.routes.reverse()
    ar.restore_route_snapshot(ar.routes)
    original = list(ar.routes)
    ar.apply_diffpair_length_tuning(pairs, verbose=False)
    assert abs(length(ar, 1) - length(ar, 2)) <= 0.5
    assert sorted(id(v) for r in ar.routes for v in r.vias) == sorted(map(id, vias))
    assert any(r is old for r in ar.routes for old in original if old.is_escape and old.net == 1)
    assert ar.diffpair_length_tracker.lengths[1] == pytest.approx(length(ar, 1))
    assert ar.diffpair_length_tracker.lengths[2] == pytest.approx(length(ar, 2))


@pytest.mark.parametrize("disabled", [False, True])
def test_untuned_fragments_keep_identity_and_complete_tracker(disabled):
    from kicad_tools.router.rules import NetClassRouting

    ar, pairs = setup(split=True)
    if disabled:
        ar.net_class_map = {"SIG_P": NetClassRouting(name="No tuning", length_critical=False)}
    else:
        # Trim the longer channel to the same complete-net length.
        ar.routes[-1].segments[0].x2 -= 1
        ar.restore_route_snapshot(ar.routes)
    original = list(ar.routes)
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("SIG_P", "SIG_N")]
    assert result.reason == ("not_length_critical" if disabled else "already_within_tolerance")
    assert all(a is b for a, b in zip(original, ar.routes, strict=True))
    assert ar.diffpair_length_tracker.lengths[1] == pytest.approx(length(ar, 1))
    assert ar.diffpair_length_tracker.lengths[2] == pytest.approx(length(ar, 2))
