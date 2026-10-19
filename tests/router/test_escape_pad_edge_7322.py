"""Issue #7322: escape-via placement keeps the drill out of the SMD mask dam.

``EscapeRouter._can_place_via`` is the gate every escape via candidate
(staggered fanout, in-pad nudge, lateral fallback) passes through.  It now
applies the #7296 predicate (``via_drill_at_pad_edge``) with
``DesignRules.min_via_pad_edge_gap`` as the floor.
"""

from __future__ import annotations

from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

GAP = 0.25


def _router(gap: float) -> EscapeRouter:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        min_via_pad_edge_gap=gap,
    )
    grid = RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )
    return EscapeRouter(grid, rules, component_holes=())


def _pad(through_hole: bool = False) -> Pad:
    return Pad(
        x=0.0,
        y=0.0,
        width=1.5,
        height=0.3,
        net=5,
        net_name="SIG",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
        through_hole=through_hole,
        drill=0.3 if through_hole else 0.0,
    )


# Drill centre 0.25 mm from the copper edge (0.15 half-height + 0.25): the
# drill edge is 0.10 mm from the copper, inside the 0.25 mm dam.
IN_ZONE = (0.0, 0.40)
CLEAR = (0.0, 0.60)  # 0.45 mm from copper: drill edge 0.30 mm away


def test_candidate_in_the_dam_band_is_rejected():
    er = _router(GAP)
    assert not er._can_place_via(*IN_ZONE, net=1, dam_pads=[_pad()])
    assert er._can_place_via(*CLEAR, net=1, dam_pads=[_pad()])


def test_control_floor_zero_accepts_the_same_candidate():
    assert _router(0.0)._can_place_via(*IN_ZONE, net=1, dam_pads=[_pad()])


def test_dam_applies_to_the_vias_own_net_pads_too():
    # ``dam_pads`` is deliberately unfiltered: a lateral via beside its own
    # pad is flagged by ``via_at_pad_edge`` just the same.
    assert not _router(GAP)._can_place_via(*IN_ZONE, net=5, dam_pads=[_pad()])


def test_through_hole_pads_have_no_dam():
    assert _router(GAP)._can_place_via(*IN_ZONE, net=1, dam_pads=[_pad(through_hole=True)])


def test_drill_inside_copper_is_the_via_in_pad_question():
    assert _router(GAP)._can_place_via(0.0, 0.0, net=5, dam_pads=[_pad()])


def _rotated_pad() -> Pad:
    pad = _pad()
    pad.rotation = 45.0  # residual non-cardinal angle (#4910)
    return pad


def test_rotated_pad_dam_follows_the_rotated_copper():
    # Issue #7467: the long axis of the 45-degree pad points along (1, -1).
    # (0.707, -0.707) is 0.25 mm past its tip: inside the dam, though the
    # unrotated box puts it 0.56 mm away and the old gate accepted it.
    er = _router(GAP)
    assert not er._can_place_via(0.7071, -0.7071, net=1, dam_pads=[_rotated_pad()])
    # (1.0, 0.0) is 0.25 mm from the unrotated box but 0.56 mm from the
    # rotated copper: the old gate rejected a clear site.
    assert er._can_place_via(1.0, 0.0, net=1, dam_pads=[_rotated_pad()])
