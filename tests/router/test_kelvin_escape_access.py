"""A trapped Kelvin surface endpoint needs a legal off-pad layer transition."""

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    PackageInfo,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.kelvin import detect_kelvin_topology
from kicad_tools.router.kelvin_escape import recover_kelvin_escapes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment, Via
from kicad_tools.router.rules import DesignRules


def fixture():
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        grid_resolution=0.05,
    )
    grid = RoutingGrid(width=20, height=20, rules=rules)
    router = EscapeRouter(grid, rules, component_holes=())
    pads = [
        Pad(
            x=10 + dx,
            y=10,
            width=0.3,
            height=1.55,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
            ref="U71",
            pin=str(net),
        )
        for dx, net, name in [(0, 1, "VSNS"), (-0.5, 2, "DRIVE_L"), (0.5, 3, "DRIVE_R")]
    ]
    terminals = [
        Pad(
            x=x,
            y=15,
            width=0.5,
            height=0.5,
            layer=Layer.F_CU,
            net=1,
            net_name="VSNS",
            ref=ref,
            pin="1",
        )
        for x, ref in [(3, "R82"), (6, "Q91"), (15, "U94")]
    ]
    for pad in pads + terminals:
        grid.add_pad(pad)
    package = PackageInfo(
        ref="U71",
        package_type=PackageType.QFP,
        center=(10, 9),
        pads=pads,
        pin_count=3,
        pin_pitch=0.5,
        bounding_box=(9.35, 9.225, 10.65, 10.775),
        is_dense=True,
    )
    surface = EscapeRoute(
        pad=pads[0],
        direction=EscapeDirection.NORTH,
        escape_point=(10, 10.7),
        escape_layer=Layer.F_CU,
        segments=[Segment(x1=10, y1=10, x2=10, y2=10.7, width=0.2, layer=Layer.F_CU, net=1)],
    )
    siblings = [
        EscapeRoute(
            pad=p,
            direction=EscapeDirection.NORTH,
            escape_point=(p.x, 11.95),
            escape_layer=Layer.B_CU,
            via_pos=(p.x, 11.25),
            via=Via(
                x=p.x, y=11.25, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=p.net
            ),
            segments=[],
        )
        for p in pads[1:]
    ]
    return router, package, [surface, *siblings], pads + terminals


def test_recovers_inward_without_changing_kelvin_star():
    router, package, escapes, pads = fixture()
    topology = detect_kelvin_topology([p for p in pads if p.net == 1])
    assert topology is not None
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0].via is not None
    assert result[0].direction == EscapeDirection.SOUTH
    assert result[0].via.y < 9.225
    assert result[0].via.diameter == 0.6
    assert result[0].via.drill == 0.3
    assert not result[0].via.in_pad
    assert result[1:] == escapes[1:]
    assert detect_kelvin_topology([p for p in pads if p.net == 1]) == topology


@pytest.mark.parametrize("blocker", ["track", "via", "pad", "edge", "unknown_holes"])
def test_rejects_inward_physical_conflicts(blocker):
    router, package, escapes, pads = fixture()
    if blocker == "track":
        escapes[1].segments.append(
            Segment(x1=9, y1=8.7, x2=11, y2=8.7, width=0.2, layer=Layer.B_CU, net=2)
        )
    elif blocker == "via":
        escapes.append(
            EscapeRoute(
                pad=package.pads[1],
                direction=EscapeDirection.SOUTH,
                escape_point=(10, 8.7),
                escape_layer=Layer.B_CU,
                via=Via(
                    x=10, y=8.7, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=2
                ),
            )
        )
    elif blocker == "pad":
        pad = Pad(
            x=10,
            y=8.7,
            width=0.5,
            height=0.5,
            layer=Layer.F_CU,
            net=7,
            net_name="FOREIGN",
            ref="C97",
            pin="1",
        )
        pads.append(pad)
        router.grid.add_pad(pad)
    elif blocker == "edge":
        router.board_bounds = (0, 9, 20, 20)
        router.edge_clearance = 0.2
    else:
        router._component_holes = None
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0] is escapes[0]


def test_name_without_shunt_does_not_activate_recovery():
    router, package, escapes, pads = fixture()
    pads = [p for p in pads if p.ref != "R82"]
    assert recover_kelvin_escapes(router, package, escapes, pads) == escapes


def test_public_prephase_uses_physical_kelvin_terminals(monkeypatch):
    from kicad_tools.router.core import Autorouter

    escape_router, package, escapes, pads = fixture()
    router = Autorouter(20, 20, rules=escape_router.rules, force_python=True)
    router.grid = escape_router.grid
    router._escape_router = escape_router
    router.pads = {pad.key: pad for pad in pads}
    router.all_pads = list(pads)
    monkeypatch.setattr(escape_router, "generate_escapes", lambda package: list(escapes))
    routes = router.generate_escape_routes([package])
    assert any(via.net == 1 for route in routes for via in route.vias)
    assert router._escape_pad_overrides[pads[0].key].layer == Layer.B_CU
    assert 1 in router._in_pad_escape_protected_nets


def test_rejects_inward_outline_cutout():
    router, package, escapes, pads = fixture()
    result = recover_kelvin_escapes(
        router, package, escapes, pads, edge_segments=[((9, 8.7), (11, 8.7))], edge_clearance=0.2
    )
    assert result[0] is escapes[0]


def test_same_net_sibling_drills_still_need_spacing():
    router, package, escapes, pads = fixture()
    escapes.append(
        EscapeRoute(
            pad=pads[0],
            direction=EscapeDirection.SOUTH,
            escape_point=(10.1, 8.7),
            escape_layer=Layer.B_CU,
            via=Via(x=10.1, y=8.7, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1),
        )
    )
    assert recover_kelvin_escapes(router, package, escapes, pads)[0] is escapes[0]


@pytest.mark.parametrize("location", ["fixed", "sibling"])
def test_kelvin_access_cannot_merge_into_existing_force_branch(location):
    from kicad_tools.router.primitives import Route

    router, package, escapes, pads = fixture()
    force = Segment(x1=9, y1=9.5, x2=11, y2=9.5, width=0.2, layer=Layer.F_CU, net=1)
    if location == "fixed":
        router.grid.mark_route(Route(net=1, net_name="VSNS", segments=[force]))
    else:
        escapes.append(
            EscapeRoute(
                pad=pads[-1],
                direction=EscapeDirection.SOUTH,
                escape_point=(11, 9.5),
                escape_layer=Layer.F_CU,
                segments=[force],
            )
        )
    assert recover_kelvin_escapes(router, package, escapes, pads)[0] is escapes[0]
