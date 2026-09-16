"""New tuning context retains broad-phase pruning and physical microvia spans."""

import pytest

from kicad_tools.router.diffpair_length_tuning import _post_insertion_clearance_ok
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("other_net", [2, 3])
def test_distant_routes_are_pruned_but_near_collision_is_measured(monkeypatch, other_net):
    import kicad_tools.core.geometry as geometry

    original = geometry.segment_clearance
    calls = []

    def measure(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(geometry, "segment_clearance", measure)
    candidate = Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1)
    other = Route(
        net=other_net,
        net_name="other",
        segments=[
            Segment(100 + i, 100, 101 + i, 100, 0.2, Layer.F_CU, net=other_net) for i in range(100)
        ],
    )
    args = {
        "new_segments": [candidate],
        "shorter_net_id": 1,
        "longer_net_id": 2,
        "routes_by_net": {other_net: other},
        "intra_pair_clearance_mm": 0.1,
    }
    assert _post_insertion_clearance_ok(**args)
    assert not calls
    other.segments.append(Segment(0, 0.25, 1, 0.25, 0.2, Layer.F_CU, net=other_net))
    assert not _post_insertion_clearance_ok(**args)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "layer,clear",
    [(Layer.F_CU, False), (Layer.IN1_CU, False), (Layer.IN2_CU, False), (Layer.B_CU, True)],
)
def test_microvia_span_includes_intermediate_layers(layer, clear):
    grid = RoutingGrid(10, 10, DesignRules(via_clearance=0.2))
    via = Via(5, 5, 0.1, 0.3, (Layer.F_CU, Layer.IN2_CU), net=2, is_micro=True)
    route = Route(net=2, net_name="other", vias=[via])
    assert (
        _post_insertion_clearance_ok(
            new_segments=[Segment(4, 5, 6, 5, 0.2, layer, net=1)],
            shorter_net_id=1,
            longer_net_id=2,
            routes_by_net={2: route},
            intra_pair_clearance_mm=0.1,
            grid=grid,
        )
        is clear
    )
