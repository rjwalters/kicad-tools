"""Regression tests for Issue #6389: the pure-Python A* resolves pad clearance
cells with exact segment-vs-pad geometry, as the C++ search has since #6315.

Before the fix ``Router._is_trace_blocked`` treated every cell of a foreign
pad's clearance halo as copper, so a pad's clearance was applied twice (once
by the halo, once by the trace disc).  On the fixture below -- two facing
3-pin columns with a small foreign-net blocker 0.2 mm off U1.2's tip -- that
closed the F.Cu corridor east of the tip, and the Python fallback detoured
west of the start pad, dropped a via and ran on B.Cu under U1, where C++
routes on F.Cu with no via.

The port keeps every off-switch of the native ``pad_cell_resolvable``: a cell
is resolved exactly only when it is an SMD pad's copper / clearance cell that
no drilled pad, stitch-via reservation, registry-less blocker (keep-out,
obstacle, board edge), routed copper, reservation or zone also claims.  The
exact test measures through the validator's own segment-vs-pad primitive, so
it can never accept a gap ``validate_segment_clearance`` rejects.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import pad_segment_edge_clearance
from kicad_tools.router.io import validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment
from kicad_tools.router.rules import DesignRules

RES = 0.065
PITCH = 0.65
PAD_LONG = 1.575
PAD_SHORT = 0.4
CLEARANCE = 0.15
A_X = 3.0
B_X = 9.0
ROW_Y = 5.02
TIP = A_X + PAD_LONG / 2  # east tip of U1.2
BLOCKER_X = TIP + 0.2
BLOCKER_Y = ROW_Y - 0.3
BLOCKER_SIZE = 0.3

#: Grid cells from the issue trace: (63, 80) is world (4.095, 5.200), 0.33 mm
#: below the blocker's bottom edge -- legal for a 0.15 mm trace at 0.15 mm.
LEGAL_CELL = (63, 80)
#: World (4.095, 5.005): 0.135 mm from the blocker's edge, under clearance.
ILLEGAL_CELL = (63, 77)


def _column(x: float, y: float) -> list[dict]:
    return [
        {
            "number": str(i + 1),
            "x": x,
            "y": y + offset,
            "width": PAD_LONG,
            "height": PAD_SHORT,
            "net": 1 if offset == 0 else 0,
            "net_name": "SIG" if offset == 0 else "",
            "layer": Layer.F_CU,
        }
        for i, offset in enumerate((-PITCH, 0.0, PITCH))
    ]


def _blocker(**extra) -> list[dict]:
    pad = {
        "number": "1",
        "x": BLOCKER_X,
        "y": BLOCKER_Y,
        "width": BLOCKER_SIZE,
        "height": BLOCKER_SIZE,
        "net": 2,
        "net_name": "OTHER",
        "layer": Layer.F_CU,
    }
    pad.update(extra)
    return [pad]


def _autorouter(*, force_python: bool, blocker: list[dict] | None = None) -> Autorouter:
    rules = DesignRules(
        trace_width=0.15,
        trace_clearance=CLEARANCE,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=CLEARANCE,
        grid_resolution=RES,
    )
    ar = Autorouter(width=12.0, height=12.0, force_python=force_python, rules=rules)
    ar.add_component("U1", _column(A_X, ROW_Y))
    ar.add_component("U2", _column(B_X, ROW_Y))
    ar.add_component("X1", blocker if blocker is not None else _blocker())
    return ar


def _route(force_python: bool) -> Autorouter:
    ar = _autorouter(force_python=force_python)
    ar.route_net(1)
    return ar


def _segments(ar: Autorouter) -> list[Segment]:
    return [seg for route in ar.routes for seg in route.segments]


def _crossings(ar: Autorouter, x: float) -> list[float]:
    """``y`` of every F.Cu segment crossing the vertical line ``x``."""
    ys = []
    for seg in _segments(ar):
        if seg.layer != Layer.F_CU or not min(seg.x1, seg.x2) <= x <= max(seg.x1, seg.x2):
            continue
        if seg.x1 == seg.x2:
            ys.extend((seg.y1, seg.y2))
        else:
            t = (x - seg.x1) / (seg.x2 - seg.x1)
            ys.append(seg.y1 + t * (seg.y2 - seg.y1))
    return ys


def _blocker_gap(ar: Autorouter) -> float:
    """Smallest edge gap from an F.Cu segment to the F.Cu-only blocker.

    Layer-aware on purpose: a B.Cu run under an SMD pad is legal copper.
    """
    blocker = next(p for p in ar.grid._pads if p.net == 2)
    gaps = [
        pad_segment_edge_clearance(blocker, s.x1, s.y1, s.x2, s.y2, s.width / 2)
        for s in _segments(ar)
        if s.layer == Layer.F_CU
    ]
    return min(gaps, default=float("inf"))


def _python_router(blocker: list[dict] | None = None):
    ar = _autorouter(force_python=True, blocker=blocker)
    return ar.router, ar.grid


def _blocker_halo_cells(grid) -> list[tuple[int, int]]:
    """F.Cu cells owned by the blocker's net that are clearance, not copper."""
    cells = []
    for gy in range(70, 82):
        for gx in range(55, 72):
            if (
                grid._blocked[0, gy, gx]
                and grid._net[0, gy, gx] == 2
                and not grid._pad_blocked[0, gy, gx]
            ):
                cells.append((gx, gy))
    return cells


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_python_fallback_routes_on_one_layer_like_cpp():
    """AC 1: the Python fallback no longer detours through two vias."""
    ar = _route(force_python=True)
    assert len(ar.routes) == 1
    assert sum(len(r.vias) for r in ar.routes) == 0
    assert {s.layer for s in _segments(ar)} == {Layer.F_CU}
    # It leaves the tip off-centre ABOVE the row (+y), as C++ does.
    ys = _crossings(ar, TIP + 0.05)
    assert ys and all(y - ROW_Y > 0.0325 for y in ys)
    assert validate_routes(ar) == []
    assert _blocker_gap(ar) >= CLEARANCE - 1e-3


