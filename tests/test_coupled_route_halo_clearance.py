"""Dynamic route-halo physical refinement in the COUPLED diff-pair search.

Issue #5410.  PR #5425 taught the per-net A* (both backends) to measure the
real copper/drill gap before rejecting a candidate that only a conservative
dynamic route halo covers.  The coupled joint-state search -- the engine that
routes every differential pair, including Board 07's DDR DQS_P/DQS_N -- kept
consulting the raster alone, so it still refused legal steps beside any
previously routed net.

These tests pin the refined semantics on both backends and, just as
importantly, pin the controls: hard or unverifiable blockage (pad metal,
static halos, keepouts, reservations, hidden overlapping owners, missing
geometry) must still reject, and the refinement must stay dormant until it has
the net-name map it needs to resolve pairwise (HV-isolation) requirements.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import (
    CppCoupledPathfinder,
    CppGrid,
    is_cpp_available,
    replay_route_halo_marks,
    sync_stored_routes,
)
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules

#: The halo cell under test: covered by net 2's conservative via halo, but far
#: enough from the actual barrel that a net-1 trace centreline there is legal.
LEGAL = (55, 56)
#: One cell closer -- the illegal control.
ILLEGAL_VIA = (56, 56)
LAYER = 2


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )


def _context(*, armed: bool = True) -> tuple[RoutingGrid, CoupledPathfinder]:
    rules = _rules()
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)
    pathfinder = CoupledPathfinder(grid, rules, target_spacing_cells=3)
    if armed:
        pathfinder.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, pathfinder


def _cpp_pathfinder(grid: RoutingGrid, pathfinder: CoupledPathfinder) -> CppCoupledPathfinder:
    """Build the C++ coupled search over a faithful copy of *grid*."""
    saved = getattr(grid, "_cpp_grid", None)
    try:
        cpp_grid = CppGrid.from_routing_grid(grid)
        sync_stored_routes(cpp_grid, grid)
        replay_route_halo_marks(cpp_grid, grid)
    finally:
        grid._cpp_grid = saved
    return CppCoupledPathfinder(
        cpp_grid,
        pathfinder.rules,
        target_spacing_cells=3,
        min_spacing_cells=2,
        trace_half_width_cells=pathfinder._trace_half_width_cells,
        via_extra_cells=pathfinder._via_extra_cells,
        via_drill_cells=2,
        spacing_penalty_factor=0.25,
        heuristic_weight=1.0,
    )


requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ coupled refinement requires the router_cpp backend (kct build-native)",
)


# ---------------------------------------------------------------------------
# The repair itself
# ---------------------------------------------------------------------------


def test_legal_candidate_is_inside_the_conservative_halo():
    """The measured trigger: the raster blocks a cell whose copper is legal."""
    grid, pathfinder = _context()
    # Blocked in the raster, owned by the foreign net, with no hard flag.
    assert pathfinder._is_cell_blocked(*LEGAL, LAYER, 1)
    cell = grid.cell_at(LAYER, LEGAL[1], LEGAL[0])
    assert cell.net == 2
    assert not cell.pad_blocked
    assert not cell.is_obstacle
    # ...and the halo can prove whose copper it is.
    assert grid._route_halo.cell_known(*LEGAL, LAYER)


def test_coupled_trace_refines_legal_dynamic_halo():
    _, pathfinder = _context()
    assert not pathfinder._is_trace_blocked(*LEGAL, LAYER, 1)


def test_coupled_via_refines_legal_dynamic_halo_and_keeps_illegal_control():
    _, pathfinder = _context()
    assert not pathfinder._is_via_blocked(*LEGAL, 1)
    assert pathfinder._is_via_blocked(*ILLEGAL_VIA, 1)


def test_refinement_is_dormant_without_a_net_name_map():
    """Fail-closed: an unwired caller keeps the pre-#5410 raster verdict."""
    _, pathfinder = _context(armed=False)
    assert pathfinder._is_trace_blocked(*LEGAL, LAYER, 1)
    assert pathfinder._is_via_blocked(*LEGAL, 1)
    pathfinder.set_net_name_to_id({"N1": 1, "N2": 2})
    assert not pathfinder._is_trace_blocked(*LEGAL, LAYER, 1)
    assert not pathfinder._is_via_blocked(*LEGAL, 1)


def test_direct_cell_gate_is_not_relaxed():
    """``_is_cell_blocked`` stays the raw raster query it always was.

    Other call sites (tail synthesis, stub construction, escape probes) read it
    as plain occupancy; the refinement is additive permissiveness applied by
    the two SEARCH predicates only, so it can never be reached by a caller that
    has not opted into the geometric check.
    """
    _, pathfinder = _context()
    assert pathfinder._is_cell_blocked(*LEGAL, LAYER, 1)


# ---------------------------------------------------------------------------
# Controls: hard and unverifiable blockage must survive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["static", "pad", "obstacle", "reserved", "missing-geometry"])
def test_refinement_preserves_hard_or_unverifiable_blockage(kind):
    grid, pathfinder = _context()
    x, y = LEGAL
    if kind == "static":
        grid._static_blocked[LAYER, y, x] = True
    elif kind == "pad":
        grid._pad_blocked[LAYER, y, x] = True
    elif kind == "obstacle":
        grid._is_obstacle[LAYER, y, x] = True
    elif kind == "reserved":
        grid._reserved_for_nets[(LAYER, y, x)] = frozenset({7})
    else:
        # The copper behind the mark is gone: coverage can no longer prove
        # what the halo is protecting, so the cell stays hard.
        grid.routes.clear()
        grid.bump_occupancy_generation()
    assert pathfinder._is_trace_blocked(*LEGAL, LAYER, 1)
    assert pathfinder._is_via_blocked(*LEGAL, 1)


