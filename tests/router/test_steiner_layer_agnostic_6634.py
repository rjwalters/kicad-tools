"""Steiner virtual pads are layer-agnostic (Issue #6634).

``build_rsmt`` used to pin each branch point to the first terminal's layer, so
an edge arriving on the other layer paid a via just to land on the virtual pad.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.algorithms.steiner import (
    build_rsmt,
    pin_steiner_arrival_layers,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment

NET = 3


def _pad(x, y, layer=Layer.F_CU) -> Pad:
    return Pad(
        x=x, y=y, width=1.0, height=1.0, net=NET, net_name="N", layer=layer, ref="U", pin="1"
    )


class TestVirtualPad:
    def test_steiner_pad_is_layer_agnostic(self):
        pads, _ = build_rsmt([_pad(0, 0), _pad(10, 0), _pad(5, 8)])
        steiner = [p for p in pads if p.steiner_point]
        assert steiner and all(p.layer_agnostic for p in steiner)
        assert not any(p.layer_agnostic for p in pads if not p.steiner_point)

    def test_pin_to_arrival_layer(self):
        pads, edges = build_rsmt([_pad(0, 0), _pad(10, 0), _pad(5, 8)])
        s_idx = next(i for i, p in enumerate(pads) if p.steiner_point)
        s = pads[s_idx]
        seg = Segment(x1=0, y1=0, x2=s.x, y2=s.y, width=0.2, layer=Layer.B_CU, net=NET)
        edge = next(e for e in edges if s_idx in e)
        pin_steiner_arrival_layers(pads, edge, Route(net=NET, net_name="N", segments=[seg]))
        assert pads[s_idx].layer == Layer.B_CU
        assert not pads[s_idx].layer_agnostic
        assert pads[s_idx].steiner_point

    def test_pin_ignores_unrelated_route(self):
        pads, edges = build_rsmt([_pad(0, 0), _pad(10, 0), _pad(5, 8)])
        s_idx = next(i for i, p in enumerate(pads) if p.steiner_point)
        seg = Segment(x1=100, y1=100, x2=101, y2=100, width=0.2, layer=Layer.B_CU, net=NET)
        pin_steiner_arrival_layers(
            pads, next(e for e in edges if s_idx in e), Route(net=NET, net_name="N", segments=[seg])
        )
        assert pads[s_idx].layer_agnostic


def _route_mixed_layer_tee(force_python: bool):
    """R1 on F.Cu (terminal 0 -> old virtual-pad layer); R2/R3 on B.Cu."""
    router = Autorouter(
        width=40.0, height=30.0, origin_x=100.0, origin_y=100.0, force_python=force_python
    )
    for ref, (x, y), layer in (
        ("R1", (120.0, 124.0), Layer.F_CU),
        ("R2", (110.0, 112.0), Layer.B_CU),
        ("R3", (130.0, 112.0), Layer.B_CU),
    ):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 1.0,
                    "height": 1.0,
                    "net": NET,
                    "net_name": "N",
                    "layer": layer,
                }
            ],
        )
    routes = router.route_net(NET)
    assert len(routes) == 3
    router.routes = routes
    return routes, router


@pytest.mark.parametrize("force_python", [True, False], ids=["python", "cpp-if-built"])
def test_branch_edges_on_one_layer_need_no_via_at_steiner_point(force_python):
    """The two B.Cu edges meet at the branch cell without a landing via.

    Before #6634 the virtual pad was pinned to F.Cu (terminal 0's layer), so
    each B.Cu edge ended in a via to land on it (2 vias; 3 once the F.Cu
    edge is counted).  Now the first edge pins the point to B.Cu and only the
    unavoidable B.Cu -> F.Cu transition toward R1 remains.
    """
    routes, router = _route_mixed_layer_tee(force_python)
    vias = [v for r in routes for v in r.vias]
    assert len(vias) == 1
    b_edges = [r for r in routes if any(s.layer == Layer.B_CU for s in r.segments)]
    assert len(b_edges) >= 2
    assert sum(len(r.vias) for r in b_edges if all(s.layer == Layer.B_CU for s in r.segments)) == 0
    # Nothing for cleanup to repair: the surviving via is a real transition.
    assert router.cleanup_artifacts()["single_layer_vias_removed"] == 0
