"""Pure-Python coverage and geometry controls for dynamic route halos."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules


def _context():
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, router


@pytest.mark.parametrize("sharing", [False, True])
def test_python_refines_legal_via_halo(sharing):
    grid, router = _context()
    assert grid._route_halo.cell_known(55, 56, 2)
    assert not router._is_trace_blocked(55, 56, 2, 1, sharing, radius=2)
    assert not router._is_diagonal_corner_blocked(55, 56, -1, 1, 2, 1, sharing)
    assert not router._is_via_blocked(55, 56, 2, 1, sharing, radius=4)
    assert router._is_via_blocked(56, 56, 2, 1, sharing, radius=4)


def test_python_unknown_and_static_cells_stay_blocked():
    grid, router = _context()
    grid._static_blocked[2, 56, 55] = True
    assert router._is_trace_blocked(55, 56, 2, 1, False, radius=2)
    grid._static_blocked[2, 56, 55] = False
    grid.routes.clear()
    grid.bump_occupancy_generation()
    assert not grid._route_halo.cell_known(55, 56, 2)
    assert router._is_trace_blocked(55, 56, 2, 1, False, radius=2)


@pytest.mark.parametrize("method", ["route", "route_bidirectional"])
@pytest.mark.parametrize("sharing", [False, True])
def test_python_search_routes_through_legal_halo(method, sharing):
    from kicad_tools.router.primitives import Pad

    grid, router = _context()
    a, b = grid.grid_to_world(52, 55), grid.grid_to_world(56, 55)
    layer = router._grid_layer_object(2)
    start = Pad(x=a[0], y=a[1], width=0.1, height=0.1, layer=layer, net=1, net_name="N1")
    end = Pad(x=b[0], y=b[1], width=0.1, height=0.1, layer=layer, net=1, net_name="N1")
    result = getattr(router, method)(start, end, negotiated_mode=sharing)
    assert result is not None
    assert result.segments
    assert all(grid.validate_segment_clearance(s, 1)[0] for s in result.segments)


def test_python_dynamic_halo_preserves_hv_widening():
    from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

    _, router = _context()
    router.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=0.15, net_voltages={"N1": 0, "N2": 300}, required_by_pair={("N1", "N2"): 0.4}
    )
    assert router._is_via_blocked(55, 56, 2, 1, True, radius=4)


def test_python_coverage_rejects_changed_owner():
    grid, _ = _context()
    assert grid._route_halo.cell_known(55, 56, 2)
    grid.cell_at(2, 56, 55).net = 99
    assert not grid._route_halo.cell_known(55, 56, 2)


@pytest.mark.parametrize("gap,blocked", [(0.1, False), (0.6, True)])
def test_python_refinement_preserves_authored_partner_gap(gap, blocked):
    from kicad_tools.router.rules import NetClassRouting

    _, router = _context()
    router.net_class_map["N1"] = NetClassRouting(
        name="PAIR",
        trace_width=0.15,
        clearance=0.15,
        diffpair_partner="N2",
        intra_pair_clearance=gap,
    )
    assert router._is_trace_blocked(55, 56, 2, 1, False, radius=2) == blocked


def test_python_refinement_checks_swept_step():
    grid, router = _context()
    grid.unmark_route(grid.routes[0])
    router.rules.trace_width, router.rules.trace_clearance = 0.15, 0.1
    x, y = grid.grid_to_world(55, 56)
    route = Route(net=2, net_name="N2")
    route.vias.append(
        Via(x + grid.resolution / 2, y + 0.474, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2")
    )
    grid.mark_route(route)
    assert not router._is_trace_blocked(55, 56, 2, 1, False, radius=2)
    assert not router._is_trace_blocked(56, 56, 2, 1, False, radius=2)
    assert router._is_trace_blocked(56, 56, 2, 1, False, radius=2, from_cell=(55, 56))


def test_python_overlap_checks_hidden_owner_and_ripup_removes_only_its_geometry():
    grid, router = _context()
    assert not router._is_via_blocked(55, 56, 2, 1, False, radius=4)
    x, y = grid.grid_to_world(59, 58)
    overlapping = Route(net=3, net_name="N3")
    overlapping.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 3, "N3"))
    grid.mark_route(overlapping)
    # First-touch ownership hides N3 beneath N2's conservative halo.
    assert grid.cell_at(2, 58, 59).net == 2
    assert router._is_via_blocked(55, 56, 2, 1, False, radius=4)
    grid.unmark_route(overlapping)
    assert grid._route_halo.complete
    assert not router._is_via_blocked(55, 56, 2, 1, False, radius=4)
