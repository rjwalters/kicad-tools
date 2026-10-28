"""Post-route authored-floor backstop honours the attach-zone floor exemption (#8048).

``Autorouter.demote_authored_floor_violation_nets`` waives a ROUTER-class floor
shortfall inside a footprint's attach zone (the zone the emitted ``.kicad_dru``
and ``kct check`` apply) and never a designer's.  Synthetic fixture:

* footprint U1 carries net A's pad ``U1.1`` and net B's pad ``U1.2``;
* net A's track leaves ``U1.1`` 0.2 mm from ``U1.2``, short of B's 0.4 mm floor
  but above the 0.1 mm board clearance.
"""

from __future__ import annotations

from shapely.geometry import box

from kicad_tools.manufacturers.attach_zone_dru import FloorZone
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

A, B = 1, 2
B_FLOOR = 0.4
W = 0.2


def _pad(net: int, name: str, number: str, x: float, y: float) -> dict:
    return {
        "width": 0.6,
        "height": 0.6,
        "net": net,
        "net_name": name,
        "number": number,
        "x": x,
        "y": y,
    }


def _router(designer: dict[int, float] | None, base: float = 0.1) -> Autorouter:
    rules = DesignRules(
        trace_width=W,
        trace_clearance=base,
        via_clearance=0.1,
        net_clearance_floors={B: B_FLOOR},
        designer_clearance_floors=designer,
    )
    router = Autorouter(width=20, height=20, rules=rules, force_python=True)
    router.add_component("U1", [_pad(A, "A", "1", 10.0, 10.0), _pad(B, "B", "2", 10.6, 9.6)])
    router.add_component("R1", [_pad(A, "A", "1", 10.0, 2.0)])
    router.add_component("R2", [_pad(A, "A", "1", 2.0, 2.0)])
    router.net_names.update({A: "A", B: "B"})
    return router


def _zone(a_layers=("F.Cu",), b_layers=("F.Cu",), courtyard=None, nets=("A", "B")) -> FloorZone:
    layers = {"A": frozenset(a_layers), "B": frozenset(b_layers)}
    members = tuple((n, layers.get(n, frozenset({"F.Cu"}))) for n in nets)
    return FloorZone("U1", members, courtyard or box(8.0, 7.0, 12.0, 12.0))


def _attach_route(router: Autorouter) -> Route:
    """R1.1 -> U1.1, whose last leg passes U1.2 at 0.2 mm (floor 0.4 mm)."""
    route = Route(
        A,
        "A",
        segments=[
            Segment(10, 2, 10, 8, W, Layer.F_CU, A, "A"),
            Segment(10, 8, 10, 10, W, Layer.F_CU, A, "A"),
        ],
    )
    router.grid.mark_route(route)
    router.routes.append(route)
    return route


def test_router_class_floor_in_attach_zone_is_not_demoted():
    router = _router(designer={})
    router._attach_floor_zones_cache = (_zone(),)
    route = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == []
    assert route in router.routes


def test_without_a_zone_the_same_copper_is_demoted():
    router = _router(designer={})
    router._attach_floor_zones_cache = ()
    route = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]
    assert route not in router.routes


def test_designer_floor_in_attach_zone_is_still_demoted():
    router = _router(designer={B: B_FLOOR})
    router._attach_floor_zones_cache = (_zone(),)
    _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]


def test_rules_without_designer_record_treat_every_floor_as_designer():
    router = _router(designer=None)
    router._attach_floor_zones_cache = (_zone(),)
    _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]


def test_zone_must_list_both_nets_and_cover_the_copper():
    for zone in (
        _zone(nets=("A", "C")),
        _zone(courtyard=box(30.0, 30.0, 31.0, 31.0)),
        _zone(b_layers=("B.Cu",)),  # #4699: B has no copper on F.Cu here
    ):
        router = _router(designer={})
        router._attach_floor_zones_cache = (zone,)
        _attach_route(router)
        assert router.demote_authored_floor_violation_nets() == [A]


def test_waiver_keeps_board_clearance_strict():
    router = _router(designer={}, base=0.25)
    router._attach_floor_zones_cache = (_zone(),)
    _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]
