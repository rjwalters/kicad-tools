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
from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledState, GridPos
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
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


# ---------------------------------------------------------------------------
# Issue #6473: validator tolerance
# ---------------------------------------------------------------------------


def _small_rules() -> DesignRules:
    """The walled-fixture rules: 0.2 mm drills on a 0.1 mm grid, floor 0.5 mm."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.5,
    )


def test_python_validator_accepts_a_pair_exactly_on_the_floor():
    """Centres 0.7 mm apart with 0.2 mm drills measure 0.4999999999999998.

    That is the cheapest legal pair on a 0.1 mm grid; the native twins accept
    it (``floor - CLEARANCE_EPSILON_MM``) and so must the Python validator.
    """
    rules = _small_rules()
    grid = RoutingGrid(width=6.0, height=3.0, rules=rules, layer_stack=LayerStack.two_layer())

    def via(x: float) -> Via:
        return Via(x=x, y=1.5, drill=0.2, diameter=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)

    a, on_floor, below = via(2.7), via(3.4), via(3.3)
    assert (on_floor.x - a.x) - 0.2 < 0.5  # the float artefact this test is about
    valid, gap, _loc = grid.validate_same_net_drill_spacing(
        on_floor, same_net=1, route_vias=[a, on_floor]
    )
    assert valid is True
    assert gap == pytest.approx(0.5, abs=1e-9)
    valid, gap, _loc = grid.validate_same_net_drill_spacing(
        below, same_net=1, route_vias=[a, below]
    )
    assert valid is False
    assert gap == pytest.approx(0.4, abs=1e-9)


# ---------------------------------------------------------------------------
# Issue #6473: coupled (diff-pair) search, both backends
# ---------------------------------------------------------------------------

FLOOR = 0.5
COUPLED_BACKENDS = [
    pytest.param(True, id="cpp", marks=requires_cpp),
    pytest.param(False, id="python"),
]


def _coupled_fixture(pitch: float, height: float, use_cpp: bool, b_wall: float | None = None):
    """A pair crossing an F.Cu wall at x=3.0 on a two-layer board.

    Both rails must hop to B.Cu.  ``b_wall`` adds a B.Cu wall that forces
    them back up.  Pads sit on F.Cu at x=1.0 and x=5.0, ``pitch`` apart.
    """
    rules = _small_rules()
    grid = RoutingGrid(width=6.0, height=height, rules=rules, layer_stack=LayerStack.two_layer())
    grid.add_keepout(3.0, 0.0, 3.0, height, layers=[Layer.F_CU])
    if b_wall is not None:
        grid.add_keepout(b_wall, 0.0, b_wall, height, layers=[Layer.B_CU])
    y_p, y_n = height / 2 - pitch / 2, height / 2 + pitch / 2

    def pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
        return Pad(
            x=x, y=y, width=0.2, height=0.2, net=net, net_name=name, layer=Layer.F_CU, ref=ref
        )

    pads = (
        pad(1.0, y_p, 1, "D_P", "A"),
        pad(5.0, y_p, 1, "D_P", "B"),
        pad(1.0, y_n, 2, "D_N", "C"),
        pad(5.0, y_n, 2, "D_N", "D"),
    )
    for p in pads:
        grid.add_pad(p)
    cells = round(pitch / rules.grid_resolution)
    pf = CoupledPathfinder(
        grid=grid, rules=rules, target_spacing_cells=cells, min_spacing_cells=cells
    )
    pf._use_cpp_coupled = use_cpp
    pf.fixture_pads = pads  # Issue #6499: the connectivity oracle needs them
    result = pf.route_coupled(*pads, max_iterations_budget=200000)
    assert pf.last_coupled_backend == ("cpp" if use_cpp else "python")
    return pf, result


def _via_cells(route: Route) -> list[tuple[float, float]]:
    return [(round(v.x, 3), round(v.y, 3)) for v in route.vias]


def _coupled_gaps(result, drill: float = 0.2) -> tuple[list[float], list[float]]:
    """(same-rail, P-vs-N) drill edge-to-edge gaps of a coupled result."""
    p_vias, n_vias = _via_cells(result[0]), _via_cells(result[1])
    same = [
        _drill_gap(a, b, drill)
        for vias in (p_vias, n_vias)
        for i, a in enumerate(vias)
        for b in vias[i + 1 :]
    ]
    cross = [_drill_gap(a, b, drill) for a in p_vias for b in n_vias]
    return same, cross


#: The three walled fixtures (``_coupled_fixture`` keyword arguments).
_TIGHT = {"pitch": 0.6, "height": 3.0}
_LEGAL = {"pitch": 0.8, "height": 6.0}
_STAGGERED = {"pitch": 0.6, "height": 6.0, "b_wall": 4.0}


@pytest.mark.parametrize("use_cpp", COUPLED_BACKENDS)
def test_coupled_search_tight_pair_has_no_via_pair_below_the_floor(use_cpp):
    """Pitch 0.6 mm: side-by-side vias are 0.4 mm drill-to-drill, under the floor.

    On main both backends returned P vias (2.8, 1.2) / (3.2, 1.2) and N vias
    (2.8, 1.8) / (3.2, 1.8): 0.2 mm same-rail and 0.4 mm P-vs-N.  ``None`` is
    an acceptable answer here; an under-floor pair is not.
    """
    pf, result = _coupled_fixture(use_cpp=use_cpp, **_TIGHT)
    assert pf.last_rejections["via_own_h2h"] > 0
    if result is None:
        return
    same, cross = _coupled_gaps(result)
    assert all(g >= FLOOR - 1e-4 for g in same), same
    assert all(g >= FLOOR - 1e-4 for g in cross), cross
    # What the search does find: the rails stagger inside the departure zone
    # so the two drills sit on a diagonal.
    # Issue #6499: the pair comes back up to the F.Cu SMD goal pads.  It used
    # to stop on B.Cu under them with one via per rail (P [(1.3, 1.2)],
    # N [(1.7, 1.8)]), an open on both nets.
    assert _via_cells(result[0]) == [(1.7, 1.2), (5.0, 1.2)]
    assert _via_cells(result[1]) == [(1.3, 1.8), (4.6, 1.8)]
    p_start, p_end, n_start, n_end = (
        pf.fixture_pads[0],
        pf.fixture_pads[1],
        pf.fixture_pads[2],
        pf.fixture_pads[3],
    )
    conn = validate_net_connectivity(list(result), {1: [p_start, p_end], 2: [n_start, n_end]})
    assert conn[1]["connected"] and conn[2]["connected"], conn


@pytest.mark.parametrize("use_cpp", COUPLED_BACKENDS)
def test_coupled_search_takes_a_legal_layer_change(use_cpp):
    """Pitch 0.8 mm: a side-by-side via pair is legal (0.6 mm), so the pair
    hops the wall coupled -- with each rail's two vias pushed from 0.2 mm
    apart (main: x=2.8 and x=3.2) out to the floor (x=2.8 and x=3.5).

    The board is 6 x 6 mm rather than 6 x 3 because ``route_coupled`` caps
    its own iterations at ``cols * rows * 4``; on the 6 x 3 board that cap
    (7564) ends the search before it gets over the wall at this pitch.
    """
    _pf, result = _coupled_fixture(use_cpp=use_cpp, **_LEGAL)
    assert result is not None
    p_route, n_route = result
    assert _via_cells(p_route) == [(2.8, 2.6), (3.5, 2.6)]
    assert _via_cells(n_route) == [(2.8, 3.4), (3.5, 3.4)]
    same, cross = _coupled_gaps(result)
    assert min(same) == pytest.approx(0.5, abs=1e-6)
    assert min(cross) == pytest.approx(0.6, abs=1e-6)
    # It really is a there-and-back layer change ending on the pad layer.
    for route in result:
        assert route.segments[0].layer == Layer.F_CU
        assert route.segments[-1].layer == Layer.F_CU
        assert any(s.layer == Layer.B_CU for s in route.segments)


@pytest.mark.parametrize("use_cpp", COUPLED_BACKENDS)
def test_coupled_search_tight_pair_staggers_both_layer_changes(use_cpp):
    """Pitch 0.6 mm with a B.Cu wall forcing the pair back up: four vias, the
    P-vs-N pairs staggered onto the floor, none below it."""
    _pf, result = _coupled_fixture(use_cpp=use_cpp, **_STAGGERED)
    assert result is not None
    assert len(result[0].vias) == 2 and len(result[1].vias) == 2
    same, cross = _coupled_gaps(result)
    assert min(same) >= FLOOR - 1e-4, same
    assert min(cross) >= FLOOR - 1e-4, cross


@requires_cpp
@pytest.mark.parametrize("fixture", [_TIGHT, _LEGAL, _STAGGERED], ids=["tight", "legal", "stag"])
def test_coupled_backends_agree_on_the_walled_fixtures(fixture):
    cpp_pf, cpp_result = _coupled_fixture(use_cpp=True, **fixture)
    py_pf, py_result = _coupled_fixture(use_cpp=False, **fixture)
    assert (cpp_result is None) == (py_result is None)
    if cpp_result is not None:
        for cpp_route, py_route in zip(cpp_result, py_result, strict=True):
            assert _via_cells(cpp_route) == _via_cells(py_route)
    assert cpp_pf.last_iterations == py_pf.last_iterations
    assert cpp_pf.last_rejections["via_own_h2h"] == py_pf.last_rejections["via_own_h2h"]


def _open_pathfinder(allow_swap_via: bool = False) -> CoupledPathfinder:
    rules = _small_rules()
    grid = RoutingGrid(width=6.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    return CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=8,
        min_spacing_cells=6,
        allow_swap_via=allow_swap_via,
    )


def _via_moves(pf: CoupledPathfinder, pitch_cells: int, own_vias=None) -> int:
    state = CoupledState(GridPos(30, 30, 0), GridPos(30, 30 + pitch_cells, 0), (1, 0))
    moves = pf._get_coupled_neighbors(
        state, p_net=1, n_net=2, target_spacing_cells=pitch_cells, own_vias=own_vias
    )
    return sum(1 for _state, _cost, is_via in moves if is_via)


@pytest.mark.parametrize("swap", [False, True], ids=["plain", "swap-via"])
def test_coupled_via_move_predicate(swap):
    """The via move itself, with nothing else in the way (Python loop).

    Centres must be >= 0.7 mm (7 cells) apart.  ``allow_swap_via`` adds a
    second, Python-only via move per layer that drills the same two cells;
    it takes the same check.
    """
    pf = _open_pathfinder(allow_swap_via=swap)
    per_layer = 2 if swap else 1
    # P-vs-N of the move itself.
    assert _via_moves(pf, pitch_cells=6) == 0
    assert pf.last_rejections["via_own_h2h"] == per_layer
    assert _via_moves(pf, pitch_cells=7) == per_layer  # exactly on the floor
    assert _via_moves(pf, pitch_cells=8) == per_layer
    # Against an earlier via of the SAME rail (P at (30, 30)) ...
    assert _via_moves(pf, 8, own_vias=[(24, 30)]) == 0
    assert _via_moves(pf, 8, own_vias=[(23, 30)]) == per_layer
    # ... and of the PARTNER rail (N at (30, 38)): the list is not per-rail.
    assert _via_moves(pf, 8, own_vias=[(100, 100), (34, 34)]) == 0
    # A via stack on the cells of an earlier via pair is the same two drills.
    assert _via_moves(pf, 8, own_vias=[(30, 30), (30, 38)]) == per_layer
