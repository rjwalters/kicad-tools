"""Tests for the post-optimization DRC verify-and-nudge pass."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from kicad_tools.router.drc_nudge import (
    COINCIDENT_THRESHOLD,
    DRCNudgeResult,
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
