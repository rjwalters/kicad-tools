"""The fab drill-pitch floor is a via-drop predicate, not a raster accident.

Issue #5673 (follow-up to #5660 / Epic #5509 Phase 3a).

#5660 switched the route-copper halo from the Chebyshev **square** to the
clearance kernel's exact disc.  The square circumscribed the disc, so it
reached ``sqrt(2) * r`` at its diagonals -- and that excess was silently
doing a second job nobody had written down: it kept a *second via* from being
dropped close enough to violate the fab's mechanical drill-to-drill minimum.
The exact disc withdraws the accident, and board 02 immediately shipped two
**same-net** vias 0.428 mm drill-to-drill against a 0.500 mm floor
(``hole_to_hole_clearance ... at (137.90, 86.75)``).

The naive repair -- widen the via-marking halo radius by the drill term --
was measured and rejected: it blocks ordinary *trace* candidates near a via
too, which re-introduces exactly the over-blocking #5410/#5660 removed, just
on the drill axis instead of the copper one
(``test_halo_kernel_geometry.py::test_issue5410_dq3_candidate_is_outside_the_python_via_halo``
is the tripwire).

So the floor is stated where it belongs: in the predicate that decides
whether a **via** may be dropped at a cell
(``RouteHaloGeometry.clear`` / ``Grid3D::route_via_geometry_clear``), leaving
cell occupancy -- what trace routing consults -- untouched.  The two
properties below are deliberately checked together; a change that satisfied
only one of them would be the failed repair, not the fix.

The same-net case is the one that regressed.  ``rules.min_drill_clearance``
(0.102 mm) is the tiny same-net via-**merge** threshold, not a fab minimum,
and it was being used as the *whole* same-net drill floor.  KiCad's
``hole_to_hole_clearance`` rule does not exempt a same-net pair, so neither
may the router's pre-check.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid, halo_offsets
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")

#: Board 02's verbatim regression geometry: two same-net 0.6/0.3 vias 0.2 mm
#: apart in x and 0.7 mm in y -- 0.728 mm centre-to-centre, 0.428 mm
#: drill-to-drill, against JLCPCB's 0.500 mm hole-to-hole floor.
OFFENDING_DX_MM = 0.2
OFFENDING_DY_MM = 0.7
VIA_DIAMETER_MM = 0.6
VIA_DRILL_MM = 0.3
RESOLUTION_MM = 0.1
NET = 10


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=VIA_DIAMETER_MM,
        via_drill=VIA_DRILL_MM,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        min_drill_clearance=0.102,
        grid_resolution=RESOLUTION_MM,
    )


def _context() -> tuple[RoutingGrid, Router, tuple[int, int]]:
    """A grid carrying one committed via of ``NET``, plus its router."""
    rules = _rules()
    grid = RoutingGrid(
        width=30, height=30, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    gx, gy = grid.world_to_grid(10.0, 10.0)
    wx, wy = grid.grid_to_world(gx, gy)
    route = Route(net=NET, net_name="N10")
    route.vias.append(
        Via(wx, wy, VIA_DRILL_MM, VIA_DIAMETER_MM, (Layer.F_CU, Layer.B_CU), NET, "N10")
    )
    grid.mark_route(route, max_trace_width=rules.trace_width)
    router = Router(grid, rules)
    router.set_net_name_to_id({"N10": NET})
    return grid, router, (gx, gy)


def _marking_radius_cells(rules: DesignRules) -> int:
    """``_mark_via``'s reach, reproduced from the caller's own arithmetic."""
    return (
        int(
            (VIA_DIAMETER_MM / 2 + rules.via_clearance + rules.trace_width / 2)
            / rules.grid_resolution
        )
        + 1
    )


def test_the_offending_cell_is_a_disc_halo_escapee() -> None:
    """The premise: the square covered this cell, the exact disc does not.

    Without this contrast the assertions below would not be evidence of
    anything -- a cell the halo still marks would be rejected by occupancy,
    and the predicate under test would never be consulted.
    """
    rules = _rules()
    offset = (
        round(OFFENDING_DX_MM / RESOLUTION_MM),
        round(OFFENDING_DY_MM / RESOLUTION_MM),
    )
    radius = _marking_radius_cells(rules)
    assert max(abs(offset[0]), abs(offset[1])) <= radius, "the square really did cover this cell"
    assert math.dist((0, 0), offset) > radius
    assert offset not in set(halo_offsets(radius)), "the exact disc no longer covers it"

    # ...and the geometry it stands for really is a DRC violation.
    distance = math.hypot(OFFENDING_DX_MM, OFFENDING_DY_MM)
    assert distance - VIA_DRILL_MM == pytest.approx(0.428, abs=5e-4)
    assert distance - VIA_DRILL_MM < rules.min_hole_to_hole


def test_same_net_via_drop_below_the_fab_drill_floor_is_rejected() -> None:
    """Blocker 1: the 0.428 mm same-net pair board 02 shipped is refused.

    Note what is *not* doing the work here: ``is_blocked_for_net`` says the
    cell is free (asserted below), so the rejection comes from the via-drop
    predicate, not from a re-widened halo.
    """
    grid, router, (gx, gy) = _context()
    cgx = gx + round(OFFENDING_DX_MM / RESOLUTION_MM)
    cgy = gy + round(OFFENDING_DY_MM / RESOLUTION_MM)

    assert not grid.is_blocked_for_net(cgx, cgy, 0, NET + 1), (
        "cell occupancy must stay as #5660 left it -- the fix is a predicate, not a halo"
    )
    assert router._is_via_blocked(cgx, cgy, 0, NET), (
        "a same-net via 0.428 mm drill-to-drill from a committed via must be refused"
    )


def test_trace_routing_through_the_same_cell_is_still_allowed() -> None:
    """The predicate fires on via drops only -- #5410's property survives.

    This is the constraint the rejected "just widen the radius" repair broke:
    a drill floor has nothing to say about a trace passing near a via, and
    enforcing it there is over-blocking by another name.
    """
    grid, router, (gx, gy) = _context()
    cgx = gx + round(OFFENDING_DX_MM / RESOLUTION_MM)
    cgy = gy + round(OFFENDING_DY_MM / RESOLUTION_MM)

    assert not router._is_trace_blocked(cgx, cgy, 0, NET + 1)
    assert not router._is_trace_blocked(cgx, cgy, 0, NET)
    assert not grid.is_blocked_for_net(cgx, cgy, 0, NET + 1)


def test_same_net_via_drop_above_the_fab_drill_floor_is_still_allowed() -> None:
    """The floor is a floor, not a blanket keep-out: 0.6 mm apart is legal."""
    grid, router, (gx, gy) = _context()
    legal_dy_mm = 0.9  # 0.600 mm drill-to-drill, comfortably over 0.500.
    assert legal_dy_mm - VIA_DRILL_MM > _rules().min_hole_to_hole
    cgy = gy + round(legal_dy_mm / RESOLUTION_MM)
    assert not router._is_via_blocked(gx, cgy, 0, NET)


def test_the_rejection_tracks_min_hole_to_hole_not_min_drill_clearance() -> None:
    """Attribution: relax the fab floor and the same candidate becomes legal.

    Pins *which* rule value the new predicate reads.  Pre-#5673 the same-net
    branch read ``min_drill_clearance`` (0.102 mm) and this candidate passed
    unconditionally; if a later change reverted to that, this test would go
    green in the first assertion and red in the second.
    """
    grid, router, (gx, gy) = _context()
    cgx = gx + round(OFFENDING_DX_MM / RESOLUTION_MM)
    cgy = gy + round(OFFENDING_DY_MM / RESOLUTION_MM)
    assert router._is_via_blocked(cgx, cgy, 0, NET)

    router.rules.min_hole_to_hole = 0.102
    grid.rules.min_hole_to_hole = 0.102
    router._via_cache.clear()
    assert not router._is_via_blocked(cgx, cgy, 0, NET), (
        "with the fab floor relaxed to the merge threshold the candidate is legal again"
    )


@requires_cpp
def test_cpp_via_drop_predicate_agrees_on_the_same_net_drill_floor() -> None:
    """One geometry, two ports (``Grid3D::route_via_geometry_clear``).

    The C++ backend is the default for every board CI job, so a Python-only
    fix would have left board 02 red.
    """
    from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

    rules = _rules()
    grid = router_cpp.Grid3D(200, 200, 4, RESOLUTION_MM, 9.0, 9.0)
    grid.add_stored_via(10.0, 10.0, VIA_DRILL_MM, VIA_DIAMETER_MM, NET, None, 0, 3)

    def candidate(x: float, y: float, net: int) -> object:
        via = router_cpp.Via()
        via.x, via.y = x, y
        via.net = net
        via.drill, via.diameter = VIA_DRILL_MM, VIA_DIAMETER_MM
        via.layer_from, via.layer_to = 0, 3
        return via

    offending = candidate(10.0 + OFFENDING_DX_MM, 10.0 + OFFENDING_DY_MM, NET)
    assert not grid.route_via_geometry_clear(
        offending, rules.via_clearance, rules.min_hole_to_hole, rules.min_drill_clearance
    ), "the same-net 0.428 mm pair must be refused on the C++ side too"

    legal = candidate(10.0, 10.9, NET)
    assert grid.route_via_geometry_clear(
        legal, rules.via_clearance, rules.min_hole_to_hole, rules.min_drill_clearance
    )


@requires_cpp
def test_cpp_foreign_net_drill_floor_is_unchanged() -> None:
    """#5410's own pair (0.513 mm drill-to-drill, foreign nets) stays legal.

    The fix raises the *same-net* floor to the fab minimum; it must not move
    the foreign-net one, which was already correct.
    """
    from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

    rules = _rules()
    grid = router_cpp.Grid3D(2000, 2000, 4, 0.127, 100.0, 100.0)
    grid.add_stored_via(143.777, 123.800, 0.3, 0.6, 1, None, 0, 3)

    via = router_cpp.Via()
    via.x, via.y = 143.142, 123.292
    via.net = 2
    via.drill, via.diameter = 0.3, 0.6
    via.layer_from, via.layer_to = 0, 3
    assert math.dist((143.777, 123.800), (143.142, 123.292)) - 0.3 == pytest.approx(0.513, abs=1e-3)
    assert grid.route_via_geometry_clear(
        via, rules.via_clearance, rules.min_hole_to_hole, rules.min_drill_clearance
    )
