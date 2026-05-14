"""Tests for C++ geometric validation parity (Issue #2439).

Validates that the C++ validate_route() implementation produces identical
pass/fail results to the Python validate_segment_clearance(),
validate_via_clearance(), validate_via_to_via_clearance(), and
validate_same_net_drill_spacing() methods.

Also includes unit tests for the C++ geometry functions (point_to_segment_distance,
segment_to_segment_distance, segments_intersect) and the FNV-1a hash function.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import (
    Pad,
    Route,
    Segment,
    Via,
)
from kicad_tools.router.rules import DesignRules

# Marker for tests requiring the C++ backend
requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)


def _make_grid_and_rules(
    width: float = 20.0,
    height: float = 20.0,
    resolution: float = 0.25,
    trace_width: float = 0.25,
    trace_clearance: float = 0.25,
    via_clearance: float = 0.25,
    via_drill: float = 0.3,
    via_diameter: float = 0.6,
    min_drill_clearance: float = 0.102,
) -> tuple[RoutingGrid, DesignRules]:
    """Create a RoutingGrid and DesignRules for testing."""
    rules = DesignRules(
        trace_width=trace_width,
        trace_clearance=trace_clearance,
        via_clearance=via_clearance,
        via_drill=via_drill,
        via_diameter=via_diameter,
        grid_resolution=resolution,
        min_drill_clearance=min_drill_clearance,
    )
    layer_stack = LayerStack.two_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )
    return grid, rules


# -----------------------------------------------------------------------
# C++ geometry function unit tests
# -----------------------------------------------------------------------


@requires_cpp
class TestCppGeometryFunctions:
    """Unit tests for C++ geometry functions ported from core/geometry.py."""

    def test_point_to_segment_distance_on_segment(self):
        """Point on segment should have distance 0."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.point_to_segment_distance(1.0, 0.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(0.0, abs=1e-6)

    def test_point_to_segment_distance_perpendicular(self):
        """Point perpendicular to segment midpoint."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.point_to_segment_distance(1.0, 1.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(1.0, abs=1e-6)

    def test_point_to_segment_distance_at_endpoint(self):
        """Point closest to segment endpoint."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.point_to_segment_distance(3.0, 0.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(1.0, abs=1e-6)

    def test_point_to_segment_distance_degenerate(self):
        """Degenerate segment (point) distance."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.point_to_segment_distance(3.0, 4.0, 0.0, 0.0, 0.0, 0.0)
        assert dist == pytest.approx(5.0, abs=1e-6)

    def test_segments_intersect_crossing(self):
        """Two crossing segments should intersect."""
        from kicad_tools.router import router_cpp

        assert router_cpp.segments_intersect(0, 0, 2, 2, 0, 2, 2, 0) is True

    def test_segments_intersect_parallel(self):
        """Parallel segments should not intersect."""
        from kicad_tools.router import router_cpp

        assert router_cpp.segments_intersect(0, 0, 2, 0, 0, 1, 2, 1) is False

    def test_segments_intersect_t_shape(self):
        """T-shape (endpoint touching) should not count as intersection."""
        from kicad_tools.router import router_cpp

        assert router_cpp.segments_intersect(0, 0, 2, 0, 1, 0, 1, 2) is False

    def test_segment_to_segment_distance_intersecting(self):
        """Intersecting segments should have distance 0."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.segment_to_segment_distance(
            0, 0, 2, 2, 0, 2, 2, 0
        )
        assert dist == pytest.approx(0.0, abs=1e-6)

    def test_segment_to_segment_distance_parallel(self):
        """Parallel horizontal segments distance apart."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.segment_to_segment_distance(
            0, 0, 2, 0, 0, 1, 2, 1
        )
        assert dist == pytest.approx(1.0, abs=1e-6)

    def test_segment_to_segment_distance_end_to_end(self):
        """End-to-end segments."""
        from kicad_tools.router import router_cpp

        dist = router_cpp.segment_to_segment_distance(
            0, 0, 1, 0, 2, 0, 3, 0
        )
        assert dist == pytest.approx(1.0, abs=1e-6)


@requires_cpp
class TestFnv1aHash:
    """Tests for the deterministic FNV-1a hash function."""

    def test_empty_string(self):
        from kicad_tools.router import router_cpp

        h = router_cpp.fnv1a_hash("")
        assert h == 2166136261  # FNV offset basis

    def test_deterministic(self):
        """Same input produces same hash across calls."""
        from kicad_tools.router import router_cpp

        h1 = router_cpp.fnv1a_hash("U1")
        h2 = router_cpp.fnv1a_hash("U1")
        assert h1 == h2

    def test_different_strings(self):
        """Different strings produce different hashes."""
        from kicad_tools.router import router_cpp

        h1 = router_cpp.fnv1a_hash("U1")
        h2 = router_cpp.fnv1a_hash("R1")
        assert h1 != h2


# -----------------------------------------------------------------------
# Validation parity tests
# -----------------------------------------------------------------------


@requires_cpp
class TestValidationDataStorage:
    """Test that pad/segment/via data is correctly stored in C++ Grid3D."""

    def test_pad_count_after_from_routing_grid(self):
        """Pads from RoutingGrid should be stored in C++ Grid3D."""
        grid, rules = _make_grid_and_rules()
        # Add a pad to the grid
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=1,
                  net_name="VCC", layer=Layer.F_CU, ref="R1")
        grid.add_pad(pad)

        cpp_grid = CppGrid.from_routing_grid(grid)
        assert cpp_grid._impl.pad_count == 1

    def test_stored_segment_count(self):
        """Stored segments should be addable to C++ Grid3D."""
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        cpp_grid._impl.add_stored_segment(1.0, 1.0, 5.0, 1.0, 0.25, 0, 1)
        assert cpp_grid._impl.stored_segment_count == 1

    def test_stored_via_count(self):
        """Stored vias should be addable to C++ Grid3D."""
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        cpp_grid._impl.add_stored_via(3.0, 3.0, 0.3, 0.6, 1)
        assert cpp_grid._impl.stored_via_count == 1

    def test_clear_validation_data(self):
        """clear_validation_data should remove all stored data."""
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        cpp_grid._impl.add_stored_segment(1.0, 1.0, 5.0, 1.0, 0.25, 0, 1)
        cpp_grid._impl.add_stored_via(3.0, 3.0, 0.3, 0.6, 1)
        cpp_grid._impl.clear_validation_data()
        assert cpp_grid._impl.stored_segment_count == 0
        assert cpp_grid._impl.stored_via_count == 0
        # Pads from from_routing_grid are also cleared
        assert cpp_grid._impl.pad_count == 0


@requires_cpp
class TestSegmentPadClearanceParity:
    """Test segment-to-pad clearance validation matches Python."""

    def test_segment_far_from_pad_passes(self):
        """Segment far from pad should pass validation."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        pad = Pad(x=10.0, y=10.0, width=1.0, height=1.0, net=2,
                  net_name="GND", layer=Layer.F_CU, ref="R1")
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Segment is 5mm away from pad -- should pass easily
        cs = router_cpp.Segment()
        cs.x1, cs.y1, cs.x2, cs.y2 = 1.0, 1.0, 3.0, 1.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True

    def test_segment_too_close_to_pad_fails(self):
        """Segment violating clearance to pad should fail."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=2,
                  net_name="GND", layer=Layer.F_CU, ref="R1")
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Segment runs right next to pad (clearance < 0.25)
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 4.0, 5.0
        cs.x2, cs.y2 = 6.0, 5.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is False
        assert result.violation_type == 1  # seg-pad

    def test_same_net_pad_excluded(self):
        """Segment near same-net pad should pass (excluded)."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=1,
                  net_name="VCC", layer=Layer.F_CU, ref="R1")
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 4.0, 5.0
        cs.x2, cs.y2 = 6.0, 5.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102  # exclude_net=1, same as pad
        )
        assert result.valid is True

    def test_exclude_ref_hash(self):
        """Segment near pad with excluded ref should pass (Issue #1764)."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=2,
                  net_name="GND", layer=Layer.F_CU, ref="R1")
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 4.0, 5.0
        cs.x2, cs.y2 = 6.0, 5.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        # Exclude R1's ref hash
        r1_hash = router_cpp.fnv1a_hash("R1")
        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [r1_hash], 0.25, 0.25, 0.102
        )
        assert result.valid is True

    def test_exclude_ref_hash_preserves_signal_pad_escape(self):
        """Issue #1764 regression guard: signal pads (net != 0) on a
        component in the exclude set MUST still be skipped so the chip's
        own signal-pin escape can be routed.

        This is a named regression guard formalising the original Issue
        #1764 reachability fix; it must continue to pass after the
        Issue #2871 narrowing (only plane-net pads are re-engaged in the
        validator).
        """
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        # Signal pad (net != 0) on R1 -- this is the path Issue #1764
        # protects: a signal-pin escape on the same component must
        # remain reachable when its ref is in the exclude set.
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=2,
                  net_name="SIG", layer=Layer.F_CU, ref="R1")
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 4.0, 5.0
        cs.x2, cs.y2 = 6.0, 5.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        # Exclude R1's ref hash; signal pad is skipped, segment passes.
        r1_hash = router_cpp.fnv1a_hash("R1")
        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [r1_hash], 0.25, 0.25, 0.102
        )
        assert result.valid is True

    def test_exclude_ref_hash_blocks_plane_net_pad(self):
        """Issue #2871: plane-net pads (net == 0) on a component in the
        exclude set MUST still participate in pad-vs-segment validation.

        This is the new behaviour the patch adds. Before the fix the
        broad ref-exclusion at grid.cpp:376 would skip every pad on the
        excluded component, letting signal traces clip the chip's own
        plane-net (GND / +3.3V) pads. After the fix the validator
        re-engages for plane-net pads and reports a seg-pad violation
        when the segment is closer than trace_clearance.
        """
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        # Plane-net pad (net == 0, the SKIPPED-net convention threaded
        # through cpp_backend.py:596-605) on U2 -- e.g. a GND or +3.3V
        # pad on an LQFP-48 chip whose signal traces are also being
        # routed.
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=0,
                  net_name="GND", layer=Layer.F_CU, ref="U2")
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Segment passes through the pad center on the same layer --
        # well inside the trace_clearance band of any non-zero pad.
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 4.0, 5.0
        cs.x2, cs.y2 = 6.0, 5.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        # Exclude U2's ref hash (mirrors what _validate_route_clearance
        # does when routing a net rooted at U2).
        u2_hash = router_cpp.fnv1a_hash("U2")
        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [u2_hash], 0.25, 0.25, 0.102
        )
        # The plane-net pad must be enforced even though U2 is in the
        # exclude set -- this is the bug Issue #2871 closes.
        assert result.valid is False
        assert result.violation_type == 1  # seg-pad


@requires_cpp
class TestSegmentSegmentClearanceParity:
    """Test segment-to-segment clearance validation."""

    def test_parallel_segments_too_close_fails(self):
        """Parallel segments closer than clearance should fail."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Add existing route segment (net 2) in stored data
        cpp_grid._impl.add_stored_segment(1.0, 5.0, 10.0, 5.0, 0.25, 0, 2)

        # New segment (net 1) runs parallel, 0.3mm away
        # Edge-to-edge: 0.3 - 0.125 - 0.125 = 0.05 < 0.25
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 1.0, 5.3
        cs.x2, cs.y2 = 10.0, 5.3
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is False
        assert result.violation_type == 2  # seg-seg

    def test_parallel_segments_far_apart_passes(self):
        """Parallel segments with sufficient clearance should pass."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_grid._impl.add_stored_segment(1.0, 5.0, 10.0, 5.0, 0.25, 0, 2)

        # Segment 1mm away -- edge-to-edge: 1.0 - 0.125 - 0.125 = 0.75 > 0.25
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 1.0, 6.0
        cs.x2, cs.y2 = 10.0, 6.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True


@requires_cpp
class TestViaClearanceParity:
    """Test via-to-segment and via-to-via clearance validation."""

    def test_via_too_close_to_segment_fails(self):
        """Via near a stored segment should fail."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(via_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Stored segment on layer 0, net 2
        cpp_grid._impl.add_stored_segment(1.0, 5.0, 10.0, 5.0, 0.25, 0, 2)

        # Via at (5.0, 5.3) near the segment
        cv = router_cpp.Via()
        cv.x, cv.y = 5.0, 5.3
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is False
        assert result.violation_type == 4  # via-seg

    def test_via_far_from_segment_passes(self):
        """Via far from stored segment should pass."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(via_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_grid._impl.add_stored_segment(1.0, 5.0, 10.0, 5.0, 0.25, 0, 2)

        cv = router_cpp.Via()
        cv.x, cv.y = 5.0, 8.0
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True

    def test_via_to_via_too_close_fails(self):
        """Two vias from different nets too close should fail."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(via_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Stored via (net 2)
        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, 0.6, 2)

        # New via (net 1) very close
        cv = router_cpp.Via()
        cv.x, cv.y = 5.5, 5.0
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        # Edge-to-edge: 0.5 - 0.3 - 0.3 = -0.1 < 0.25
        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is False
        assert result.violation_type == 5  # via-via

    def test_via_to_via_far_apart_passes(self):
        """Two vias from different nets far apart should pass."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(via_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, 0.6, 2)

        cv = router_cpp.Via()
        cv.x, cv.y = 8.0, 5.0
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True


@requires_cpp
class TestSameNetDrillSpacingParity:
    """Test same-net drill spacing validation (Issue #1782)."""

    def test_same_net_drills_too_close_fails(self):
        """Same-net vias with insufficient drill spacing should fail."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(min_drill_clearance=0.102)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Stored via (net 1 -- same net)
        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, 0.6, 1)

        # New via (net 1) very close -- drill edge-to-edge < 0.102
        cv = router_cpp.Via()
        cv.x, cv.y = 5.3, 5.0
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        # Drill clearance: 0.3 - 0.15 - 0.15 = 0.0 < 0.102
        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is False
        assert result.violation_type == 6  # drill spacing

    def test_same_net_drills_far_apart_passes(self):
        """Same-net vias with sufficient drill spacing should pass."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(min_drill_clearance=0.102)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, 0.6, 1)

        cv = router_cpp.Via()
        cv.x, cv.y = 6.0, 5.0
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        # Drill clearance: 1.0 - 0.15 - 0.15 = 0.7 > 0.102
        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True

    def test_same_position_via_excluded(self):
        """Via at exact same position should not fail (self-check skip)."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(min_drill_clearance=0.102)
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, 0.6, 1)

        # Exact same position
        cv = router_cpp.Via()
        cv.x, cv.y = 5.0, 5.0
        cv.drill, cv.diameter = 0.3, 0.6
        cv.layer_from, cv.layer_to = 0, 1
        cv.net = 1

        result = cpp_grid._impl.validate_route(
            [], [cv], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True


@requires_cpp
class TestPerComponentClearance:
    """Test per-component clearance overrides (Issue #1016)."""

    def test_component_clearance_override(self):
        """Component with explicit clearance override should use that value."""
        from kicad_tools.router import router_cpp

        rules = DesignRules(
            trace_clearance=0.25,
            component_clearances={"U1": 0.08},
            grid_resolution=0.25,
        )
        layer_stack = LayerStack.two_layer()
        grid = RoutingGrid(width=20.0, height=20.0, rules=rules,
                           layer_stack=layer_stack)

        # U1 pad with 0.08mm clearance override
        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=2,
                  net_name="GND", layer=Layer.F_CU, ref="U1")
        grid.add_pad(pad)

        cpp_grid = CppGrid.from_routing_grid(grid)

        # Segment at distance where edge-to-edge is ~0.1mm (between 0.08 and 0.25)
        # Point-to-seg dist from (5, 5) to seg at y=5.35 = 0.35
        # Clearance: 0.35 - 0.125 - 0.25 = -0.025  (this would violate default)
        # But with 0.08 override: 0.35 - 0.125 - 0.25 = -0.025 (still violates pad radius)
        # Let me use a better distance:
        # Seg at y=5.5 -> dist=0.5 -> clearance = 0.5 - 0.125 - 0.25 = 0.125
        # 0.125 > 0.08 -> pass with override, but < 0.25 -> would fail with default
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 3.0, 5.5
        cs.x2, cs.y2 = 7.0, 5.5
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        # Should pass because U1's clearance is 0.08, and edge-to-edge is 0.125
        assert result.valid is True


@requires_cpp
class TestLayerFiltering:
    """Test that layer filtering works correctly in validation."""

    def test_different_layer_segment_ignored(self):
        """Stored segment on different layer should not cause violation."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Stored segment on layer 1 (back copper), net 2
        cpp_grid._impl.add_stored_segment(1.0, 5.0, 10.0, 5.0, 0.25, 1, 2)

        # New segment on layer 0 (front copper), very close
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 1.0, 5.0
        cs.x2, cs.y2 = 10.0, 5.0
        cs.width = 0.25
        cs.layer = 0
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is True

    def test_through_hole_pad_checked_on_all_layers(self):
        """Through-hole pad (layer_idx=-1) should be checked on all layers."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_grid_and_rules(trace_clearance=0.25)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=2,
                  net_name="GND", layer=Layer.F_CU, ref="R1",
                  through_hole=True)
        grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Segment on layer 1 (back copper) near through-hole pad
        cs = router_cpp.Segment()
        cs.x1, cs.y1 = 4.0, 5.0
        cs.x2, cs.y2 = 6.0, 5.0
        cs.width = 0.25
        cs.layer = 1
        cs.net = 1

        result = cpp_grid._impl.validate_route(
            [cs], [], 1, [], 0.25, 0.25, 0.102
        )
        assert result.valid is False
        assert result.violation_type == 1  # seg-pad


