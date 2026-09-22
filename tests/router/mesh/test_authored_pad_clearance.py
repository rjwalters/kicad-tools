"""Mesh pad obstacles preserve per-net authored electrical floors."""

import pytest

from kicad_tools.router.layers import LayerStack
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,blocked", [(0.15, False), (0.8, True)])
@pytest.mark.parametrize("mode", ["planar", "layered", "via"])
def test_mesh_pad_clearance_uses_either_net_floor(strict_net, floor, blocked, mode):
    rules = DesignRules(
        trace_width=0.2, trace_clearance=0.15, net_clearance_floors={strict_net: floor, 3: 4.0}
    )
    pf = MeshPathfinder(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        [Pad(5, 5, 1, 1, 2, "foreign")],
        rules,
        layer_stack=LayerStack.two_layer(),
    )
    point = (6.2, 5)
    if mode == "via":
        assert pf._via_allowed_at(point, 1, 0.45, (0, 1), {}) is not blocked
    else:
        rects = pf._keepouts(1, 0.25) if mode == "planar" else pf._keepouts_layer(1, 0.25, 0)
        assert (
            any(x0 <= point[0] <= x1 and y0 <= point[1] <= y1 for x0, y0, x1, y1 in rects)
            is blocked
        )
