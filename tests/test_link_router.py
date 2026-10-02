"""Single-link router used by the oracle completion loop (Issue #5785)."""

from __future__ import annotations

import pytest

shapely = pytest.importorskip("shapely")
from shapely.geometry import box  # noqa: E402

from kicad_tools.router.link_router import (  # noqa: E402
    LinkRouteRules,
    LinkTerminal,
    _BoardModel,
    _Item,
    _octilinear,
    _shortcut,
    net_components,
    padless_components,
    route_link,
)

RULES = LinkRouteRules(trace_width=0.2, clearance=0.2, via_size=0.6, via_drill=0.3, margin=2.0)


def _model(copper, items=(), fills=None) -> _BoardModel:
    return _BoardModel(
        copper=list(copper),
        items=list(items),
        drills=[],
        pad_geoms=[],
        fills=fills or {},
        outline=None,
    )


def _term(label, x, y, layer="F.Cu", half=0.4) -> LinkTerminal:
    return LinkTerminal(label, (x, y), {layer: box(x - half, y - half, x + half, y + half)})


class TestShortcut:
    def test_staircase_collapses_to_one_diagonal(self):
        stairs = [(0.0, 0.0), (0.05, 0.0), (0.05, 0.05), (0.1, 0.05), (0.1, 0.1)]
        # Octilinear only: the whole thing is the (0,0)-(0.1,0.1) diagonal.
        assert _shortcut(stairs, None, 0.3, None) == [(0.0, 0.0), (0.1, 0.1)]

    def test_blocked_shortcut_is_refused(self):
        pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
        wall = box(0.4, 0.4, 0.6, 0.6)  # sits on the diagonal, clear of both legs
        assert _shortcut(pts, wall, 0.1, None) == pts

    def test_endpoints_always_kept(self):
        pts = [(0.0, 0.0), (0.3, 0.1), (0.5, 0.5)]
        out = _shortcut(pts, None, 0.1, None)
        assert out[0] == pts[0] and out[-1] == pts[-1]

    @pytest.mark.parametrize(
        ("p", "q", "ok"),
        [
            ((0, 0), (1, 0), True),
            ((0, 0), (0, 2), True),
            ((0, 0), (2, 2), True),
            ((0, 0), (2, 1), False),
        ],
    )
    def test_octilinear(self, p, q, ok):
        assert _octilinear(p, q) is ok


class TestRouteLink:
    def test_straight_link_lands_on_centi_mm_inside_terminals(self):
        a, b = _term("a", 10.003, 10.0), _term("b", 14.0, 10.0)
        route = route_link(None, 1, "N", a, b, RULES, model=_model(["F.Cu", "B.Cu"]))
        assert route is not None and not route.vias
        for x1, y1, x2, y2, layer in route.segments:
            assert layer == "F.Cu"
            for v in (x1, y1, x2, y2):
                assert round(v, 2) == pytest.approx(v)  # exactly what the writer keeps
        (sx, sy, *_), (*_, ex, ey, _l) = route.segments[0], route.segments[-1]
        assert box(9.6, 9.6, 10.4, 10.4).contains(shapely.geometry.Point(sx, sy))
        assert box(13.6, 9.6, 14.4, 10.4).contains(shapely.geometry.Point(ex, ey))

    def test_foreign_wall_forces_detour_clear_of_it(self):
        wall = _Item(2, {"F.Cu": box(11.8, 8.0, 12.2, 12.0)})
        a, b = _term("a", 10.0, 10.0), _term("b", 14.0, 10.0)
        route = route_link(None, 1, "N", a, b, RULES, model=_model(["F.Cu", "B.Cu"], [wall]))
        assert route is not None
        assert route.vias  # the wall spans the F.Cu window: hop to B.Cu
        for x1, y1, x2, y2, layer in route.segments:
            if layer == "F.Cu":
                line = shapely.geometry.LineString([(x1, y1), (x2, y2)])
                assert line.distance(wall.layers["F.Cu"]) >= 0.2 + 0.1 - 1e-6

    def test_no_track_runs_along_a_plane_layer(self):
        plane = {"In1.Cu": [(9, box(0, 0, 40, 40))]}  # foreign pour on In1
        wall = _Item(2, {"F.Cu": box(11.8, 0.0, 12.2, 40.0), "B.Cu": box(11.8, 0.0, 12.2, 40.0)})
        a, b = _term("a", 10.0, 10.0), _term("b", 14.0, 10.0)
        model = _model(["F.Cu", "In1.Cu", "B.Cu"], [wall], plane)
        route = route_link(None, 1, "N", a, b, RULES, model=model)
        # F.Cu and B.Cu are walled; In1 is a plane layer: no legal route.
        assert route is None

    def test_unreachable_returns_none(self):
        ring = [
            _Item(2, {"F.Cu": box(12.0, 8.0, 12.4, 12.0), "B.Cu": box(12.0, 8.0, 12.4, 12.0)}),
        ]
        a, b = _term("a", 10.0, 10.0), _term("b", 14.0, 10.0)
        tight = LinkRouteRules(margin=0.5)
        assert route_link(None, 1, "N", a, b, tight, model=_model(["F.Cu", "B.Cu"], ring)) is None


class TestPadlessComponents:
    def test_pad_bearing_island_is_not_floating(self):
        pad = _Item(1, {"F.Cu": box(0, 0, 1, 1)}, True)
        model = _model(
            ["F.Cu"],
            [pad],
            {"F.Cu": [(1, box(0.5, 0.5, 3, 3)), (1, box(20, 20, 24, 24))]},
        )
        comps = net_components(model, 1)
        assert len(comps) == 2
        floating = padless_components(model, 1, comps)
        assert len(floating) == 1
        assert floating[0]["F.Cu"].bounds == (20.0, 20.0, 24.0, 24.0)
