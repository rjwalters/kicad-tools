"""Issue #9019: the grid router (Python + C++) keeps own-net legs and the
neck-down taper out of the fab ``pad_spacing`` slit band.

#9011 measured board 06's pad-vs-trace slits as grid A* legs and the #1018
taper staircase running beside an OWN-net pad inside the fab band: same-net
copper is passable to the search, so nothing looked at those pads.  The fix
holds every planar step, and every emitted taper sub-segment, to the two
acceptable regions of
:func:`~kicad_tools.router.via_clearance.same_net_segment_pad_gap_acceptable`
(#9012) -- union with the pad (gap <= 0) or clear of the detection edge -- in
BOTH engines, so CI's pure-Python run and a local C++ run route the same bytes.

* the step predicate, Python vs native, over a lattice of steps around a pad;
* the taper width helper (a narrowing that opens a slit keeps the base width);
* a routed fixture: the guard-off route skirts the own-net pad in the band, the
  guard-on route never does, and the two backends emit identical segments;
* the guard is off when the stackup resolves no edge (``None``/``0``).

The board-level slit gate (board 06's 13 findings) is re-measured by the
board regeneration gate, not here.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.neck_down import band_safe_width
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import same_net_segment_pad_gap_acceptable

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

EDGE = 0.15  # jlcpcb pad_spacing detection edge (#9016)
WIDTH = 0.2
NET = 1


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


def _pad(x: float, y: float, w: float = 0.6, h: float = 0.6, net: int = NET) -> Pad:
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
    )


def _fixture(edge: float | None) -> tuple[RoutingGrid, DesignRules, Pad, Pad]:
    """A -> B on one row, a THIRD own-net pad C 0.1 mm of air off the row.

    C spans y 2.2..2.8; a straight A-B centerline at y=3.0 with a 0.1 mm half
    width leaves 0.1 mm of air beside C -- inside ``(0, 0.15)``.
    """
    rules = _rules(edge)
    grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    a, b, c = _pad(2.0, 3.0), _pad(10.0, 3.0), _pad(6.0, 2.5)
    for p in (a, b, c):
        grid.add_pad(p)
    return grid, rules, a, b


def _band_segments(route, pads: list[Pad]) -> list:
    """F.Cu segments whose air gap to any of ``pads`` sits inside ``(0, EDGE)``.

    The fixture pads are F.Cu SMD pads, so copper on another layer (the
    re-route may hop to B.Cu through a via) shares nothing with them.
    """
    bboxes = [
        (p.x - p.width / 2, p.y - p.height / 2, p.x + p.width / 2, p.y + p.height / 2) for p in pads
    ]
    return [
        s
        for s in route.segments
        if s.layer == Layer.F_CU
        and not same_net_segment_pad_gap_acceptable(
            (s.x1, s.y1), (s.x2, s.y2), s.width / 2, bboxes, EDGE
        )
    ]


def _signature(route) -> list[tuple]:
    """The route's copper as collinear-merged polyline corners.

    The raw native adapter emits one segment per grid cell where the Python
    router merges collinear runs inline (#972); both draw the same copper, so
    compare corners, widths and layers, not the segmentation.
    """
    out: list[tuple] = [("via", round(v.x, 4), round(v.y, 4)) for v in route.vias]
    merged: list[list] = []  # [x1, y1, x2, y2, width, layer]
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


class TestTaperWidth:
    """``band_safe_width``: the taper only narrows, so it can open a slit."""

    PAD = [(0.0, 0.0, 1.0, 1.0)]

    def test_narrowing_that_opens_a_slit_keeps_base_width(self) -> None:
        # Base 0.2 mm centerline 0.6 from the box edge: gap 0.5 -> fine; use a
        # leg that touches at base width but would sit 0.05 mm off at 0.1.
        a, b = (1.15, 2.0), (1.15, 0.5)  # x=1.15: edge distance 0.15
        assert same_net_segment_pad_gap_acceptable(a, b, 0.15, self.PAD, EDGE)  # base 0.3: gap 0.0
        assert not same_net_segment_pad_gap_acceptable(a, b, 0.05, self.PAD, EDGE)  # 0.1 wide: 0.1
        assert band_safe_width(a, b, 0.1, 0.3, self.PAD, EDGE) == 0.3

    def test_taper_clear_of_the_band_stands(self) -> None:
        a, b = (3.0, 2.0), (3.0, 0.5)  # 2.0 mm clear of the pad
        assert band_safe_width(a, b, 0.1, 0.3, self.PAD, EDGE) == 0.1

    def test_off_without_an_edge_or_pads(self) -> None:
        a, b = (1.15, 2.0), (1.15, 0.5)
        assert band_safe_width(a, b, 0.1, 0.3, self.PAD, 0.0) == 0.1
        assert band_safe_width(a, b, 0.1, 0.3, None, EDGE) == 0.1
        assert band_safe_width(a, b, 0.3, 0.3, self.PAD, EDGE) == 0.3


class TestPythonStepGate:
    def _router(self, edge: float | None) -> tuple[Router, RoutingGrid]:
        grid, rules, _a, _b = _fixture(edge)
        return Router(grid, rules, diagonal_routing=True), grid

    def test_step_in_band_refused_union_and_clear_accepted(self) -> None:
        router, grid = self._router(EDGE)
        layer = grid.layer_to_index(Layer.F_CU.value)
        cx, cy = grid.world_to_grid(6.0, 3.0)  # y=3.0: 0.1 mm of air beside C
        assert not router._own_net_band_step_ok(cx - 1, cy, cx, cy, layer, NET, WIDTH, EDGE)
        # Through the pad (y=2.5): the leg is unioned with C.
        px, py = grid.world_to_grid(6.0, 2.5)
        assert router._own_net_band_step_ok(px - 1, py, px, py, layer, NET, WIDTH, EDGE)
        # At the edge (y=3.05 -> 0.15 mm of air): acceptable.
        ex, ey = grid.world_to_grid(6.0, 3.05)
        assert router._own_net_band_step_ok(ex - 1, ey, ex, ey, layer, NET, WIDTH, EDGE)

    def test_other_net_and_disabled_edge_exempt(self) -> None:
        router, grid = self._router(EDGE)
        layer = grid.layer_to_index(Layer.F_CU.value)
        cx, cy = grid.world_to_grid(6.0, 3.0)
        assert router._own_net_band_step_ok(cx - 1, cy, cx, cy, layer, NET + 1, WIDTH, EDGE)
        assert router._own_net_band_step_ok(cx - 1, cy, cx, cy, layer, NET, WIDTH, 0.0)


class TestRoutedFixture:
    def test_guard_off_skirts_the_band_guard_on_does_not(self) -> None:
        grid, rules, a, b = _fixture(None)
        baseline = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert baseline is not None
        c = _pad(6.0, 2.5)
        assert _band_segments(baseline, [c]), "fixture must exercise the band with the guard off"

        grid, rules, a, b = _fixture(EDGE)
        guarded = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert guarded is not None, "the guard must re-route, not fail the net"
        assert not _band_segments(guarded, [c])

    @requires_cpp
    def test_python_and_cpp_emit_identical_routes(self) -> None:
        grid, rules, a, b = _fixture(EDGE)
        py_route = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert py_route is not None

        grid2, rules2, a2, b2 = _fixture(EDGE)
        cpp_route = CppPathfinder(CppGrid.from_routing_grid(grid2), rules2).route(a2, b2)
        assert cpp_route is not None
        assert not _band_segments(cpp_route, [_pad(6.0, 2.5)])
        assert _signature(cpp_route) == _signature(py_route)

    @requires_cpp
    def test_cpp_guard_off_without_an_edge(self) -> None:
        grid, rules, a, b = _fixture(None)
        py_route = Router(grid, rules, diagonal_routing=True).route(a, b)
        grid2, rules2, a2, b2 = _fixture(None)
        cpp_route = CppPathfinder(CppGrid.from_routing_grid(grid2), rules2).route(a2, b2)
        assert py_route is not None and cpp_route is not None
        assert _signature(cpp_route) == _signature(py_route)


@requires_cpp
class TestStepPredicateParity:
    """Native ``own_net_band_step_ok`` == Python ``_own_net_band_step_ok``."""

    def test_lattice_of_steps_agrees(self) -> None:
        grid, rules, _a, _b = _fixture(EDGE)
        router = Router(grid, rules, diagonal_routing=True)
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules)
        pf._impl.set_search_own_net_band(EDGE)
        layer = grid.layer_to_index(Layer.F_CU.value)
        cpp_layer = cpp_grid.layer_to_index(Layer.F_CU.value)
        x0, y0 = grid.world_to_grid(5.0, 1.8)
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
        assert refused > 0 and math.isfinite(refused), "the lattice must cover the band"
