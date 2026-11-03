"""Checkpoint serialisation collapses per-cell segments (Issue #8270)."""

from kicad_tools.cli.route_cmd import _merge_checkpoint_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _seg(x1, x2, width=0.2, y=1.0):
    return Segment(x1=x1, y1=y, x2=x2, y2=y, width=width, layer=Layer.F_CU, net=1, net_name="N")


def test_straight_run_becomes_one_segment_without_mutating_input():
    segs = [_seg(i * 0.05, (i + 1) * 0.05) for i in range(100)]
    route = Route(net=1, net_name="N", segments=segs)
    merged = _merge_checkpoint_routes([route])
    assert len(merged[0].segments) == 1
    seg = merged[0].segments[0]
    assert (seg.x1, seg.x2) == (0.0, segs[-1].x2)
    assert len(route.segments) == 100  # live routes untouched


def test_width_change_is_not_merged():
    route = Route(net=1, net_name="N", segments=[_seg(0, 1), _seg(1, 2, width=0.3)])
    assert len(_merge_checkpoint_routes([route])[0].segments) == 2


def test_failure_falls_back_to_raw(monkeypatch):
    import kicad_tools.router.optimizer.merge as m

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(m, "merge_collinear_same_width", boom)
    routes = [Route(net=1, net_name="N", segments=[_seg(0, 1), _seg(1, 2)])]
    assert _merge_checkpoint_routes(routes) is routes
