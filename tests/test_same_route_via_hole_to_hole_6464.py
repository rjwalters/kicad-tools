"""Issue #6464: a route's OWN vias must respect the fab hole-to-hole floor.

On board 05 (grid engine) a single EN_A route went F.Cu -> via (147.74, 98.77)
-> B.Cu -> via (147.025, 98.51) -> F.Cu, leaving the two drills 0.461 mm apart
edge-to-edge against a 0.5 mm hole-to-hole minimum.  Every same-net drill check
compared a via only against *committed* copper, which a route's own vias are
not yet part of:

* the C++ A* (``Grid3D::route_via_geometry_clear``) only saw stored vias;
* ``Grid3D::validate_route`` only saw stored vias, and at the 0.102 mm
  same-net via-MERGE threshold rather than the fab floor;
* ``RoutingGrid.validate_same_net_drill_spacing`` only saw ``grid.routes``,
  again at 0.102 mm.

KiCad's ``hole_to_hole`` rule does not exempt same-net (or same-route) pairs.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not available")

#: Board 05 EN_A: centres 0.761 mm apart, 0.3 mm drills -> 0.461 mm edge gap.
EN_A_VIA_1 = (147.74, 98.77)
EN_A_VIA_2 = (147.025, 98.51)
DRILL = 0.3


def _drill_gap(a: tuple[float, float], b: tuple[float, float], drill: float = DRILL) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) - drill


def _via(x: float, y: float, net: int = 1) -> Via:
    return Via(
        x=x,
        y=y,
        drill=DRILL,
        diameter=0.6,
        layers=(Layer.F_CU, Layer.B_CU),
        net=net,
        net_name="EN_A",
    )


def _local(p: tuple[float, float]) -> tuple[float, float]:
    """Board 05 coordinates moved onto a small test grid, spacing preserved."""
    return p[0] - 140.0, p[1] - 90.0


def test_en_a_pair_is_below_the_floor():
    assert _drill_gap(EN_A_VIA_1, EN_A_VIA_2) == pytest.approx(0.461, abs=1e-3)


# ---------------------------------------------------------------------------
# Python validation
# ---------------------------------------------------------------------------


def _python_grid() -> tuple[RoutingGrid, DesignRules]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=DRILL,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.05,
    )
    assert rules.min_hole_to_hole == 0.5
    return RoutingGrid(width=20.0, height=20.0, rules=rules), rules


def test_python_validator_sees_the_routes_own_vias():
    grid, _rules = _python_grid()
    a, b = _via(*_local(EN_A_VIA_1)), _via(*_local(EN_A_VIA_2))
    # Nothing is committed: the pre-#6464 validator compared against nothing.
    assert grid.validate_same_net_drill_spacing(b, same_net=1)[0] is True
    is_valid, clearance, location = grid.validate_same_net_drill_spacing(
        b, same_net=1, route_vias=[a, b]
    )
    assert is_valid is False
    assert clearance == pytest.approx(0.461, abs=1e-3)
    assert location == (b.x, b.y)


def test_python_validator_default_floor_is_hole_to_hole():
    """A committed same-net via 0.461 mm away was legal at the 0.102 floor."""
    grid, _rules = _python_grid()
    committed = Route(net=1, net_name="EN_A")
    committed.vias.append(_via(*_local(EN_A_VIA_1)))
    grid.routes.append(committed)
    assert grid.validate_same_net_drill_spacing(_via(*_local(EN_A_VIA_2)), same_net=1)[0] is False


def test_python_validator_accepts_vias_at_the_floor():
    grid, _rules = _python_grid()
    a, b = _via(5.0, 5.0), _via(5.0 + DRILL + 0.501, 5.0)
    assert grid.validate_same_net_drill_spacing(b, same_net=1, route_vias=[a, b])[0] is True


def test_python_route_validation_rejects_close_own_vias():
    grid, rules = _python_grid()
    router = Router(grid, rules)
    (x1, y1), (x2, y2) = _local(EN_A_VIA_1), _local(EN_A_VIA_2)
    route = Route(
        net=1,
        net_name="EN_A",
        segments=[
            Segment(x1 - 2.0, y1, x1, y1, 0.2, Layer.F_CU, net=1),
            Segment(x1, y1, x2, y2, 0.2, Layer.B_CU, net=1),
            Segment(x2, y2, x2 - 2.0, y2, 0.2, Layer.F_CU, net=1),
        ],
        vias=[_via(x1, y1), _via(x2, y2)],
    )
    assert router._validate_route_clearance(route, exclude_net=1) is False

    # Same shape, vias moved apart to a legal pitch: accepted.
    x2b = x1 - (DRILL + 0.6)
    route.vias[1] = _via(x2b, y1)
    route.segments[1] = Segment(x1, y1, x2b, y1, 0.2, Layer.B_CU, net=1)
    route.segments[2] = Segment(x2b, y1, x2b - 2.0, y1, 0.2, Layer.F_CU, net=1)
    assert router._validate_route_clearance(route, exclude_net=1) is True


# ---------------------------------------------------------------------------
# C++ validation
# ---------------------------------------------------------------------------


def _cpp_via(x: float, y: float, net: int = 1):
    from kicad_tools.router import router_cpp

    cv = router_cpp.Via()
    cv.x, cv.y = x, y
    cv.drill, cv.diameter = DRILL, 0.6
    cv.layer_from, cv.layer_to = 0, 1
    cv.net = net
    return cv


@requires_cpp
def test_cpp_validate_route_checks_vias_within_one_route():
    grid, rules = _python_grid()
    cpp_grid = CppGrid.from_routing_grid(grid)
    vias = [_cpp_via(*_local(EN_A_VIA_1)), _cpp_via(*_local(EN_A_VIA_2))]
    result = cpp_grid._impl.validate_route(
        [], vias, 1, [], 0.15, 0.15, rules.min_drill_clearance, -1, 0.0, [], rules.min_hole_to_hole
    )
    assert result.valid is False
    assert result.violation_type == 6  # drill spacing

    # A via projected onto several layer spans is one drill, not a pair.
    stacked = [_cpp_via(5.0, 5.0), _cpp_via(5.0, 5.0)]
    result = cpp_grid._impl.validate_route(
        [], stacked, 1, [], 0.15, 0.15, rules.min_drill_clearance, -1, 0.0, [], 0.5
    )
    assert result.valid is True


@requires_cpp
def test_cpp_validate_route_same_net_stored_floor_is_hole_to_hole():
    grid, rules = _python_grid()
    cpp_grid = CppGrid.from_routing_grid(grid)
    cpp_grid._impl.add_stored_via(*_local(EN_A_VIA_1), DRILL, 0.6, 1)
    via = [_cpp_via(*_local(EN_A_VIA_2))]
    result = cpp_grid._impl.validate_route(
        [], via, 1, [], 0.15, 0.15, rules.min_drill_clearance, -1, 0.0, [], 0.5
    )
    assert result.valid is False
    assert result.violation_type == 6
    # Raw callers that pass no hole floor keep the historical merge floor.
    result = cpp_grid._impl.validate_route([], via, 1, [], 0.15, 0.15, rules.min_drill_clearance)
    assert result.valid is True


# ---------------------------------------------------------------------------
# C++ search
# ---------------------------------------------------------------------------


def _walled_pathfinder() -> CppPathfinder:
    """F.Cu wall across a 2-layer board: the route must hop to B.Cu and back.

    With small vias the cheapest hop puts the two vias 0.4 mm drill-to-drill
    (0.6 mm centres, one wall cell plus each via's keepout); the floor
    demands 0.5 mm.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.5,
    )
    grid = RoutingGrid(width=6.0, height=3.0, rules=rules, layer_stack=LayerStack.two_layer())
    cpp_grid = CppGrid.from_routing_grid(grid)
    wall_gx, _ = grid.world_to_grid(3.0, 0.0)
    for gy in range(grid.rows):
        cpp_grid._impl.mark_blocked(wall_gx, gy, 0, 2, True)
    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder


@requires_cpp
@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_cpp_search_spaces_its_own_vias(method):
    pathfinder = _walled_pathfinder()
    result = getattr(pathfinder._impl, method)(1.0, 1.5, 0, 5.0, 1.5, 0, 1)
    assert result.success
    vias = [(v.x, v.y, v.drill) for v in result.vias]
    assert len(vias) == 2
    (ax, ay, ad), (bx, by, bd) = vias
    gap = math.hypot(ax - bx, ay - by) - (ad + bd) / 2
    assert gap >= 0.5 - 1e-4, f"same-route vias {gap:.3f} mm drill-to-drill (< 0.5)"
