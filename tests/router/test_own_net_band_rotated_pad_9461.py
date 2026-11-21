"""Issue #9461: the own-net pad slit-band gate (#9019) honours ``Pad.rotation``.

The gate measures a step's air gap to each own-net SMD pad's box and refuses a
gap inside ``(0, edge)``.  It used to build that box from raw ``width/2`` /
``height/2``, so a pad rotated at a non-cardinal angle got a box SMALLER than
its board-space AABB.  An under-sized box overstates the gap, so the failure is
a silent ACCEPT: a leg parked in the band beside the rotated pad passed.  Both
backends now build the box from :func:`~kicad_tools.router.primitives.pad_half_extents`
(#4910) -- the Python gate directly, the native gate through its twin.

The fixture puts a square own-net pad C beside an A->B row so that its rotated
AABB sits 0.07 mm of air from a 0.2 mm leg on the row (inside the 0.15 mm
band), while the UNROTATED box at the same centre sits ~0.18-0.19 mm away
(outside it).  So the step is refused only when the rotation is honoured.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, pad_half_extents
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import same_net_segment_pad_gap_acceptable

EDGE = 0.15  # jlcpcb pad_spacing detection edge (#9016)
WIDTH = 0.2
NET = 1
ROW_Y = 3.0
C_SIDE = 0.6
# Air between the leg's copper edge (ROW_Y - WIDTH/2) and C's rotated AABB.
TARGET_AIR = 0.07

ROTATIONS = [30.0, 45.0]


def _rules(edge: float | None) -> DesignRules:
    return DesignRules(
        trace_width=WIDTH,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.05,
        same_net_pad_gap_band_edge_mm=edge,
    )


def _pad(
    x: float,
    y: float,
    w: float = 0.6,
    h: float = 0.6,
    rotation: float = 0.0,
    shape: str = "rect",
    net: int = NET,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=w,
        height=h,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref="U1",
        pin="1",
        rotation=rotation,
        shape=shape,
    )


def _c_center_y(rotation: float) -> float:
    """C's centre so its ROTATED AABB top leaves ``TARGET_AIR`` beside the leg."""
    _hw, hh = pad_half_extents(_pad(0.0, 0.0, C_SIDE, C_SIDE, rotation))
    return ROW_Y - WIDTH / 2 - TARGET_AIR - hh


def _c_pad(rotation: float, *, center_rotation: float | None = None) -> Pad:
    """Pad C at ``rotation``; ``center_rotation`` picks where it sits."""
    cy = _c_center_y(rotation if center_rotation is None else center_rotation)
    return _pad(6.0, cy, C_SIDE, C_SIDE, rotation)


def _fixture(c: Pad, edge: float | None = EDGE) -> tuple[RoutingGrid, DesignRules, Pad, Pad]:
    rules = _rules(edge)
    grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    a, b = _pad(2.0, ROW_Y), _pad(10.0, ROW_Y)
    for p in (a, b, c):
        grid.add_pad(p)
    return grid, rules, a, b


def _row_step(grid: RoutingGrid) -> tuple[int, int, int, int]:
    cx, cy = grid.world_to_grid(6.0, ROW_Y)
    return cx - 1, cy, cx, cy


def _cpp_gate(grid: RoutingGrid, rules: DesignRules):
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules)
    pf._impl.set_search_own_net_band(EDGE)
    return pf, cpp_grid.layer_to_index(Layer.F_CU.value)


class TestFixtureGeometry:
    """The fixture really separates the rotated box from the raw one."""

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_rotated_box_in_band_raw_box_clear(self, rotation: float) -> None:
        c = _c_pad(rotation)
        hw, hh = pad_half_extents(c)
        assert hh > c.height / 2  # non-cardinal: the AABB is larger
        a, b = (5.95, ROW_Y), (6.0, ROW_Y)
        rotated = [(c.x - hw, c.y - hh, c.x + hw, c.y + hh)]
        raw = [(c.x - 0.3, c.y - 0.3, c.x + 0.3, c.y + 0.3)]
        assert not same_net_segment_pad_gap_acceptable(a, b, WIDTH / 2, rotated, EDGE)
        assert same_net_segment_pad_gap_acceptable(a, b, WIDTH / 2, raw, EDGE)


class TestPythonGate:
    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_step_beside_rotated_pad_refused(self, rotation: float) -> None:
        grid, rules, _a, _b = _fixture(_c_pad(rotation))
        router = Router(grid, rules, diagonal_routing=True)
        layer = grid.layer_to_index(Layer.F_CU.value)
        assert not router._own_net_band_step_ok(*_row_step(grid), layer, NET, WIDTH, EDGE)

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_unrotated_control_at_same_centre_accepted(self, rotation: float) -> None:
        # Same centre, rotation 0: the box is the raw one, clear of the band.
        grid, rules, _a, _b = _fixture(_c_pad(0.0, center_rotation=rotation))
        router = Router(grid, rules, diagonal_routing=True)
        layer = grid.layer_to_index(Layer.F_CU.value)
        assert router._own_net_band_step_ok(*_row_step(grid), layer, NET, WIDTH, EDGE)

    def test_rotation_zero_box_is_raw_width_height(self) -> None:
        grid, rules, _a, _b = _fixture(_c_pad(0.0, center_rotation=45.0))
        router = Router(grid, rules, diagonal_routing=True)
        layer = grid.layer_to_index(Layer.F_CU.value)
        c = _c_pad(0.0, center_rotation=45.0)
        expected = (c.x - c.width / 2, c.y - c.height / 2, c.x + c.width / 2, c.y + c.height / 2)
        assert expected in router._own_net_band_bboxes(NET, layer)


