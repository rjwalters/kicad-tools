"""Width-aware collinear fuse used before diff-pair skew tuning (Issue #7548)."""

from types import SimpleNamespace

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.merge import merge_collinear_same_width
from kicad_tools.router.primitives import Route, Segment, Via


def _seg(x1, y1, x2, y2, width=0.2, layer=Layer.F_CU, net=1):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net, net_name="N+")


def _router(*routes):
    return SimpleNamespace(routes=list(routes))


def _route(segments, vias=(), net=1):
    return Route(net=net, net_name="N+", segments=list(segments), vias=list(vias))


def _steps(n, step=0.05, **kw):
    return [_seg(i * step, 0.0, (i + 1) * step, 0.0, **kw) for i in range(n)]


def test_fuses_a_straight_chain_into_one_segment_of_equal_length():
    route = _route(_steps(200))
    assert merge_collinear_same_width(_router(route)) == 199
    (seg,) = route.segments
    assert (seg.x1, seg.y1) == (0.0, 0.0)
    assert abs(seg.x2 - 10.0) < 1e-9 and seg.y2 == 0.0
    assert seg.width == 0.2


def test_never_fuses_across_a_width_change():
    route = _route(
        _steps(100, width=0.2)
        + [_seg(5.0 + i * 0.05, 0, 5.05 + i * 0.05, 0, 0.25) for i in range(100)]
    )
    merge_collinear_same_width(_router(route))
    assert [s.width for s in route.segments] == [0.2, 0.25]


def test_keeps_a_corner():
    route = _route([_seg(0, 0, 1, 0), _seg(1, 0, 2, 0), _seg(2, 0, 2, 1)])
    merge_collinear_same_width(_router(route))
    assert [(s.x2, s.y2) for s in route.segments] == [(2, 0), (2, 1)]


def test_keeps_a_staircase_that_drifts_off_the_line():
    # Each joint is only 0.02 mm off the previous direction, so a pairwise
    # "same direction" test would pass; the chord test must not.
    segs = [_seg(i, 0.02 * i * i, i + 1, 0.02 * (i + 1) * (i + 1)) for i in range(6)]
    route = _route(segs)
    assert merge_collinear_same_width(_router(route), tolerance_mm=1e-3) == 0
    assert len(route.segments) == 6


def test_keeps_a_joint_another_segment_ends_on():
    branch = _route([_seg(5.0, 0.0, 5.0, 3.0)])
    route = _route(_steps(200))
    merge_collinear_same_width(_router(route, branch))
    assert any(abs(s.x2 - 5.0) < 1e-9 for s in route.segments)
    assert len(route.segments) == 2


def test_keeps_a_via_joint():
    route = _route(
        _steps(200),
        vias=[Via(x=5.0, y=0.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)],
    )
    merge_collinear_same_width(_router(route))
    assert len(route.segments) == 2


def test_min_chain_leaves_short_runs_alone():
    short = _route(_steps(20))  # 1.0 mm of 0.05 mm steps
    long = _route(_steps(200), net=2)
    assert merge_collinear_same_width(_router(short, long), min_chain_mm=5.0) == 199
    assert len(short.segments) == 20
    assert len(long.segments) == 1


def test_only_listed_nets_and_never_escape_routes():
    a = _route(_steps(50))
    b = Route(net=2, net_name="M+", segments=_steps(50, net=2), vias=[])
    esc = _route(_steps(50), net=3)
    esc.is_escape = True
    assert merge_collinear_same_width(_router(a, b, esc), {"N+"}) == 49
    assert len(a.segments) == 1
    assert len(b.segments) == 50
    assert len(esc.segments) == 50
