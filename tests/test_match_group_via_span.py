"""Tuning must respect via copper between the declared endpoint layers."""

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_tuning import _post_insertion_clearance_detail_group
from kicad_tools.router.primitives import Route, Segment, Via


@pytest.mark.parametrize(
    "endpoints,layer,blocked",
    [
        ((Layer.F_CU, Layer.B_CU), Layer.IN2_CU, True),
        ((Layer.B_CU, Layer.F_CU), Layer.IN2_CU, True),
        ((Layer.IN1_CU, Layer.IN3_CU), Layer.IN2_CU, True),
        ((Layer.IN1_CU, Layer.IN2_CU), Layer.IN1_CU, True),
        ((Layer.IN1_CU, Layer.IN2_CU), Layer.F_CU, False),
        ((Layer.IN1_CU, Layer.IN2_CU), Layer.IN3_CU, False),
    ],
)
def test_tuning_rejects_crossing_via_barrel(endpoints, layer, blocked):
    via = Via(5, 5, 0.3, 0.6, endpoints, net=2, net_name="DQ5")
    detail = _post_insertion_clearance_detail_group(
        new_segments=[Segment(4, 5, 6, 5, 0.2, layer, net=1)],
        candidate_net_id=1,
        group_net_ids={1, 2},
        routes_by_net={2: Route(net=2, net_name="DQ5", vias=[via])},
        intra_group_clearance_mm=0.2,
        via_clearance_mm=0.2,
    )
    if blocked:
        assert detail is not None
        assert "segment-vs-via" in detail
        assert "DQ5" in detail
    else:
        assert detail is None