@pytest.mark.requires_router_cpp
class TestCppGate:
    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_step_beside_rotated_pad_refused(self, rotation: float) -> None:
        grid, rules, _a, _b = _fixture(_c_pad(rotation))
        pf, layer = _cpp_gate(grid, rules)
        assert not pf._impl.own_net_band_step_ok(*_row_step(grid), layer, NET, WIDTH)

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_unrotated_control_at_same_centre_accepted(self, rotation: float) -> None:
        grid, rules, _a, _b = _fixture(_c_pad(0.0, center_rotation=rotation))
        pf, layer = _cpp_gate(grid, rules)
        assert pf._impl.own_net_band_step_ok(*_row_step(grid), layer, NET, WIDTH)


@pytest.mark.requires_router_cpp
class TestStepPredicateParity:
    """Native ``own_net_band_step_ok`` == Python ``_own_net_band_step_ok``."""

    @pytest.mark.parametrize(
        "c",
        [
            _c_pad(30.0),
            _c_pad(45.0),
            _c_pad(0.0, center_rotation=45.0),
            # A 90-degree residual on a non-square pad (dimensions swap).
            _pad(6.0, 2.3, 0.8, 0.3, rotation=90.0),
            # A circle: its bounding square, whatever the rotation says.
            _pad(6.0, 2.4, 0.6, 0.6, rotation=30.0, shape="circle"),
        ],
        ids=["rot30", "rot45", "rot0", "rot90-rect", "circle"],
    )
    def test_lattice_of_steps_agrees(self, c: Pad) -> None:
        grid, rules, _a, _b = _fixture(c)
        router = Router(grid, rules, diagonal_routing=True)
        pf, cpp_layer = _cpp_gate(grid, rules)
        layer = grid.layer_to_index(Layer.F_CU.value)
        x0, y0 = grid.world_to_grid(5.0, 1.6)
        x1, y1 = grid.world_to_grid(7.0, 3.4)
        steps = [(1, 0), (0, 1), (1, 1), (1, -1), (-1, 0), (0, -1)]
        checked = refused = 0
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                for dx, dy in steps:
                    py = router._own_net_band_step_ok(
                        gx, gy, gx + dx, gy + dy, layer, NET, WIDTH, EDGE
                    )
                    cpp = pf._impl.own_net_band_step_ok(
                        gx, gy, gx + dx, gy + dy, cpp_layer, NET, WIDTH
                    )
                    assert py == cpp, f"step ({gx},{gy})+({dx},{dy}) py={py} cpp={cpp}"
                    checked += 1
                    refused += not py
        assert checked > 1000
        assert refused > 0, "the lattice must cover the band"


def _band_segments(route, c: Pad) -> list:
    """F.Cu segments in the band beside C's rotated AABB (C not unioned)."""
    hw, hh = pad_half_extents(c)
    bb = (c.x - hw, c.y - hh, c.x + hw, c.y + hh)
    fc = [s for s in route.segments if s.layer == Layer.F_CU]
    unioned = any(
        min(s.x1, s.x2) - s.width / 2 <= bb[2]
        and max(s.x1, s.x2) + s.width / 2 >= bb[0]
        and min(s.y1, s.y2) - s.width / 2 <= bb[3]
        and max(s.y1, s.y2) + s.width / 2 >= bb[1]
        for s in fc
    )
    if unioned:
        return []
    return [
        s
        for s in fc
        if not same_net_segment_pad_gap_acceptable(
            (s.x1, s.y1), (s.x2, s.y2), s.width / 2, [bb], EDGE
        )
    ]


def _signature(route) -> list[tuple]:
    """Collinear-merged corners (the native adapter emits per-cell segments)."""
    out: list[tuple] = [("via", round(v.x, 4), round(v.y, 4)) for v in route.vias]
    merged: list[list] = []
    for s in route.segments:
        seg = [s.x1, s.y1, s.x2, s.y2, s.width, s.layer.value]
        if merged:
            m = merged[-1]
            same = m[4] == seg[4] and m[5] == seg[5] and (m[2], m[3]) == (seg[0], seg[1])
            cross = (m[2] - m[0]) * (seg[3] - seg[1]) - (m[3] - m[1]) * (seg[2] - seg[0])
            dot = (m[2] - m[0]) * (seg[2] - seg[0]) + (m[3] - m[1]) * (seg[3] - seg[1])
            if same and abs(cross) < 1e-9 and dot > 0:
                m[2], m[3] = seg[2], seg[3]
                continue
        merged.append(seg)
    out += [tuple(round(v, 4) for v in m) for m in merged]
    return out


class TestRoutedFixture:
    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_guard_keeps_the_route_out_of_the_rotated_band(self, rotation: float) -> None:
        c = _c_pad(rotation)
        grid, rules, a, b = _fixture(c, edge=None)
        baseline = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert baseline is not None
        assert _band_segments(baseline, c), "fixture must exercise the band with the guard off"

        grid, rules, a, b = _fixture(c)
        guarded = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert guarded is not None
        assert not _band_segments(guarded, c)

    @pytest.mark.requires_router_cpp
    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_python_and_cpp_emit_identical_routes(self, rotation: float) -> None:
        c = _c_pad(rotation)
        grid, rules, a, b = _fixture(c)
        py_route = Router(grid, rules, diagonal_routing=True).route(a, b)
        grid2, rules2, a2, b2 = _fixture(c)
        cpp_route = CppPathfinder(CppGrid.from_routing_grid(grid2), rules2).route(a2, b2)
        assert py_route is not None and cpp_route is not None
        assert not _band_segments(cpp_route, c)
        assert _signature(cpp_route) == _signature(py_route)
