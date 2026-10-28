"""Post-route authored-floor backstop: per-connection demotion (#8029).

``Autorouter.demote_authored_floor_violation_nets`` used to strip every route
of a net with one violating segment.  On softstart rev-C that deleted six nets
whose only violation was a ROUTER-class clearance (#6427) to a foreign pad on
the footprint the net terminates on.  The backstop now strips only the
offending connection, so the net reports partial.

Router-class floors are not waived inside a #4506 attach zone.  Since #6390
`kct route` writes each router class into the routed `.kicad_dru`, and
`kct check`'s `netclass_clearance_copper` mirrors that rule, so a waived gap
would fail both DRC engines.  An attach-zone exemption in both engines is a
DRC-contract change that needs an operator ruling first.

The fixtures below are synthetic:

* footprint U1 carries net A's pad ``U1.1`` and net B's pad ``U1.2``;
* net A's track leaves ``U1.1`` 0.2 mm from ``U1.2``, short of B's 0.4 mm floor
  but above the 0.1 mm board clearance.
"""

from __future__ import annotations

import pytest

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


def _attach_route(router: Autorouter) -> Route:
    """R1.1 -> U1.1, whose last leg passes U1.2 at 0.2 mm (floor 0.4 mm)."""
    return _commit(router, Route(A, "A", segments=[_seg(10, 2, 10, 8), _seg(10, 8, 10, 10)]))


@pytest.mark.parametrize(
    "designer", [{}, {B: B_FLOOR}], ids=["router-class-floor", "designer-floor"]
)
def test_gap_inside_attach_zone_is_still_demoted(designer, capsys):
    """(a) The formerly waived geometry inside an attach zone is still demoted.

    A router-class floor is written into the routed ``.kicad_dru`` (#6390), so
    an attach zone listing both nets does not excuse the gap.  The demotion is
    connection-level: the log names the connection, not "demoted to unrouted"
    for a net with other routed copper.
    """
    router = _router(designer=designer)
    router._pairwise_attach_zones_cache = (
        AttachZone(
            9.2,
            8.8,
            11.4,
            10.8,
            frozenset({"A", "B"}),
            frozenset((n, frozenset({"F.Cu"})) for n in ("A", "B")),
        ),
    )
    keep = _commit(router, Route(A, "A", segments=[_seg(2, 2, 10, 2)]))
    bad = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]

    assert bad not in router.routes
    assert keep in router.routes and len(keep.segments) == 1
    out = capsys.readouterr().out
    assert "demoted connection" in out and "net left partial" in out
    assert router.demote_authored_floor_violation_nets() == []


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


def test_lone_violating_connection_reports_unrouted(capsys):
    """With no other copper of the net left, the log still says unrouted."""
    router = _router(designer={B: B_FLOOR})
    bad = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]

    assert bad not in router.routes
    assert "demoted to unrouted" in capsys.readouterr().out


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


@pytest.mark.timeout(60)
def test_backstop_stops_when_a_violation_cannot_be_removed(monkeypatch, capsys):
    """An unremovable violation ends the loop instead of spinning forever."""
    router = _router(designer={B: B_FLOOR})
    bad = _attach_route(router)
    calls: list[int] = []

    def _strip_nothing(net, copper):
        calls.append(net)
        return 0, 0, "U1.1 -> R1.1", True

    monkeypatch.setattr(router, "_strip_floor_violating_copper", _strip_nothing)

    # The net is still reported, so route-auto's refusal keeps firing.
    assert router.demote_authored_floor_violation_nets() == [A]
    assert calls == [A]
    assert bad in router.routes
    out = capsys.readouterr().out
    assert "could not remove" in out


@pytest.mark.timeout(60)
def test_backstop_stops_when_stripped_copper_reappears(monkeypatch, capsys):
    """A strip that claims progress but leaves the same copper also stops.

    For example a ``Segment`` object shared by two ``Route`` objects: removing
    it from one leaves the other still violating with the same copper.
    """
    router = _router(designer={B: B_FLOOR})
    bad = _attach_route(router)
    calls: list[int] = []

    def _strip_claims_progress(net, copper):
        calls.append(net)
        return 1, 0, "U1.1 -> R1.1", True

    monkeypatch.setattr(router, "_strip_floor_violating_copper", _strip_claims_progress)

    assert router.demote_authored_floor_violation_nets() == [A]
    assert calls == [A]
    assert bad in router.routes
    assert "could not remove" in capsys.readouterr().out


def _zone_cell(router: Autorouter, layer: Layer, x: float, y: float) -> None:
    gx, gy = router.grid.world_to_grid(x, y)
    idx = router.grid.layer_to_index(layer.value)
    router.grid._is_zone[idx, gy, gx] = True
    router.grid._net[idx, gy, gx] = A


def test_zone_on_other_layer_does_not_anchor_stub():
    """A same-net zone on B.Cu does not anchor an F.Cu track end: the stub is stripped."""
    router = _router(designer={B: B_FLOOR})
    _zone_cell(router, Layer.B_CU, 10, 8)
    bad = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]

    assert bad.segments == [] or all(s.y2 != 8 and s.y1 != 8 for s in bad.segments)
    assert router.demote_authored_floor_violation_nets() == []


def test_zone_on_same_layer_anchors_stub():
    """The same zone cell on F.Cu anchors the junction, so the upstream leg is kept."""
    router = _router(designer={B: B_FLOOR})
    _zone_cell(router, Layer.F_CU, 10, 8)
    bad = _attach_route(router)

    assert router.demote_authored_floor_violation_nets() == [A]

    assert bad in router.routes and len(bad.segments) == 1
    assert (bad.segments[0].y1, bad.segments[0].y2) == (2, 8)
