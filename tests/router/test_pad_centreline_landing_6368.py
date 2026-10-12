"""Regression tests for Issue #6368: traces leave and land on the pad centreline.

Both A* loops seed every cell of the start pad's (eroded) metal box and accept
any cell of the end pad's box as a goal, and ``reconstruct_path`` draws the
off-grid tail from the pad centre to that first/last cell for free.  An
off-centre row therefore got its row change as a free dogleg inside the pad,
while a centred landing had to pay two turns to change rows outside it, so the
search preferred the off-centre row and the run left the pad tip displaced
towards the neighbour pin (board 05 U2: VCP -0.125 mm, GND_SENSE -0.125 mm,
SENSE_IN +0.070 mm).

The fix charges that skipped jog on the step that crosses the pad's tip line,
in both native loops and the Python fallback.  It is a soft cost: every legal
seed and goal stays legal.

The fixture is a pair of facing 3-pin columns of 1.575 x 0.4 mm pads at a
0.65 mm pitch on a 0.065 mm grid (board 05 U2's left column), with the rows
off-grid and the far column shifted in y so the run must change rows
somewhere between the two pads.  On unfixed ``main`` the change happens in the
free pad tails: every case below lands 0.08-0.185 mm off-centre at one end or
both.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_landing import (
    AXIS_NONE,
    AXIS_X,
    AXIS_Y,
    LandingGeometry,
    landing_cost,
    pad_landing_geometry,
    pad_long_axis,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)

RES = 0.065
PITCH = 0.65
PAD_LONG = 1.575
PAD_SHORT = 0.4
CLEARANCE = 0.15
A_X = 3.0
B_X = 9.0
ROW_Y = 5.02  # off-grid: 77.23 cells
TIP_PROBE = 0.05  # measure 0.05 mm outside the pad tip, as on board 05


def _rules(width: float, *, carveout: bool = False) -> DesignRules:
    return DesignRules(
        trace_width=width,
        trace_clearance=CLEARANCE,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=CLEARANCE,
        grid_resolution=RES,
        # The legacy fine-pitch carve-out exempts the column from seed
        # erosion, so its metal box spans the full 0.4 mm pad height --
        # the original #6315 wide-trace case.
        legacy_fine_pitch_carveout=carveout,
    )


def _column(x: float, y: float) -> list[dict]:
    """NC, SIG, NC -- the signal pad between two net-0 neighbours."""
    return [
        {
            "number": str(i + 1),
            "x": x,
            "y": y + off,
            "width": PAD_LONG,
            "height": PAD_SHORT,
            "net": 1 if off == 0.0 else 0,
            "net_name": "SIG" if off == 0.0 else "",
            "layer": Layer.F_CU,
        }
        for i, off in enumerate((-PITCH, 0.0, PITCH))
    ]


def _route(width: float, dy: float, *, carveout=False, force_python=False, extra=()):
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(
        width=12.0,
        height=12.0,
        rules=_rules(width, carveout=carveout),
        force_python=force_python,
    )
    ar.add_component("U1", _column(A_X, ROW_Y))
    ar.add_component("U2", _column(B_X, ROW_Y + dy))
    for ref, pads in extra:
        ar.add_component(ref, pads)
    expected = "python" if force_python else "cpp"
    if ar.backend_info.get("active") != expected:
        pytest.skip(f"Autorouter did not select the {expected} backend")
    assert ar.route_net(1), "SIG did not route"
    return ar


def _tip_crossings(ar, x: float, pad_y: float) -> list[tuple[float, bool]]:
    """(y - pad_y, straight) wherever SIG copper crosses the vertical line ``x``."""
    out = []
    for route in ar.routes:
        for s in route.segments:
            if s.x1 != s.x2 and min(s.x1, s.x2) <= x <= max(s.x1, s.x2):
                y = s.y1 + (s.y2 - s.y1) * (x - s.x1) / (s.x2 - s.x1)
                out.append((y - pad_y, abs(s.y2 - s.y1) < 1e-6))
    return out


def _on_grid(v: float) -> bool:
    return abs(v / RES - round(v / RES)) < 1e-3


def _cells_on_pad(ar, pad_x: float, pad_y: float) -> list[tuple[float, float]]:
    """Every on-grid SIG vertex on the pad's copper: the first/last grid cells."""
    out = []
    for route in ar.routes:
        for s in route.segments:
            for x, y in ((s.x1, s.y1), (s.x2, s.y2)):
                if (
                    _on_grid(x)
                    and _on_grid(y)
                    and abs(x - pad_x) <= PAD_LONG / 2
                    and abs(y - pad_y) <= PAD_SHORT / 2
                ):
                    out.append((x, y))
    return out


