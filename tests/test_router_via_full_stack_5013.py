"""Regression tests for Issue #5013.

Router counterpart of stitch issue #5001: an ordinary multilayer route
transition (e.g. F.Cu -> In2.Cu on a 4-layer board) is a LOGICAL search
transition, not the via's physical drilled span. No blind/buried process
is ever selected by the current router (Issue #4007:
``blind_buried_supported`` is False for every board today), so every via
these codepaths construct is manufactured as an ordinary through-hole
whose barrel spans the FULL physical copper stack -- it must be reported
as such regardless of which two layers the search happened to bridge.

Covers both routing backends (``CppPathfinder`` and the Python
``pathfinder.Router``), the shared ``Route.validate_layer_transitions``
safety net, and the downstream DRC consumer (``via_spans_layer``) that
depends on the reported span to catch foreign copper on an intermediate
layer the via's barrel physically passes through.
"""

from __future__ import annotations

import pytest

from kicad_tools.core.layers import via_spans_layer
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Obstacle, Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


def _route_forced_inner_crossing(backend: str):
    """Route a net across a wall that blocks BOTH outer layers.

    Mirrors the wall-obstacle recipe in
    ``TestPerNetAvoidLayersHardConstraint._route_hv_net``
    (tests/test_router_core.py, Issue #4433): the only continuous path
    across the wall is an inner-layer via, which is exactly the ordinary
    multilayer transition this issue is about.
    """
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available

    if backend == "cpp" and not is_cpp_available():
        pytest.skip("C++ router backend not available")

    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=20.0,
        height=10.0,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    start = Pad(
        x=3.0,
        y=5.0,
        width=1.0,
        height=1.0,
        net=1,
        net_name="SIG",
        layer=Layer.F_CU,
        ref="J1",
        pin="1",
        through_hole=True,
        drill=0.6,
    )
    end = Pad(
        x=17.0,
        y=5.0,
        width=1.0,
        height=1.0,
        net=1,
        net_name="SIG",
        layer=Layer.F_CU,
        ref="J2",
        pin="1",
        through_hole=True,
        drill=0.6,
    )
    grid.add_pad(start)
    grid.add_pad(end)
    # Seal BOTH outer layers at x=10 -> the only continuous crossing is on
    # an inner layer, forcing at least one F.Cu<->inner via each way.
    grid.add_obstacle(Obstacle(x=10.0, y=5.0, width=2.0, height=16.0, layer=Layer.F_CU))
    grid.add_obstacle(Obstacle(x=10.0, y=5.0, width=2.0, height=16.0, layer=Layer.B_CU))

    if backend == "cpp":
        cpp_grid = CppGrid.from_routing_grid(grid)
        pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    else:
        pf = Router(grid, rules)
    return pf.route(start, end)


class TestRouterViaFullPhysicalStack:
    """Issue #5013: standard vias must report the full physical span."""

    @pytest.mark.parametrize("backend", ["cpp", "python"])
    def test_forced_inner_crossing_emits_full_stack_vias(self, backend):
        """A route forced through an inner layer must report F.Cu/B.Cu vias.

        Pre-fix, the emitted via for an F.Cu -> In*.Cu transition recorded
        only the two logical layers it searched between (e.g.
        ``(F.Cu, In1.Cu)``), under-reporting the drilled span on a
        4-layer board.
        """
        route = _route_forced_inner_crossing(backend)
        assert route is not None, f"[{backend}] forced inner-layer route failed"
        assert route.vias, f"[{backend}] expected at least one via for the forced crossing"

        for via in route.vias:
            assert not via.is_micro, (
                f"[{backend}] unexpected micro via -- this recipe only exercises "
                "ordinary through-hole transitions"
            )
            assert via.layers == (Layer.F_CU, Layer.B_CU), (
                f"[{backend}] via at ({via.x}, {via.y}) reported partial-stack "
                f"span {[layer.kicad_name for layer in via.layers]} instead of "
                "the full physical F.Cu/B.Cu stack (Issue #5013)"
            )

    def test_both_backends_agree_on_full_stack_span(self):
        """Parity: C++ and Python backends must report the same physical span."""
        from kicad_tools.router.cpp_backend import is_cpp_available

        if not is_cpp_available():
            pytest.skip("C++ router backend not available")

        cpp_route = _route_forced_inner_crossing("cpp")
        py_route = _route_forced_inner_crossing("python")
        assert cpp_route is not None and cpp_route.vias
        assert py_route is not None and py_route.vias
        for via in (*cpp_route.vias, *py_route.vias):
            assert via.layers == (Layer.F_CU, Layer.B_CU)


