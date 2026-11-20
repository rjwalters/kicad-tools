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

import logging
import math
from types import SimpleNamespace

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerDefinition, LayerStack, LayerType
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
    """A -> B on one row, a THIRD own-net pad C 0.13 mm of air off the row.

    C spans y 2.17..2.77; a straight A-B centerline at y=3.0 with a 0.1 mm
    half width leaves 0.13 mm of air beside C -- inside ``(0, 0.15)``.  The
    guard-on detour one row up (y=3.05) clears C by 0.18 mm: comfortably
    past the edge in both engines (the native grid stores float32, whose
    ulp at this scale would flip an exactly-at-edge row into the band).
    """
    rules = _rules(edge)
    grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    a, b, c = _pad(2.0, 3.0), _pad(10.0, 3.0), _pad(6.0, 2.47)
    for p in (a, b, c):
        grid.add_pad(p)
    return grid, rules, a, b


def _band_segments(route, pads: list[Pad]) -> list:
    """F.Cu segments whose air gap to any of ``pads`` sits inside ``(0, EDGE)``.

    Pads the route's copper already unions (a same-layer segment's swept
    rectangle overlaps the pad box) are skipped, per the 9019 review: the
    leg is connected to that pad through the path, so an isolated gap of a
    sub-segment behind the union is not a slit.  Only pass-by pads -- whose
    band the leg merely skirts -- are measured.  The fixture pads are F.Cu
    SMD pads, so copper on another layer (the re-route may hop to B.Cu
    through a via) shares nothing with them.
    """
    bboxes = [
        (p.x - p.width / 2, p.y - p.height / 2, p.x + p.width / 2, p.y + p.height / 2) for p in pads
    ]
    fc_segments = [s for s in route.segments if s.layer == Layer.F_CU]

    def unions(bb) -> bool:
        return any(
            s.x1 - s.width / 2 <= bb[2]
            and s.x2 + s.width / 2 >= bb[0]
            and s.y1 - s.width / 2 <= bb[3]
            and s.y2 + s.width / 2 >= bb[1]
            for s in fc_segments
        )

    measured = [bb for bb in bboxes if not unions(bb)]
    return [
        s
        for s in fc_segments
        if not same_net_segment_pad_gap_acceptable(
            (s.x1, s.y1), (s.x2, s.y2), s.width / 2, measured, EDGE
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
        cx, cy = grid.world_to_grid(6.0, 3.0)  # y=3.0: 0.13 mm of air beside C
        assert not router._own_net_band_step_ok(cx - 1, cy, cx, cy, layer, NET, WIDTH, EDGE)
        # Through the pad (y=2.5): the leg is unioned with C.
        px, py = grid.world_to_grid(6.0, 2.47)
        assert router._own_net_band_step_ok(px - 1, py, px, py, layer, NET, WIDTH, EDGE)
        # At the edge (y=3.05 -> 0.18 mm of air): acceptable.
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
        c = _pad(6.0, 2.47)
        assert _band_segments(baseline, [c]), "fixture must exercise the band with the guard off"

        grid, rules, a, b = _fixture(EDGE)
        guarded = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert guarded is not None, "the guard must re-route, not fail the net"
        assert not _band_segments(guarded, [c])
        # 9019 review: the pre-fix gate refused every planar pad exit and the
        # route escaped through a pair of pad-edge vias instead.  A plain
        # pad-to-pad leg is unioned with its pads; the guard must never cost
        # it a via.
        assert not guarded.vias

    @requires_cpp
    def test_python_and_cpp_emit_identical_routes(self) -> None:
        grid, rules, a, b = _fixture(EDGE)
        py_route = Router(grid, rules, diagonal_routing=True).route(a, b)
        assert py_route is not None

        grid2, rules2, a2, b2 = _fixture(EDGE)
        cpp_route = CppPathfinder(CppGrid.from_routing_grid(grid2), rules2).route(a2, b2)
        assert cpp_route is not None
        assert not _band_segments(cpp_route, [_pad(6.0, 2.47)])
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


def _single_layer_stack() -> LayerStack:
    """A single-sided stack: no B.Cu, so a via cannot rescue a planar dead end."""
    return LayerStack(
        name="1-Layer",
        description="Single-sided",
        layers=[LayerDefinition("F.Cu", 0, LayerType.SIGNAL, is_outer=True)],
    )


class TestRoutedPlanarity:
    """A guarded pad-to-pad leg stays planar (#9019 review, section 1).

    The pre-fix gate measured every step against the pad being left or
    approached, refused every planar exit, and the route escaped through a
    pair of vias seated on the pad edge (board 02: 29 -> 36 vias, 7 new
    ``physical_copper_gap`` findings on those annuli).  The exemption (skip
    pads the leg is unioned with -- its own start/end) must keep a plain
    connection via-free, on two pads and when a third own-net pad sits in
    the band, on a single-layer board where a via is not even available.
    """

    def _fixture(self, three_pads: bool) -> tuple[RoutingGrid, DesignRules, Pad, Pad, list[Pad]]:
        rules = _rules(EDGE)
        grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=_single_layer_stack())
        pads = [_pad(2.0, 3.0), _pad(10.0, 3.0)]
        if three_pads:
            pads.append(_pad(6.0, 2.47))
        for p in pads:
            grid.add_pad(p)
        return grid, rules, pads[0], pads[1], pads

    @pytest.mark.parametrize("three_pads", [False, True], ids=["two-pad", "three-pad"])
    def test_guarded_route_keeps_vias_zero(self, three_pads: bool) -> None:
        grid, rules, a, b, pads = self._fixture(three_pads)
        router = Router(grid, rules, diagonal_routing=True)
        route = router.route(a, b)
        assert route is not None, "the guarded route must exist planarly"
        assert route.vias == []
        assert not _band_segments(route, pads)
        # The fix is the exemption, not the gate-off retry: no fallback ran.
        assert router._own_net_band_fallbacks == 0

    @requires_cpp
    @pytest.mark.parametrize("three_pads", [False, True], ids=["two-pad", "three-pad"])
    def test_guarded_route_keeps_vias_zero_cpp(self, three_pads: bool) -> None:
        grid, rules, a, b, pads = self._fixture(three_pads)
        cpp = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        route = cpp.route(a, b)
        assert route is not None, "the guarded route must exist planarly"
        assert route.vias == []
        assert not _band_segments(route, pads)
        assert getattr(cpp, "_own_net_band_fallbacks", 0) == 0


class TestBandSafeTaperWidths:
    """The route conversion calls ``band_safe_width`` on emitted taper
    sub-segments (#9019 review, section 4).

    Regression shape: disabling the ``band_safe_width`` call sites in BOTH
    ``_convert_path_to_route`` and the C++ route conversion used to fail
    zero tests.  A fine-pitch leg tapers to ``min_trace_width`` beside its
    own pad C; the narrowed sub-segments over C's span sit in the band the
    base width unions away, so they must be emitted at the base width.
    """

    MIN_W = 0.1

    def _rules(self) -> DesignRules:
        return DesignRules(
            trace_width=WIDTH,
            trace_clearance=0.15,
            min_trace_width=self.MIN_W,
            grid_resolution=0.05,
            same_net_pad_gap_band_edge_mm=EDGE,
        )

    def _fixture(self) -> tuple[RoutingGrid, DesignRules, Pad, Pad, Pad]:
        # A fine-pitch component (A + its neighbour A2, 0.6 mm apart < 0.8)
        # on a row grazing C's top edge: at base width 0.2 the leg's edge
        # unions C (gap 0); at the taper's 0.1..0.17 it sits 0.03..0.05 mm
        # off -- inside the band.  C sits inside A's taper zone (x < 3.0)
        # but its span starts 0.3 mm right of A's metal, so the base width
        # is itself band-clear against A there and widening is available.
        # The hand-built path is the row a search emits.
        rules = self._rules()
        grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
        a, a2, b, c = _pad(2.0, 2.9), _pad(2.0, 2.3), _pad(10.0, 2.9), _pad(2.9, 2.5)
        for p in (a, a2, b, c):
            grid.add_pad(p)
        return grid, rules, a, b, c

    def _row_path(self) -> list[tuple[float, float, int, bool]]:
        return [(round(2.0 + 0.05 * i, 2), 2.9, 0, False) for i in range(161)]

    def test_python_conversion_keeps_base_width_over_the_pad(self) -> None:
        grid, rules, a, b, c = self._fixture()
        router = Router(grid, rules, diagonal_routing=True)
        assert router.rules.should_apply_neck_down(a.ref, 0.6), "fixture must be fine-pitch"
        route = _new_route(a)
        router._convert_path_to_route(self._row_path(), route, a, b)
        _assert_band_safe_taper(route, c)

    @requires_cpp
    def test_cpp_conversion_keeps_base_width_over_the_pad(self) -> None:
        _grid, rules, a, b, c = self._fixture()
        pitches = {"U1": 0.6}
        finder = SimpleNamespace(
            _rules=rules,
            _grid=SimpleNamespace(
                num_layers=1,
                # The real CppGrid maps index -> layer enum VALUE (an int),
                # as RoutingGrid._index_to_layer does.
                index_to_layer=lambda i: (Layer.F_CU.value, Layer.B_CU.value)[i],
                resolution=0.05,
                _py_grid=SimpleNamespace(_pads=[a, _pad(2.0, 2.3), b, c]),
            ),
            _get_component_pitches=lambda: pitches,
        )
        result = SimpleNamespace(
            segments=[SimpleNamespace(x1=2.0, y1=2.9, x2=10.0, y2=2.9, layer=0, net=NET)],
            vias=[],
        )
        route = CppPathfinder._convert_result_to_route(finder, result, a, b, None)
        _assert_band_safe_taper(route, c)


def _new_route(a: Pad):
    from kicad_tools.router.primitives import Route

    return Route(net=a.net, net_name=a.net_name)


def _assert_band_safe_taper(route, c: Pad) -> None:
    """Shared assertions for the conversion-level taper fixtures."""
    # The taper did narrow (the fixture is fine-pitch and the row leaves
    # both pads), so the widths below are the taper's, not the corridor's.
    assert any(s.width < WIDTH for s in route.segments), "fixture must exercise the taper"
    c_left, c_right = c.x - c.width / 2, c.x + c.width / 2
    over_c = [
        s
        for s in route.segments
        if s.y1 == s.y2
        and c_left + WIDTH / 2 <= min(s.x1, s.x2)
        and max(s.x1, s.x2) <= c_right - WIDTH / 2
    ]
    assert over_c, "the row must cross C's span"
    # Every sub-segment fully above C sits in the band at its taper width
    # and unions C at the base width: emitted at the base width.
    for s in over_c:
        assert s.width == pytest.approx(WIDTH), f"sub-segment {s.x1}..{s.x2} widened, got {s.width}"
    # And narrowing away from C still happens (the taper is not disabled).
    clear = [
        s
        for s in route.segments
        if s.y1 == s.y2 and max(s.x1, s.x2) < c_left - WIDTH and min(s.x1, s.x2) > 2.0 + WIDTH
    ]
    assert clear, "segments between the pad and C must exist"
    assert any(s.width < WIDTH for s in clear), "the taper must still narrow off the pad"


class TestBandFallbackRetry:
    """The gate-off retry fires only on gate-caused failures, and is counted
    (#9019 review, section 3).

    Positive: an own-net wall pad spans the board on a single-layer board --
    every planar crossing passes through the slit-band ring the gate
    refuses, so the gated search fails WITH band rejections and the retry
    (gate suspended for that one search) keeps the connection.  The
    fallback is counted and logged, never silent.

    Negative: a net the gate never influenced (a foreign-pad wall around
    the target -- no own-net pad in band position, so zero band rejections)
    is returned as-is; no second search, no fallback count.
    """

    def _wall_fixture(self, edge: float | None) -> tuple[RoutingGrid, DesignRules, Pad, Pad]:
        rules = _rules(edge)
        grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=_single_layer_stack())
        a, b = _pad(2.0, 3.0), _pad(10.0, 3.0)
        # Own-net slab covering every row between A and B: each crossing's
        # exit steps leave C's metal through the (0, edge) ring.
        c = _pad(6.0, 3.0, w=0.6, h=7.0)
        for p in (a, b, c):
            grid.add_pad(p)
        return grid, rules, a, b

    def _walled_target_fixture(
        self, edge: float | None
    ) -> tuple[RoutingGrid, DesignRules, Pad, Pad]:
        rules = _rules(edge)
        grid = RoutingGrid(width=12.0, height=6.0, rules=rules, layer_stack=_single_layer_stack())
        a, b = _pad(2.0, 3.0), _pad(10.0, 3.0)
        for p in (a, b):
            grid.add_pad(p)
        # Foreign ring of overlapping pads around B: an airtight obstacle
        # wall the gate has nothing to do with (no own-net pad in band
        # position -> zero band rejections).
        for i in range(16):
            ang = math.pi * i / 8
            grid.add_pad(
                _pad(
                    10.0 + 0.9 * math.cos(ang),
                    3.0 + 0.9 * math.sin(ang),
                    w=0.4,
                    h=0.4,
                    net=NET + 98,
                )
            )
        return grid, rules, a, b

    def test_retry_rescues_a_gate_blocked_net_and_counts(self, caplog) -> None:
        grid, rules, a, b = self._wall_fixture(EDGE)
        router = Router(grid, rules, diagonal_routing=True)
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.pathfinder"):
            route = router.route(a, b)
        assert route is not None, "the gate-off retry must keep the connection"
        assert router._own_net_band_fallbacks == 1
        assert any("gate suspended" in r.message for r in caplog.records)

    def test_no_retry_when_the_gate_never_refused(self, caplog) -> None:
        grid, rules, a, b = self._walled_target_fixture(EDGE)
        router = Router(grid, rules, diagonal_routing=True)
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.pathfinder"):
            route = router.route(a, b)
        assert route is None, "the foreign wall is unroutable regardless of the gate"
        assert router._own_net_band_fallbacks == 0
        assert not any("gate suspended" in r.message for r in caplog.records)

    @requires_cpp
    def test_retry_rescues_a_gate_blocked_net_and_counts_cpp(self, caplog) -> None:
        grid, rules, a, b = self._wall_fixture(EDGE)
        cpp = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.cpp_backend"):
            route = cpp.route(a, b)
        assert route is not None, "the gate-off retry must keep the connection"
        assert getattr(cpp, "_own_net_band_fallbacks", 0) == 1
        assert any("gate suspended" in r.message for r in caplog.records)

    @requires_cpp
    def test_no_retry_when_the_gate_never_refused_cpp(self, caplog) -> None:
        grid, rules, a, b = self._walled_target_fixture(EDGE)
        cpp = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.cpp_backend"):
            route = cpp.route(a, b)
        assert route is None, "the foreign wall is unroutable regardless of the gate"
        assert getattr(cpp, "_own_net_band_fallbacks", 0) == 0
        assert not any("gate suspended" in r.message for r in caplog.records)