def _assert_centred(ar, dy: float) -> None:
    """The AC, at both pads.

    * Every grid cell the run uses on the pad's copper -- the first cell
      after the pad-centre tail and any before the run leaves the pad -- is
      within one grid cell of the centreline.
    * The copper crosses the pad's tip line within one grid cell of it.
    * A straight approach is still within one grid cell 0.05 mm outside the
      tip (the board 05 measurement; a diagonal departure is not a landing).
    """
    ends = (("start", A_X, ROW_Y, +1), ("end", B_X, ROW_Y + dy, -1))
    for name, pad_x, pad_y, side in ends:
        cells = _cells_on_pad(ar, pad_x, pad_y)
        assert cells, f"{name} pad: no grid cell of the run lies on the pad"
        for x, y in cells:
            assert abs(y - pad_y) <= RES + 1e-6, (
                f"{name} pad: grid cell ({x:.4f}, {y:.4f}) is {y - pad_y:+.4f} mm "
                f"off the centreline (limit +/-{RES} mm)"
            )
        tip = pad_x + side * PAD_LONG / 2
        crossings = _tip_crossings(ar, tip, pad_y)
        assert crossings, f"{name} pad: no SIG copper crosses the tip line"
        for off, _ in crossings:
            assert abs(off) <= RES + 1e-6, (
                f"{name} pad: the run crosses the tip line {off:+.4f} mm off the centreline"
            )
        for off, straight in _tip_crossings(ar, tip + side * TIP_PROBE, pad_y):
            if straight:
                assert abs(off) <= RES + 1e-6, (
                    f"{name} pad: a straight approach runs {off:+.4f} mm off the "
                    f"centreline {TIP_PROBE} mm outside the tip"
                )


# Row shifts between the two columns: up, down, a 2-cell and a ~5-cell shift.
NARROW_CASES = [
    pytest.param(w, dy, id=f"w{w}-dy{dy:+}") for w in (0.15, 0.2) for dy in (0.2, -0.2, 0.13, 0.3)
]


@requires_cpp
@pytest.mark.parametrize(("width", "dy"), NARROW_CASES)
def test_narrow_nets_land_on_the_centreline(width, dy) -> None:
    """AC: 0.15/0.2 mm nets on an eroded fine-pitch box, both ends centred."""
    _assert_centred(_route(width, dy), dy)


@requires_cpp
@pytest.mark.parametrize("dy", [0.2, 0.3, -0.3])
def test_wide_net_on_an_uneroded_pad_lands_on_the_centreline(dy) -> None:
    """AC: a 0.4 mm net on a pad that skips erosion (carve-out eligible).

    Unfixed ``main`` leaves the start pad up to 0.18 mm off-centre here.
    """
    _assert_centred(_route(0.4, dy, carveout=True), dy)


@pytest.mark.parametrize(
    ("width", "dy", "carveout"),
    [
        pytest.param(0.15, 0.2, False, id="w0.15"),
        pytest.param(0.2, -0.2, False, id="w0.2"),
        pytest.param(0.4, 0.3, True, id="w0.4-carveout"),
    ],
)
def test_python_fallback_lands_on_the_centreline(width, dy, carveout) -> None:
    """AC: the pure-Python A* applies the same landing cost."""
    _assert_centred(_route(width, dy, carveout=carveout, force_python=True), dy)


def _blocker() -> list[dict]:
    """A foreign pad just off U1.2's tip, below its centreline.

    Its top edge sits 0.15 mm below the pad centre, so a 0.15 mm run on the
    centre row passes within 0.075 mm of it at the tip -- the only legal
    landings are the rows above the centreline.
    """
    tip = A_X + PAD_LONG / 2
    return [
        {
            "number": "1",
            "x": tip + 0.2,
            "y": ROW_Y - 0.3,
            "width": 0.3,
            "height": 0.3,
            "net": 2,
            "net_name": "OTHER",
            "layer": Layer.F_CU,
        }
    ]


@requires_cpp
def test_never_stricter_off_centre_landing_when_the_centre_is_blocked() -> None:
    """AC: when only an off-centre landing is legal, the route still lands there.

    Native backend only: on this fixture the pure-Python fallback emits a
    self-overlapping route through the blocker's clearance on unfixed
    ``main`` too, which is a separate defect.
    """
    ar = _route(0.15, 0.0, extra=[("X1", _blocker())])
    offsets = [off for off, _ in _tip_crossings(ar, A_X + PAD_LONG / 2 + TIP_PROBE, ROW_Y)]
    assert offsets and all(off > RES / 2 for off in offsets), (
        f"expected an off-centre landing above the blocked centre row, got {offsets}"
    )
    blocker = next(p for p in ar.pads.values() if p.net == 2)
    for route in ar.routes:
        for s in route.segments:
            best = math.inf
            for i in range(201):
                t = i / 200
                px, py = s.x1 + (s.x2 - s.x1) * t, s.y1 + (s.y2 - s.y1) * t
                best = min(
                    best,
                    math.hypot(
                        max(abs(px - blocker.x) - blocker.width / 2, 0.0),
                        max(abs(py - blocker.y) - blocker.height / 2, 0.0),
                    ),
                )
            assert best - s.width / 2 >= CLEARANCE - 1e-3


# --- the cost function itself -------------------------------------------------


def _pad(width: float, height: float, **kw) -> Pad:
    return Pad(x=1.0, y=1.0, width=width, height=height, net=1, net_name="N", **kw)


class _Grid:
    resolution = 0.1
    origin_x = 0.0
    origin_y = 0.0


