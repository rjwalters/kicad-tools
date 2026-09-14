"""Pair tuning can use common copper spans without modifying fixed escapes."""

import math
from dataclasses import replace

import pytest
from shapely.geometry import LineString
from shapely.ops import unary_union

from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import MatchGroup
from kicad_tools.router.match_group_tuning import _shared_pair_window, tune_match_group_v2
from kicad_tools.router.primitives import Route, Segment


def segment(net, x1, y1, x2, y2):
    return Segment(x1, y1, x2, y2, 0.2, Layer.F_CU, net=net)


@pytest.mark.parametrize("angle", [0, math.pi / 4, math.pi / 2])
@pytest.mark.parametrize("reverse", [False, True])
def test_window_preserves_copper_and_input_identity(angle, reverse):
    def rotate(s):
        c, sn = math.cos(angle), math.sin(angle)
        return replace(
            s,
            x1=s.x1 * c - s.y1 * sn,
            y1=s.x1 * sn + s.y1 * c,
            x2=s.x2 * c - s.y2 * sn,
            y2=s.x2 * sn + s.y2 * c,
        )

    p = Route(net=1, net_name="N1", segments=[rotate(segment(1, 0, 0, 10, 0))])
    nseg = segment(2, 2, 0.4, 12, 0.4)
    if reverse:
        nseg = replace(nseg, x1=nseg.x2, x2=nseg.x1)
    n = Route(net=2, net_name="N2", segments=[rotate(nseg)])
    originals = (p.segments, n.segments, p.segments[0], n.segments[0])
    prepared = _shared_pair_window(p, n, 1)
    assert prepared is not None
    pp, pi, nn, ni = prepared
    for old, new in ((p, pp), (n, nn)):
        a = unary_union([LineString([s.start, s.end]) for s in old.segments])
        b = unary_union([LineString([s.start, s.end]) for s in new.segments])
        assert a.hausdorff_distance(b) < 1e-9
        assert LengthTracker.calculate_route_length(old) == pytest.approx(
            LengthTracker.calculate_route_length(new)
        )
    for host in (pp.segments[pi], nn.segments[ni]):
        assert math.hypot(host.x2 - host.x1, host.y2 - host.y1) == pytest.approx(8)
    assert p.segments is originals[0] and n.segments is originals[1]
    assert p.segments[0] is originals[2] and n.segments[0] is originals[3]
    assert _shared_pair_window(p, n, 1, {id(n.segments[0])}) is None


def test_no_window_for_crossing_or_disjoint_hosts():
    p = Route(net=1, net_name="N1", segments=[segment(1, 0, 0, 10, 0)])
    for nseg in (segment(2, 5, -5, 5, 5), segment(2, 11, 0.4, 20, 0.4)):
        assert _shared_pair_window(p, Route(net=2, net_name="N2", segments=[nseg]), 1) is None


@pytest.mark.parametrize("reject", [False, True])
def test_fragmented_pair_tunes_shared_span_and_rolls_back_atomically(monkeypatch, reject):
    p = Route(net=1, net_name="N1", segments=[segment(1, 0, 0, 1, 0), segment(1, 1, 0, 10, 0)])
    n = Route(net=2, net_name="N2", segments=[segment(2, 0, 0.4, 10, 0.4)])
    fixed = p.segments[0]
    original_p, original_n = p.segments, n.segments
    corpus = {
        1: p,
        2: n,
        3: Route(net=3, net_name="N3", segments=[segment(3, 0, 5, 11.13, 5)]),
        4: Route(net=4, net_name="N4", segments=[segment(4, 0, 5.4, 11.13, 5.4)]),
    }
    if reject:
        monkeypatch.setattr(
            "kicad_tools.router.match_group_tuning._post_insertion_clearance_detail_pair_group",
            lambda **kwargs: "blocked candidate",
        )
    result = tune_match_group_v2(
        MatchGroup(name="shared", net_ids=[], pair_ids=[(1, 2), (3, 4)], tolerance=0.05),
        corpus,
        intra_group_clearance_mm=0.15,
        intra_pair_clearance_mm=0.1,
        grid_resolution_mm=0.127,
        fixed_segment_ids={id(fixed)},
    )
    assert p.segments is original_p and n.segments is original_n
    if reject:
        assert result[1][0] is p and result[2][0] is n
        assert not result[1][1].success and not result[2][1].success
    else:
        assert result[1][1].success and result[2][1].success
        assert result[1][0].segments[0] is fixed
        for net in (1, 2):
            assert LengthTracker.calculate_route_length(result[net][0]) == pytest.approx(
                11.13, abs=1e-7
            )
            chain = result[net][0].segments
            for a, b in zip(chain[:-1], chain[1:], strict=True):
                assert a.end == pytest.approx(b.start)
