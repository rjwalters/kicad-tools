"""The polarity-swap via move of the coupled search (Issue #6847).

Follow-ups to #6840 on the Python-only ``allow_swap_via`` move, which drills
the same two cells as the plain coupled layer change:

1. It skipped the fab's via-in-pad gate, so with a manufacturer that cannot
   fill vias (jlcpcb) a swap via could still land in an SMD pad -- at the
   pair's own endpoint pads in particular, where ``_is_via_blocked`` is
   waived.
2. The search's path-history walk filed each rail's via at the CHILD state's
   cell.  The swap exchanges the rails' cells, so P's via was recorded at N's
   drill and vice versa, while ``_build_route_from_path`` drills each via at
   the rail's own pre-move cell.  The per-rail partner-via guards (#6840)
   then measured each rail against its own barrel.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.diffpair_routing import (
    CoupledNode,
    CoupledPathfinder,
    CoupledState,
    GridPos,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _rules(manufacturer: str | None) -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.25,
        manufacturer=manufacturer,
    )


# ---------------------------------------------------------------------------
# 1. Via-in-pad
# ---------------------------------------------------------------------------


def _via_moves_from_smd_pads(manufacturer: str | None, swap: bool) -> tuple[int, int]:
    """(via moves, ``via_in_pad`` rejections) from a state on two SMD pads.

    The state sits on the pair's own start pads, as at the start of a search:
    ``_is_via_blocked`` is waived there, so only the via-in-pad gate can stop
    a drill into the pad.
    """
    rules = _rules(manufacturer)
    grid = RoutingGrid(width=6.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    p_xy, n_xy = (3.0, 3.0), (3.0, 3.8)
    for (x, y), net, name in ((p_xy, 1, "D+"), (n_xy, 2, "D-")):
        grid.add_pad(Pad(x=x, y=y, width=0.3, height=0.6, net=net, net_name=name, layer=Layer.F_CU))
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=8,
        min_spacing_cells=6,
        allow_swap_via=swap,
    )
    p = GridPos(*grid.world_to_grid(*p_xy), 0)
    n = GridPos(*grid.world_to_grid(*n_xy), 0)
    moves = pf._get_coupled_neighbors(
        CoupledState(p, n, (1, 0)),
        p_net=1,
        n_net=2,
        p_start=p,
        n_start=n,
        target_spacing_cells=8,
    )
    vias = sum(1 for _state, _cost, is_via in moves if is_via)
    return vias, pf.last_rejections["via_in_pad"]


@pytest.mark.parametrize("swap", [False, True], ids=["plain", "swap-via"])
def test_control_without_a_manufacturer_both_via_moves_may_drill_the_pads(swap):
    vias, rejected = _via_moves_from_smd_pads(None, swap)
    assert vias == (2 if swap else 1)
    assert rejected == 0


@pytest.mark.parametrize("swap", [False, True], ids=["plain", "swap-via"])
def test_no_via_move_drills_an_smd_pad_when_the_fab_cannot_fill_it(swap):
    """On main the swap move still drilled both pads under jlcpcb rules."""
    vias, rejected = _via_moves_from_smd_pads("jlcpcb", swap)
    assert vias == 0
    # One rejection per move kind per layer (the gate is shared, evaluated
    # once, and counted each time a move is refused).
    assert rejected == (2 if swap else 1)


# ---------------------------------------------------------------------------
# 2. Per-rail via bookkeeping after a swap
# ---------------------------------------------------------------------------


def test_swap_records_each_rails_via_at_its_own_drill(monkeypatch):
    """After a swap, P's recorded via is P's own pre-move cell, not N's.

    Drive the real search: from the start state keep only the swap move
    (the one whose rails exchange cells), then capture the per-rail via
    lists the path-history walk hands to the next expansion.
    """
    rules = _rules(None)
    grid = RoutingGrid(width=6.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    pads = (
        Pad(x=1.0, y=3.0, width=0.2, height=0.2, net=1, net_name="D+", layer=Layer.F_CU),
        Pad(x=5.0, y=3.8, width=0.2, height=0.2, net=1, net_name="D+", layer=Layer.F_CU),
        Pad(x=1.0, y=3.8, width=0.2, height=0.2, net=2, net_name="D-", layer=Layer.F_CU),
        Pad(x=5.0, y=3.0, width=0.2, height=0.2, net=2, net_name="D-", layer=Layer.F_CU),
    )
    for pad in pads:
        grid.add_pad(pad)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=8,
        min_spacing_cells=6,
        allow_swap_via=True,
    )
    p_start = GridPos(*grid.world_to_grid(1.0, 3.0), 0)
    n_start = GridPos(*grid.world_to_grid(1.0, 3.8), 0)

    real = CoupledPathfinder._get_coupled_neighbors
    seen: list[tuple[CoupledState, list, list, list]] = []

    def spy(self, state, *args, **kwargs):
        if (state.p_pos.x, state.p_pos.y) == (p_start.x, p_start.y):
            moves = real(self, state, *args, **kwargs)
            swaps = [
                m
                for m in moves
                if m[2] and (m[0].p_pos.x, m[0].p_pos.y) == (state.n_pos.x, state.n_pos.y)
            ]
            assert swaps, "the fixture must offer a swap move at the start"
            return swaps[:1]
        seen.append(
            (state, list(kwargs["p_vias"]), list(kwargs["n_vias"]), list(kwargs["own_vias"]))
        )
        return []

    monkeypatch.setattr(CoupledPathfinder, "_get_coupled_neighbors", spy)
    assert pf.route_coupled(*pads, max_iterations_budget=50) is None

    [(state, p_vias, n_vias, own_vias)] = seen
    # The swapped state: P now continues from N's start cell and vice versa.
    assert (state.p_pos.x, state.p_pos.y) == (n_start.x, n_start.y)
    assert (state.n_pos.x, state.n_pos.y) == (p_start.x, p_start.y)
    # Each rail's via is where that rail drilled it.
    assert p_vias == [(p_start.x, p_start.y)]
    assert n_vias == [(n_start.x, n_start.y)]
    assert sorted(own_vias) == sorted([(p_start.x, p_start.y), (n_start.x, n_start.y)])


def test_reconstructed_swap_vias_sit_at_each_rails_own_cell():
    """The cell the walk records is the cell ``_build_route_from_path`` drills."""
    rules = _rules(None)
    grid = RoutingGrid(width=6.0, height=6.0, rules=rules, layer_stack=LayerStack.two_layer())
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=8, allow_swap_via=True)

    p0, n0 = GridPos(10, 30, 0), GridPos(10, 38, 0)
    start = CoupledNode(f_score=0.0, g_score=0.0, state=CoupledState(p0, n0, (1, 0)))
    swapped = CoupledNode(
        f_score=0.0,
        g_score=0.0,
        state=CoupledState(GridPos(10, 38, 1), GridPos(10, 30, 1), (0, 0)),
        parent=start,
        via_from_parent=True,
    )
    p_pad = Pad(x=1.0, y=3.0, width=0.2, height=0.2, net=1, net_name="D+", layer=Layer.F_CU)
    n_pad = Pad(x=1.0, y=3.8, width=0.2, height=0.2, net=2, net_name="D-", layer=Layer.F_CU)
    p_route, n_route = pf._reconstruct_coupled_routes(swapped, p_pad, p_pad, n_pad, n_pad)
    assert [grid.world_to_grid(v.x, v.y) for v in p_route.vias] == [(p0.x, p0.y)]
    assert [grid.world_to_grid(v.x, v.y) for v in n_route.vias] == [(n0.x, n0.y)]