@pytest.mark.parametrize(
    "force_python",
    [
        pytest.param(True, id="python"),
        pytest.param(
            False,
            id="cpp",
            marks=pytest.mark.skipif(not is_cpp_available(), reason="native build required"),
        ),
    ],
)
def test_backends_agree_on_the_fixture(force_python):
    """AC 2: each backend is legal, via-free, F.Cu-only and lands on one side."""
    ar = _route(force_python=force_python)
    assert len(ar.routes) == 1
    assert validate_routes(ar) == []
    assert sum(len(r.vias) for r in ar.routes) == 0
    assert {s.layer for s in _segments(ar)} == {Layer.F_CU}
    assert _blocker_gap(ar) >= CLEARANCE - 1e-3
    ys = _crossings(ar, TIP + 0.05)
    assert ys and all(y > ROW_Y for y in ys)


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
def test_landing_side_parity():
    """Both backends cross ``x = tip + 0.05`` on the same side of the row."""
    sides = []
    for force_python in (True, False):
        ys = _crossings(_route(force_python), TIP + 0.05)
        assert ys
        sides.append({y > ROW_Y for y in ys})
    assert sides[0] == sides[1] == {True}


# ---------------------------------------------------------------------------
# _is_trace_blocked
# ---------------------------------------------------------------------------


def test_legal_step_past_the_blocker_is_not_blocked():
    """AC 3: the step the issue traced is resolved exactly and admitted."""
    router, grid = _python_router()
    gx, gy = LEGAL_CELL
    # The disc does reach the blocker's clearance halo (the pre-fix verdict).
    assert any(
        (x - gx) ** 2 + (y - gy) ** 2 <= router._trace_half_width_cells**2
        for x, y in _blocker_halo_cells(grid)
    )
    assert not router._is_trace_blocked(gx, gy, 0, 1, from_cell=(gx - 1, gy))