class TestValidateLayerTransitionsFullStack:
    """Issue #5013: the shared missing-via safety net must also normalize."""

    def test_inserted_via_spans_full_physical_stack(self):
        """A defensively-inserted via must not under-report the drilled span.

        ``Route.validate_layer_transitions`` inserts a via when two
        consecutive segments change layer without one already present.
        This codepath has no blind/buried process selection either, so
        the inserted via must be a full through-hole span, not the bare
        ``seg1.layer``/``seg2.layer`` pair.
        """
        route = Route(net=1, net_name="SIG")
        route.segments = [
            Segment(
                x1=0.0, y1=0.0, x2=5.0, y2=0.0, width=0.2, layer=Layer.IN1_CU, net=1, net_name="SIG"
            ),
            Segment(
                x1=5.0,
                y1=0.0,
                x2=10.0,
                y2=0.0,
                width=0.2,
                layer=Layer.IN2_CU,
                net=1,
                net_name="SIG",
            ),
        ]
        inserted = route.validate_layer_transitions()
        assert inserted == 1
        assert len(route.vias) == 1
        assert route.vias[0].layers == (Layer.F_CU, Layer.B_CU)


class TestConvertPathToRouteFullStack:
    """Direct unit coverage of ``Router._convert_path_to_route`` (Issue #5013)."""

    def test_inner_to_inner_transition_normalizes_to_full_stack(self):
        """An In1.Cu -> In2.Cu search transition must emit an F.Cu/B.Cu via.

        This is the sharpest reproduction of the reported defect: a
        transition between two INNER layers (neither is F.Cu or B.Cu) is
        the case most likely to under-report the drilled span if the fix
        regresses to echoing the logical pair.
        """
        rules = DesignRules(trace_width=0.25, via_diameter=0.6, via_drill=0.3)
        grid = RoutingGrid(
            width=10.0, height=10.0, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
        )
        router = Router(grid, rules)
        start_pad = Pad(
            x=0.0,
            y=0.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="SIG",
            layer=Layer.IN1_CU,
            ref="J1",
            pin="1",
        )
        end_pad = Pad(
            x=5.0,
            y=0.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="SIG",
            layer=Layer.IN2_CU,
            ref="J2",
            pin="1",
        )
        route = Route(net=1, net_name="SIG")
        # Synthetic A* path: two cells on In1.Cu (grid index 1), a via to
        # In2.Cu (grid index 2), then two cells on In2.Cu.
        path = [
            (0.0, 0.0, 1, False),
            (2.0, 0.0, 1, False),
            (2.0, 0.0, 2, True),
            (5.0, 0.0, 2, False),
        ]
        router._convert_path_to_route(path, route, start_pad, end_pad)

        assert len(route.vias) == 1
        via = route.vias[0]
        assert not via.is_micro
        assert via.layers == (Layer.F_CU, Layer.B_CU), (
            f"via reported partial-stack span {[layer.kicad_name for layer in via.layers]} "
            "for an In1.Cu -> In2.Cu transition (Issue #5013)"
        )


class TestViaSpansLayerDetectsIntermediateForeignCopper:
    """Issue #5013 acceptance criterion: foreign copper on an intermediate,
    non-contact layer must be visible to DRC once the physical span is
    reported correctly.
    """

    def test_partial_span_hides_foreign_copper_on_skipped_layer(self):
        """Documents the historical gap the router used to reproduce.

        A via reported with only its two logical endpoints (the pre-fix
        behavior) is blind to a layer its barrel physically passes
        through but that was not named as an endpoint.
        """
        partial_span = ["F.Cu", "In2.Cu"]
        assert via_spans_layer(partial_span, "In1.Cu") is True  # between the endpoints: still seen
        assert via_spans_layer(partial_span, "B.Cu") is False  # BLIND SPOT: real copper, unreported

    def test_full_stack_span_sees_every_physical_layer(self):
        """Post-fix: the router-reported full F.Cu/B.Cu span is transparent
        to DRC on every copper layer the via's barrel actually touches,
        including layers that were not part of the logical search
        transition.
        """
        full_span = [Layer.F_CU.kicad_name, Layer.B_CU.kicad_name]
        assert via_spans_layer(full_span, "In1.Cu") is True
        assert via_spans_layer(full_span, "In2.Cu") is True
        assert via_spans_layer(full_span, "B.Cu") is True
