"""Pure-Python coverage and geometry controls for dynamic route halos.

Issue #5660 (Epic #5509 Phase 3a): route halos are the clearance kernel's
exact disc, not the Chebyshev square they used to be.  Every probe below sits
at ``(55, 56)`` -- offset ``(-5, -4)`` from the via at ``(60, 60)``, Euclidean
distance ``sqrt(41) = 6.40`` cells -- which the old square covered at its
corner and the disc does not.  ``_context`` therefore marks with a seven-cell
radius (via ``max_trace_width``, the only knob on ``_mark_via``'s reach), which
restores the premise these tests are about: a cell inside a *conservative*
halo whose real geometry is legal.  The physical predicates are untouched --
they read rule values, never the marking radius.
"""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules

#: Fed to ``mark_route`` as ``max_trace_width`` so ``_mark_via`` reaches seven
#: cells on this 0.127 mm grid -- see the module docstring (#5660).
HALO_TRACE_WIDTH_MM = 0.6


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
    grid.mark_route(route, max_trace_width=HALO_TRACE_WIDTH_MM)
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
    from kicad_tools.router.primitives import Segment
    from kicad_tools.router.rules import NetClassRouting

    grid, router = _context()
    router.net_class_map["N1"] = NetClassRouting(
        name="PAIR",
        trace_width=0.15,
        clearance=0.15,
        diffpair_partner="N2",
        intra_pair_clearance=gap,
    )
    # Issue #6272: the partner's VIA keeps the trace/via floor whatever the
    # authored gap -- (55, 56) clears it either way.
    assert not router._is_trace_blocked(55, 56, 2, 1, False, radius=2)
    # Partner TRACE copper 0.4 mm (edge-to-edge) away: the authored gap decides.
    x, y = grid.grid_to_world(55, 56)
    rail_x = x - 0.075 - 0.4 - 0.1
    rail = Segment(rail_x, y - 1.0, rail_x, y + 1.0, 0.2, Layer.IN2_CU, 2, "N2")
    grid.mark_route(Route(net=2, net_name="N2", segments=[rail]))
    assert router._is_trace_blocked(55, 56, 2, 1, False, radius=2) == blocked


def test_python_refinement_checks_swept_step():
    grid, router = _context()
    grid.unmark_route(grid.routes[0], max_trace_width=HALO_TRACE_WIDTH_MM)
    # Issue #5661: ``trace_clearance`` set to 0.2 (not 0.1) so the swept-vs-point
    # distinction this test is about is driven by the pair's own resolved
    # requirement rather than by the retired ``max(required, via_clearance)``
    # widening.  The real geometric gap is ~0.2025 mm from either endpoint
    # alone but ~0.199 mm from the swept segment between them -- just below
    # 0.2 mm -- so this still exercises the ``from_cell`` sweep without
    # depending on the asymmetry #5661 fixes.
    router.rules.trace_width, router.rules.trace_clearance = 0.15, 0.2
    x, y = grid.grid_to_world(55, 56)
    route = Route(net=2, net_name="N2")
    route.vias.append(
        Via(x + grid.resolution / 2, y + 0.574, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2")
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


@pytest.mark.parametrize("sharing", [False, True])
def test_python_trace_halo_uses_trace_via_floor_for_seg_via_pairs(sharing):
    """Issue #6272: a trace-vs-via requirement is ``max(trace, via)``.

    The real gap here (~0.168 mm) is legal by ``trace_clearance`` (0.15 mm)
    and illegal by ``via_clearance`` (0.20 mm).  Issue #5661 had dropped
    ``via_clearance`` from this verdict to match the commit validators, which
    then held a trace beside an earlier via to ``trace_clearance`` alone;
    #6272 made every trace/via gate resolve the shared floor in either
    insertion order, so the search-time refinement tracks it again.
    """
    _, router = _context()
    router.rules.via_clearance = 0.15
    assert not router._is_trace_blocked(58, 56, 2, 1, sharing, radius=2)
    router.rules.via_clearance = 0.2
    assert router._is_trace_blocked(58, 56, 2, 1, sharing, radius=2)


def test_python_trace_clearance_expands_geometry_lookup_across_bins():
    """The bins-based search margin still reaches a cross-bin via (#5425).

    Issue #5661: the margin's own ``max(scalar, via_clearance, ...)`` still
    includes ``via_clearance`` (a conservative widening of the *search*, never
    of the *verdict*), but the verdict a found via is held to is now purely
    ``trace_clearance`` -- the pair's own resolved requirement, not a
    via-specific override. Driving the gap past that requirement with
    ``trace_clearance`` itself (rather than ``via_clearance``, as before
    #5661) keeps this test's property -- a via only reachable across a 2 mm
    bin boundary is still found and still measured exactly.
    """
    from kicad_tools.router.primitives import Segment

    grid, router = _context()
    _, y = grid.grid_to_world(60, 60)
    router.rules.trace_clearance = 2.0
    segment = Segment(5.5, y, 5.5, y + 0.1, 0.2, Layer.F_CU, 1)
    assert not grid._route_halo.clear(segment, router)
