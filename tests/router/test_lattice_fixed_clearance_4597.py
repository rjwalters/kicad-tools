"""The lattice driver resolves preserved copper's net-class clearance (#4597).

``--preserve-existing`` composition (the documented HV-outer recipe, generalized
to voltage-binned groups) routes one net group per step and hands the earlier
steps' copper to the next step as ``router.existing_routes``.
:meth:`Autorouter._negotiate_lattice_netset` used to pass that copper to
``LatticePathfinder.route_netset`` with no clearance information at all, so the
pathfinder seeded every preserved segment/via at the board-global DRU floor and
``CommittedCopper``'s ``max(own_clr, stored_clr)`` collapsed to the ROUTING
net's own clearance -- cross-step pairs landed at ~0.2mm however large the
``--net-class-map`` clearance on the preserved net was.

These tests pin the DRIVER half of the fix: that the ``{net_id: clearance}``
map handed to the pathfinder is resolved with exactly the same lookup and the
same ``max(class, rules)`` floor the listed-net branch already uses, and that
it stays EMPTY (a byte-identical no-op) whenever no class asks for more than the
board-global clearance.  The geometry half lives in
``tests/router/lattice/test_preserve_existing_obstacle.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import NetClassRouting

_HV_CLEARANCE = 2.0


class _CapturingPathfinder:
    """Captures ``route_netset``'s kwargs and routes nothing."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.failure_reasons: dict[object, str] = {}

    def set_pairwise(self, pairwise: Any) -> None:
        """No-op stand-in for ``LatticePathfinder.set_pairwise`` (#4602)."""

    def route_netset(self, connections: list[Any], **kwargs: Any) -> tuple[dict, Any]:
        self.kwargs = kwargs
        return {}, SimpleNamespace(
            routed=0,
            total=len(connections),
            iterations=0,
            converged=True,
            lattice_builds=1,
        )


def _pad(x: float, y: float, net: int, *, ref: str, net_name: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _preserved_route(net: int, net_name: str) -> Route:
    """One preserved segment + one preserved via on ``net``."""
    return Route(
        net=net,
        net_name=net_name,
        segments=[
            Segment(
                x1=10.0,
                y1=5.0,
                x2=10.0,
                y2=25.0,
                width=0.5,
                layer=Layer.F_CU,
                net=net,
                net_name=net_name,
            )
        ],
        vias=[
            Via(
                x=10.0,
                y=25.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=net,
                net_name=net_name,
            )
        ],
    )


def _driver(
    *,
    net_class_map: dict[str, NetClassRouting] | None = None,
    preserved: list[Route] | None = None,
    register_net_name: bool = True,
) -> _CapturingPathfinder:
    """Run ``_negotiate_lattice_netset`` against a capturing pathfinder."""
    router = Autorouter(60, 50, strategy="lattice")
    # Listed net 1 (two pads -> one connection).
    for pad in (
        _pad(5.0, 15.0, 1, ref="U1", net_name="/LV_SIG"),
        _pad(50.0, 15.0, 1, ref="U2", net_name="/LV_SIG"),
    ):
        router.pads[(pad.ref, pad.pin)] = pad
        router.nets.setdefault(pad.net, []).append((pad.ref, pad.pin))
        router.net_names[pad.net] = pad.net_name

    router.existing_routes = list(preserved or [])
    if register_net_name:
        for route in router.existing_routes:
            router.net_names[route.net] = route.net_name
    if net_class_map is not None:
        router.net_class_map.update(net_class_map)

    stub = _CapturingPathfinder()
    router._lattice_pathfinder = stub  # type: ignore[assignment]
    router._negotiate_lattice_netset()
    return stub


def test_preserved_hv_net_clearance_reaches_the_pathfinder() -> None:
    """The mapped 2.0mm HV clearance is handed down keyed by NET ID."""
    stub = _driver(
        net_class_map={"/FUSED_LINE": NetClassRouting(name="HV", clearance=_HV_CLEARANCE)},
        preserved=[_preserved_route(2, "/FUSED_LINE")],
    )

    assert [r.net for r in stub.kwargs["fixed_copper"]] == [2]
    assert stub.kwargs["fixed_clearance"] == {2: _HV_CLEARANCE}


def test_route_net_name_resolves_when_net_names_has_no_entry() -> None:
    """A preserved net absent from ``net_names`` falls back to ``route.net_name``."""
    stub = _driver(
        net_class_map={"/FUSED_LINE": NetClassRouting(name="HV", clearance=_HV_CLEARANCE)},
        preserved=[_preserved_route(2, "/FUSED_LINE")],
        register_net_name=False,
    )

    assert stub.kwargs["fixed_clearance"] == {2: _HV_CLEARANCE}


def test_unmapped_preserved_net_yields_an_empty_map() -> None:
    """No map entry -> no clearance override; the pre-#4597 seed path exactly."""
    stub = _driver(preserved=[_preserved_route(2, "/FUSED_LINE")])

    assert stub.kwargs["fixed_copper"]
    assert stub.kwargs["fixed_clearance"] == {}


def test_class_clearance_at_or_below_the_dru_floor_is_omitted() -> None:
    """A class may only GROW the gap -- at/below the floor it is a no-op.

    Omitting the entry (rather than storing the floor) keeps the map empty on
    every default-map board, so the pathfinder's seed path is byte-identical.
    """
    router_floor = Autorouter(60, 50, strategy="lattice").rules.trace_clearance
    for clearance in (router_floor, router_floor / 2.0):
        stub = _driver(
            net_class_map={"/FUSED_LINE": NetClassRouting(name="X", clearance=clearance)},
            preserved=[_preserved_route(2, "/FUSED_LINE")],
        )
        assert stub.kwargs["fixed_clearance"] == {}, clearance


def test_listed_nets_are_not_given_a_preserved_clearance() -> None:
    """A LISTED net's own stale copper is filtered out of ``fixed_copper``.

    It must therefore never appear in the clearance map either -- a net may not
    fix itself in place, whatever its class says.
    """
    stub = _driver(
        net_class_map={"/LV_SIG": NetClassRouting(name="HV", clearance=_HV_CLEARANCE)},
        preserved=[_preserved_route(1, "/LV_SIG")],
    )

    assert stub.kwargs["fixed_copper"] == []
    assert stub.kwargs["fixed_clearance"] == {}


def test_multiple_preserved_nets_each_resolve_independently() -> None:
    """Voltage-binned composition: several preserved nets, several clearances."""
    stub = _driver(
        net_class_map={
            "/FUSED_LINE": NetClassRouting(name="HV", clearance=3.2),
            "/SRC_NEG": NetClassRouting(name="HV", clearance=_HV_CLEARANCE),
            # Below the floor -> omitted, not stored at the floor.
            "/CHASSIS": NetClassRouting(name="LV", clearance=0.05),
        },
        preserved=[
            _preserved_route(2, "/FUSED_LINE"),
            _preserved_route(3, "/SRC_NEG"),
            _preserved_route(4, "/CHASSIS"),
            _preserved_route(5, "/UNMAPPED"),
        ],
    )

    assert stub.kwargs["fixed_clearance"] == {2: 3.2, 3: _HV_CLEARANCE}
