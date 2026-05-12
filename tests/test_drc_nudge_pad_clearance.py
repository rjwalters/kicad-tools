"""Tests for skipped-pour-net pad clearance violations (Issue #2757).

When ``load_pcb_for_routing`` is given a ``skip_nets`` list (e.g.
``["GND", "+3V3"]`` for plane nets handled by zone pours), it rewrites
matching pads' ``net`` field to 0 so the autorouter does not try to route
them.  But the pads themselves are still real copper that routed traces
must keep clearance from.

Before #2757:
* ``validate_routes`` skipped every ``pad.net == 0`` pad, so post-route
  clearance violations against e.g. BGA GND pads were invisible to the
  in-router DRC pass.
* ``GridCollisionChecker`` / ``VectorCollisionChecker.path_is_clear`` did
  not block the trace optimiser from chamfering corners THROUGH those
  pads' metal cells, since the cells had ``pad_blocked=True`` but
  ``is_obstacle=False`` and ``cell.net=0``.

These tests cover the fixes:
1. ``validate_routes`` emits ``ClearanceViolation`` for skipped-pour-net
   pads (``pad.net == 0`` with non-empty ``net_name``).
2. The new violations are NOT ``component_inherent`` -- they flow through
   ``drc_verify_and_nudge`` and can be repaired.
3. ``GridCollisionChecker.path_is_clear`` treats pad-metal cells as hard
   obstacles regardless of ``cell.net``, with an exclude_net escape for
   the route's own destination pad.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import (
    GridCollisionChecker,
    OptimizationConfig,
    TraceOptimizer,
)
from kicad_tools.router.optimizer.collision import VectorCollisionChecker
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


def _make_router_with_skipped_pour_pad() -> Autorouter:
    """Build a tiny router with one signal net and one GND pad as obstacle.

    Layout (mm):

        signal_pad (1, 1) ----[route on F.Cu]----> (8, 1)  signal_pad
                                                     ^
                                                     |
                                          GND pad at (5, 1.3)
                                          net=0, net_name="GND"

    The trace is 0.2mm wide; the GND pad is 1.0x1.0 (radius 0.5).
    Trace center to pad center distance = 0.3mm in Y.  Edge-to-edge:
    0.3 - 0.5 (pad half) - 0.1 (trace half) = -0.3mm (overlap).
    With trace_clearance=0.2 this is a -0.5mm clearance violation.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        grid_resolution=0.05,
    )
    router = Autorouter(width=20, height=10, rules=rules)

    # Signal endpoints on net 1
    router.add_component(
        "U1",
        [
            {"number": "1", "x": 1.0, "y": 1.0, "width": 0.5, "height": 0.5,
             "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
            {"number": "2", "x": 8.0, "y": 1.0, "width": 0.5, "height": 0.5,
             "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
        ],
    )
    router.net_names[1] = "SIG"
    # Skipped-pour-net obstacle: net=0 but net_name="GND"
    router.add_component(
        "U2",
        [
            {"number": "1", "x": 5.0, "y": 1.3, "width": 1.0, "height": 1.0,
             "net": 0, "net_name": "GND", "layer": Layer.F_CU},
        ],
    )

    # Manually add a trace that grazes the GND pad.
    seg = Segment(
        x1=1.0, y1=1.0, x2=8.0, y2=1.0,
        layer=Layer.F_CU, width=0.2, net=1, net_name="SIG",
    )
    router.routes.append(Route(net=1, net_name="SIG", segments=[seg], vias=[]))
    return router


# ---------------------------------------------------------------------------
# validate_routes: detection of skipped-pour-net pad violations
# ---------------------------------------------------------------------------


class TestValidateRoutesSkippedPourPad:
    """validate_routes must detect pad violations where pad.net == 0 but
    net_name is non-empty (skipped pour net pads -- Issue #2757)."""

    def test_emits_violation_for_skipped_gnd_pad(self):
        """Trace grazing a GND pad (net=0, net_name=GND) is reported."""
        router = _make_router_with_skipped_pour_pad()

        violations = validate_routes(router)
        pad_viols = [v for v in violations if v.obstacle_type == "pad"]

        assert len(pad_viols) == 1
        v = pad_viols[0]
        assert v.net == 1
        assert v.obstacle_net == 0
        assert v.obstacle_net_name == "GND"  # Named obstacle, not "Net 0"
        # Trace at y=1.0, pad at y=1.3, pad radius=0.5, trace half-width=0.1
        # Center distance = 0.3, edge-to-edge = 0.3 - 0.5 - 0.1 = -0.3
        assert v.distance == pytest.approx(-0.3, abs=1e-3)
        assert v.location == (5.0, 1.3)

    def test_does_not_emit_for_truly_unconnected_pad(self):
        """Pad with net=0 AND no net_name is genuinely unconnected -- skip."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.05)
        router = Autorouter(width=20, height=10, rules=rules)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
                {"number": "2", "x": 8.0, "y": 1.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
            ],
        )
        router.net_names[1] = "SIG"
        # Truly unconnected pad: net=0, net_name=""
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 5.0, "y": 1.3, "width": 1.0, "height": 1.0,
                 "net": 0, "net_name": "", "layer": Layer.F_CU},
            ],
        )
        seg = Segment(x1=1.0, y1=1.0, x2=8.0, y2=1.0,
                      layer=Layer.F_CU, width=0.2, net=1, net_name="SIG")
        router.routes.append(Route(net=1, net_name="SIG", segments=[seg], vias=[]))

        violations = validate_routes(router)
        pad_viols = [v for v in violations if v.obstacle_type == "pad"]
        assert len(pad_viols) == 0, (
            "Truly unconnected pads (no net_name) must not generate violations"
        )

    def test_skipped_pour_pad_not_component_inherent(self):
        """Skipped-pour-net pads on the route's component are NOT inherent.

        The route's signal pads share component U1; the GND pad is on U2
        in this fixture so neither code path collides.  Here we extend the
        fixture so the GND pad is on U1 (same component as the route's
        endpoints) and verify ``component_inherent=False`` so
        ``drc_verify_and_nudge`` will still try to repair it.
        """
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.05)
        router = Autorouter(width=20, height=10, rules=rules)

        # Both signal pads AND the GND pad live on the SAME component U1
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
                {"number": "2", "x": 8.0, "y": 1.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
                {"number": "3", "x": 5.0, "y": 1.3, "width": 1.0, "height": 1.0,
                 "net": 0, "net_name": "GND", "layer": Layer.F_CU},
            ],
        )
        router.net_names[1] = "SIG"
        seg = Segment(x1=1.0, y1=1.0, x2=8.0, y2=1.0,
                      layer=Layer.F_CU, width=0.2, net=1, net_name="SIG")
        router.routes.append(Route(net=1, net_name="SIG", segments=[seg], vias=[]))

        violations = validate_routes(router)
        pad_viols = [v for v in violations if v.obstacle_type == "pad"]
        assert len(pad_viols) == 1
        # Even though the GND pad is on the same component, it's a cross-net
        # routing-clearance defect -- nudgeable.
        assert pad_viols[0].component_inherent is False, (
            "Skipped-pour-net pad on same component must be non-inherent "
            "so drc_verify_and_nudge attempts repair"
        )

    def test_no_violation_when_route_clears_pad(self):
        """A trace that keeps clearance from the GND pad emits no violation."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.05)
        router = Autorouter(width=20, height=10, rules=rules)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
                {"number": "2", "x": 8.0, "y": 1.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
            ],
        )
        router.net_names[1] = "SIG"
        # GND pad far from trace
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 5.0, "y": 5.0, "width": 1.0, "height": 1.0,
                 "net": 0, "net_name": "GND", "layer": Layer.F_CU},
            ],
        )
        seg = Segment(x1=1.0, y1=1.0, x2=8.0, y2=1.0,
                      layer=Layer.F_CU, width=0.2, net=1, net_name="SIG")
        router.routes.append(Route(net=1, net_name="SIG", segments=[seg], vias=[]))

        violations = validate_routes(router)
        pad_viols = [v for v in violations if v.obstacle_type == "pad"]
        assert len(pad_viols) == 0


