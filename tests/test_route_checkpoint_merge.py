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


# --- Issue #8270 remaining ACs: pad-centre join, _partial merge, stub listing ---
from types import SimpleNamespace  # noqa: E402

from kicad_tools.cli.route_cmd import _partial_net_stubs  # noqa: E402
from kicad_tools.router.primitives import Pad  # noqa: E402


def _router(routes, pads):
    return SimpleNamespace(
        routes=routes,
        pads={(p.ref, p.pin): p for p in pads},
        grid=SimpleNamespace(resolution=0.1),
        rules=SimpleNamespace(trace_clearance=0.2),
    )


def _pad(x, y, net=1, ref="U1", pin="1"):
    return Pad(x=x, y=y, width=0.5, height=0.5, net=net, net_name="N", ref=ref, pin=pin)


def test_off_grid_pad_end_joins_pad_centre_without_jog():
    pad = _pad(1.03, 1.02)
    jog = Segment(
        x1=1.03, y1=1.02, x2=1.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1, net_name="N"
    )
    run = _seg(1.0, 5.0)
    route = Route(net=1, net_name="N", segments=[jog, run])
    merged = _merge_checkpoint_routes([route], _router([route], [pad]))
    first = merged[0].segments[0]
    assert (first.x1, first.y1) == (1.03, 1.02)
    assert len(merged[0].segments) == 1  # no sub-grid diagonal left
    assert len(route.segments) == 2  # live route untouched


def test_pad_join_skipped_when_it_violates_clearance():
    pad = _pad(1.03, 1.02)
    jog = Segment(
        x1=1.03, y1=1.02, x2=1.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1, net_name="N"
    )
    route = Route(net=1, net_name="N", segments=[jog, _seg(1.0, 5.0)])
    foreign = Route(
        net=2,
        net_name="O",
        segments=[
            Segment(
                x1=1.5, y1=1.12, x2=2.0, y2=1.12, width=0.2, layer=Layer.F_CU, net=2, net_name="O"
            )
        ],
    )
    merged = _merge_checkpoint_routes([route, foreign], _router([route, foreign], [pad]))
    assert len(merged[0].segments) == 2  # unchanged


def test_partial_net_stubs_lists_dangling_endpoint():
    pad = _pad(0.0, 1.0)
    done = Route(net=1, net_name="N", segments=[_seg(0.0, 2.0)])
    partial = Route(
        net=2,
        net_name="P",
        segments=[
            Segment(
                x1=0.0, y1=3.0, x2=2.0, y2=3.0, width=0.2, layer=Layer.F_CU, net=2, net_name="P"
            )
        ],
    )
    stubs = _partial_net_stubs(_router([done, partial], [pad, _pad(2.0, 1.0, ref="U2")]))
    assert {(s["net"], s["layer"]) for s in stubs} == {("P", "F_CU")}
    assert len(stubs) == 2
