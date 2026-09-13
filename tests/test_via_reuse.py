"""Via reuse must preserve committed geometry and join each candidate layer."""

import copy

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.router.via_reuse import reuse_same_net_vias


def candidate():
    return Route(
        net=1,
        net_name="N1",
        segments=[
            Segment(0, 1, 1, 1, 0.2, Layer.F_CU, net=1),
            Segment(1, 1, 2, 1, 0.25, Layer.IN2_CU, net=1),
        ],
        vias=[Via(1, 1, 0.25, 0.6, (Layer.F_CU, Layer.IN2_CU), net=1)],
    )


@pytest.mark.parametrize("end_layer", [Layer.B_CU, Layer.IN1_CU])
def test_reuses_barrel_without_mutating_existing_copper(end_layer):
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.15, 1.1, 0.25, 0.45, (Layer.F_CU, end_layer), net=1)]
    )
    before = copy.deepcopy(existing)
    route = candidate()
    original_segments = list(route.segments)
    reuse_same_net_vias(route, [existing], 0.25)
    assert existing == before
    assert not route.vias
    assert route.segments[:2] == original_segments
    for layer in (Layer.F_CU, Layer.IN2_CU):
        stubs = [s for s in route.segments[2:] if s.layer == layer]
        assert stubs[0].start == (1, 1)
        assert stubs[-1].end == (1.15, 1.1)
        assert all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1) for s in stubs)


@pytest.mark.parametrize(
    "net,layers,x,is_micro",
    [
        (2, (Layer.F_CU, Layer.B_CU), 1.1, False),
        (1, (Layer.F_CU, Layer.IN1_CU), 1.1, True),
        (1, (Layer.F_CU, Layer.B_CU), 5, False),
    ],
)
def test_ineligible_via_does_not_change_candidate(net, layers, x, is_micro):
    route = candidate()
    before = copy.deepcopy(route)
    existing = Route(
        net=net, net_name=f"N{net}", vias=[Via(x, 1, 0.25, 0.6, layers, net=net, is_micro=is_micro)]
    )
    reuse_same_net_vias(route, [existing], 0.25)
    assert route == before


@pytest.mark.parametrize("marker", ["is_micro", "in_pad"])
def test_fixed_escape_candidate_is_not_replaced(marker):
    route = candidate()
    setattr(route.vias[0], marker, True)
    before = copy.deepcopy(route)
    existing = Route(
        net=1, net_name="N1", vias=[Via(1.1, 1, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1)]
    )
    reuse_same_net_vias(route, [existing], 0.25)
    assert route == before
