"""A via barrel occupies every copper layer between its endpoint layers."""

import math

import pytest

from kicad_tools.router.core import Autorouter, _TraceResolverTransaction
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

LAYERS = (Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.IN3_CU, Layer.IN4_CU, Layer.B_CU)
SPANS = (
    (Layer.F_CU, Layer.B_CU),
    (Layer.B_CU, Layer.F_CU),
    (Layer.F_CU, Layer.IN2_CU),
    (Layer.IN1_CU, Layer.IN4_CU),
    (Layer.IN2_CU, Layer.B_CU),
)


def rules():
    return DesignRules(trace_width=0.2, trace_clearance=0.2, via_clearance=0.2, grid_resolution=0.2)


@pytest.mark.parametrize("span", SPANS)
@pytest.mark.parametrize("layer", LAYERS)
def test_through_blind_buried_span_against_foreign_trace(span, layer):
    grid = RoutingGrid(20, 20, rules(), layer_stack=LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig())
    trace = Route(net=1, net_name="TRACE", segments=[Segment(5, 10, 15, 10, 0.2, layer, 1)])
    grid.mark_route(trace)
    via = Via(x=10, y=10, diameter=0.6, drill=0.3, layers=span, net=2)
    indices = sorted(LAYERS.index(endpoint) for endpoint in span)
    overlaps = indices[0] <= LAYERS.index(layer) <= indices[1]
    valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)
    assert valid is not overlaps
    assert clearance == pytest.approx(-0.4) if overlaps else math.isinf(clearance)
    assert location == (10, 10) if overlaps else location is None
    deficit, _ = grid.worst_via_segment_deficit(via, exclude_net=2)
    assert deficit == pytest.approx(0.6 if overlaps else 0)
    # Same-net contact remains legal across exactly the same span.
    assert grid.validate_via_clearance(via, exclude_net=1) == (True, math.inf, None)
    # Physical space, rather than layer exclusion, makes this one legal.
    via.y = 10.7
    assert grid.validate_via_clearance(via, exclude_net=2)[0]


@pytest.mark.parametrize("layer", [Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU])
@pytest.mark.parametrize("via_first", [False, True])
def test_transaction_rejects_barrel_trace_overlap_in_either_commit_order(layer, via_first):
    router = Autorouter(
        20, 20, rules=rules(), layer_stack=LayerStack.four_layer_all_signal(), force_python=True
    )
    trace = Route(net=1, net_name="TRACE", segments=[Segment(5, 10, 15, 10, 0.2, layer, 1)])
    barrel = Route(
        net=2,
        net_name="BARREL",
        vias=[Via(x=10, y=10, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=2)],
    )
    existing, proposed = (barrel, trace) if via_first else (trace, barrel)
    router.routes.append(existing)
    router._mark_route(existing)
    transaction = _TraceResolverTransaction(router)
    transaction.begin()
    router.routes.append(proposed)
    router._mark_route(proposed)
    assert transaction.validate_committed_geometry() is False
    transaction.rollback()
    assert router.routes == [existing]
    assert router.grid.routes == [existing]
