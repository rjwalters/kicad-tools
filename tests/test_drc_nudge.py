"""Tests for the post-optimization DRC verify-and-nudge pass."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from kicad_tools.router.drc_nudge import (
    COINCIDENT_THRESHOLD,
    DRCNudgeResult,
    _compute_merge_threshold,
    _expand_via_layers,
    _merge_same_net_vias,
    _nudge_segment,
    _nudge_segment_with_chain,
    _perpendicular_unit,
    _reconnect_segments,
    _segment_endpoints_anchored_to_net_pads,
    _segment_endpoints_anchored_to_net_vias,
    _segment_length,
    drc_verify_and_nudge,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


# ---------------------------------------------------------------------------
# Lightweight stub for Autorouter -- just the attributes drc_nudge touches.
# ---------------------------------------------------------------------------

@dataclass
class _StubAutorouter:
    """Minimal stand-in for Autorouter used by drc_nudge."""

    routes: list[Route] = field(default_factory=list)
    existing_routes: list[Route] = field(default_factory=list)
    rules: DesignRules = field(default_factory=DesignRules)
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    net_names: dict = field(default_factory=dict)
    net_class_map: dict | None = None


# ---------------------------------------------------------------------------
# Geometry helper tests
# ---------------------------------------------------------------------------

class TestSegmentLength:
    def test_horizontal(self):
        seg = Segment(x1=0, y1=0, x2=3, y2=0, width=0.2, layer=Layer.F_CU)
        assert math.isclose(_segment_length(seg), 3.0)

    def test_diagonal(self):
        seg = Segment(x1=0, y1=0, x2=3, y2=4, width=0.2, layer=Layer.F_CU)
        assert math.isclose(_segment_length(seg), 5.0)

    def test_zero_length(self):
        seg = Segment(x1=1, y1=1, x2=1, y2=1, width=0.2, layer=Layer.F_CU)
        assert _segment_length(seg) == 0.0


class TestPerpendicularUnit:
    def test_horizontal(self):
        seg = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU)
        nx, ny = _perpendicular_unit(seg)
        assert math.isclose(nx, 0.0, abs_tol=1e-9)
        assert math.isclose(abs(ny), 1.0)

    def test_vertical(self):
        seg = Segment(x1=0, y1=0, x2=0, y2=5, width=0.2, layer=Layer.F_CU)
        nx, ny = _perpendicular_unit(seg)
        assert math.isclose(abs(nx), 1.0)
        assert math.isclose(ny, 0.0, abs_tol=1e-9)

    def test_zero_length_returns_zero(self):
        seg = Segment(x1=1, y1=1, x2=1, y2=1, width=0.2, layer=Layer.F_CU)
        assert _perpendicular_unit(seg) == (0.0, 0.0)


class TestNudgeSegment:
    def test_nudge_up(self):
        seg = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU)
        _nudge_segment(seg, 0.0, 1.0, 0.1)
        assert math.isclose(seg.y1, 0.1)
        assert math.isclose(seg.y2, 0.1)
        assert math.isclose(seg.x1, 0.0)
        assert math.isclose(seg.x2, 5.0)

    def test_nudge_diagonal(self):
        seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU)
        _nudge_segment(seg, 1.0, 1.0, 0.1)
        assert math.isclose(seg.x1, 0.1)
        assert math.isclose(seg.y1, 0.1)


# ---------------------------------------------------------------------------
# Same-net via merge tests
# ---------------------------------------------------------------------------

class TestMergeSameNetVias:
    def test_coincident_vias_merged(self):
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7, layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=10.005, y=10.005, drill=0.35, diameter=0.7, layers=(Layer.F_CU, Layer.B_CU), net=1)
        seg = Segment(x1=5.0, y1=5.0, x2=10.005, y2=10.005, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="Net1", segments=[seg], vias=[via_a, via_b])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 1
        assert len(route.vias) == 1
        # Segment endpoint should have been reconnected to via_a
        assert math.isclose(seg.x2, 10.0)
        assert math.isclose(seg.y2, 10.0)

    def test_distant_vias_not_merged(self):
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7, layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=15.0, y=15.0, drill=0.35, diameter=0.7, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[], vias=[via_a, via_b])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 0
        assert len(route.vias) == 2

    def test_single_via_no_merge(self):
        via = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 0


class TestReconnectSegments:
    def test_start_endpoint_reconnected(self):
        seg = Segment(x1=10.005, y1=10.005, x2=20.0, y2=20.0, width=0.2, layer=Layer.F_CU)
        _reconnect_segments([seg], 10.005, 10.005, 10.0, 10.0)
        assert math.isclose(seg.x1, 10.0)
        assert math.isclose(seg.y1, 10.0)
        # End should be untouched
        assert math.isclose(seg.x2, 20.0)

    def test_end_endpoint_reconnected(self):
        seg = Segment(x1=5.0, y1=5.0, x2=10.005, y2=10.005, width=0.2, layer=Layer.F_CU)
        _reconnect_segments([seg], 10.005, 10.005, 10.0, 10.0)
        assert math.isclose(seg.x2, 10.0)
        assert math.isclose(seg.y2, 10.0)


class TestComputeMergeThreshold:
    def test_uses_design_rules(self):
        """Merge threshold should be via_diameter + min_drill_clearance."""
        rules = DesignRules(via_diameter=0.7, min_drill_clearance=0.102)
        router = _StubAutorouter(rules=rules)
        threshold = _compute_merge_threshold(router)
        assert math.isclose(threshold, 0.802)

    def test_falls_back_to_coincident(self):
        """When rules are missing, use COINCIDENT_THRESHOLD."""
        router = _StubAutorouter()
        router.rules = None  # type: ignore[assignment]
        threshold = _compute_merge_threshold(router)
        assert threshold == COINCIDENT_THRESHOLD


class TestMergeSameNetViasWithDrillThreshold:
    """Tests for the drill-overlap merge threshold (Issue #1796)."""

    def test_vias_within_drill_overlap_merged(self):
        """Vias within via_diameter + min_drill_clearance should be merged."""
        # Default rules: via_diameter=0.7, min_drill_clearance=0.102
        # merge threshold = 0.802mm
        # Place two vias 0.5mm apart (< 0.802)
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=10.5, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        seg = Segment(x1=5.0, y1=5.0, x2=10.5, y2=10.0,
                      width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="Net1", segments=[seg], vias=[via_a, via_b])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 1
        assert len(route.vias) == 1
        # Segment endpoint reconnected to surviving via_a
        assert math.isclose(seg.x2, 10.0)
        assert math.isclose(seg.y2, 10.0)

    def test_vias_beyond_drill_overlap_not_merged(self):
        """Vias farther than the threshold should NOT be merged."""
        # merge threshold = 0.802mm; place vias 1.0mm apart
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=11.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[], vias=[via_a, via_b])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 0
        assert len(route.vias) == 2


