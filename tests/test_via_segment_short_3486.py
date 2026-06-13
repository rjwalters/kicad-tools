"""Issue #3486: via-vs-committed-foreign-segment clearance finalization gate.

A through-via barrel is physical copper on EVERY layer it spans (#3487),
so a via placed within ``via_radius + seg.width/2 + via_clearance`` of a
foreign-net trace centreline is a cross-net SHORT -- and the same-net
connectivity audit cannot see it (it unions same-net copper only).

The negotiated loop's post-iteration
``NegotiatedRouter.find_nets_with_via_segment_violations`` hook (#3020)
feeds such violators back into the rip-up cohort BETWEEN iterations, but
when the loop EXITS (converged / stagnated / timed out) still holding a
residual violation, nothing strips it -- it ships uncaught.  The measured
softstart artifact (PR #3481): a SRC_NEG via landed 0.40 mm from a
UCC_LO_NEG trace centreline where 0.552 mm was required -- the copper
edges OVERLAP by 0.05 mm.

These tests pin:

* ``RoutingGrid.worst_via_segment_deficit`` exact geometry (the STANDARD
  ``via_clears_foreign_segment`` threshold), including same-net exclusion
  and the layer-span gate (a barrel only conflicts on layers it spans),
* the finalization backstop
  ``Autorouter._demote_via_segment_violation_nets`` demotes a net whose
  committed via shorts a foreign trace, and leaves clean nets alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router import DesignRules, load_pcb_for_routing
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment, Via

FIXTURE = Path(__file__).parent / "fixtures" / "routing-diagnostic.kicad_pcb"


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.3,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.2,
        via_clearance=0.2,
    )


@pytest.fixture
def grid(rules: DesignRules) -> RoutingGrid:
    # 20mm x 20mm board at 0.2mm resolution.
    return RoutingGrid(100, 100, rules, origin_x=0.0, origin_y=0.0)


@pytest.fixture
def grid_4l(rules: DesignRules) -> RoutingGrid:
    """4-layer all-signal grid so the barrel-span tests can place copper
    on inner layers (the 2-layer default has only F.Cu / B.Cu)."""
    return RoutingGrid(
        100,
        100,
        rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.four_layer_all_signal(),
    )


def _foreign_segment_route(
    net: int,
    layer: Layer,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float = 0.3,
) -> Route:
    r = Route(net=net, net_name=f"NET{net}")
    r.segments.append(
        Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net)
    )
    return r


def _via(x: float, y: float, net: int, layers: tuple[Layer, Layer]) -> Via:
    return Via(
        x=x,
        y=y,
        drill=0.3,
        diameter=0.6,
        layers=layers,
        net=net,
        net_name=f"NET{net}",
    )


class TestWorstViaSegmentDeficitGeometry:
    """Exact STANDARD-threshold geometry for the via-vs-segment quadrant."""

    def test_overlapping_via_reports_deficit(self, grid: RoutingGrid) -> None:
        """The softstart geometry: via 0.40mm from a 0.3mm-wide trace.

        required center distance = via_radius (0.3) + seg_half (0.15) +
        via_clearance (0.2) = 0.65mm.  At 0.40mm the edges overlap.
        clearance = 0.40 - 0.3 - 0.15 = -0.05mm; deficit = 0.2 - (-0.05)
        = 0.25mm.
        """
        foreign = _foreign_segment_route(
            net=1, layer=Layer.F_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        grid.mark_route(foreign)

        via = _via(7.0, 12.4, net=3, layers=(Layer.F_CU, Layer.B_CU))
        deficit, loc = grid.worst_via_segment_deficit(via, exclude_net=3)

        assert deficit == pytest.approx(0.25, abs=1e-6)
        assert loc == (7.0, 12.4)

    def test_clean_via_has_no_deficit(self, grid: RoutingGrid) -> None:
        foreign = _foreign_segment_route(
            net=1, layer=Layer.F_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        grid.mark_route(foreign)

        # 2mm away from the trace -- comfortably clear.
        via = _via(7.0, 14.0, net=3, layers=(Layer.F_CU, Layer.B_CU))
        deficit, loc = grid.worst_via_segment_deficit(via, exclude_net=3)

        assert deficit == 0.0
        assert loc is None

    def test_same_net_segment_excluded(self, grid: RoutingGrid) -> None:
        """A via tight against its OWN net's trace never registers."""
        own = _foreign_segment_route(
            net=3, layer=Layer.F_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        grid.mark_route(own)

        via = _via(7.0, 12.4, net=3, layers=(Layer.F_CU, Layer.B_CU))
        deficit, _loc = grid.worst_via_segment_deficit(via, exclude_net=3)

        assert deficit == 0.0

    def test_segment_on_layer_outside_barrel_span_ignored(
        self, grid_4l: RoutingGrid
    ) -> None:
        """A barrel that does not reach the segment's layer cannot short it.

        A blind via spanning only F.Cu..In1.Cu must not flag an
        overlapping In2.Cu trace.
        """
        # Overlapping trace on In2.Cu.
        foreign = _foreign_segment_route(
            net=1, layer=Layer.IN2_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        grid_4l.mark_route(foreign)

        # Barrel spans F.Cu..In1.Cu only -- does not reach In2.Cu.
        via = _via(7.0, 12.4, net=3, layers=(Layer.F_CU, Layer.IN1_CU))
        deficit, loc = grid_4l.worst_via_segment_deficit(via, exclude_net=3)

        assert deficit == 0.0
        assert loc is None

    def test_inner_layer_short_detected(self, grid_4l: RoutingGrid) -> None:
        """A through-via barrel is copper on inner layers too (#3487)."""
        foreign = _foreign_segment_route(
            net=1, layer=Layer.IN1_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        grid_4l.mark_route(foreign)

        # Through via spans F.Cu..B.Cu -- includes In1.Cu.
        via = _via(7.0, 12.4, net=3, layers=(Layer.F_CU, Layer.B_CU))
        deficit, loc = grid_4l.worst_via_segment_deficit(via, exclude_net=3)

        assert deficit == pytest.approx(0.25, abs=1e-6)
        assert loc == (7.0, 12.4)


class TestViaSegmentDemotionBackstop:
    """``_demote_via_segment_violation_nets`` strips via-vs-foreign-segment
    shorts at finalization, never shipping them as physical shorts."""

    def test_shorting_via_net_is_demoted(self, rules: DesignRules) -> None:
        """A committed via overlapping a foreign trace strips the net."""
        router, _ = load_pcb_for_routing(str(FIXTURE), rules=rules, validate_drc=False)

        # Foreign-net (NET1) trace on F.Cu.
        foreign = _foreign_segment_route(
            net=1, layer=Layer.F_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        router.grid.mark_route(foreign)
        router.routes.append(foreign)

        # NET3 via centered 0.40mm from the trace -> -0.05mm overlap,
        # deficit 0.25mm, far beyond the 0.2mm grid-resolution nudge reach.
        bad = Route(net=3, net_name="NET3")
        bad.vias.append(_via(7.0, 12.4, net=3, layers=(Layer.F_CU, Layer.B_CU)))
        router.grid.mark_route(bad)
        router.routes.append(bad)
        net_routes = {1: [foreign], 3: [bad]}

        demoted = router._demote_via_segment_violation_nets(net_routes)

        assert demoted == [3]
        assert net_routes[3] == []
        assert bad not in router.routes
        # The foreign (segment-owning) net is NOT the victim per #3020.
        assert net_routes[1] == [foreign]
        assert foreign in router.routes

    def test_clean_via_route_is_not_demoted(self, rules: DesignRules) -> None:
        router, _ = load_pcb_for_routing(str(FIXTURE), rules=rules, validate_drc=False)

        foreign = _foreign_segment_route(
            net=1, layer=Layer.F_CU, x1=5.0, y1=12.0, x2=9.0, y2=12.0
        )
        router.grid.mark_route(foreign)
        router.routes.append(foreign)

        # NET3 via 2mm clear of the trace.
        good = Route(net=3, net_name="NET3")
        good.vias.append(_via(7.0, 14.0, net=3, layers=(Layer.F_CU, Layer.B_CU)))
        router.grid.mark_route(good)
        router.routes.append(good)
        net_routes = {1: [foreign], 3: [good]}

        demoted = router._demote_via_segment_violation_nets(net_routes)

        assert demoted == []
        assert net_routes[3] == [good]
        assert good in router.routes