def test_step_under_clearance_is_still_blocked():
    """AC 3: the exact test is not a blanket loosening."""
    router, _grid = _python_router()
    gx, gy = ILLEGAL_CELL
    assert router._is_trace_blocked(gx, gy, 0, 1, from_cell=(gx - 1, gy))
    assert router._is_trace_blocked(gx, gy, 0, 1)


def test_drilled_pad_halo_keeps_the_disc_verdict():
    """AC 4: a drilled pad's cells are never resolved by the copper-only test."""
    router, grid = _python_router(_blocker(through_hole=True, drill=0.2))
    halo = _blocker_halo_cells(grid)
    assert halo
    assert not any(grid.pad_cell_resolvable(x, y, 0) for x, y in halo)
    gx, gy = LEGAL_CELL
    assert router._is_trace_blocked(gx, gy, 0, 1, from_cell=(gx - 1, gy))


def test_routed_usage_keeps_the_disc_verdict():
    """AC 4: a halo cell in use by routed copper is not pad-only."""
    router, grid = _python_router()
    gx, gy = LEGAL_CELL
    for x, y in _blocker_halo_cells(grid):
        grid._usage_count[0, y, x] = 1
    assert router._is_trace_blocked(gx, gy, 0, 1, from_cell=(gx - 1, gy))


def test_registry_less_blocker_keeps_the_disc_verdict():
    """AC 4: a keep-out over the halo makes it unmeasurable."""
    router, grid = _python_router()
    gx, gy = LEGAL_CELL
    for x, y in _blocker_halo_cells(grid):
        grid._mark_raster_only_region(0, x, y, x, y)
    assert router._is_trace_blocked(gx, gy, 0, 1, from_cell=(gx - 1, gy))


def test_stitch_policy_cell_keeps_the_disc_verdict():
    """AC 4: a stitch-via reservation (``_pad_policy_cells``) is not pad geometry."""
    router, grid = _python_router()
    gx, gy = LEGAL_CELL
    for x, y in _blocker_halo_cells(grid):
        grid._pad_policy_cells.add(grid._pad_cell_key(0, y, x))
    assert router._is_trace_blocked(gx, gy, 0, 1, from_cell=(gx - 1, gy))


def test_provenance_void_is_sticky():
    """A cell a non-pad source voided stays voided when an SMD pad later covers it."""
    _router, grid = _python_router()
    x, y = _blocker_halo_cells(grid)[0]
    assert grid.pad_cell_resolvable(x, y, 0)
    grid._void_pad_provenance(0, y, x)
    grid._record_pad_provenance([0], x, y, x, y, pad_source=True)
    assert not grid.pad_cell_resolvable(x, y, 0)


# ---------------------------------------------------------------------------
# Never looser than the validator
# ---------------------------------------------------------------------------


def test_exact_pad_test_never_accepts_what_the_validator_rejects():
    """Randomised: ``pad_step_geometry_clear`` accepting implies the validator does."""
    _router, grid = _python_router()
    rng = random.Random(6389)
    accepted = 0
    for _ in range(3000):
        ax = rng.uniform(3.2, 5.0)
        ay = rng.uniform(4.2, 5.8)
        bx = ax + rng.choice((-1, 0, 1)) * RES
        by = ay + rng.choice((-1, 0, 1)) * RES
        if not grid.pad_step_geometry_clear(ax, ay, bx, by, 0, 1, 0.075, CLEARANCE):
            continue
        accepted += 1
        seg = Segment(ax, ay, bx, by, 0.15, Layer.F_CU, 1, "SIG")
        assert grid.validate_segment_clearance(seg, exclude_net=1)[0], (ax, ay, bx, by)
    assert accepted > 100  # the sample does exercise the accepting branch