class TestCrossRouteMerge:
    """Tests for cross-route same-net via merging (Issue #1796)."""

    def test_cross_route_vias_merged(self):
        """Vias on different routes of the same net should be merged."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=10.3, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        seg_b = Segment(x1=5.0, y1=5.0, x2=10.3, y2=10.0,
                        width=0.2, layer=Layer.F_CU, net=1)
        route_a = Route(net=1, net_name="Net1", segments=[], vias=[via_a])
        route_b = Route(net=1, net_name="Net1", segments=[seg_b], vias=[via_b])
        router = _StubAutorouter(routes=[route_a, route_b])

        merged = _merge_same_net_vias(router)
        assert merged == 1
        # via_b should have been removed from route_b
        assert len(route_a.vias) == 1
        assert len(route_b.vias) == 0
        # segment in route_b reconnected to via_a position
        assert math.isclose(seg_b.x2, 10.0)
        assert math.isclose(seg_b.y2, 10.0)

    def test_cross_route_different_nets_not_merged(self):
        """Vias on different nets should NOT be merged even if close."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=10.3, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=2)
        route_a = Route(net=1, net_name="Net1", segments=[], vias=[via_a])
        route_b = Route(net=2, net_name="Net2", segments=[], vias=[via_b])
        router = _StubAutorouter(routes=[route_a, route_b])

        merged = _merge_same_net_vias(router)
        assert merged == 0
        assert len(route_a.vias) == 1
        assert len(route_b.vias) == 1

    def test_cross_route_only_one_has_vias(self):
        """No crash when only one route on a net has vias."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        route_a = Route(net=1, net_name="Net1", segments=[], vias=[via_a])
        route_b = Route(net=1, net_name="Net1", segments=[], vias=[])
        router = _StubAutorouter(routes=[route_a, route_b])

        merged = _merge_same_net_vias(router)
        assert merged == 0


# ---------------------------------------------------------------------------
# Cross-layer-pair via merge tests (Issue #1802)
# ---------------------------------------------------------------------------

class TestExpandViaLayers:
    """Unit tests for _expand_via_layers helper."""

    def test_same_layers_no_change(self):
        """Vias with identical layers should not be modified."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=10.3, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        _expand_via_layers(via_a, via_b)
        assert via_a.layers == (Layer.F_CU, Layer.B_CU)

    def test_different_layers_expanded(self):
        """Surviving via should span all layers from both vias."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.IN1_CU), net=1)
        via_b = Via(x=10.3, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.B_CU, Layer.F_CU), net=1)
        _expand_via_layers(via_a, via_b)
        assert via_a.layers[0] == Layer.F_CU
        assert via_a.layers[1] == Layer.B_CU

    def test_inner_layers_expanded(self):
        """Merge of two inner-layer vias expands to cover both spans."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.IN1_CU), net=1)
        via_b = Via(x=10.3, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.IN1_CU, Layer.IN2_CU), net=1)
        _expand_via_layers(via_a, via_b)
        assert via_a.layers[0] == Layer.F_CU
        assert via_a.layers[1] == Layer.IN2_CU