def test_refinement_rejects_a_changed_cell_owner():
    grid, pathfinder = _context()
    grid.cell_at(LAYER, LEGAL[1], LEGAL[0]).net = 99
    assert not grid._route_halo.cell_known(*LEGAL, LAYER)
    assert pathfinder._is_trace_blocked(*LEGAL, LAYER, 1)


def test_refinement_preserves_pairwise_hv_widening():
    from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

    _, pathfinder = _context()
    assert not pathfinder._is_via_blocked(*LEGAL, 1)
    pathfinder.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=0.15, net_voltages={"N1": 0, "N2": 300}, required_by_pair={("N1", "N2"): 0.4}
    )
    assert pathfinder._is_via_blocked(*LEGAL, 1)


def test_refinement_checks_the_swept_step_not_only_its_endpoint():
    grid, pathfinder = _context()
    grid.unmark_route(grid.routes[0])
    pathfinder.rules.trace_width, pathfinder.rules.trace_clearance = 0.15, 0.1
    x, y = grid.grid_to_world(55, 56)
    route = Route(net=2, net_name="N2")
    route.vias.append(
        Via(x + grid.resolution / 2, y + 0.574, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2")
    )
    grid.mark_route(route)
    assert not pathfinder._is_trace_blocked(55, 56, LAYER, 1)
    assert not pathfinder._is_trace_blocked(56, 56, LAYER, 1)
    # The step 55 -> 56 sweeps between the two legal endpoints, straight past
    # the barrel: checking the head cell alone would have admitted it.
    assert pathfinder._is_trace_blocked(56, 56, LAYER, 1, (55, 56))


def test_overlapping_route_ownership_and_ripup():
    """A foreign via hidden under another net's halo still blocks (#5410)."""
    grid, pathfinder = _context()
    assert not pathfinder._is_via_blocked(*LEGAL, 1)
    x, y = grid.grid_to_world(59, 58)
    overlapping = Route(net=3, net_name="N3")
    overlapping.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 3, "N3"))
    grid.mark_route(overlapping)
    # First-touch ownership leaves net 2 as the visible owner of the cell.
    assert grid.cell_at(LAYER, 58, 59).net == 2
    assert pathfinder._is_via_blocked(*LEGAL, 1)
    grid.unmark_route(overlapping)
    assert not pathfinder._is_via_blocked(*LEGAL, 1)


def test_trace_refinement_preserves_the_larger_via_clearance():
    _, pathfinder = _context()
    pathfinder.rules.via_clearance = 0.15
    assert not pathfinder._is_trace_blocked(58, 56, LAYER, 1)
    pathfinder.rules.via_clearance = 0.2
    assert pathfinder._is_trace_blocked(58, 56, LAYER, 1)


# ---------------------------------------------------------------------------
# Backend parity
# ---------------------------------------------------------------------------


@requires_cpp
def test_cpp_grid_copy_carries_halo_provenance():
    """``from_routing_grid`` alone cannot support refinement; the replay can.

    The bulk copy reaches the C++ grid exclusively through ``mark_blocked``,
    which records every cell as STATIC board geometry -- so without the #5410
    replay every dynamic halo cell reads as a hard constraint.
    """
    grid, _ = _context()
    saved = getattr(grid, "_cpp_grid", None)
    try:
        cpp_grid = CppGrid.from_routing_grid(grid)
        assert not cpp_grid._impl.route_cell_has_geometry(*LEGAL, LAYER)
        sync_stored_routes(cpp_grid, grid)
        replay_route_halo_marks(cpp_grid, grid)
        assert cpp_grid._impl.route_cell_has_geometry(*LEGAL, LAYER)
    finally:
        grid._cpp_grid = saved


@requires_cpp
@pytest.mark.parametrize("gx", [53, 54, 55, 56, 57, 58, 59])
def test_backend_parity_of_the_refined_predicates(gx):
    grid, pathfinder = _context()
    cpp = _cpp_pathfinder(grid, pathfinder)
    assert cpp.trace_blocked(gx, 56, LAYER, 1) == pathfinder._is_trace_blocked(gx, 56, LAYER, 1)
    assert cpp.via_blocked(gx, 56, 1) == pathfinder._is_via_blocked(gx, 56, 1)


@requires_cpp
def test_cpp_refines_the_legal_candidate_and_keeps_the_control():
    grid, pathfinder = _context()
    cpp = _cpp_pathfinder(grid, pathfinder)
    assert not cpp.trace_blocked(*LEGAL, LAYER, 1)
    assert not cpp.via_blocked(*LEGAL, 1)
    assert cpp.via_blocked(*ILLEGAL_VIA, 1)


@requires_cpp
def test_cpp_preserves_hard_blockage_inside_the_halo():
    grid, pathfinder = _context()
    grid._static_blocked[LAYER, LEGAL[1], LEGAL[0]] = True
    cpp = _cpp_pathfinder(grid, pathfinder)
    assert cpp.trace_blocked(*LEGAL, LAYER, 1)
    assert cpp.via_blocked(*LEGAL, 1)


@requires_cpp
def test_cpp_checks_the_swept_step_not_only_its_endpoint():
    grid, pathfinder = _context()
    grid.unmark_route(grid.routes[0])
    pathfinder.rules.trace_width, pathfinder.rules.trace_clearance = 0.15, 0.1
    x, y = grid.grid_to_world(55, 56)
    route = Route(net=2, net_name="N2")
    route.vias.append(
        Via(x + grid.resolution / 2, y + 0.574, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2")
    )
    grid.mark_route(route)
    cpp = _cpp_pathfinder(grid, pathfinder)
    assert not cpp.trace_blocked(56, 56, LAYER, 1)
    assert cpp.trace_blocked(56, 56, LAYER, 1, (55, 56))
