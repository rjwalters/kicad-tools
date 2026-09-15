"""Kelvin branches must meet in physical shunt copper, not along the force path."""

import math

import pytest
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules


def _route_fixture(strategy, net_name, *, layer=Layer.F_CU, dispatch="net"):
    router = Autorouter(
        20,
        20,
        rules=DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2),
        force_python=True,
        physics_enabled=False,
        strategy=strategy,
    )
    positions = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}
    # Pad insertion order must not determine the physical shunt join.
    for ref in ("R10", "U3", "Q1", "U2"):
        x, y = positions[ref]
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": net_name,
                    "layer": layer,
                }
            ],
        )
    routes = (
        router.route_net(1, per_net_timeout=15)
        if dispatch == "net"
        else router.route_all(per_net_timeout=15)
    )
    assert routes
    # This initial fixture deliberately uses one layer; never silently discard vias.
    assert not any(route.vias for route in routes)
    segments = [s for route in routes for s in route.segments]
    assert all(s.layer == layer for s in segments)
    pads = {ref: box(x - 0.5, y - 0.5, x + 0.5, y + 0.5) for ref, (x, y) in positions.items()}
    copper = unary_union(
        [
            *pads.values(),
            *(LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2) for s in segments),
        ]
    )
    components = list(copper.geoms) if hasattr(copper, "geoms") else [copper]
    assert any(
        all(component.intersects(pad) for pad in pads.values()) for component in components
    ), "All four terminals must connect; omitting a difficult branch is not Kelvin acceptance"
    removed = copper.difference(pads["R10"].buffer(1e-6))
    components = list(removed.geoms) if hasattr(removed, "geoms") else [removed]
    return [
        {ref for ref, pad in pads.items() if ref != "R10" and component.intersects(pad)}
        for component in components
    ]


@pytest.mark.parametrize("strategy", ["grid", "lattice"])
def test_kelvin_branches_disconnect_when_shunt_copper_is_removed(strategy):
    components = _route_fixture(strategy, "ISENSE_A+")
    assert all(len(terminals) <= 1 for terminals in components), components


def test_ordinary_lattice_net_can_reuse_same_net_copper():
    components = _route_fixture("lattice", "SIGNAL")
    assert any(len(terminals) > 1 for terminals in components)


def test_branch_guard_uses_layer_and_via_bodies_and_physical_pad_keys():
    from kicad_tools.router.lattice.kelvin import KelvinBranchGuard
    from kicad_tools.router.lattice.obstacles import CommittedCopper
    from kicad_tools.router.primitives import Pad

    def pad(x, component_id):
        return Pad(
            x=x,
            y=3,
            width=1,
            height=1,
            net=1,
            layer=Layer.F_CU,
            ref="DUP",
            pin="1",
            component_id=component_id,
            net_name="ISENSE_A+",
        )

    root, target, other = pad(3, "root"), pad(8, "target"), pad(6, "other")
    copper = CommittedCopper(
        2,
        trace_half=0.1,
        clearance=0.2,
        via_radius=0.3,
        via_via_gap=0.8,
        same_net_via_gap=0.5,
    )
    copper.add_run(0, [(3, 3), (4, 3)], 1, 0.1)
    copper.add_run(1, [(3, 3), (4, 3)], 1, 0.1)
    copper.add_via((10, 3), 1)
    copper.add_via((12, 3), 1, radius=0.8, layers=(1,))
    copper.add_run(0, [(14, 2), (14, 4)], 2, 0.2, clearance=0.7)
    copper.kelvin_guard = KelvinBranchGuard(
        copper, [root, target, other], root, target, lambda p: [0]
    )
    assert copper.node_clear((3, 3), 0, 1)  # Join inside actual root metal.
    assert not copper.node_clear((3, 3), 1, 1)  # No root copper on the back.
    assert not copper.seg_clear((3.8, 2), (3.8, 4), 0, 1)
    assert not copper.node_clear((6, 3), 0, 1)  # Same authored ref/pin, different pad.
    assert copper.node_clear((6, 3), 1, 1)
    assert copper.node_clear((8, 3), 0, 1)  # The actual target is accessible.
    assert not copper.via_clear((6, 3), 1)  # Through-via hits front pad.
    for layer in (0, 1):
        assert not copper.node_clear((10.35, 3), layer, 1)
        # Probe between polygon vertices: a rounded via is a disk, not an
        # inscribed polygon whose facets permit a tiny physical overlap.
        angle = math.pi / 64
        tangent = (10 + 0.3999 * math.cos(angle), 3 + 0.3999 * math.sin(angle))
        assert not copper.node_clear(tangent, layer, 1)
    assert copper.node_clear((12.7, 3), 0, 1)
    assert not copper.node_clear((12.7, 3), 1, 1)
    assert not copper.node_clear((14.8, 3), 0, 1)  # Foreign class clearance remains active.
    assert copper.node_clear((15.1, 3), 0, 1)
    copper.kelvin_guard = None
    assert copper.node_clear((6, 3), 0, 1)


