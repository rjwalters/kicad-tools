"""Router barrel readers see the drilled span, not the logical pair (issue #6630).

A non-micro ``Via`` recorded as ``(F.Cu, In1.Cu)`` is still a mechanically
drilled through barrel (``Via.drilled_layers`` is ``F.Cu..B.Cu``, issue
#6485).  Each clearance/connectivity reader below must see it on B.Cu.
"""

from types import SimpleNamespace

import pytest
from shapely.geometry import box

from kicad_tools.router.escape import EscapeDirection, EscapeRoute, escape_endpoint_copper_extent
from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.terminal_connectivity import physical_copper_components


def _via(**kw) -> Via:
    return Via(
        x=5.0,
        y=5.0,
        drill=0.3,
        diameter=0.6,
        layers=(Layer.F_CU, Layer.IN1_CU),
        net=1,
        net_name="N",
        **kw,
    )


def _pad(x, y, layer, net=1, pin="1", ref="U1", **kw) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.6,
        height=0.6,
        net=net,
        net_name="N",
        layer=layer,
        ref=ref,
        pin=pin,
        **kw,
    )


def test_logical_pair_is_not_the_barrel():
    via = _via()
    assert not via.is_micro
    assert via.drilled_layers == (Layer.F_CU, Layer.B_CU)


def test_terminal_connectivity_sees_barrel_on_bcu():
    pads = [_pad(5, 5, Layer.F_CU, ref="U1"), _pad(5, 5, Layer.B_CU, ref="U2")]
    comps = physical_copper_components(pads, [], [_via()])
    assert comps[0] == comps[1]


def test_escape_extent_sees_barrel_on_bcu():
    esc = EscapeRoute(
        pad=_pad(5, 5, Layer.F_CU),
        direction=EscapeDirection.NORTH,
        escape_point=(5.0, 5.0),
        escape_layer=Layer.B_CU,
        via=_via(),
    )
    assert escape_endpoint_copper_extent(esc) == pytest.approx(0.6)


def test_kelvin_isolation_blocks_barrel_on_bcu():
    from kicad_tools.router.kelvin_obstacles import isolate_kelvin_branch

    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    grid = RoutingGrid(10, 10, rules=DesignRules(grid_resolution=0.1), layer_stack=stack)
    route = Route(net=1, net_name="N", segments=[], vias=[_via()])
    grid.routes.append(route)
    root = _pad(2, 2, Layer.F_CU, pin="1")
    target = _pad(8, 8, Layer.F_CU, pin="2")
    bcu = grid.layer_to_index(Layer.B_CU.value)
    row, col = round(5.0 / grid.resolution), round(5.0 / grid.resolution)
    with isolate_kelvin_branch(grid, [root, target], root, target):
        assert grid._blocked[bcu, row, col]


def test_io_fixed_copper_clearance_sees_barrel_on_bcu():
    from kicad_tools.router.io import validate_routes

    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(10, 10, rules=rules, layer_stack=stack)
    bcu = grid.layer_to_index(Layer.B_CU.value)
    grid.install_fixed_fills(
        FixedFillObstacles((FixedFill("GND", 2, bcu, 0.2, box(4.9, 4.9, 5.1, 5.1)),))
    )
    router = SimpleNamespace(
        grid=grid,
        routes=[Route(net=1, net_name="N", segments=[], vias=[_via()])],
        rules=rules,
        net_names={1: "N"},
        net_class_map=None,
        pads={},
        nets={},
        component_pitches={},
    )
    violations = validate_routes(router)
    assert any(v.obstacle_type == "fixed_copper" for v in violations)


def test_lattice_fixed_copper_span_includes_bcu():
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder

    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    pf = object.__new__(LatticePathfinder)
    pf.layer_stack = stack
    pf.rules = DesignRules(grid_resolution=0.1)
    pf._set_fixed_copper([Route(net=1, net_name="N", segments=[], vias=[_via()])])
    occupied = pf._fixed_vias[0][4]
    assert stack.layer_enum_to_index(Layer.B_CU) in occupied
    assert len(occupied) == stack.num_layers


def test_class_pad_repair_via_span_includes_bcu():
    from kicad_tools.router.lattice.class_pad_repair import repair_class_pad_pairs

    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    rules = DesignRules(grid_resolution=0.1)
    pad = _pad(5.6, 5.0, Layer.B_CU, net=2, ref="U9")
    pf = SimpleNamespace(
        pad_class_clearances=[0.5],
        _pairwise=None,
        pads=[pad],
        rules=rules,
        layer_stack=stack,
        _pad_layer_indices=lambda p: (stack.layer_enum_to_index(p.layer),),
        _conn_geometry=lambda cls, net=None: (0.1, rules.trace_clearance),
    )
    route = Route(net=1, net_name="N", segments=[], vias=[_via()])
    stats = repair_class_pad_pairs(pf, {"k": route}, {"k": None})
    assert stats.via_found == 1
