"""Via-in-pad gate of the coupled layer-change move (Issue #6847).

The coupled search must not drill a via into an SMD pad when the fab cannot
fill vias (jlcpcb), at the pair's own endpoint pads in particular, where
``_is_via_blocked`` is waived.  (#6847 also covered the Python-only swap-via
move; that move was removed in #6853, so only the plain layer change is
tested here.)
"""

from __future__ import annotations

from kicad_tools.router.diffpair_routing import (
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


def _via_moves_from_smd_pads(manufacturer: str | None) -> tuple[int, int]:
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


def test_control_without_a_manufacturer_the_via_move_may_drill_the_pads():
    vias, rejected = _via_moves_from_smd_pads(None)
    assert vias == 1
    assert rejected == 0


def test_no_via_move_drills_an_smd_pad_when_the_fab_cannot_fill_it():
    vias, rejected = _via_moves_from_smd_pads("jlcpcb")
    assert vias == 0
    assert rejected == 1