# ---------------------------------------------------------------------------
# Collision checker: pad-metal cells block paths regardless of net
# ---------------------------------------------------------------------------


class TestCollisionCheckerPadBlocked:
    """The trace optimiser must treat pad-metal cells as hard obstacles even
    when the pad's net is 0 (skipped pour) -- Issue #2757."""

    def _build_router_with_gnd_pad(self) -> Autorouter:
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            grid_resolution=0.05,
        )
        router = Autorouter(width=20, height=10, rules=rules)
        # GND pad at center; trace would walk through it
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 5.0, "y": 1.0, "width": 1.0, "height": 1.0,
                 "net": 0, "net_name": "GND", "layer": Layer.F_CU},
            ],
        )
        return router

    def test_grid_collision_blocks_path_through_gnd_pad(self):
        """``GridCollisionChecker.path_is_clear`` must return False when the
        path crosses a GND pad's metal."""
        router = self._build_router_with_gnd_pad()
        checker = GridCollisionChecker(router.grid)

        # Path that walks through GND pad center
        clear = checker.path_is_clear(
            x1=1.0, y1=1.0, x2=9.0, y2=1.0,
            layer=Layer.F_CU, width=0.2, exclude_net=1,  # our route is net 1
        )
        assert clear is False, (
            "Path through GND pad metal must be blocked even though "
            "pad.net == 0 (Issue #2757)"
        )

    def test_grid_collision_allows_path_clear_of_gnd_pad(self):
        """``GridCollisionChecker.path_is_clear`` must return True when the
        path is well clear of the GND pad."""
        router = self._build_router_with_gnd_pad()
        checker = GridCollisionChecker(router.grid)

        # Path well above the pad (Y=4 vs pad at Y=1, pad half=0.5, clearance=0.2)
        clear = checker.path_is_clear(
            x1=1.0, y1=4.0, x2=9.0, y2=4.0,
            layer=Layer.F_CU, width=0.2, exclude_net=1,
        )
        assert clear is True

    def test_grid_collision_allows_own_net_path_through_destination_pad(self):
        """When the path's exclude_net matches a real signal pad's net, the
        pad's metal cells should NOT block the path (route terminates there).
        This is the safety net for normal own-pad anchoring."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.05)
        router = Autorouter(width=20, height=10, rules=rules)
        # Signal pad on net 1 -- the trace should be allowed to walk into it
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 1.0, "y": 1.0, "width": 1.0, "height": 1.0,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
                {"number": "2", "x": 5.0, "y": 1.0, "width": 1.0, "height": 1.0,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
            ],
        )
        checker = GridCollisionChecker(router.grid)
        clear = checker.path_is_clear(
            x1=1.0, y1=1.0, x2=5.0, y2=1.0,
            layer=Layer.F_CU, width=0.2, exclude_net=1,
        )
        assert clear is True, (
            "Own-net path through own-net pad metal must remain clear"
        )

    def test_vector_collision_blocks_path_through_gnd_pad(self):
        """``VectorCollisionChecker._check_obstacles_clear`` must also block
        paths through GND pad cells."""
        router = self._build_router_with_gnd_pad()
        checker = VectorCollisionChecker(router.grid)

        clear = checker.path_is_clear(
            x1=1.0, y1=1.0, x2=9.0, y2=1.0,
            layer=Layer.F_CU, width=0.2, exclude_net=1,
        )
        assert clear is False


# ---------------------------------------------------------------------------
# Trace optimiser regression: pad violations not introduced post-optimise
# ---------------------------------------------------------------------------


class TestOptimizerDoesNotChamferThroughGndPad:
    """Issue #2757: ``TraceOptimizer.convert_corners_45`` could chamfer a
    90-degree corner into a diagonal that passed THROUGH a GND pad's metal
    because ``GridCollisionChecker.path_is_clear`` did not recognise the
    pad cells as hard obstacles (``cell.net == 0``, ``is_obstacle == False``)."""

    def test_chamfer_blocked_by_gnd_pad(self):
        """Build a 4-segment L-shaped route around a GND pad such that
        chamfering the corner would diagonal-cut THROUGH the GND pad.
        The optimiser must reject the chamfer."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.05,
        )
        router = Autorouter(width=20, height=20, rules=rules)
        # Signal endpoints
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 2.0, "y": 2.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
                {"number": "2", "x": 10.0, "y": 10.0, "width": 0.5, "height": 0.5,
                 "net": 1, "net_name": "SIG", "layer": Layer.F_CU},
            ],
        )
        router.net_names[1] = "SIG"
        # GND pad squarely between the two endpoints diagonally
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 6.0, "y": 6.0, "width": 1.0, "height": 1.0,
                 "net": 0, "net_name": "GND", "layer": Layer.F_CU},
            ],
        )

        # L-shaped route that goes around the GND pad with 90-deg corners.
        # If chamfered, the diagonal would cut through (6, 6).
        segs = [
            Segment(x1=2.0, y1=2.0, x2=2.0, y2=10.0,
                    layer=Layer.F_CU, width=0.2, net=1, net_name="SIG"),
            Segment(x1=2.0, y1=10.0, x2=10.0, y2=10.0,
                    layer=Layer.F_CU, width=0.2, net=1, net_name="SIG"),
        ]
        route = Route(net=1, net_name="SIG", segments=segs, vias=[])
        router.routes.append(route)

        # Run trace optimiser with collision checker
        config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            minimize_vias=True,
        )
        checker = GridCollisionChecker(router.grid)
        optimizer = TraceOptimizer(config=config, collision_checker=checker)
        optimized = optimizer.optimize_route(route)

        # The optimised route must not contain a segment whose path
        # passes through the GND pad metal.  We check by walking each
        # segment and computing point-to-segment distance from the pad.
        from kicad_tools.router.geometry import point_to_segment_distance
        for seg in optimized.segments:
            d = point_to_segment_distance(6.0, 6.0, seg.x1, seg.y1, seg.x2, seg.y2)
            # Pad half-width + trace half-width = 0.5 + 0.1 = 0.6
            assert d > 0.6 - 1e-4, (
                f"Optimised segment ({seg.x1},{seg.y1})->({seg.x2},{seg.y2}) "
                f"crosses or grazes GND pad metal: d={d:.4f} mm"
            )
