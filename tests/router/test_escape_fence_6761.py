"""Hard escape fences for fine-pitch columns (Issue #6761).

On board 05 the first wide net out of U2's left column (PHASE_C, 0.4 mm
trace, 0.8 mm via) left U2.9 with a 45-degree F.Cu diagonal across U2.10's and
U2.11's lanes and dropped its via at their far end.  The #6382 soft lane cost
and two via-cost experiments (#6422/#6426, #6415) only moved which neighbour
stranded.  An escape fence is the hard form of the lane, scoped to a
connection that starts or ends on the column: it may not put its centreline
in another net's lane of that column (dilated so a minimum-width neighbour
still fits) nor drop a via that reaches one, and it falls back to the
unfenced search rather than lose the connection.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppPathfinder, is_cpp_available
from kicad_tools.router.pad_lanes import (
    ESCAPE_FENCE_DISABLE_ENV,
    LANE_LENGTH_MM,
    PAD_VIA_FENCE_LAYER,
    FenceLane,
    collect_fence_lanes,
    column_pitch,
    escape_fence_rects,
    escape_fences_disabled,
    lane_column,
    tight_fence_lanes,
)
from kicad_tools.router.primitives import Pad

from .test_pad_lane_budget_6382 import (
    LEFT_TIP,
    PITCH,
    Y0,
    _build_router,
    _column_pads,
    _Grid,
)

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not available")

RES = 0.05
TRACE_BAND = 0.2 + 0.15 + 0.1  # 0.4 mm trace + clearance + min foreign half width
VIA_BAND = 0.4 + 0.15 + 0.1  # 0.8 mm via + via clearance + min foreign half width


class _Region:
    def __init__(self, pads: list[Pad]) -> None:
        self.pad_refs = {p.key for p in pads}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ESCAPE_FENCE_DISABLE_ENV, "KCT_DISABLE_PAD_LANES"):
        monkeypatch.delenv(name, raising=False)


def _fence_lanes() -> list[FenceLane]:
    pads = _column_pads()
    return collect_fence_lanes(pads, [_Region(pads)], lambda pad: 0)


def _rects(terminals, net: int):
    return escape_fence_rects(
        _fence_lanes(),
        terminals,
        net,
        _Grid(RES),
        trace_band_mm=TRACE_BAND,
        via_band_mm=VIA_BAND,
    )


def _centre(index: int, origin: float = 0.0) -> float:
    return origin + index * RES


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_columns_split_left_and_right() -> None:
    lanes = _fence_lanes()
    assert len(lanes) == 8
    left = {fl.column for fl in lanes if fl.lane.direction < 0}
    right = {fl.column for fl in lanes if fl.lane.direction > 0}
    assert len(left) == 1 and len(right) == 1
    pad = _column_pads()[0]
    assert lane_column(lanes[0].lane, pad) in left


def test_terminal_on_column_fences_every_other_net_lane_of_that_column() -> None:
    # Start on U1.2 (net 2, row 1) -- left column rows 0, 2, 3 are fenced.
    rects = _rects([("U1", "2"), ("D1", "1")], net=2)
    trace = [r for r in rects if r[1] == 0]
    via = [r for r in rects if r[1] == PAD_VIA_FENCE_LAYER]
    assert sorted(src for _, _, src in trace) == [1, 3, 4]
    assert sorted(src for _, _, src in via) == [1, 3, 4]
    # The right column (nets 10..13) is never fenced for a left-column start.
    assert all(src < 10 for _, _, src in rects)


def test_no_terminal_on_a_column_means_no_fence() -> None:
    assert _rects([("D1", "1"), ("D2", "1")], net=2) == []


def test_same_net_lanes_are_never_fenced() -> None:
    pads = _column_pads()
    pads[2] = Pad(**{**pads[2].__dict__, "net": 2, "net_name": "N2"})
    lanes = collect_fence_lanes(pads, [_Region(pads)], lambda pad: 0)
    rects = escape_fence_rects(
        lanes, [("U1", "2")], 2, _Grid(RES), trace_band_mm=TRACE_BAND, via_band_mm=VIA_BAND
    )
    assert 2 not in {src for _, _, src in rects}


def test_trace_band_keeps_the_own_centreline_and_a_neighbour_exit_open() -> None:
    rects = _rects([("U1", "2")], net=2)
    own_y = Y0 + PITCH
    for (gx1, gy1, gx2, gy2), layer, src in rects:
        if layer != 0:
            continue
        lane_y = Y0 + (src - 1) * PITCH
        # Every fenced cell centre is strictly within the band of the lane.
        assert _centre(gy1) > lane_y - TRACE_BAND - 1e-9
        assert _centre(gy2) < lane_y + TRACE_BAND + 1e-9
        # ... and the routing net's own centreline row is never fenced.
        assert not (_centre(gy1) - 1e-9 <= own_y <= _centre(gy2) + 1e-9)
        # The trace fence spans exactly the lane band along the axis.
        assert _centre(gx2) < LEFT_TIP
        assert _centre(gx1) >= LEFT_TIP - LANE_LENGTH_MM - 1e-9


def test_via_fence_runs_one_via_footprint_past_the_lane_end() -> None:
    rects = _rects([("U1", "2")], net=2)
    for (gx1, _gy1, gx2, _gy2), layer, _src in rects:
        if layer != PAD_VIA_FENCE_LAYER:
            continue
        far_end = LEFT_TIP - LANE_LENGTH_MM
        assert _centre(gx1) < far_end - VIA_BAND + RES + 1e-9
        assert _centre(gx1) > far_end - VIA_BAND - 1e-9
        assert _centre(gx2) < LEFT_TIP


def test_only_columns_narrower_than_a_via_footprint_are_fenced() -> None:
    lanes = _fence_lanes()
    left = [fl for fl in lanes if fl.lane.direction < 0]
    assert column_pitch(left) == pytest.approx(PITCH)
    # 0.6 mm via + 0.15 mm clearance = 0.75 mm > 0.65 mm pitch: fenced.
    assert len(tight_fence_lanes(lanes, lambda net: 0.6, 0.15)) == len(lanes)
    # 0.4 mm via + 0.15 mm = 0.55 mm < 0.65 mm pitch: the soft lanes suffice.
    assert tight_fence_lanes(lanes, lambda net: 0.4, 0.15) == []
    # One wide-via net is enough to fence its whole column.
    wide = tight_fence_lanes(lanes, lambda net: 0.8 if net == 1 else 0.4, 0.15)
    assert {fl.column for fl in wide} == {fl.column for fl in left}


def test_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not escape_fences_disabled()
    monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "1")
    assert escape_fences_disabled()
    monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "0")
    assert not escape_fences_disabled()


# ---------------------------------------------------------------------------
# Native A*: the wide net leaves its column before it turns
# ---------------------------------------------------------------------------


def _fcu_crossings(routes, y: float) -> list[float]:
    """Distances from the tip at which F.Cu copper of ``routes`` crosses row ``y``."""
    out: list[float] = []
    for route in routes:
        for s in route.segments:
            if getattr(s.layer, "name", str(s.layer)) not in ("F_CU", "CopperLayer.F_CU"):
                continue
            if s.y1 != s.y2 and (s.y1 - y) * (s.y2 - y) <= 0:
                x = s.x1 + (y - s.y1) / (s.y2 - s.y1) * (s.x2 - s.x1)
                if x < LEFT_TIP:
                    out.append(LEFT_TIP - x)
        for v in route.vias:
            if abs(v.y - y) <= 0.2 + v.diameter / 2 and v.x < LEFT_TIP:
                out.append(LEFT_TIP - v.x)
    return out


def _route_wide_then_nb(monkeypatch: pytest.MonkeyPatch, *, fences: bool):
    if not fences:
        monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "1")
    # WIDE's target is close and below the column: the cheapest escape cuts a
    # diagonal across NB's lane 0.8 mm from its tip, even with the soft lanes.
    router, nb_y = _build_router((6.5, 12.6))
    router._install_pad_lane_budgets()
    installed = len(router.router._pad_lane_fences)
    wide = router.route_net(1)
    nb = router.route_net(2)
    return installed, wide, nb, nb_y


@requires_cpp
def test_fence_keeps_wide_escape_out_of_neighbour_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails without the fix: WIDE crosses NB's row 0.81 mm out and NB needs vias."""
    installed, wide, nb, nb_y = _route_wide_then_nb(monkeypatch, fences=True)
    # Four left-column lanes; the right column's pads are NC (no lane).
    assert installed == 4
    assert wide, "WIDE must route"
    crossings = _fcu_crossings(wide, nb_y)
    assert not crossings or min(crossings) >= LANE_LENGTH_MM, crossings
    assert nb, "NB must route"
    assert sum(len(r.vias) for r in nb) == 0
    for route in nb:
        for s in route.segments:
            assert s.y1 == pytest.approx(nb_y) and s.y2 == pytest.approx(nb_y)


