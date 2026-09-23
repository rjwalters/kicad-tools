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
    install_pairwise_domains,
    is_cpp_available,
    replay_route_halo_marks,
    sync_stored_routes,
)
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

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


#: A net class demanding materially MORE clearance than the global rule
#: (0.60 mm vs ``_rules().trace_clearance`` = 0.15 mm).  The raster halo is
#: dilated with this value, so a refinement that re-measures with the global
#: scalar would waive a constraint the raster enforced -- issue #5410's B1.
def _wide_net_class_map() -> dict[str, NetClassRouting]:
    wide = NetClassRouting(name="WIDE", trace_width=0.2, clearance=0.60, via_size=0.6)
    return {"N1": wide, "N2": wide}


def _context(
    *, armed: bool = True, net_class_map: dict[str, NetClassRouting] | None = None
) -> tuple[RoutingGrid, CoupledPathfinder]:
    rules = _rules()
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)
    pathfinder = CoupledPathfinder(grid, rules, target_spacing_cells=3, net_class_map=net_class_map)
    if armed:
        pathfinder.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, pathfinder


def _cpp_pathfinder(grid: RoutingGrid, pathfinder: CoupledPathfinder) -> CppCoupledPathfinder:
    """Build the C++ coupled search over a faithful copy of *grid*.

    Mirrors ``CoupledPathfinder._ensure_cpp_coupled``, including its
    fail-closed pairwise gate: provenance is replayed only onto a grid whose
    cross-domain widening was installed.
    """
    saved = getattr(grid, "_cpp_grid", None)
    replayed = False
    try:
        cpp_grid = CppGrid.from_routing_grid(grid)
        names = pathfinder._halo_refiner._net_name_to_id
        # The ``armed`` conjunct is not belt-and-braces: ``install_pairwise_domains``
        # answers True whenever there is no table at all, so without it an
        # UNARMED search would still receive the full C++ refinement while the
        # Python predicates stayed dormant (#5410 B2).
        if pathfinder._halo_refiner.armed and install_pairwise_domains(
            cpp_grid, pathfinder.rules, names
        ):
            sync_stored_routes(cpp_grid, grid)
            replay_route_halo_marks(cpp_grid, grid)
            replayed = True
    finally:
        grid._cpp_grid = saved
    cpp = CppCoupledPathfinder(
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
    if replayed:
        pathfinder._install_cpp_halo_net_dimensions(cpp)
    return cpp


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
@pytest.mark.parametrize("gx", [53, 54, 55, 56, 57, 58, 59, 63, 64, 65, 66, 67])
def test_backend_parity_under_a_wider_net_class(gx):
    """Issue #5410 B1: the refinement must use the NET-CLASS clearance.

    ``RouteHaloGeometry.clear`` derives ``scalar = nc.clearance`` and builds
    the candidate at ``nc.trace_width`` / ``nc.via_size``.  The C++ coupled
    predicate used the global ``DesignRules`` scalars instead, so with a class
    demanding 0.60 mm against a 0.15 mm global rule it re-measured the
    candidate against the narrower value and ADMITTED cells the raster halo --
    dilated with the net-class clearance -- had correctly blocked.  Six cells
    in this sweep diverged, every one in the under-blocking direction (#5673).

    ``test_backend_parity_of_the_refined_predicates`` cannot see it: its
    fixture installs no ``net_class_map``, so ``nc`` is ``None`` on both arms
    and both read ``rules.trace_clearance``.  This sibling supplies one.
    """
    _, pathfinder = _context(net_class_map=_wide_net_class_map())
    assert pathfinder._halo_refiner._halo_net_class(1) is not None
    assert pathfinder._halo_refiner._halo_net_class(1).clearance > pathfinder.rules.trace_clearance
    # Drive the PRODUCTION wiring (``_get_cpp_coupled_impl``), not the local
    # helper -- the net-class dimensions are installed there.
    cpp = pathfinder._get_cpp_coupled_impl()
    assert cpp is not None
    assert cpp.trace_blocked(gx, 56, LAYER, 1) == pathfinder._is_trace_blocked(gx, 56, LAYER, 1)
    assert cpp.via_blocked(gx, 56, 1) == pathfinder._is_via_blocked(gx, 56, 1)


@requires_cpp
def test_a_wider_net_class_keeps_cells_the_global_rule_would_admit():
    """The same divergence stated as a direction, not just as parity.

    Guards against a future "parity" fix that relaxed the PYTHON arm to the
    global scalar instead of tightening the C++ one: under the wide class both
    backends must still REJECT a cell that the global-rule fixture refines away.
    """
    _, global_rule = _context()
    assert not global_rule._is_trace_blocked(*LEGAL, LAYER, 1)

    _, wide = _context(net_class_map=_wide_net_class_map())
    cpp = wide._get_cpp_coupled_impl()
    assert cpp is not None
    assert wide._is_trace_blocked(*LEGAL, LAYER, 1)
    assert cpp.trace_blocked(*LEGAL, LAYER, 1)


@requires_cpp
def test_unarmed_coupled_search_is_dormant_on_both_backends():
    """Issue #5410 B2: ``armed`` gates the C++ replay, as the docstrings say.

    The replay used to be gated on ``install_pairwise_domains`` alone, which
    answers True whenever ``rules.pairwise_clearance is None`` -- the common
    case -- so an unarmed coupled search still received the full C++
    refinement while the Python predicates stayed dormant.  Both arms must now
    keep the pre-#5410 raster verdict.
    """
    _, pathfinder = _context(armed=False)
    assert not pathfinder._halo_refiner.armed
    assert pathfinder.rules.pairwise_clearance is None
    # Production wiring: the ``armed`` conjunct lives in
    # ``_get_cpp_coupled_impl``, so a helper that reimplements the gate would
    # pass this test no matter what the shipped code does.
    cpp = pathfinder._get_cpp_coupled_impl()
    assert cpp is not None
    for gx in range(53, 68):
        assert cpp.trace_blocked(gx, 56, LAYER, 1) == pathfinder._is_trace_blocked(gx, 56, LAYER, 1)
        assert cpp.via_blocked(gx, 56, 1) == pathfinder._is_via_blocked(gx, 56, 1)
    # ...and dormant means the raster verdict, not merely an agreeing one.
    assert cpp.trace_blocked(*LEGAL, LAYER, 1)
    assert cpp.via_blocked(*LEGAL, 1)


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


@requires_cpp
def test_cpp_refinement_preserves_pairwise_hv_widening():
    """The C++ grid re-measures against its OWN pairwise matrix (#5410).

    The coupled search builds a private ``CppGrid`` that no ``CppPathfinder``
    ever installs cross-domain widening on, so replaying halo provenance onto
    it without the matrix would waive an HV requirement the Python side
    applies.  The refinement is gated on that install.
    """
    from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

    grid, pathfinder = _context()
    pathfinder.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=0.15, net_voltages={"N1": 0, "N2": 300}, required_by_pair={("N1", "N2"): 0.4}
    )
    cpp = _cpp_pathfinder(grid, pathfinder)
    assert cpp.via_blocked(*LEGAL, 1)
    assert cpp.via_blocked(*LEGAL, 1) == pathfinder._is_via_blocked(*LEGAL, 1)