class TestCrossLayerPairMerge:
    """Integration tests for cross-layer-pair via merging (Issue #1802)."""

    def test_intra_route_different_layers_merged(self):
        """Vias within one route with different layer pairs should be merged."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.IN1_CU), net=1)
        via_b = Via(x=10.15, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.B_CU, Layer.F_CU), net=1)
        seg = Segment(x1=5.0, y1=5.0, x2=10.15, y2=10.0,
                      width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="Net1", segments=[seg], vias=[via_a, via_b])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 1
        assert len(route.vias) == 1
        # Surviving via should span F.Cu to B.Cu (through-via)
        assert route.vias[0].layers[0] == Layer.F_CU
        assert route.vias[0].layers[1] == Layer.B_CU

    def test_cross_route_different_layers_merged(self):
        """Cross-route vias with different layer pairs should be merged."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.IN1_CU, Layer.F_CU), net=1)
        via_b = Via(x=10.15, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.B_CU, Layer.F_CU), net=1)
        seg_b = Segment(x1=5.0, y1=5.0, x2=10.15, y2=10.0,
                        width=0.2, layer=Layer.F_CU, net=1)
        route_a = Route(net=1, net_name="Net1", segments=[], vias=[via_a])
        route_b = Route(net=1, net_name="Net1", segments=[seg_b], vias=[via_b])
        router = _StubAutorouter(routes=[route_a, route_b])

        merged = _merge_same_net_vias(router)
        assert merged == 1
        assert len(route_a.vias) == 1
        assert len(route_b.vias) == 0
        # Surviving via should span F.Cu to B.Cu
        assert route_a.vias[0].layers[0] == Layer.F_CU
        assert route_a.vias[0].layers[1] == Layer.B_CU

    def test_same_layer_pair_still_works(self):
        """Same layer pair merges still work as before (no regression)."""
        via_a = Via(x=10.0, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        via_b = Via(x=10.15, y=10.0, drill=0.35, diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[], vias=[via_a, via_b])
        router = _StubAutorouter(routes=[route])

        merged = _merge_same_net_vias(router)
        assert merged == 1
        assert len(route.vias) == 1
        assert route.vias[0].layers == (Layer.F_CU, Layer.B_CU)


# ---------------------------------------------------------------------------
# Full drc_verify_and_nudge integration tests
# ---------------------------------------------------------------------------

class TestDRCVerifyAndNudge:
    def test_no_violations_is_noop(self):
        """When there are no routes, the function returns immediately."""
        router = _StubAutorouter(routes=[])
        result = drc_verify_and_nudge(router)
        assert result.initial_violations == 0
        assert result.remaining_violations == 0
        assert result.segments_nudged == 0
        assert result.vias_merged == 0

    def test_no_violations_with_routes(self):
        """Routes that don't violate clearance should produce a no-op."""
        # Two segments far apart on the same layer, different nets
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_b = Segment(x1=0, y1=5, x2=10, y2=5, width=0.2, layer=Layer.F_CU, net=2)
        route_a = Route(net=1, net_name="A", segments=[seg_a])
        route_b = Route(net=2, net_name="B", segments=[seg_b])
        rules = DesignRules(trace_clearance=0.2, via_clearance=0.2)
        router = _StubAutorouter(routes=[route_a, route_b], rules=rules)

        result = drc_verify_and_nudge(router)
        assert result.initial_violations == 0
        assert result.segments_nudged == 0

    def test_segment_violation_nudged(self):
        """Two segments within clearance on same layer should be nudged apart."""
        # Place two horizontal segments 0.25mm apart (center-to-center),
        # each with width 0.2mm -> edge-to-edge = 0.25 - 0.1 - 0.1 = 0.05mm
        # With clearance=0.2, this is a violation (0.05 < 0.2).
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_b = Segment(x1=0, y1=0.25, x2=10, y2=0.25, width=0.2, layer=Layer.F_CU, net=2)
        route_a = Route(net=1, net_name="A", segments=[seg_a])
        route_b = Route(net=2, net_name="B", segments=[seg_b])
        rules = DesignRules(trace_clearance=0.2, via_clearance=0.2)
        router = _StubAutorouter(routes=[route_a, route_b], rules=rules)

        result = drc_verify_and_nudge(router)
        assert result.initial_violations > 0
        assert result.segments_nudged > 0


    def test_inner_layer_segment_violation_nudged(self):
        """Segments violating clearance on an inner layer should be nudged (Issue #1798)."""
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.IN1_CU, net=1)
        seg_b = Segment(x1=0, y1=0.25, x2=10, y2=0.25, width=0.2, layer=Layer.IN1_CU, net=2)
        route_a = Route(net=1, net_name="A", segments=[seg_a])
        route_b = Route(net=2, net_name="B", segments=[seg_b])
        rules = DesignRules(trace_clearance=0.2, via_clearance=0.2)
        router = _StubAutorouter(routes=[route_a, route_b], rules=rules)

        result = drc_verify_and_nudge(router)
        assert result.initial_violations > 0
        assert result.segments_nudged > 0

    def test_inner_layer_no_cross_layer_violation(self):
        """Segments on different inner layers should not report violations (Issue #1798)."""
        # Two segments at the same position but on different layers
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.IN1_CU, net=1)
        seg_b = Segment(x1=0, y1=0.25, x2=10, y2=0.25, width=0.2, layer=Layer.IN2_CU, net=2)
        route_a = Route(net=1, net_name="A", segments=[seg_a])
        route_b = Route(net=2, net_name="B", segments=[seg_b])
        rules = DesignRules(trace_clearance=0.2, via_clearance=0.2)
        router = _StubAutorouter(routes=[route_a, route_b], rules=rules)

        result = drc_verify_and_nudge(router)
        assert result.initial_violations == 0
        assert result.segments_nudged == 0

    def test_find_segment_matches_correct_layer(self):
        """_find_segment should match the correct layer when specified (Issue #1798)."""
        from kicad_tools.router.drc_nudge import _find_segment

        # Two segments with same coordinates on different layers
        seg_fcu = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_in1 = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.IN1_CU, net=1)
        route = Route(net=1, net_name="A", segments=[seg_fcu, seg_in1])
        router = _StubAutorouter(routes=[route])

        # Without layer filter, should find the first match (F_CU)
        found = _find_segment(router, 1, 0, 0, 0, 10, 0)
        assert found is seg_fcu

        # With layer filter for IN1_CU, should find the inner-layer segment
        found = _find_segment(router, 1, 0, 0, 0, 10, 0, layer=Layer.IN1_CU)
        assert found is seg_in1

        # With layer filter for F_CU, should find the outer-layer segment
        found = _find_segment(router, 1, 0, 0, 0, 10, 0, layer=Layer.F_CU)
        assert found is seg_fcu

    def test_violation_exceeding_budget_not_nudged(self):
        """Violations requiring more than max_displacement should not be nudged."""
        # Segments very close: edge-to-edge ~ -0.1mm (overlap)
        # deficit = 0.2 - (-0.1) = 0.3mm, well over budget of 0.05
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_b = Segment(x1=0, y1=0.1, x2=10, y2=0.1, width=0.2, layer=Layer.F_CU, net=2)
        route_a = Route(net=1, net_name="A", segments=[seg_a])
        route_b = Route(net=2, net_name="B", segments=[seg_b])
        rules = DesignRules(trace_clearance=0.2, via_clearance=0.2)
        router = _StubAutorouter(routes=[route_a, route_b], rules=rules)

        result = drc_verify_and_nudge(router, max_displacement=0.05)
        # With a very tight budget, nudges should be skipped
        assert result.initial_violations > 0

    def test_idempotent_no_violations(self):
        """Running on a board with no violations should produce identical routes."""
        seg = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="A", segments=[seg])
        rules = DesignRules(trace_clearance=0.2, via_clearance=0.2)
        router = _StubAutorouter(routes=[route], rules=rules)

        # Save original coordinates
        orig_x1, orig_y1 = seg.x1, seg.y1
        orig_x2, orig_y2 = seg.x2, seg.y2

        result = drc_verify_and_nudge(router)
        assert result.initial_violations == 0
        # Coordinates unchanged
        assert seg.x1 == orig_x1
        assert seg.y1 == orig_y1
        assert seg.x2 == orig_x2
        assert seg.y2 == orig_y2