def test_kelvin_isolation_voids_and_restores_pad_provenance():
    """Kelvin branch isolation must hide its cells from the exact pad test.

    ``isolate_kelvin_branch`` repaints a same-net terminal's metal as a
    no-net obstacle for one search.  The exact pad test skips own-net pads, so
    a cell that kept its ``PAD`` provenance would be measured against no pad
    and resolved as clear -- letting a sense branch onto the force terminal.
    The native ``pad_cell`` bit is reset for the search; the Python plane must
    be too, and both must come back afterwards.
    """
    import numpy as np

    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.kelvin_obstacles import isolate_kelvin_branch
    from kicad_tools.router.primitives import Pad

    grid = RoutingGrid(6, 6, DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2))
    pads = [
        Pad(
            x=x,
            y=3,
            width=0.6,
            height=0.6,
            net=1,
            net_name="K",
            layer=Layer.F_CU,
            ref="R1",
            pin=str(i + 1),
        )
        for i, x in enumerate((1.5, 3.0, 4.5))
    ]
    for pad in pads:
        grid.add_pad(pad)
    before = grid._pad_provenance.copy()
    net_before = np.asarray(grid._net).copy()
    with isolate_kelvin_branch(grid, pads, pads[0], pads[2]):
        # Cells repainted from the Kelvin net to a no-net obstacle.
        isolated = (net_before == 1) & (np.asarray(grid._net) == 0)
        assert isolated.any()  # the middle terminal's metal is isolated
        assert not (grid._pad_provenance[isolated] == RoutingGrid.PAD_PROVENANCE_PAD).any()
        layer, gy, gx = (int(v[0]) for v in np.nonzero(isolated))
        assert not grid.pad_cell_resolvable(gx, gy, layer)
    np.testing.assert_array_equal(grid._pad_provenance, before)


# ---------------------------------------------------------------------------
# Own-pad step gate (Python port of the native #6375 gate)
# ---------------------------------------------------------------------------

#: U1.2's tip column, one row off the centreline: inside the start pad's
#: metal box, owned by net 1, but a trace end here sits 0.136 mm from X1.
OFF_CENTRE_TIP_CELL = (58, 78)


def test_own_pad_step_gate_rejects_the_off_centre_tip_exit():
    """The step leaving U1.2's tip one row off-centre is held to the validator.

    Under #6368's one-cell landing deadband this exit is free, and before the
    gate the same-net branch admitted it unchecked: the validator then
    rejected every goal built on it and the search drained (0 routes).
    """
    router, grid = _python_router()
    fx, fy = OFF_CENTRE_TIP_CELL
    assert router._own_pad_cell(fx, fy, 0, 1)
    width = router.rules.trace_width
    refs = {"U1", "U2"}
    memo: dict = {}
    assert not router._own_pad_step_clear(fx, fy, fx + 1, fy, 0, 1, width, refs, memo)
    assert not router._own_pad_step_clear(fx, fy, fx + 1, fy + 1, 0, 1, width, refs, memo)
    # Two rows off the centreline the same exit is legal.
    assert router._own_pad_step_clear(fx, fy + 1, fx + 1, fy + 1, 0, 1, width, refs, memo)
    # The gate asks exactly the validator's pad question.
    ax, ay = grid.grid_to_world(fx, fy)
    bx, by = grid.grid_to_world(fx + 1, fy)
    ok, _clr, _loc = grid.validate_segment_clearance(
        Segment(ax, ay, bx, by, width, Layer.F_CU, 1), exclude_net=1, exclude_refs=refs
    )
    assert not ok


def test_python_search_returns_no_validator_rejected_candidate(monkeypatch):
    """No goal the search reaches is thrown away by the post-route validator."""
    ar = _autorouter(force_python=True)
    router = ar.router
    verdicts: list[bool] = []
    original = router._validate_route_clearance

    def _spy(*args, **kwargs):
        ok = original(*args, **kwargs)
        verdicts.append(ok)
        return ok

    monkeypatch.setattr(router, "_validate_route_clearance", _spy)
    ar.route_net(1)
    assert len(ar.routes) == 1
    assert verdicts and all(verdicts)
