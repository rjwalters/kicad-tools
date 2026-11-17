"""Issue #6646: a micro via spans stack-adjacent copper layers only.

The in-pad micro-via producers (``EscapeRouter._try_in_pad_escape`` and
``SubGridRouter._try_in_pad_via_rescue``) fall back to the opposite outer
layer when the stack has no inner SIGNAL layer.  On a 4-layer board with
plane inners that yields an F.Cu-B.Cu *micro* via, which no fab can drill.
The producers must decline instead.
"""

from __future__ import annotations

from kicad_tools.router.escape import EscapeDirection, EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.subgrid import SubGridPad, SubGridRouter
from tests.fixtures.strict_in_pad_min import (
    CLEARANCE,
    make_package,
    make_rules,
)


def _grid(stack: LayerStack | None, rules) -> RoutingGrid:
    return RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        origin_x=-5.0,
        origin_y=-5.0,
        layer_stack=stack,
    )


def _escape(stack: LayerStack | None):
    rules = make_rules(manufacturer="jlcpcb-tier1")
    router = EscapeRouter(_grid(stack, rules), rules, component_holes=())
    router._via_in_pad_process = None
    router.micro_via_in_pad_fallback = True
    package = make_package()
    return router._try_in_pad_escape(
        pad=package.pads[0],
        direction=EscapeDirection.SOUTH,
        effective_clearance=CLEARANCE,
        escape_width=0.2,
        package=package,
        skip_on_clearance_violation=True,
    )


def _rescue(stack: LayerStack | None):
    rules = make_rules(manufacturer="jlcpcb-tier1")
    grid = _grid(stack, rules)
    # Pad too short for the standard via -> micro-via branch.
    pad = Pad(
        x=0.0,
        y=0.0,
        width=0.4,
        height=0.3,
        net=1,
        net_name="N1",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    gx, gy = grid.world_to_grid(0.0, 0.0)
    sgp = SubGridPad(
        pad=pad, grid_x=gx, grid_y=gy, offset_x=0.0, offset_y=0.0, snap_x=0.0, snap_y=0.0
    )
    return SubGridRouter(grid, rules)._try_in_pad_via_rescue(sgp)


class TestEscapeProducer:
    def test_plane_inners_decline_micro(self):
        route = _escape(LayerStack.four_layer_sig_gnd_pwr_sig())
        assert route is None or route.via is None or not route.via.is_micro

    def test_inner_signal_layer_unchanged(self):
        route = _escape(LayerStack.four_layer_sig_sig_gnd_pwr())
        assert route is not None and route.via is not None
        assert route.via.is_micro
        assert route.via.layers == (Layer.F_CU, Layer.IN1_CU)


class TestSubgridProducer:
    def test_plane_inners_decline_micro(self):
        assert _rescue(LayerStack.four_layer_sig_gnd_pwr_sig()) is None

    def test_inner_signal_layer_unchanged(self):
        esc = _rescue(LayerStack.four_layer_sig_sig_gnd_pwr())
        assert esc is not None and esc.via is not None
        assert esc.via.is_micro
        assert esc.via.layers == (Layer.F_CU, Layer.IN1_CU)

    def test_two_layer_unchanged(self):
        esc = _rescue(LayerStack.two_layer())
        assert esc is not None and esc.via is not None
        assert esc.via.is_micro
        assert esc.via.layers == (Layer.F_CU, Layer.B_CU)


def test_layer_adjacency_helper():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    assert stack.are_layers_adjacent(Layer.F_CU, Layer.IN1_CU)
    assert not stack.are_layers_adjacent(Layer.F_CU, Layer.B_CU)
    assert LayerStack.two_layer().are_layers_adjacent(Layer.F_CU, Layer.B_CU)
