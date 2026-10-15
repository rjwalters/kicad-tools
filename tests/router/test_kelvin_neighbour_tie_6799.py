"""Neighbouring duplicate pins on a Kelvin net are one terminal (issue #6799).

Board 05's GND_SENSE is a Kelvin net (shunt R1).  The DRV8313's PGND1/PGND2
pins U2.6 and U2.7 sit side by side at 0.65 mm pitch.  The Kelvin star routed
each of them to the shunt on its own, isolated from the other, so U2.7's
branch dropped a via in U2.6's only exit lane and U2.6 stayed unconnected.
The pins are now tied pad-to-pad first and the star routes one branch for the
pair.  Pins with room for a trace between them, and resistor pads, keep their
own branch.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import get_backend_info
from kicad_tools.router.path import create_intra_ic_routes
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

SENSE = 1
PITCH = 0.65
COLUMN_X = 14.0
# Pin number -> (net id, net name) for a DRV8313-like west column.
COLUMN = {
    "5": (2, "PHASE_A"),
    "6": (SENSE, "GND_SENSE"),
    "7": (SENSE, "GND_SENSE"),
    "8": (3, "PHASE_B"),
    "9": (4, "PHASE_C"),
    "10": (SENSE, "GND_SENSE"),
}


def _pin_y(pin: str) -> float:
    return 8.0 + (int(pin) - 5) * PITCH


def _board(force_python: bool) -> Autorouter:
    router = Autorouter(
        22, 22, force_python=force_python, physics_enabled=False, per_net_iterations=50000
    )
    router.add_component(
        "U1",
        [
            {
                "number": pin,
                "x": COLUMN_X,
                "y": _pin_y(pin),
                "width": 1.575,
                "height": 0.4,
                "net": net,
                "net_name": name,
            }
            for pin, (net, name) in COLUMN.items()
        ],
    )
    router.add_component(
        "R1",
        [
            {
                "number": "1",
                "x": 4.0,
                "y": 15.0,
                "width": 1.2,
                "height": 3.0,
                "net": SENSE,
                "net_name": "GND_SENSE",
            }
        ],
    )
    return router


def _metal(routes):
    return unary_union(
        [
            LineString([s.start, s.end]).buffer(s.width / 2)
            for route in routes
            for s in route.segments
        ]
        + [Point(v.x, v.y).buffer(v.diameter / 2) for route in routes for v in route.vias]
    )


def _pad_box(pad: Pad):
    return box(
        pad.x - pad.width / 2, pad.y - pad.height / 2, pad.x + pad.width / 2, pad.y + pad.height / 2
    )


def _is_tie(route) -> bool:
    return (
        len(route.segments) == 1
        and not route.vias
        and {
            (round(p[0], 4), round(p[1], 4))
            for p in (route.segments[0].start, route.segments[0].end)
        }
        == {(COLUMN_X, round(_pin_y("6"), 4)), (COLUMN_X, round(_pin_y("7"), 4))}
    )


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("mode", ["mst", "negotiated"])
def test_adjacent_duplicate_pins_tie_and_every_terminal_connects(force_python, mode):
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    router = _board(force_python)

    routes = (
        router._route_net_negotiated(SENSE, 1.0)
        if mode == "negotiated"
        else router.route_net(SENSE, use_mst=True)
    )

    ties = [r for r in routes if _is_tie(r)]
    assert len(ties) == 1, "U1.6 and U1.7 must be tied pad-to-pad"

    # One conductor joins the shunt and every sense-net pin.
    sense_pads = [p for p in router.pads.values() if p.net == SENSE]
    conductor = unary_union([_metal(routes)] + [_pad_box(p) for p in sense_pads])
    shunt = _pad_box(router.pads["R1", "1"])
    parts = getattr(conductor, "geoms", [conductor])
    root_part = next(part for part in parts if part.intersects(shunt))
    for pad in sense_pads:
        assert root_part.intersects(_pad_box(pad)), f"{pad.ref}.{pad.pin} is not connected"

    # The tied pair and U1.10 are still separate Kelvin branches: they share
    # copper only at the shunt.
    branches = [r for r in routes if not _is_tie(r)]
    assert len(branches) == 2
    shared = _metal([branches[0]]).intersection(_metal([branches[1]])).difference(shunt)
    assert shared.area < 1e-9


def test_kelvin_ties_skip_resistor_pads_and_spaced_pins():
    router = _board(force_python=True)
    # A four-terminal shunt: force and sense pads on the same net, close together.
    router.add_component(
        "R2",
        [
            {
                "number": str(n),
                "x": 4.0 + 0.6 * n,
                "y": 4.0,
                "width": 0.4,
                "height": 1.0,
                "net": SENSE,
                "net_name": "GND_SENSE",
            }
            for n in (1, 2)
        ],
    )
    pads = list(router.nets[SENSE])
    routes, tied = router._create_kelvin_terminal_ties(SENSE, pads)

    assert {pads[i] for i in tied} == {("U1", "6"), ("U1", "7")}
    assert len(routes) == 1 and _is_tie(routes[0])


def test_neighbours_only_never_ties_across_room_for_a_trace():
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15)
    lookup = {
        ("U1", "1"): Pad(x=0, y=0, width=1.5, height=0.4, net=1, net_name="X", ref="U1", pin="1"),
        # 0.25 mm gap: no trace fits between -> one terminal
        ("U1", "2"): Pad(
            x=0, y=0.65, width=1.5, height=0.4, net=1, net_name="X", ref="U1", pin="2"
        ),
        # 1.6 mm gap: a trace fits between -> separate terminals
        ("U1", "3"): Pad(
            x=0, y=2.65, width=1.5, height=0.4, net=1, net_name="X", ref="U1", pin="3"
        ),
    }
    keys = list(lookup)

    routes, connected = create_intra_ic_routes(1, keys, lookup, rules, neighbours_only=True)
    assert connected == {0, 1}
    assert len(routes) == 1

    # The ordinary pass still ties every same-IC pair it can.
    _, connected_all = create_intra_ic_routes(1, keys, lookup, rules)
    assert connected_all == {0, 1, 2}