# -----------------------------------------------------------------------
# Python path twin: RoutingGrid.validate_segment_clearance(...)
# -----------------------------------------------------------------------
#
# Issue #2874 mirrors the C++ narrowing from PR #2873 on the Python side.
# The same-component-ref exclusion at ``grid.py:1503`` must permit signal
# pads (``net != 0``) to be skipped (so signal-pin escape routing works,
# Issue #1764) while keeping plane-net pads (``net == 0``) under
# validator scrutiny.  These tests exercise the Python validator
# directly so the fix is regression-guarded independently of the C++
# backend availability.


class TestPythonValidateSegmentClearanceExcludeRefs:
    """Issue #2874 (Python twin of #2871/PR #2873).

    Direct unit tests for ``RoutingGrid.validate_segment_clearance`` on
    the same-component-ref exclusion path.  Mirrors the C++ tests
    ``test_exclude_ref_hash_preserves_signal_pad_escape`` and
    ``test_exclude_ref_hash_blocks_plane_net_pad`` above.
    """

    def test_validate_segment_clearance_preserves_signal_pad_escape_on_excluded_ref(
        self,
    ):
        """Issue #1764 regression guard: signal pads (``net != 0``) on a
        component in the exclude set MUST still be skipped so the
        chip's own signal-pin escape can be routed.

        This is the named regression guard for the Python path that
        formalises the original Issue #1764 reachability fix; it must
        continue to pass after the Issue #2874 narrowing (only
        plane-net pads are re-engaged in the Python validator).
        """
        grid, _rules = _make_grid_and_rules(trace_clearance=0.25)
        # Signal pad (net != 0) on R1 -- this is the path Issue #1764
        # protects: a signal-pin escape on the same component must
        # remain reachable when its ref is in the exclude set.
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=2,
            net_name="SIG",
            layer=Layer.F_CU,
            ref="R1",
        )
        grid.add_pad(pad)

        # Segment passes through the pad center on the same layer --
        # the signal-pad escape geometry the original Issue #1764
        # exclusion was designed to permit.
        seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=6.0,
            y2=5.0,
            width=0.25,
            layer=Layer.F_CU,
            net=1,
            net_name="OTHER",
        )

        is_valid, _clearance, _location = grid.validate_segment_clearance(
            seg, exclude_net=1, exclude_refs={"R1"}
        )

        # Signal pad on excluded ref is skipped -- segment passes.
        assert is_valid is True

    def test_validate_segment_clearance_blocks_plane_net_pad_on_excluded_ref(self):
        """Issue #2874: plane-net pads (``net == 0``) on a component in
        the exclude set MUST still participate in pad-vs-segment
        validation.

        This is the new behaviour the patch adds.  Before the fix the
        broad ref-exclusion at ``grid.py:1495`` would skip every pad on
        the excluded component, letting signal traces clip the chip's
        own plane-net (GND / +3.3V) pads.  After the fix the validator
        re-engages for plane-net pads and reports a seg-pad violation
        when the segment is closer than ``trace_clearance``.

        Mirrors the C++ test
        ``test_exclude_ref_hash_blocks_plane_net_pad`` above.
        """
        grid, _rules = _make_grid_and_rules(trace_clearance=0.25)
        # Plane-net pad (net == 0, the SKIPPED-net convention threaded
        # through ``cpp_backend.py:596-605``) on U2 -- e.g. a GND or
        # +3.3V pad on an LQFP-48 chip whose signal traces are also
        # being routed.
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=0,
            net_name="GND",
            layer=Layer.F_CU,
            ref="U2",
        )
        grid.add_pad(pad)

        # Segment passes through the pad center on the same layer --
        # well inside the trace_clearance band of any non-zero pad.
        seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=6.0,
            y2=5.0,
            width=0.25,
            layer=Layer.F_CU,
            net=1,
            net_name="SIG",
        )

        is_valid, _clearance, location = grid.validate_segment_clearance(
            seg, exclude_net=1, exclude_refs={"U2"}
        )

        # The plane-net pad must be enforced even though U2 is in the
        # exclude set -- this is the bug Issue #2874 closes (Python
        # twin of #2871 / PR #2873).
        assert is_valid is False
        assert location is not None