def test_long_axis_detection() -> None:
    assert pad_long_axis(_pad(1.575, 0.4)) == AXIS_X
    assert pad_long_axis(_pad(0.4, 1.575)) == AXIS_Y
    assert pad_long_axis(_pad(1.0, 1.1)) == AXIS_NONE  # near-square
    assert pad_long_axis(_pad(1.575, 0.4, rotation=45.0)) == AXIS_NONE
    assert pad_long_axis(_pad(1.575, 0.4, rotation=180.0)) == AXIS_X
    assert pad_long_axis(_pad(0.0, 0.0, steiner_point=True)) == AXIS_NONE


def test_landing_geometry() -> None:
    geom = pad_landing_geometry(_pad(1.6, 0.4), _Grid())
    assert geom.long_axis == AXIS_X
    assert geom.centre_gx == pytest.approx(10.0)
    assert geom.centre_gy == pytest.approx(10.0)
    # Copper spans x 0.2..1.8 -> cells 2..18; lane = one short side.
    assert (geom.tip_lo, geom.tip_hi) == (2, 18)
    assert geom.lane_half_cells == pytest.approx(4.0)


# An x-long pad centred at (10, 10.3) in cells; copper cells 2..18 along x,
# a metal box eroded to x 4..16, y 9..11.
GEOM = LandingGeometry(AXIS_X, 10.0, 10.3, 4.0, 2, 18)
BOX = (4, 9, 16, 11)


def _cost(ax, ay, bx, by, outward=True, geom=GEOM):
    return landing_cost(geom, BOX, ax, ay, bx, by, outward, 5.0, 1.0)


def test_landing_cost_charges_off_centre_tip_crossings() -> None:
    # Straight off the tip on the nearest row (0.3 cell) or the next one
    # (0.7 cell): within one grid cell, free.
    assert _cost(18, 10, 19, 10) == 0.0
    assert _cost(18, 11, 19, 11) == 0.0
    # Two rows up (1.7 cells): two turns plus 1.7 straight steps.
    assert _cost(18, 12, 19, 12) == pytest.approx(10.0 + 1.7)
    # A diagonal jog across the tip is charged by its worse cell.
    assert _cost(18, 11, 19, 12) == pytest.approx(11.7)
    # Not a tip crossing: beyond the pad.
    assert _cost(19, 12, 20, 12) == 0.0
    # Direction matters: outward for the start pad, inward for the end pad.
    assert _cost(19, 12, 18, 12, outward=True) == 0.0
    assert _cost(19, 12, 18, 12, outward=False) == pytest.approx(11.7)
    # Crossing the tip line beside the pad (a neighbour's lane) is free.
    assert _cost(18, 20, 19, 20) == 0.0
    # No long axis, no cost.
    assert _cost(18, 12, 19, 12, geom=GEOM._replace(long_axis=AXIS_NONE)) == 0.0


def test_landing_cost_charges_off_centre_box_crossings() -> None:
    # Leaving the eroded box along the long axis, still on the copper: a
    # diagonal from the next row (0.7 cell) to two rows up is charged...
    assert _cost(16, 11, 17, 12) == pytest.approx(11.7)
    # ...a straight step on a centred row is not...
    assert _cost(16, 10, 17, 10) == 0.0
    # ...and neither is a pure side step out of the box.
    assert _cost(10, 11, 10, 12) == 0.0
    # Reaching the end pad's box diagonally across the centreline.
    assert _cost(17, 8, 16, 9, outward=False) == pytest.approx(10.0 + 2.3)


def test_landing_cost_y_long_axis() -> None:
    geom = LandingGeometry(AXIS_Y, 10.3, 10.0, 4.0, 2, 18)
    box = (9, 4, 11, 16)
    assert landing_cost(geom, box, 10, 18, 10, 19, True, 5.0, 1.0) == 0.0
    assert landing_cost(geom, box, 12, 18, 12, 19, True, 5.0, 1.0) == pytest.approx(11.7)


@requires_cpp
def test_native_pad_bounds_carry_the_landing_fields() -> None:
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import LayerStack

    rules = _rules(0.2)
    grid = RoutingGrid(width=12.0, height=12.0, rules=rules, layer_stack=LayerStack.two_layer())
    pathfinder = CppPathfinder(CppGrid.from_routing_grid(grid), rules, diagonal_routing=True)
    pad = Pad(x=3.0, y=ROW_Y, width=PAD_LONG, height=PAD_SHORT, net=1, net_name="SIG")
    bounds = pathfinder._compute_pad_bounds(pad, 0.2)
    expected = pad_landing_geometry(pad, grid)
    assert bounds.long_axis == AXIS_X
    assert bounds.centre_gy == pytest.approx(expected.centre_gy, abs=1e-4)
    assert bounds.centre_gx == pytest.approx(expected.centre_gx, abs=1e-4)
    assert bounds.lane_half_cells == pytest.approx(expected.lane_half_cells, abs=1e-4)
    assert (bounds.tip_lo, bounds.tip_hi) == (expected.tip_lo, expected.tip_hi)
