"""Preserved fills retain authored floors without becoming reusable copper."""

import pytest
from shapely.geometry import box

from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("backend", ["grid", "lattice"])
@pytest.mark.parametrize("kind", ["trace", "via"])
@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("floor,valid", [(0.3, True), (0.4, False)])
def test_fill_checks_both_authored_floors(kind, strict_net, floor, valid, backend):
    rules = DesignRules(
        trace_clearance=0.1, via_clearance=0.1, net_clearance_floors={strict_net: floor, 3: 4.0}
    )
    grid = RoutingGrid(10, 10, rules)
    grid.fixed_fills = FixedFillObstacles((FixedFill("foreign", 2, 0, 0.1, box(4, 4, 6, 6)),))
    if backend == "lattice":
        from kicad_tools.router.lattice.obstacles import CommittedCopper

        model = CommittedCopper(
            2,
            trace_half=0.1,
            clearance=0.1,
            via_radius=0.1,
            via_via_gap=0.3,
            same_net_via_gap=0.2,
            net_clearance_floors=rules.net_clearance_floors,
        )
        model.fixed_fills = grid.fixed_fills
        if kind == "trace":
            assert model.seg_clear((3, 6.45), (7, 6.45), 0, 1) is valid
            assert model.node_clear((5, 6.45), 0, 1) is valid
        else:
            assert model.via_clear((5, 6.45), 1) is valid
        return
    if kind == "trace":
        candidate = Segment(3, 6.45, 7, 6.45, 0.2, Layer.F_CU, 1)
        actual = grid.validate_segment_clearance(candidate, 1)[0]
    else:
        candidate = Via(5, 6.45, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
        actual = grid.validate_via_clearance(candidate, 1)[0]
    assert actual is valid


def test_same_source_net_does_not_make_placement_invalid_fill_reusable():
    fills = FixedFillObstacles((FixedFill("same", 1, 0, 0.1, box(4, 4, 6, 6)),))
    assert not fills.segment_clear(
        (3, 5), (7, 5), 0, 0.1, 0.1, net=1, net_clearance_floors={1: 0.4}
    )
    assert not fills.segment_clear(
        (3, 6.45), (7, 6.45), 0, 0.1, 0.1, net=1, net_clearance_floors={1: 0.4}
    )