@requires_cpp
def test_cpp_refinement_stays_dormant_when_widening_cannot_be_installed():
    """An untranslatable table keeps the conservative raster verdict.

    The refiner is ARMED here on purpose: ``armed`` is now a separate, earlier
    gate (#5410 B2), so leaving it off would seal the search for the wrong
    reason and stop exercising the pairwise gate at all.  The table names nets
    the board does not have, so ``build_cpp_domain_matrix`` cannot resolve two
    participating ids and ``install_pairwise_domains`` answers False.
    """
    from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

    grid, pathfinder = _context(armed=True)
    pathfinder.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=0.15,
        net_voltages={"HV_A": 0, "HV_B": 300},
        required_by_pair={("HV_A", "HV_B"): 0.4},
    )
    assert pathfinder._halo_refiner.armed
    cpp = _cpp_pathfinder(grid, pathfinder)
    assert cpp.trace_blocked(*LEGAL, LAYER, 1)
    assert cpp.via_blocked(*LEGAL, 1)


# ---------------------------------------------------------------------------
# Search-level witness
# ---------------------------------------------------------------------------


def _channel_context(armed: bool, use_cpp: bool):
    """A coupled pair whose straight channel is sealed only by a halo.

    A committed foreign via sits 0.7 mm above the P trace's row: its
    conservative halo reaches the row, its copper does not.
    """
    from kicad_tools.router.primitives import Pad

    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    obstacle = Route(net=9, net_name="OBST")
    obstacle.vias.append(
        Via(6.0, 3.3, rules.via_drill, rules.via_diameter, (Layer.F_CU, Layer.B_CU), 9, "OBST")
    )
    grid.mark_route(obstacle)
    pathfinder = CoupledPathfinder(
        grid=grid, rules=DesignRules(), target_spacing_cells=2, min_spacing_cells=2
    )
    pathfinder._use_cpp_coupled = use_cpp
    if armed:
        pathfinder.set_net_name_to_id({"D+": 1, "D-": 2, "OBST": 9})
    pads = (
        Pad(x=2.0, y=4.0, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU),
        Pad(x=10.0, y=4.0, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU),
        Pad(x=2.0, y=6.0, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU),
        Pad(x=10.0, y=6.0, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU),
    )
    return grid, pathfinder, pads


