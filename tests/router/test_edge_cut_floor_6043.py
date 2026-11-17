"""Copper must never cross an Edge.Cuts cut, even with no edge clearance (#6043).

Before #6043 ``load_pcb_for_routing`` installed the board-edge keepout only
when the resolved edge clearance was positive.  With no manufacturer
(``edge_clearance=None``) or an explicit ``edge_clearance=0`` nothing was
blocked, and a net whose straight line crossed an interior board window was
routed straight through it.

The fix gives the grid a *physical floor* in those cases -- the widest copper
half-extent plus a half-diagonal-cell quantisation margin -- stored apart
from ``_edge_clearance``.  These tests use synthetic boards only and assert a
geometric invariant (copper never overlaps an outline segment), not a sampled
one.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.router import io as io_module
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.edge_keepout import (
    compute_edge_keepout_floor,
    edge_copper_half_extent,
    edge_keepout_radius,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import load_pcb_for_routing, validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

BACKENDS = [
    pytest.param(True, id="python"),
    pytest.param(
        False,
        id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built"),
    ),
]

CLEARANCES = [
    pytest.param(None, id="ec-none"),
    pytest.param(0.0, id="ec-0"),
    pytest.param(0.3, id="ec-0.3"),
]

RESOLUTIONS = [pytest.param(0.1, id="res-0.1"), pytest.param(0.5, id="res-0.5")]

# 40x30 mm board, window centred on (20, 15); the net's straight line is y=15.
BOARD_W, BOARD_H = 40.0, 30.0

WINDOWS = {
    "rect": """  (gr_rect (start 15 10) (end 25 20)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
""",
    # Two diagonal edges cross the net's straight line.
    "triangle": """  (gr_poly (pts (xy 15 20) (xy 25 20) (xy 20 10))
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
""",
    # A chord chain with a certified ``max_error_mm > 0``.
    "circle": """  (gr_circle (center 20 15) (end 25 15)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
""",
}

# Where the net's straight line (y=15) first meets each window's cut.
ON_CUT = {"rect": (15.0, 15.0), "triangle": (17.5, 15.0), "circle": (15.0, 15.0)}


def _pad(x: float, y: float, layer: str = "F") -> str:
    return f"""  (footprint "TestPad"
    (layer "{layer}.Cu")
    (uuid "00000000-0000-0000-0000-{int(x * 1000):012d}")
    (at {x} {y})
    (property "Reference" "TP{int(x)}" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1)
      (layers "{layer}.Cu" "{layer}.Paste" "{layer}.Mask") (net 1 "SIG"))
  )
"""


def _board(window: str, pads: str | None = None) -> str:
    if pads is None:
        pads = _pad(10.0, 15.0) + _pad(30.0, 15.0)
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (gr_rect (start 0 0) (end {BOARD_W} {BOARD_H})
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{WINDOWS[window]}{pads})
"""


def _rules(resolution: float) -> DesignRules:
    return DesignRules(
        grid_resolution=resolution,
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
    )


def _load(tmp_path: Path, text: str, *, force_python: bool, ec, resolution: float = 0.1):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text)
    router, _net_map = load_pcb_for_routing(
        str(pcb),
        rules=_rules(resolution),
        use_pcb_rules=False,
        validate_drc=False,
        force_python=force_python,
        edge_clearance=ec,
    )
    return router


def _route(router) -> list[Route]:
    router.route_all(suppress_no_timeout_warning=True)
    return list(router.routes)


# --- exact geometry (independent of the router's own helpers) ---------------


def _pt_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _cross(ox, oy, ax, ay, bx, by) -> float:
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def _seg_seg(a, b, c, d) -> float:
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = a, b, c, d
    d1, d2 = _cross(cx, cy, dx, dy, ax, ay), _cross(cx, cy, dx, dy, bx, by)
    d3, d4 = _cross(ax, ay, bx, by, cx, cy), _cross(ax, ay, bx, by, dx, dy)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0  # proper crossing
    return min(
        _pt_seg(ax, ay, cx, cy, dx, dy),
        _pt_seg(bx, by, cx, cy, dx, dy),
        _pt_seg(cx, cy, ax, ay, bx, by),
        _pt_seg(dx, dy, ax, ay, bx, by),
    )


def _assert_no_copper_on_cuts(routes: list[Route], edges) -> None:
    for route in routes:
        for seg in route.segments:
            for c, d in edges:
                gap = _seg_seg((seg.x1, seg.y1), (seg.x2, seg.y2), c, d) - seg.width / 2
                assert gap >= -1e-6, (
                    f"trace ({seg.x1:.3f},{seg.y1:.3f})->({seg.x2:.3f},{seg.y2:.3f}) "
                    f"w={seg.width} overlaps Edge.Cuts {c}->{d} by {-gap:.4f} mm"
                )
        for via in route.vias:
            for (cx, cy), (dx, dy) in edges:
                gap = _pt_seg(via.x, via.y, cx, cy, dx, dy) - via.diameter / 2
                assert gap >= -1e-6, (
                    f"via ({via.x:.3f},{via.y:.3f}) d={via.diameter} overlaps "
                    f"Edge.Cuts ({cx},{cy})->({dx},{dy}) by {-gap:.4f} mm"
                )


def _routed(routes: list[Route]) -> bool:
    return any(route.segments for route in routes)


# --- the 36-case matrix -----------------------------------------------------


@pytest.mark.parametrize("resolution", RESOLUTIONS)
@pytest.mark.parametrize("window", sorted(WINDOWS))
@pytest.mark.parametrize("ec", CLEARANCES)
@pytest.mark.parametrize("force_python", BACKENDS)
def test_route_never_crosses_an_edge_cut(tmp_path, force_python, ec, window, resolution):
    router = _load(
        tmp_path, _board(window), force_python=force_python, ec=ec, resolution=resolution
    )
    edges = router._edge_segments
    assert edges, "the loader must extract Edge.Cuts"

    if ec is None or ec == 0:
        # The floor is stored apart from the copper-to-edge clearance.
        assert router._edge_clearance is None
        expected = 0.6 / 2 + resolution * math.sqrt(2) / 2  # via 0.6 is the widest
        assert router._edge_keepout_floor == pytest.approx(expected)
        assert edge_keepout_radius(router) == pytest.approx(expected)
    else:
        assert router._edge_clearance == ec
        assert edge_keepout_radius(router) == ec

    # The keepout must reach the native grid too, not just Python cells.
    if not force_python:
        cpp_grid = router.grid._cpp_grid
        assert cpp_grid is not None, "native backend must be attached"
        # A point on the window's cut where the net's straight line meets it.
        gx, gy = router.grid.world_to_grid(*ON_CUT[window])
        for layer in router.grid.get_routable_indices():
            assert cpp_grid.is_blocked(gx, gy, layer)

    routes = _route(router)
    assert _routed(routes), "the single net must still route around the window"
    _assert_no_copper_on_cuts(routes, edges)


@pytest.mark.parametrize("resolution", RESOLUTIONS)
@pytest.mark.parametrize("window", sorted(WINDOWS))
def test_positive_clearance_mask_is_unchanged(tmp_path, monkeypatch, window, resolution):
    """``edge_clearance=0.3`` blocks exactly what a direct call blocks."""
    calls: list[float] = []
    original = RoutingGrid.add_edge_keepout

    def spy(grid, segments, clearance):
        calls.append(clearance)
        return original(grid, segments, clearance)

    monkeypatch.setattr(RoutingGrid, "add_edge_keepout", spy)
    text = _board(window)
    loaded = _load(tmp_path, text, force_python=True, ec=0.3, resolution=resolution)
    assert calls == [0.3]
    loaded_mask = np.asarray(loaded.grid._blocked).copy()

    # Same board with the loader's keepout disabled, then the direct call.
    monkeypatch.setattr(io_module, "install_edge_keepout", lambda router, grid: 0)
    calls.clear()
    bare = _load(tmp_path, text, force_python=True, ec=0.3, resolution=resolution)
    assert calls == []
    bare.grid.add_edge_keepout(bare._edge_segments, 0.3)
    assert np.array_equal(loaded_mask, np.asarray(bare.grid._blocked))


# --- via, wide trace --------------------------------------------------------


@pytest.mark.parametrize("ec", [pytest.param(None, id="ec-none"), pytest.param(0.0, id="ec-0")])
@pytest.mark.parametrize("force_python", BACKENDS)
def test_via_copper_stays_off_the_cut(tmp_path, force_python, ec):
    """Pads on opposite layers force a via; its barrel must clear the cut."""
    pads = _pad(13.0, 15.0, "F") + _pad(27.0, 15.0, "B")
    router = _load(tmp_path, _board("rect", pads), force_python=force_python, ec=ec)
    routes = _route(router)
    assert _routed(routes)
    assert any(route.vias for route in routes), "a layer change needs a via"
    _assert_no_copper_on_cuts(routes, router._edge_segments)


@pytest.mark.parametrize("force_python", BACKENDS)
def test_floor_scales_with_a_wide_net_class(tmp_path, force_python):
    """A 0.6 mm-wide class widens the floor, even if applied after load."""
    router = _load(tmp_path, _board("rect"), force_python=force_python, ec=None)
    narrow_floor = router._edge_keepout_floor
    narrow_blocked = int(np.asarray(router.grid._blocked).sum())

    router.net_class_map["SIG"] = NetClassRouting(
        name="Wide", trace_width=0.6, clearance=0.2, via_size=1.2
    )
    assert edge_copper_half_extent(router) == pytest.approx(0.6)
    routes = _route(router)

    assert router._edge_keepout_floor == pytest.approx(compute_edge_keepout_floor(router))
    assert router._edge_keepout_floor > narrow_floor
    assert int(np.asarray(router.grid._blocked).sum()) > narrow_blocked
    assert _routed(routes)
    assert any(seg.width >= 0.6 - 1e-9 for route in routes for seg in route.segments)
    _assert_no_copper_on_cuts(routes, router._edge_segments)


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize("ec", [pytest.param(None, id="ec-none"), pytest.param(0.0, id="ec-0")])
def test_validate_routes_reports_a_crossing_without_clearance(tmp_path, ec):
    router = _load(tmp_path, _board("rect"), force_python=True, ec=ec)
    crossing = Segment(10.0, 15.0, 30.0, 15.0, 0.2, Layer.F_CU, 1)
    router.routes = [Route(net=1, net_name="SIG", segments=[crossing])]

    edge = [v for v in validate_routes(router) if v.obstacle_type == "edge"]
    assert edge, "copper across the window must be reported"
    assert all(v.required == 0.0 for v in edge)
    assert all(v.distance < 0 for v in edge)


def test_validate_routes_quiet_for_clear_copper_without_clearance(tmp_path):
    """``required = 0`` reports overlap only -- no phantom floor violations."""
    router = _load(tmp_path, _board("rect"), force_python=True, ec=None)
    # 0.25 mm from the window's left edge: inside the grid floor, but clear.
    clear = Segment(14.65, 12.0, 14.65, 18.0, 0.2, Layer.F_CU, 1)
    router.routes = [Route(net=1, net_name="SIG", segments=[clear])]
    assert not [v for v in validate_routes(router) if v.obstacle_type == "edge"]
