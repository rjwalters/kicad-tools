"""Via-footprint charging of per-pad budgets (Issue #6415).

The grid router charged the #3143 channel budgets and the #6382 pad lanes at
a via's CENTRE cell only.  On board 05 PHASE_C's 0.8 mm via sat 0.06 mm past
U2.11's lane end: it paid nothing, yet its copper sealed the VM pour link at
U2.11.  Via expansions now charge every budget the via disc (radius incl.
clearance) touches, once, scaled by the bbox cells under the disc and capped
at the bbox's long side in cells.

The footprint charge is opt-in (``KCT_PAD_BUDGET_VIA_SCALE`` > 0; default 0
keeps the centre-cell charge): on board 05 it moves PHASE_C's via from
U2.11's lane end onto U2.10's tip, which the pad-access guard refuses.  These
tests therefore enable it explicitly, and pin that the default is inert.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import (
    PAD_BUDGET_VIA_SCALE_ENV,
    CppGrid,
    CppPathfinder,
    is_cpp_available,
    pad_budget_via_scale,
    router_cpp,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not available")

RES = 0.1
PENALTY = 0.5
# 0.6 mm via + 0.15 mm clearance at 0.1 mm -> ceil(4.5) = 5 cells, the same
# formula ``cpp_backend`` uses for ``via_radius_cells``.
VIA_R = math.ceil((0.6 / 2 + 0.15) / RES)


@pytest.fixture(autouse=True)
def _enable_footprint_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAD_BUDGET_VIA_SCALE_ENV, "1.0")


def _pathfinder() -> tuple[CppPathfinder, RoutingGrid]:
    rules = DesignRules(
        trace_width=0.15,
        trace_clearance=0.1,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.15,
        grid_resolution=RES,
    )
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules, layer_stack=LayerStack.two_layer())
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    pf.set_routable_layers(cpp_grid.get_routable_indices())
    return pf, grid


def _budget(gx1: int, gy1: int, gx2: int, gy2: int, *, net: int = 7, layer: int = -1):
    b = router_cpp.PadChannelBudget()
    b.gx1, b.gy1, b.gx2, b.gy2 = gx1, gy1, gx2, gy2
    b.layer = layer
    b.overflow_penalty = PENALTY
    b.source_net = net
    return b


# A 31 x 7-cell lane (3.1 mm x 0.7 mm), the long side along x.
LANE = (20, 51, 50, 57)
LANE_LONG = 31


def _cost(pf: CppPathfinder, budgets, x: int, y: int, r: int = VIA_R) -> float:
    return pf._impl.pad_budget_via_cost_at(budgets, r, x, y)


# ---------------------------------------------------------------------------
# Cost shape (test hook)
# ---------------------------------------------------------------------------


@requires_cpp
def test_via_centre_outside_lane_but_disc_inside_now_pays() -> None:
    """The board-05 case: centre just past the lane end, disc across it.

    The old centre-cell charge is 0 there; the footprint charge is not.
    """
    pf, _ = _pathfinder()
    lane = [_budget(*LANE)]
    # Centre 2 cells past the lane end (x = 52 > 50), on the lane's row.
    assert _cost(pf, lane, 52, 54, r=0) == 0.0  # what the centre cell saw
    assert _cost(pf, lane, 52, 54) > 0.0


@requires_cpp
def test_grazing_via_costs_less_than_sealing_via() -> None:
    pf, _ = _pathfinder()
    lane = [_budget(*LANE)]
    cap = PENALTY * LANE_LONG
    sealing = _cost(pf, lane, 35, 54)  # centred in the lane
    # Disc just clipping the lane's top-left corner (20, 51): (17, 47) is
    # sqrt(9 + 16) = 5 cells from it.
    grazing = _cost(pf, lane, 17, 47)
    assert 0.0 < grazing < sealing <= cap
    assert grazing == pytest.approx(PENALTY * 1)
    assert sealing == pytest.approx(cap)  # 11 x 7 = 77 cells, capped at 31
    # One cell further out the disc misses the corner entirely.
    assert _cost(pf, lane, 16, 47) == 0.0


@requires_cpp
def test_each_budget_charges_once_and_overlaps_sum() -> None:
    pf, _ = _pathfinder()
    a = _budget(*LANE)
    b = _budget(*LANE, net=8)
    single = _cost(pf, [a], 35, 54)
    assert _cost(pf, [a, b], 35, 54) == pytest.approx(2 * single)


@requires_cpp
def test_zero_radius_reduces_to_centre_cell_charge() -> None:
    pf, _ = _pathfinder()
    lane = [_budget(*LANE)]
    assert _cost(pf, lane, 35, 54, r=0) == pytest.approx(PENALTY)
    assert _cost(pf, lane, 51, 54, r=0) == 0.0


@requires_cpp
def test_layer_specific_budget_charges_every_through_via() -> None:
    """A #3143 budget on one layer charges the via regardless of which layer
    it lands on (a through via blocks the channel on every layer)."""
    pf, _ = _pathfinder()
    for layer in (0, 1):
        assert _cost(pf, [_budget(*LANE, layer=layer)], 35, 54) > 0.0
    # An out-of-range layer stays inert, like the per-cell trace map.
    assert _cost(pf, [_budget(*LANE, layer=9)], 35, 54) == 0.0


@requires_cpp
@pytest.mark.parametrize(
    ("raw", "factor"), [("2.0", 2.0), ("0", 0.0), (None, 0.0), ("bogus", 0.0), ("-1", 0.0)]
)
def test_scale_knob(monkeypatch: pytest.MonkeyPatch, raw: str | None, factor: float) -> None:
    base_pf, _ = _pathfinder()  # scale 1.0
    base = _cost(base_pf, [_budget(*LANE)], 35, 54)
    assert base > 0.0
    if raw is None:
        monkeypatch.delenv(PAD_BUDGET_VIA_SCALE_ENV)
    else:
        monkeypatch.setenv(PAD_BUDGET_VIA_SCALE_ENV, raw)
    assert pad_budget_via_scale() == factor
    pf, _ = _pathfinder()
    assert _cost(pf, [_budget(*LANE)], 35, 54) == pytest.approx(base * factor)


# ---------------------------------------------------------------------------
# Search behaviour
# ---------------------------------------------------------------------------

# Start on F.Cu, end on B.Cu: the route must change layer somewhere on the
# straight run y = 5.0 (gy 50).  The lane sits just above the run (gy 51-54)
# over the whole board width, so every on-line via centre is OUTSIDE it but
# its 5-cell disc reaches into it.
RUN_LANE = (0, 51, 99, 54)


def _route(pf: CppPathfinder, budgets):
    result = pf._impl.route_resumable(
        2.0,
        5.0,
        0,
        8.0,
        5.0,
        1,
        1,
        via_radius_cells=VIA_R,
        pad_channel_budgets=budgets,
    )
    pf._impl.clear_search_state()
    return result


def _via_cell(grid: RoutingGrid, via) -> tuple[int, int]:
    return grid.world_to_grid(via.x, via.y)


@requires_cpp
def test_route_moves_via_off_foreign_lane() -> None:
    """Fails on main: there the on-line via (centre outside the lane, disc
    across it) paid nothing, so the search kept it on the run.  Now it pays
    the capped footprint charge and the via steps off the run until its disc
    at most grazes the lane."""
    pf, grid = _pathfinder()
    lane = [_budget(*RUN_LANE)]
    baseline = _route(pf, [])
    assert baseline.success and baseline.vias
    base_cells = [_via_cell(grid, v) for v in baseline.vias]
    # The unbudgeted via sits on the run, centre outside the lane ...
    assert all(gy < RUN_LANE[1] for _, gy in base_cells)
    # ... where the footprint charge is large but the centre cell is free.
    assert all(_cost(pf, lane, gx, gy, r=0) == 0.0 for gx, gy in base_cells)
    assert all(_cost(pf, lane, gx, gy) >= 5 * PENALTY for gx, gy in base_cells)

    result = _route(pf, lane)
    assert result.success and result.vias
    for gx, gy in (_via_cell(grid, v) for v in result.vias):
        assert gy < 50, (gx, gy)  # moved off the run, away from the lane
        assert _cost(pf, lane, gx, gy) <= PENALTY, (gx, gy)


@requires_cpp
def test_default_keeps_centre_cell_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Knob unset: the via keeps main's on-run site (centre outside the lane
    pays nothing), i.e. the pre-#6415 search is unchanged."""
    monkeypatch.delenv(PAD_BUDGET_VIA_SCALE_ENV)
    pf, grid = _pathfinder()
    baseline = _route(pf, [])
    result = _route(pf, [_budget(*RUN_LANE)])
    assert [_via_cell(grid, v) for v in result.vias] == [_via_cell(grid, v) for v in baseline.vias]
    assert all(gy == 50 for _, gy in (_via_cell(grid, v) for v in result.vias))


@requires_cpp
def test_still_routes_when_every_via_site_is_in_a_lane() -> None:
    """Soft: a lane over the whole board cannot stop the layer change."""
    pf, _ = _pathfinder()
    everywhere = _budget(0, 0, 99, 99)
    result = _route(pf, [everywhere])
    assert result.success and result.vias


@requires_cpp
def test_own_lane_never_charges_its_via() -> None:
    """The own-net filter runs before the native call, so a net's own lane
    contributes nothing to its via cost, while the same lane charges a
    foreign net's via."""
    pf, grid = _pathfinder()
    own = _budget(*RUN_LANE, net=1)
    pf.set_pad_lane_budgets([own])

    assert pf._filter_pad_channel_budgets_for_net(1) == []
    assert _cost(pf, pf._filter_pad_channel_budgets_for_net(1), 35, 50) == 0.0
    assert _cost(pf, pf._filter_pad_channel_budgets_for_net(2), 35, 50) > 0.0

    result = _route(pf, pf._filter_pad_channel_budgets_for_net(1))
    baseline = _route(pf, [])
    assert [(v.x, v.y) for v in result.vias] == [(v.x, v.y) for v in baseline.vias]