class TestDRCNudgeResult:
    def test_summary_no_violations(self):
        result = DRCNudgeResult(initial_violations=0, remaining_violations=0, passes_run=0)
        summary = result.summary()
        assert "0/0" in summary

    def test_summary_with_nudges(self):
        result = DRCNudgeResult(
            initial_violations=5,
            remaining_violations=1,
            segments_nudged=4,
            vias_merged=2,
            passes_run=2,
        )
        summary = result.summary()
        assert "4/5" in summary
        assert "Segments nudged: 4" in summary
        assert "Same-net vias merged: 2" in summary
        assert "Remaining violations: 1" in summary


# ---------------------------------------------------------------------------
# Tests for existing-routes awareness (Issue #1809)
# ---------------------------------------------------------------------------


class TestMergeExistingRouteVias:
    """Merging new vias against pre-existing vias (Phase 3)."""

    def test_new_via_merged_into_existing(self):
        """A new via within threshold of an existing via is removed."""
        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        existing_route = Route(
            net=1, net_name="Net1", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=10.3, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        seg = Segment(
            x1=5.0, y1=5.0, x2=10.3, y2=10.0,
            width=0.2, layer=Layer.F_CU, net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[seg], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        merged = _merge_same_net_vias(router)
        assert merged == 1
        # New via removed
        assert len(new_route.vias) == 0
        # Existing via survives
        assert len(existing_route.vias) == 1
        assert existing_route.vias[0] is existing_via
        # Segment endpoint reconnected to existing via position
        assert math.isclose(seg.x2, 10.0)
        assert math.isclose(seg.y2, 10.0)

    def test_existing_via_survives_merge(self):
        """The pre-existing via is kept, not the new via."""
        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        existing_route = Route(
            net=1, net_name="Net1", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=10.0, y=10.005, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        _merge_same_net_vias(router)
        # Existing via still present
        assert existing_via in existing_route.vias
        # New via gone
        assert new_via not in new_route.vias

    def test_distant_existing_via_not_merged(self):
        """Vias beyond the threshold should not be merged."""
        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        existing_route = Route(
            net=1, net_name="Net1", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=15.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        merged = _merge_same_net_vias(router)
        assert merged == 0
        assert len(new_route.vias) == 1

    def test_different_net_existing_via_not_merged(self):
        """Existing vias on a different net should not be merged."""
        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        existing_route = Route(
            net=2, net_name="Net2", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=10.005, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        merged = _merge_same_net_vias(router)
        assert merged == 0
        assert len(new_route.vias) == 1

    def test_empty_existing_routes_no_change(self):
        """With no existing routes, Phase 3 is a no-op."""
        via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        route = Route(net=1, net_name="Net1", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route], existing_routes=[])

        merged = _merge_same_net_vias(router)
        assert merged == 0
        assert len(route.vias) == 1

    def test_exact_same_location_existing_via(self):
        """New via at exactly the same location as existing merges cleanly."""
        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        existing_route = Route(
            net=1, net_name="Net1", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        seg = Segment(
            x1=5.0, y1=5.0, x2=10.0, y2=10.0,
            width=0.2, layer=Layer.F_CU, net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[seg], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        merged = _merge_same_net_vias(router)
        assert merged == 1
        assert len(new_route.vias) == 0
        assert len(existing_route.vias) == 1


class TestValidateRoutesWithExistingRoutes:
    """DRC validation detects violations between new and pre-existing vias."""

    def test_cross_origin_via_violation_detected(self):
        """A new via and an existing via on different nets within clearance
        should produce a ClearanceViolation."""
        from kicad_tools.router.io import validate_routes

        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        existing_route = Route(
            net=2, net_name="Net2", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=10.3, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        violations = validate_routes(router)
        via_violations = [v for v in violations if v.obstacle_type == "via"]
        assert len(via_violations) >= 1, (
            "Should detect via-to-via violation between new and existing routes"
        )

    def test_no_violation_when_far_apart(self):
        """Vias on different nets that are far apart should not produce violations."""
        from kicad_tools.router.io import validate_routes

        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        existing_route = Route(
            net=2, net_name="Net2", segments=[], vias=[existing_via],
        )

        new_via = Via(
            x=20.0, y=20.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        new_route = Route(net=1, net_name="Net1", segments=[], vias=[new_via])

        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        violations = validate_routes(router)
        via_violations = [v for v in violations if v.obstacle_type == "via"]
        assert len(via_violations) == 0

    def test_existing_routes_not_in_to_sexp(self):
        """Existing routes must not appear in to_sexp() output."""
        existing_via = Via(
            x=10.0, y=10.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        existing_route = Route(
            net=1, net_name="Net1", segments=[], vias=[existing_via],
        )

        new_seg = Segment(
            x1=1.0, y1=1.0, x2=5.0, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        new_route = Route(net=2, net_name="Net2", segments=[new_seg], vias=[])

        # Use a stub that has a to_sexp method similar to Autorouter
        router = _StubAutorouter(
            routes=[new_route],
            existing_routes=[existing_route],
        )

        # Simulate Autorouter.to_sexp() which only iterates self.routes
        sexp_output = "\n\t".join(r.to_sexp() for r in router.routes)
        assert "10.0" not in sexp_output or "Net2" in sexp_output
        # The existing via coordinates should not appear in the new route sexp
        existing_sexp = existing_route.to_sexp()
        assert existing_sexp not in sexp_output


# ---------------------------------------------------------------------------
# Tests for chain-aware nudging and pad-anchor preservation (Issue #2475)
# ---------------------------------------------------------------------------


class TestSegmentEndpointsAnchoredToNetPads:
    """Pad-anchor detection used by chain-aware nudge to preserve connectivity."""

    def _make_pad(self, ref: str, pin: str, x: float, y: float, net: int = 1) -> Pad:
        return Pad(
            x=x, y=y, width=1.0, height=1.0,
            net=net, net_name="N",
            layer=Layer.F_CU, ref=ref, pin=pin,
        )

    def test_segment_with_endpoint_on_pad_is_detected(self):
        pad = self._make_pad("J1", "1", 10.0, 5.0)
        router = _StubAutorouter(
            pads={("J1", "1"): pad},
            nets={1: [("J1", "1")]},
        )
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_pads(seg, 1, router) is True

    def test_segment_close_but_not_at_pad_is_not_detected(self):
        pad = self._make_pad("J1", "1", 10.0, 5.0)
        router = _StubAutorouter(
            pads={("J1", "1"): pad},
            nets={1: [("J1", "1")]},
        )
        # 0.05mm offset -- outside the 0.02mm pad anchor tolerance.
        seg = Segment(x1=10.05, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_pads(seg, 1, router) is False

    def test_segment_with_no_pad_endpoint_returns_false(self):
        pad = self._make_pad("J1", "1", 0.0, 0.0)
        router = _StubAutorouter(
            pads={("J1", "1"): pad},
            nets={1: [("J1", "1")]},
        )
        # Segment far from any pad of net 1.
        seg = Segment(x1=10.0, y1=10.0, x2=15.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_pads(seg, 1, router) is False

    def test_pad_of_different_net_does_not_match(self):
        pad = self._make_pad("J1", "1", 10.0, 5.0, net=2)
        router = _StubAutorouter(
            pads={("J1", "1"): pad},
            nets={2: [("J1", "1")]},
        )
        # Segment endpoint coincides with pad of *different* net -- not an anchor.
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_pads(seg, 1, router) is False

    def test_missing_pads_attribute_returns_false(self):
        # _StubAutorouter has empty pads/nets -- helper must tolerate this.
        router = _StubAutorouter()
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_pads(seg, 1, router) is False


class TestNudgeSegmentWithChain:
    """Chain-aware segment nudge that preserves abutting same-net segments."""

    def test_chain_endpoints_follow_nudged_segment(self):
        """Issue #2475: nudging a middle segment must drag adjacent endpoints with it."""
        # Three connected segments forming an L-shape: seg_a -> seg_b -> seg_c.
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_b = Segment(x1=10, y1=0, x2=10, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        seg_c = Segment(x1=10, y1=5, x2=20, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="N", segments=[seg_a, seg_b, seg_c], vias=[])
        router = _StubAutorouter(routes=[route])

        ok = _nudge_segment_with_chain(seg_b, 1.0, 0.0, 0.5, router)
        assert ok is True
        # seg_b moved +0.5 in x
        assert math.isclose(seg_b.x1, 10.5)
        assert math.isclose(seg_b.x2, 10.5)
        # seg_a's endpoint that abutted seg_b should follow it.
        assert math.isclose(seg_a.x2, 10.5), "seg_a.x2 should follow seg_b's new x1"
        # seg_c's endpoint that abutted seg_b should follow it.
        assert math.isclose(seg_c.x1, 10.5), "seg_c.x1 should follow seg_b's new x2"
        # Other endpoints stay put.
        assert math.isclose(seg_a.x1, 0.0)
        assert math.isclose(seg_c.x2, 20.0)

    def test_pad_anchored_segment_not_nudged(self):
        """A segment with an endpoint on a same-net pad must not be moved."""
        pad = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=1, net_name="N", layer=Layer.F_CU,
            ref="J1", pin="1",
        )
        seg = Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="N", segments=[seg], vias=[])
        router = _StubAutorouter(
            routes=[route],
            pads={("J1", "1"): pad},
            nets={1: [("J1", "1")]},
        )

        ok = _nudge_segment_with_chain(seg, 0.0, 1.0, 0.2, router)
        assert ok is False
        # Segment must be unchanged.
        assert seg.x1 == 0.0 and seg.y1 == 0.0
        assert seg.x2 == 10.0 and seg.y2 == 0.0

    def test_different_net_segment_not_affected(self):
        """Endpoints on a different net should not snap to the nudged segment."""
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        # seg_b touches seg_a.x2 but is on a *different* net -- must be ignored.
        seg_b = Segment(x1=10, y1=0, x2=10, y2=5, width=0.2, layer=Layer.F_CU, net=2)
        route_a = Route(net=1, net_name="A", segments=[seg_a], vias=[])
        route_b = Route(net=2, net_name="B", segments=[seg_b], vias=[])
        router = _StubAutorouter(routes=[route_a, route_b])

        ok = _nudge_segment_with_chain(seg_a, 0.0, 1.0, 0.2, router)
        assert ok is True
        # seg_a moved +0.2 in y
        assert math.isclose(seg_a.y1, 0.2)
        # seg_b is on a different net -- should NOT have moved.
        assert seg_b.x1 == 10.0 and seg_b.y1 == 0.0
        assert seg_b.x2 == 10.0 and seg_b.y2 == 5.0

    def test_different_layer_segment_not_affected(self):
        """Segments on a different layer should not be snapped."""
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        # Same net, but on a different layer -- chain link goes through a via,
        # which provides positional freedom; we should not drag it.
        seg_b = Segment(x1=10, y1=0, x2=10, y2=5, width=0.2, layer=Layer.B_CU, net=1)
        route = Route(net=1, net_name="N", segments=[seg_a, seg_b], vias=[])
        router = _StubAutorouter(routes=[route])

        ok = _nudge_segment_with_chain(seg_a, 0.0, 1.0, 0.2, router)
        assert ok is True
        # seg_a moved.
        assert math.isclose(seg_a.y1, 0.2)
        # seg_b is on a different layer -- coordinates unchanged.
        assert seg_b.x1 == 10.0 and seg_b.y1 == 0.0


class TestNoSilentDisconnectFromNudge:
    """End-to-end scenario: nudging a clearance violation must not disconnect a chain.

    Mirrors the board 05 PHASE_B scenario (issue #2475): a 4-pad net with a
    chain that crosses near another net's pad.  Before the fix, the nudge
    moved a single segment, breaking continuity and reducing PHASE_B from
    4/4 to 3/4 pads.  The chain-aware nudge keeps the chain intact.
    """

    def test_chain_remains_connected_after_nudge(self):
        """A 3-segment chain on net 1 stays connected after a nudge."""
        # Chain: pad(0,0) -- seg_a -- seg_b -- seg_c -- pad(20,5)
        # seg_b is the middle segment that we'll nudge to repair clearance.
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_b = Segment(x1=10, y1=0, x2=10, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        seg_c = Segment(x1=10, y1=5, x2=20, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="PHASE_B", segments=[seg_a, seg_b, seg_c], vias=[])

        # Pin pads on net 1 at the ends of the chain.
        p_start = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=1, net_name="PHASE_B", layer=Layer.F_CU, ref="J1", pin="1",
        )
        p_end = Pad(
            x=20.0, y=5.0, width=1.0, height=1.0,
            net=1, net_name="PHASE_B", layer=Layer.F_CU, ref="J2", pin="2",
        )
        router = _StubAutorouter(
            routes=[route],
            pads={("J1", "1"): p_start, ("J2", "2"): p_end},
            nets={1: [("J1", "1"), ("J2", "2")]},
        )

        # Nudge the middle segment -- this is the case that broke before #2475.
        ok = _nudge_segment_with_chain(seg_b, 1.0, 0.0, 0.18, router)
        assert ok is True

        # Walk the chain and verify it's still connected.
        # seg_a.x2/y2 should equal seg_b.x1/y1
        assert math.isclose(seg_a.x2, seg_b.x1)
        assert math.isclose(seg_a.y2, seg_b.y1)
        # seg_b.x2/y2 should equal seg_c.x1/y1
        assert math.isclose(seg_b.x2, seg_c.x1)
        assert math.isclose(seg_b.y2, seg_c.y1)
        # Pads stay anchored at chain ends.
        assert math.isclose(seg_a.x1, p_start.x)
        assert math.isclose(seg_a.y1, p_start.y)
        assert math.isclose(seg_c.x2, p_end.x)
        assert math.isclose(seg_c.y2, p_end.y)

    def test_chain_disconnects_with_legacy_nudge(self):
        """Sanity: the legacy ``_nudge_segment`` does NOT preserve the chain.

        This documents the regression that motivated the chain-aware variant.
        """
        seg_a = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg_b = Segment(x1=10, y1=0, x2=10, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        seg_c = Segment(x1=10, y1=5, x2=20, y2=5, width=0.2, layer=Layer.F_CU, net=1)

        _nudge_segment(seg_b, 1.0, 0.0, 0.18)

        # seg_b moved, but seg_a and seg_c did not -- chain is broken.
        assert not math.isclose(seg_a.x2, seg_b.x1), (
            "Legacy nudge leaves seg_a stranded -- chain breaks"
        )
        assert not math.isclose(seg_b.x2, seg_c.x1), (
            "Legacy nudge leaves seg_c stranded -- chain breaks"
        )


# ---------------------------------------------------------------------------
# Tests for via-anchor preservation in chain-aware nudge (Issue #2483)
# ---------------------------------------------------------------------------


class TestSegmentEndpointsAnchoredToNetVias:
    """Via-anchor detection used by chain-aware nudge to preserve layer transitions."""

    def test_segment_with_endpoint_on_via_is_detected(self):
        """A segment endpoint coincident with a via centre is detected as anchored."""
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        route = Route(net=1, net_name="N", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route])
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is True

    def test_segment_with_second_endpoint_on_via_is_detected(self):
        """The helper checks both endpoints, not just the first."""
        via = Via(
            x=15.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        route = Route(net=1, net_name="N", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route])
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is True

    def test_segment_close_but_not_at_via_is_not_detected(self):
        """Endpoints outside the 0.02mm tolerance are not treated as anchored."""
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        route = Route(net=1, net_name="N", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route])
        # 0.05 mm offset -- outside the 0.02 mm via anchor tolerance.
        seg = Segment(x1=10.05, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is False

    def test_segment_with_no_via_endpoint_returns_false(self):
        """Segments far from any via on the net are not anchored."""
        via = Via(
            x=0.0, y=0.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        route = Route(net=1, net_name="N", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route])
        seg = Segment(x1=10.0, y1=10.0, x2=15.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is False

    def test_via_of_different_net_does_not_match(self):
        """Vias on a different net should not anchor segments of this net."""
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        route = Route(net=2, net_name="Other", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route])
        # Segment on net 1 has an endpoint at the via centre, but the via
        # belongs to net 2 -- not an anchor for this net.
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is False

    def test_no_routes_returns_false(self):
        """An empty router (no routes / no vias) returns False without error."""
        router = _StubAutorouter()
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is False

    def test_via_in_different_route_same_net_is_detected(self):
        """A via on a different route but the same net is still an anchor."""
        # Same net, two routes -- the via lives on the second route while
        # the segment under inspection is on the first.  We must still
        # consider the via because it's on the same net.
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        route_with_seg = Route(net=1, net_name="N", segments=[], vias=[])
        route_with_via = Route(net=1, net_name="N", segments=[], vias=[via])
        router = _StubAutorouter(routes=[route_with_seg, route_with_via])
        seg = Segment(x1=10.0, y1=5.0, x2=15.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        assert _segment_endpoints_anchored_to_net_vias(seg, 1, router) is True


class TestNudgeSegmentWithChainViaAnchor:
    """Regression tests for Issue #2483: chain-aware nudge must respect via anchors."""

    def test_via_anchored_segment_not_nudged(self):
        """A segment whose endpoint sits on a via centre must not be translated.

        Regression for #2483: chain-aware nudge must treat via centres as
        anchors, just like pad centres.  Otherwise the segment slides off
        the via and the layer transition breaks because the same-layer
        chain walk cannot drag the via (and its other-layer continuation)
        along.
        """
        # Build a 3-pad net: P1 (top), P2 (top), P3 (bottom).
        # Route: P1 --seg_a(top)--> via --seg_b(bottom)--> P3
        #        P1 --seg_c(top)--> P2
        p1 = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=1, net_name="N", layer=Layer.F_CU, ref="J1", pin="1",
        )
        p2 = Pad(
            x=0.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="N", layer=Layer.F_CU, ref="J1", pin="2",
        )
        p3 = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0,
            net=1, net_name="N", layer=Layer.B_CU, ref="J1", pin="3",
        )
        via = Via(
            x=10.0, y=0.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        # seg_a runs top-layer from P1 to the via centre
        seg_a = Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU, net=1)
        # seg_b runs bottom-layer from via centre to P3
        seg_b = Segment(x1=10.0, y1=0.0, x2=10.0, y2=10.0, width=0.2, layer=Layer.B_CU, net=1)
        # seg_c provides a pad-anchored stub on the top layer for P2
        seg_c = Segment(x1=0.0, y1=0.0, x2=0.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)

        route = Route(
            net=1, net_name="N",
            segments=[seg_a, seg_b, seg_c],
            vias=[via],
        )
        router = _StubAutorouter(
            routes=[route],
            pads={
                ("J1", "1"): p1,
                ("J1", "2"): p2,
                ("J1", "3"): p3,
            },
            nets={1: [("J1", "1"), ("J1", "2"), ("J1", "3")]},
        )

        # seg_a has an endpoint at the via centre AND another at a pad centre.
        # Capture pre-nudge state.
        pre_x1, pre_y1, pre_x2, pre_y2 = seg_a.x1, seg_a.y1, seg_a.x2, seg_a.y2
        via_x, via_y = via.x, via.y

        # Attempt to nudge seg_a perpendicular to its run (push it +y by 0.1mm).
        result = _nudge_segment_with_chain(seg_a, 0.0, 1.0, 0.1, router)

        # The chain-aware nudge must decline this nudge.
        assert result is False, (
            "Nudge must be declined: seg_a is via-anchored at (10, 0) "
            "and pad-anchored at (0, 0); translating it disconnects the chain."
        )
        # seg_a unchanged.
        assert seg_a.x1 == pre_x1 and seg_a.y1 == pre_y1
        assert seg_a.x2 == pre_x2 and seg_a.y2 == pre_y2
        # via unchanged.
        assert via.x == via_x and via.y == via_y
        # The chain-walk must not have been applied: seg_b's endpoint at the
        # via centre is still there, so the layer transition is intact.
        assert seg_b.x1 == 10.0 and seg_b.y1 == 0.0

    def test_via_anchored_segment_not_pad_anchored_still_declines(self):
        """A segment with only a via anchor (no pad anchor) is still declined.

        This isolates the via-anchor guard from the pre-existing pad-anchor
        guard to confirm the new logic actually runs.
        """
        # No pads on this net at the segment endpoints; only a via.
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        # seg has its x2/y2 endpoint coincident with the via centre, but its
        # x1/y1 endpoint is in free space (not a pad and not a via).
        seg = Segment(x1=0.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="N", segments=[seg], vias=[via])
        router = _StubAutorouter(routes=[route])

        pre_x1, pre_y1, pre_x2, pre_y2 = seg.x1, seg.y1, seg.x2, seg.y2

        result = _nudge_segment_with_chain(seg, 0.0, 1.0, 0.1, router)

        assert result is False, "Via-anchor guard alone must decline the nudge"
        # Segment unchanged byte-for-byte.
        assert seg.x1 == pre_x1 and seg.y1 == pre_y1
        assert seg.x2 == pre_x2 and seg.y2 == pre_y2
        # Via unchanged.
        assert via.x == 10.0 and via.y == 5.0

    def test_segment_far_from_any_via_is_still_nudged(self):
        """Vias that don't coincide with the segment do not block the nudge.

        Regression guard: the via-anchor check must use the per-via
        coordinate test, not just the presence of any via in the route.
        """
        # A via exists on this net but is far from the segment we'll nudge.
        via = Via(
            x=50.0, y=50.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        seg = Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="N", segments=[seg], vias=[via])
        router = _StubAutorouter(routes=[route])

        result = _nudge_segment_with_chain(seg, 0.0, 1.0, 0.2, router)

        assert result is True, (
            "Segments far from any via should still be nudgable -- "
            "the guard must be position-sensitive, not route-presence-sensitive."
        )
        # Segment moved as expected.
        assert math.isclose(seg.y1, 0.2)
        assert math.isclose(seg.y2, 0.2)
