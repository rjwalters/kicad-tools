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
        net_name={1: "USB_D+", 2: "USB_D-"}.get(net, "OTHER"),
        is_escape=escape,
        segments=[Segment(a, y, b, y, 0.2, Layer.F_CU, net=net)],
    )


def setup(shorter=1, *, native=False, escape_length=1, split=False):
    if native:
        from kicad_tools.router.cpp_backend import is_cpp_available

        if not is_cpp_available():
            pytest.skip("matching C++ backend unavailable")
    ar = Autorouter(width=60, height=40, force_python=not native)
    ar.net_names = {1: "USB_D+", 2: "USB_D-"}
    for net, y in [(1, 10), (2, 20)]:
        end = 5 + escape_length + (9 if net == shorter else 13)
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
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("USB_D+", "USB_D-")]
    assert result.reason == "tuned"
    assert abs(length(ar, 1) - length(ar, 2)) <= 0.5
    assert result.skew_after_mm == pytest.approx(abs(length(ar, 1) - length(ar, 2)))
    assert ar.diffpair_length_tracker.lengths[shorter] == pytest.approx(length(ar, shorter))
    assert all(any(r is escape for r in ar.routes) for escape in escapes)
    assert [(r.segments, r.vias) for r in escapes] == fixed_geometry
    assert sum(r.net == shorter and not r.is_escape for r in ar.routes) == 1
    assert not any(r in ar.routes for r in original if r.net == shorter and not r.is_escape)
    assert {id(r) for r in ar.grid.routes} == {id(r) for r in ar.routes} | {id(independent)}
    enforce_connectivity_invariant(
        ar, connected, phase="split pair tuning", strict=True, quiet=True
    )
    segment = next(
        s
        for r in ar.routes
        if r.net == shorter and not r.is_escape
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
    second = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("USB_D+", "USB_D-")]
    assert second.reason == "already_within_tolerance"
    assert all(a is b for a, b in zip(first, ar.routes, strict=True))


def test_escape_only_and_via_identity_are_preserved():
    ar, pairs = setup()
    for route in ar.routes:
        route.is_escape = True
    via = Via(6, 10, 0.6, 0.3, (Layer.F_CU, Layer.B_CU), net=1)
    ar.routes[0].vias.append(via)
    original = list(ar.routes)
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("USB_D+", "USB_D-")]
    assert result.reason == "no_suitable_segment"
    assert all(a is b for a, b in zip(original, ar.routes, strict=True))
    assert ar.routes[0].vias[0] is via


def test_earlier_foreign_fragment_rejects_and_retains_original_objects():
    ar, pairs = setup()
    # The shorter half bulges away from the partner at y=20, toward y<10.
    ar.routes.extend([fragment(9, 5, 20, 9.5), fragment(9, 2, 3, 35)])
    for route in ar.routes[-2:]:
        ar._mark_route(route)
    original = list(ar.routes)
    geometry = copy.deepcopy([(r.segments, r.vias) for r in original])
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("USB_D+", "USB_D-")]
    assert result.reason == "post_insertion_drc_violation"
    assert all(a is b for a, b in zip(original, ar.routes, strict=True))
    assert [(r.segments, r.vias) for r in ar.routes] == geometry
    assert result.skew_after_mm == pytest.approx(4)


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
        ar.net_class_map = {"USB_D+": NetClassRouting(name="No tuning", length_critical=False)}
    else:
        # Trim the longer channel to the same complete-net length.
        ar.routes[-1].segments[0].x2 -= 4
        ar.restore_route_snapshot(ar.routes)
    original = list(ar.routes)
    result = ar.apply_diffpair_length_tuning(pairs, verbose=False)[("USB_D+", "USB_D-")]
    assert result.reason == ("not_length_critical" if disabled else "already_within_tolerance")
    assert all(a is b for a, b in zip(original, ar.routes, strict=True))
    assert ar.diffpair_length_tracker.lengths[1] == pytest.approx(length(ar, 1))
    assert ar.diffpair_length_tracker.lengths[2] == pytest.approx(length(ar, 2))
