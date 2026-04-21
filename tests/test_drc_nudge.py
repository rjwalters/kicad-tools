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
    _perpendicular_unit,
    _reconnect_segments,
    _segment_length,
    drc_verify_and_nudge,
)
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
