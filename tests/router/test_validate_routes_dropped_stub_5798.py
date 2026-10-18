"""validate_routes must not report shorts against copper the writer drops (#5798).

Board 07 routed with ``--preserve-existing --length-match-groups`` failed its
post-route short gate with 19 ``routing short [via]`` findings while kicad-cli
10.0.6 found no error on the saved board. Every finding paired a routed trace
(mostly a length-tuning meander) with an input escape-stub via of an SDRAM net
the run had re-routed. ``_finalize_routes`` drops a re-routed net's stale
input copper, so those vias are not in the output, but ``validate_routes`` used
all of ``router.existing_routes`` as its obstacle universe.

The obstacle universe is now :func:`kept_input_routes`: the copper the output
carries (``_emitted_preserved_routes`` after finalize; before it, existing
copper minus re-routed nets).
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import shorting_violations, validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _router() -> Autorouter:
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
    return Autorouter(width=50, height=50, rules=rules)


def _stub(net: int, name: str, x: float, y: float) -> Route:
    """An input escape stub: a short trace ending on a through via."""
    return Route(
        net=net,
        net_name=name,
        segments=[Segment(x1=x - 2.0, y1=y, x2=x, y2=y, layer=Layer.F_CU, width=0.2, net=net)],
        vias=[Via(x=x, y=y, drill=0.2, diameter=0.5, layers=(Layer.F_CU, Layer.B_CU), net=net)],
    )


def _trace_over(net: int, name: str, x: float, y: float) -> Route:
    """A routed B.Cu trace of another net straight across ``(x, y)``."""
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(x1=x, y1=y - 3.0, x2=x, y2=y + 3.0, layer=Layer.B_CU, width=0.2, net=net)
        ],
        vias=[],
    )


def _rerouted(net: int, name: str) -> Route:
    """The fresh route of the stub's net, far away from the stub."""
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(x1=40.0, y1=40.0, x2=45.0, y2=40.0, layer=Layer.F_CU, width=0.2, net=net)
        ],
        vias=[],
    )


def _shorts_against(router: Autorouter, obstacle_net: int) -> list:
    return [
        v
        for v in shorting_violations(validate_routes(router, audit_kept_copper=True))
        if v.obstacle_net == obstacle_net or v.net == obstacle_net
    ]


class TestDroppedStubIsNotAnObstacle:
    def test_rerouted_nets_stale_stub_via_is_not_a_short(self) -> None:
        """Before finalize: a re-routed net's input stub is not kept copper."""
        router = _router()
        router.existing_routes.append(_stub(9, "DQ1", 20.0, 20.0))
        router.routes.append(_rerouted(9, "DQ1"))
        router.routes.append(_trace_over(8, "DQ0", 20.0, 20.0))

        assert _shorts_against(router, 9) == [], (
            "DQ1 was re-routed, so its input stub is dropped on write; a trace "
            "over the stub's via is not a short in the output."
        )

    def test_unemitted_stub_via_is_not_a_short_after_finalize(self) -> None:
        """After finalize: only ``_emitted_preserved_routes`` is kept copper."""
        router = _router()
        router.existing_routes.append(_stub(9, "DQ1", 20.0, 20.0))
        router.routes.append(_trace_over(8, "DQ0", 20.0, 20.0))
        router._emitted_preserved_routes = []

        assert _shorts_against(router, 9) == []


class TestWrittenKeptCopperIsStillAnObstacle:
    def test_kept_stub_via_still_shorts(self) -> None:
        """A stub of a net the run did NOT re-route is written, so it shorts."""
        router = _router()
        router.existing_routes.append(_stub(9, "DQ1", 20.0, 20.0))
        router.routes.append(_trace_over(8, "DQ0", 20.0, 20.0))

        shorts = _shorts_against(router, 9)
        assert shorts, "a trace across a written foreign via must still be a short"
        assert any(v.obstacle_type == "via" for v in shorts)

    def test_emitted_stub_via_still_shorts(self) -> None:
        router = _router()
        stub = _stub(9, "DQ1", 20.0, 20.0)
        router.existing_routes.append(stub)
        router.routes.append(_rerouted(9, "DQ1"))
        router.routes.append(_trace_over(8, "DQ0", 20.0, 20.0))
        # The writer kept it (e.g. a stub-terminal net, #4170).
        router._emitted_preserved_routes = [stub]

        assert any(v.obstacle_type == "via" for v in _shorts_against(router, 9))

    def test_kept_stub_trace_still_shorts(self) -> None:
        """Segment-to-kept-segment quadrant (#5862) keeps working."""
        router = _router()
        router.existing_routes.append(_stub(9, "DQ1", 20.0, 20.0))
        router.routes.append(
            Route(
                net=8,
                net_name="DQ0",
                segments=[
                    Segment(x1=19.0, y1=17.0, x2=19.0, y2=23.0, layer=Layer.F_CU, width=0.2, net=8)
                ],
                vias=[],
            )
        )
        assert any(v.obstacle_type == "segment" for v in _shorts_against(router, 9))

    def test_routed_via_on_kept_via_still_shorts(self) -> None:
        """Via-to-kept-via quadrant keeps working."""
        router = _router()
        router.existing_routes.append(_stub(9, "DQ1", 20.0, 20.0))
        router.routes.append(
            Route(
                net=8,
                net_name="DQ0",
                segments=[],
                vias=[
                    Via(
                        x=20.2,
                        y=20.0,
                        drill=0.2,
                        diameter=0.5,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=8,
                    )
                ],
            )
        )
        assert any(v.obstacle_type == "via" for v in _shorts_against(router, 9))
