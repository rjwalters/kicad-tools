"""Post-route authored-floor backstop: attach zones and per-connection demotion (#8029).

``Autorouter.demote_authored_floor_violation_nets`` used to judge every pair at
the full floor and strip the whole net.  On softstart rev-C that deleted six
nets whose only "violation" was a ROUTER-class clearance (#6427) to a foreign
pad on the very footprint the net terminates on -- a #4506 attach zone the
search itself relaxes.  The fixtures below are synthetic:

* footprint U1 carries net A's pad ``U1.1`` and net B's pad ``U1.2``;
* net A's track leaves ``U1.1`` 0.2 mm from ``U1.2``, short of B's 0.4 mm floor
  but above the 0.1 mm board clearance.
"""

from __future__ import annotations

from kicad_tools.router.authored_clearance import attach_zone_floor_waiver
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import AttachZone
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


def _router(designer: dict[int, float] | None) -> Autorouter:
    rules = DesignRules(
        trace_width=W,
        trace_clearance=0.1,
        via_clearance=0.1,
        net_clearance_floors={B: B_FLOOR},
        designer_clearance_floors=designer,
    )
    router = Autorouter(width=20, height=20, rules=rules, force_python=True)
    # U1.2 (net B) sits 0.6 mm right of net A's track below U1.1: x 10.3..10.9.
    router.add_component("U1", [_pad(A, "A", "1", 10.0, 10.0), _pad(B, "B", "2", 10.6, 9.6)])
    router.add_component("R1", [_pad(A, "A", "1", 10.0, 2.0)])
    router.add_component("R2", [_pad(A, "A", "1", 2.0, 2.0)])
    router.net_names.update({A: "A", B: "B"})
    return router


def _commit(router: Autorouter, route: Route) -> Route:
    router.grid.mark_route(route)
    router.routes.append(route)
    return route


def _seg(x1: float, y1: float, x2: float, y2: float) -> Segment:
    return Segment(x1, y1, x2, y2, W, Layer.F_CU, A, "A")


def _zone(names: frozenset[str]) -> AttachZone:
    # U1's pad field plus the #4506 margin, both nets' pad copper on F.Cu.
    return AttachZone(
        9.2,
        8.8,
        11.4,
        10.8,
        names,
        frozenset((n, frozenset({"F.Cu"})) for n in names),
    )


def _attach_route(router: Autorouter) -> Route:
    """R1.1 -> U1.1, whose last leg passes U1.2 at 0.2 mm (floor 0.4 mm)."""
    return _commit(router, Route(A, "A", segments=[_seg(10, 2, 10, 8), _seg(10, 8, 10, 10)]))


def test_router_class_floor_in_attach_zone_is_not_demoted():
    """(a) A router-class floor (no designer minimum) inside a zone listing both nets."""
    router = _router(designer={})
    router._pairwise_attach_zones_cache = (_zone(frozenset({"A", "B"})),)
    route = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == []
    assert route in router.routes and len(route.segments) == 2


def test_designer_floor_in_attach_zone_is_still_demoted():
    """(b) The same geometry with B's 0.4 mm a designer minimum stays strict."""
    router = _router(designer={B: B_FLOOR})
    router._pairwise_attach_zones_cache = (_zone(frozenset({"A", "B"})),)
    route = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]
    assert route not in router.routes


def test_rules_without_designer_record_treat_every_floor_as_designer():
    """``designer_clearance_floors=None`` (rules built outside the loader, #6449)."""
    router = _router(designer=None)
    router._pairwise_attach_zones_cache = (_zone(frozenset({"A", "B"})),)
    _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]


def test_zone_must_list_both_nets():
    router = _router(designer={})
    router._pairwise_attach_zones_cache = (_zone(frozenset({"A", "C"})),)
    _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]


def test_zone_layer_scope_is_honoured():
    """#4699: B has pad copper on B.Cu only here, so an F.Cu gap is not waived."""
    router = _router(designer={})
    zone = AttachZone(
        9.2,
        8.8,
        11.4,
        10.8,
        frozenset({"A", "B"}),
        frozenset({("A", frozenset({"F.Cu"})), ("B", frozenset({"B.Cu"}))}),
    )
    router._pairwise_attach_zones_cache = (zone,)
    _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]


def test_waiver_keeps_board_clearance_strict():
    """A gap under the scalar board clearance is never waived, zone or not."""
    waive = attach_zone_floor_waiver(
        (_zone(frozenset({"A", "B"})),), base=0.3, designer={}, net_names={A: "A", B: "B"}
    )
    assert waive is not None
    router = _router(designer={})
    route = _attach_route(router)
    from kicad_tools.router.authored_clearance import authored_violations

    found = authored_violations(router.rules.net_clearance_floors, router.grid.pads, [route])
    assert found and found[0].actual < 0.3
    assert waive(found[0]) is False
    assert attach_zone_floor_waiver((), base=0.1, designer={}) is None
    assert (
        attach_zone_floor_waiver((_zone(frozenset({"A", "B"})),), base=0.1, designer=None) is None
    )


def test_real_violation_strips_only_the_offending_connection(capsys):
    """(c) R2.1 -> R1.1 stays routed when R1.1 -> U1.1 is demoted."""
    router = _router(designer={B: B_FLOOR})
    keep = _commit(router, Route(A, "A", segments=[_seg(2, 2, 10, 2)]))
    bad = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]

    assert keep in router.routes and len(keep.segments) == 1
    assert keep in router.grid.routes
    assert bad not in router.routes
    out = capsys.readouterr().out
    assert "demoted connection" in out and "net left partial" in out
    assert "U1.1" in out and "R1.1" in out
    assert router.demote_authored_floor_violation_nets() == []


def test_strip_stops_at_a_junction_shared_with_another_connection():
    """A trunk shared by two connections keeps the branch it still feeds."""
    router = _router(designer={B: B_FLOOR})
    # One Route spanning R2.1 -> (10, 2) junction -> R1.1, plus the bad leg
    # from the junction to U1.1 in the same Route object.
    route = _commit(
        router,
        Route(
            A,
            "A",
            segments=[_seg(2, 2, 10, 2), _seg(10, 2, 10, 8), _seg(10, 8, 10, 10)],
        ),
    )

    assert router.demote_authored_floor_violation_nets() == [A]
    assert route in router.routes
    assert [(s.x1, s.y1, s.x2, s.y2) for s in route.segments] == [(2, 2, 10, 2)]
