"""Physical shadow copper checks must use manufactured barrels and net identity."""

import pytest
from tests.test_diffpair_shadow import _pad_gate_router, _via_gate_seg, _via_gate_via
from kicad_tools.router.primitives import Route
from kicad_tools.router.layers import Layer


@pytest.mark.parametrize("micro", [False, True])
@pytest.mark.parametrize("same_net", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_short_span_barrel_against_outer_trace(micro, same_net, reverse):
    dpr = _pad_gate_router()
    via = _via_gate_via(5, 5, 7 if same_net else 99, "V", layers=(Layer.IN1_CU, Layer.IN2_CU))
    via.is_micro = micro
    seg = _via_gate_seg(4, 5.55, 6, 5.55, layer=Layer.B_CU)
    foreign = Route(
        net_name="CONTROL",
        net=seg.net if reverse else via.net,
        segments=[seg] if reverse else [],
        vias=[] if reverse else [via],
    )
    with dpr._shadow_foreign_copper(foreign):
        deficit = (dpr._via_copper_deficit(via) if reverse else dpr._segment_via_deficit(seg))[0]
    assert (deficit > 0) is (not micro and not same_net)


@pytest.mark.parametrize("same_net", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_endpoint_contact_respects_net_identity(same_net, reverse):
    dpr = _pad_gate_router()
    via = _via_gate_via(5, 5, 7 if same_net else 99, "V")
    seg = _via_gate_seg(5, 5, 8, 5)
    foreign = Route(
        net_name="CONTROL",
        net=seg.net if reverse else via.net,
        segments=[seg] if reverse else [],
        vias=[] if reverse else [via],
    )
    with dpr._shadow_foreign_copper(foreign):
        deficit = (dpr._via_copper_deficit(via) if reverse else dpr._segment_via_deficit(seg))[0]
    assert (deficit > 0) is (not same_net)


@pytest.mark.parametrize("both_micro", [False, True])
def test_disjoint_nominal_spans_use_physical_barrel(both_micro):
    dpr = _pad_gate_router()
    a = _via_gate_via(5, 5, 7, "A", layers=(Layer.F_CU, Layer.IN1_CU))
    b = _via_gate_via(5.8, 5, 99, "B", layers=(Layer.IN2_CU, Layer.B_CU))
    a.is_micro = b.is_micro = both_micro
    with dpr._shadow_foreign_copper(Route(net_name="CONTROL", net=99, vias=[b])):
        assert (dpr._via_copper_deficit(a)[0] > 0) is (not both_micro)


@pytest.mark.parametrize("distance,violates", [(0.65, True), (0.8, False)])
def test_barrel_uses_via_clearance_floor(distance, violates):
    dpr = _pad_gate_router()
    dpr.autorouter.rules.trace_clearance = 0.1
    dpr.autorouter.rules.via_clearance = 0.3
    seg = _via_gate_seg(4, 5 + distance, 6, 5 + distance)
    via = _via_gate_via(5, 5, 99, "V")
    with dpr._shadow_foreign_copper(Route(net_name="CONTROL", net=99, vias=[via])):
        assert (dpr._segment_via_deficit(seg)[0] > 0) is violates