def test_python_coupled_search_crosses_a_legal_halo_channel():
    grid, pathfinder, pads = _channel_context(armed=False, use_cpp=False)
    assert pathfinder.route_coupled(*pads, max_iterations_budget=20000) is None

    grid, pathfinder, pads = _channel_context(armed=True, use_cpp=False)
    result = pathfinder.route_coupled(*pads, max_iterations_budget=20000)
    assert result is not None
    p_route, n_route = result
    assert pathfinder.last_coupled_backend == "python"
    for segment in p_route.segments + n_route.segments:
        assert grid.validate_segment_clearance(segment, segment.net)[0]


@requires_cpp
def test_cpp_coupled_search_crosses_a_legal_halo_channel():
    grid, pathfinder, pads = _channel_context(armed=True, use_cpp=True)
    result = pathfinder.route_coupled(*pads, max_iterations_budget=20000)
    assert result is not None
    assert pathfinder.last_coupled_backend == "cpp"
    p_route, n_route = result
    for segment in p_route.segments + n_route.segments:
        assert grid.validate_segment_clearance(segment, segment.net)[0]


@requires_cpp
def test_cpp_channel_is_sealed_again_when_provenance_is_not_replayed():
    """The C++ refinement is load-bearing for this channel, and fail-closed.

    An untranslatable pairwise table blocks the provenance replay (the C++
    grid's own cross-domain widening could not be installed), so the coupled
    search falls back to the conservative raster -- and the channel seals shut
    again, exactly as it does on the unrefined Python backend.

    ARMED on purpose: ``armed`` is a separate, earlier gate since #5410 B2, so
    an unarmed fixture would seal the channel for the wrong reason and stop
    exercising the pairwise gate.  The table names nets this board does not
    have, which is what makes it untranslatable.
    """
    from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

    _, pathfinder, pads = _channel_context(armed=True, use_cpp=True)
    pathfinder.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=0.15,
        net_voltages={"HV_A": 0, "HV_B": 300},
        required_by_pair={("HV_A", "HV_B"): 0.4},
    )
    assert pathfinder.route_coupled(*pads, max_iterations_budget=20000) is None


@requires_cpp
def test_cpp_channel_is_sealed_again_when_the_refiner_is_unarmed():
    """The search-level counterpart of the #5410 B2 gate.

    Without a net-name map the C++ coupled search must keep the conservative
    raster verdict, exactly as the Python one does -- so the channel the
    refinement opens stays sealed.  Before the fix the replay ran anyway (the
    pairwise gate answers True when there is no table) and the C++ arm routed
    while the Python arm did not.
    """
    _, pathfinder, pads = _channel_context(armed=False, use_cpp=True)
    assert not pathfinder._halo_refiner.armed
    assert pathfinder.rules.pairwise_clearance is None
    assert pathfinder.route_coupled(*pads, max_iterations_budget=20000) is None