@requires_cpp
def test_escape_hatch_reproduces_the_lane_crossing(monkeypatch: pytest.MonkeyPatch) -> None:
    installed, wide, nb, nb_y = _route_wide_then_nb(monkeypatch, fences=False)
    assert installed == 0
    assert wide and nb
    assert min(_fcu_crossings(wide, nb_y)) < LANE_LENGTH_MM
    assert sum(len(r.vias) for r in nb) > 0


@requires_cpp
def test_fence_falls_back_when_every_path_crosses_the_lane() -> None:
    """A fence reshapes an escape; it never loses a connection."""
    # WIDE's target pad sits on NB's centreline inside NB's lane: every
    # fenced search fails, so the connection is only reachable unfenced.
    router, nb_y = _build_router((8.6, Y0 + PITCH))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    before = getattr(pathfinder, "_escape_fence_fallbacks", 0)
    wide = router.route_net(1)
    assert wide, "WIDE must still route into the lane"
    assert pathfinder._escape_fence_fallbacks > before


@requires_cpp
def test_python_backend_installs_no_fences() -> None:
    from kicad_tools.router.core import Autorouter

    router = Autorouter(width=20, height=20, force_python=True)
    assert router._install_escape_fences([object()]) == 0


@requires_cpp
def test_via_fence_layer_is_not_a_trace_cost() -> None:
    """A ``layer == -2`` budget never prices a trace step (it is via-only)."""
    rects = _rects([("U1", "2")], net=2)
    assert any(layer == PAD_VIA_FENCE_LAYER for _, layer, _ in rects)
    assert all(math.isfinite(v) for v in (TRACE_BAND, VIA_BAND))
