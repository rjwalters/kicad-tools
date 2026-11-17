"""Pad-centre join emits only 0/45/90/135 segments (Issue #8296)."""

from types import SimpleNamespace

import pytest

from kicad_tools.cli.route_cmd import _merge_checkpoint_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.quantize import is_45_aligned


def _router(routes, pads):
    return SimpleNamespace(
        routes=routes,
        pads={(p.ref, p.pin): p for p in pads},
        grid=SimpleNamespace(resolution=0.1),
        rules=SimpleNamespace(trace_clearance=0.2),
    )


def _pad(x, y):
    return Pad(x=x, y=y, width=0.5, height=0.5, net=1, net_name="N", ref="U1", pin="1")


def _seg(x1, y1, x2, y2):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=1, net_name="N")


def _check(merged, run):
    segs = merged[0].segments
    assert all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1) for s in segs)
    assert run in [(s.x1, s.y1, s.x2, s.y2) for s in segs]  # run untouched


@pytest.mark.parametrize("dy", [0.1, -0.1])
def test_end_only_join_never_tilts_run(dy):
    # Run ends one grid step off the pad axis (the 17.9 mm tilt case).
    run = (10.5, 10.0, 28.4, 10.0)
    route = Route(net=1, net_name="N", segments=[_seg(*run)])
    merged = _merge_checkpoint_routes([route], _router([route], [_pad(28.5, 10.0 + dy)]))
    _check(merged, run)


def test_run_end_on_pad_axis_is_extended_without_jog():
    route = Route(net=1, net_name="N", segments=[_seg(10.0, 10.0, 28.4, 10.0)])
    merged = _merge_checkpoint_routes([route], _router([route], [_pad(28.5, 10.0)]))
    assert [(s.x1, s.y1, s.x2, s.y2) for s in merged[0].segments] == [(10.0, 10.0, 28.5, 10.0)]


def test_jog_branch_dogleg_keeps_run():
    run = (9.6, 9.95, 20.0, 9.95)
    jog = _seg(9.5, 10.0, 9.6, 9.95)
    route = Route(net=1, net_name="N", segments=[jog, _seg(*run)])
    merged = _merge_checkpoint_routes([route], _router([route], [_pad(9.55, 10.03)]))
    _check(merged, run)


def test_join_skipped_when_dogleg_blocked():
    run = (10.5, 10.0, 28.4, 10.0)
    route = Route(net=1, net_name="N", segments=[_seg(*run)])
    foreign = Route(
        net=2,
        net_name="O",
        segments=[
            Segment(
                x1=28.4, y1=10.3, x2=28.6, y2=10.3, width=0.2, layer=Layer.F_CU, net=2, net_name="O"
            )
        ],
    )
    merged = _merge_checkpoint_routes(
        [route, foreign], _router([route, foreign], [_pad(28.5, 10.1)])
    )
    assert [(s.x1, s.y1, s.x2, s.y2) for s in merged[0].segments] == [run]