@pytest.mark.parametrize("layer", [Layer.F_CU, Layer.B_CU])
def test_negotiated_kelvin_branches_remain_physically_separate(layer):
    components = _route_fixture("lattice", "ISENSE_A+", layer=layer, dispatch="all")
    assert all(len(terminals) <= 1 for terminals in components), components


def test_preserved_buried_via_keeps_actual_diameter_and_span():
    from kicad_tools.router.lattice.kelvin import KelvinBranchGuard
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Pad, Route, Via

    stack = LayerStack.four_layer_all_signal()
    root = Pad(3, 3, 1, 1, 1, "ISENSE_A+", layer=Layer.F_CU, ref="R1", pin="1")
    target = Pad(8, 3, 1, 1, 1, "ISENSE_A+", layer=Layer.F_CU, ref="U1", pin="1")
    pf = LatticePathfinder([(0, 0), (20, 0), (20, 20), (0, 20)], [root, target], layer_stack=stack)
    via = Via(
        x=12,
        y=3,
        diameter=1.6,
        drill=0.3,
        net=1,
        layers=(stack.layers[2].layer_enum, stack.layers[1].layer_enum),
    )
    preserved = Route(net=1, net_name="ISENSE_A+", vias=[via])
    pf._set_fixed_copper([preserved])
    for _ in range(2):  # Every negotiation pass receives the same immutable bodies.
        committed = pf._fresh_committed()
        guard = KelvinBranchGuard(committed, [root, target], root, target, pf._pad_layer_indices)
        assert guard.clear((12.7, 3), (12.7, 3), 0, 0.1)
        assert guard.clear((12.7, 3), (12.7, 3), 3, 0.1)
        for layer in (1, 2):
            assert not guard.clear((12.7, 3), (12.7, 3), layer, 0.1)
    assert preserved.vias == [via]


@pytest.mark.parametrize("through", [False, True])
def test_imported_via_span_is_projected_onto_selected_routing_layers(through):
    from kicad_tools.router.lattice.kelvin import KelvinBranchGuard
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType
    from kicad_tools.router.primitives import Pad, Route, Via

    stack = LayerStack([LayerDefinition("F.Cu", 0, LayerType.SIGNAL, True)])
    root = Pad(3, 3, 1, 1, 1, "ISENSE_A+", layer=Layer.F_CU, ref="R1", pin="1")
    target = Pad(8, 3, 1, 1, 1, "ISENSE_A+", layer=Layer.F_CU, ref="U1", pin="1")
    pf = LatticePathfinder([(0, 0), (20, 0), (20, 20), (0, 20)], [root, target], layer_stack=stack)
    via = Via(
        x=12,
        y=3,
        diameter=1.6,
        drill=0.3,
        net=1,
        layers=(Layer.F_CU if through else Layer.IN1_CU, Layer.B_CU),
    )
    pf._set_fixed_copper([Route(net=1, net_name="ISENSE_A+", vias=[via])])
    committed = pf._fresh_committed()
    guard = KelvinBranchGuard(committed, [root, target], root, target, pf._pad_layer_indices)
    assert guard.clear((12.7, 3), (12.7, 3), 0, 0.1) is (not through)
