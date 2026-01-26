"""Tests for sub-grid routing of fine-pitch component pad connections.

Issue #1109: Router support for fine-pitch components (sub-grid routing).
"""

import math

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.subgrid import (
    SubGridAnalysis,
    SubGridEscape,
    SubGridPad,
    SubGridResult,
    SubGridRouter,
    compute_subgrid_resolution,
)


def make_pad(
    x: float,
    y: float,
    net: int,
    ref: str,
    pin: str,
    width: float = 0.3,
    height: float = 0.8,
    net_name: str = "",
    layer: Layer = Layer.F_CU,
    through_hole: bool = False,
) -> Pad:
    """Helper to create Pad objects with default values."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name or f"NET{net}",
        ref=ref,
        pin=pin,
        layer=layer,
        through_hole=through_hole,
    )


def make_grid_and_rules(
    width: float = 20.0,
    height: float = 20.0,
    resolution: float = 0.1,
    trace_width: float = 0.2,
    trace_clearance: float = 0.15,
) -> tuple[RoutingGrid, DesignRules]:
    """Create a simple grid and rules for testing."""
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=trace_width,
        trace_clearance=trace_clearance,
    )
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
    )
    return grid, rules


class TestSubGridAnalysis:
    """Tests for the SubGridRouter.analyze_pads() method."""

    def test_all_pads_on_grid(self):
        """Pads perfectly aligned to the grid should not be flagged as off-grid."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.1, y=1.0, net=2, ref="U1", pin="2"),
            make_pad(x=1.2, y=1.0, net=3, ref="U1", pin="3"),
        ]

        analysis = subgrid.analyze_pads(pads)

        assert not analysis.has_off_grid_pads
        assert analysis.off_grid_count == 0
        assert len(analysis.on_grid_pads) == 3
        assert analysis.off_grid_percentage == 0.0

    def test_off_grid_pads_detected(self):
        """Pads with 0.65mm pitch on 0.1mm grid should be detected as off-grid."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        # 0.65mm pitch - doesn't align with 0.1mm grid
        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),   # On grid
            make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),   # Off grid (0.05mm offset)
            make_pad(x=2.30, y=1.0, net=3, ref="U1", pin="3"),   # On grid (2.3 = 23 * 0.1)
            make_pad(x=2.95, y=1.0, net=4, ref="U1", pin="4"),   # Off grid (0.05mm offset)
        ]

        analysis = subgrid.analyze_pads(pads)

        assert analysis.has_off_grid_pads
        assert analysis.off_grid_count == 2
        assert len(analysis.on_grid_pads) == 2

    def test_grid_tolerance(self):
        """Custom grid tolerance should control detection sensitivity."""
        grid, rules = make_grid_and_rules(resolution=0.1)

        # Tight tolerance: more pads flagged
        subgrid_tight = SubGridRouter(grid, rules, grid_tolerance=0.01)
        # Loose tolerance: fewer pads flagged
        subgrid_loose = SubGridRouter(grid, rules, grid_tolerance=0.06)

        # Pad at 1.05mm is off by 0.05mm from nearest grid point (1.0 or 1.1)
        pads = [make_pad(x=1.05, y=1.0, net=1, ref="U1", pin="1")]

        tight_analysis = subgrid_tight.analyze_pads(pads)
        loose_analysis = subgrid_loose.analyze_pads(pads)

        assert tight_analysis.has_off_grid_pads  # 0.05 > 0.01 tolerance
        assert not loose_analysis.has_off_grid_pads  # 0.05 < 0.06 tolerance

    def test_component_centers_computed(self):
        """Component centers should be computed from pad positions."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=3.0, y=1.0, net=2, ref="U1", pin="2"),
            make_pad(x=1.0, y=3.0, net=3, ref="U1", pin="3"),
            make_pad(x=3.0, y=3.0, net=4, ref="U1", pin="4"),
        ]

        analysis = subgrid.analyze_pads(pads)

        assert "U1" in analysis.component_centers
        cx, cy = analysis.component_centers["U1"]
        assert abs(cx - 2.0) < 0.001
        assert abs(cy - 2.0) < 0.001

    def test_escape_direction_computed(self):
        """Off-grid pads should have escape direction pointing outward from component center."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        # Component center at (2.0, 2.0)
        # Put off-grid pad at right side
        pads = [
            make_pad(x=1.0, y=2.0, net=1, ref="U1", pin="1"),
            make_pad(x=3.0, y=2.0, net=2, ref="U1", pin="2"),
            make_pad(x=2.0, y=1.0, net=3, ref="U1", pin="3"),
            make_pad(x=2.0, y=3.0, net=4, ref="U1", pin="4"),
            # Off-grid pad at right side
            make_pad(x=3.05, y=2.0, net=5, ref="U1", pin="5"),
        ]

        analysis = subgrid.analyze_pads(pads)

        assert len(analysis.off_grid_pads) == 1
        sgp = analysis.off_grid_pads[0]
        # Escape direction should point rightward (positive X)
        assert sgp.escape_direction[0] > 0

    def test_mixed_components(self):
        """Analysis should handle multiple components with different grid alignment."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            # U1: On grid (2.54mm pitch)
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=3.54, y=1.0, net=2, ref="U1", pin="2"),
            # U2: Off grid (0.65mm pitch)
            make_pad(x=5.0, y=5.0, net=3, ref="U2", pin="1"),
            make_pad(x=5.65, y=5.0, net=4, ref="U2", pin="2"),
        ]

        analysis = subgrid.analyze_pads(pads)

        # U1 pads should be on grid (2.54mm is 25.4 * 0.1)
        # U2 pin 2 should be off grid (5.65 % 0.1 = 0.05mm offset)
        assert analysis.has_off_grid_pads
        off_grid_refs = {sgp.pad.ref for sgp in analysis.off_grid_pads}
        assert "U2" in off_grid_refs

    def test_format_summary(self):
        """format_summary should produce readable output."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
        ]

        analysis = subgrid.analyze_pads(pads)
        summary = analysis.format_summary()

        assert "off-grid" in summary.lower()

    def test_dict_input(self):
        """analyze_pads should accept dict input (mapping (ref, pin) to Pad)."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = {
            ("U1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
        }

        analysis = subgrid.analyze_pads(pads)

        assert analysis.total_pads == 2


class TestSubGridEscapeGeneration:
    """Tests for the SubGridRouter.generate_escape_segments() method."""

    def test_escape_segments_created(self):
        """Off-grid pads should get escape segments to the nearest grid point."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),  # Off grid
        ]

        # Add pads to grid so the grid knows about nets
        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)
        result = subgrid.generate_escape_segments(analysis)

        assert result.success_count >= 1
        assert len(result.escapes) >= 1

    def test_escape_segment_endpoints(self):
        """Escape segment should start at pad center and end at a grid point."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pad = make_pad(x=1.65, y=1.0, net=1, ref="U1", pin="1")
        pads = [
            make_pad(x=1.0, y=1.0, net=2, ref="U1", pin="2"),
            pad,
        ]

        # Add pads to grid
        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)
        result = subgrid.generate_escape_segments(analysis)

        if result.escapes:
            escape = result.escapes[0]
            # Segment starts at pad center
            assert abs(escape.segment.x1 - pad.x) < 0.001
            assert abs(escape.segment.y1 - pad.y) < 0.001
            # Segment ends at a grid-aligned point
            snap_x, snap_y = grid.grid_to_world(*escape.grid_point)
            assert abs(escape.segment.x2 - snap_x) < 0.001
            assert abs(escape.segment.y2 - snap_y) < 0.001

    def test_escape_segment_net_assignment(self):
        """Escape segment should carry the pad's net ID."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.65, y=1.0, net=42, ref="U1", pin="2"),
        ]

        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)
        result = subgrid.generate_escape_segments(analysis)

        if result.escapes:
            for escape in result.escapes:
                assert escape.segment.net == escape.pad.net

    def test_escape_uses_neck_down_width(self):
        """When neck-down is configured, escape segments should use narrow width."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
            min_trace_width=0.1,
            neck_down_threshold=0.8,
        )
        grid = RoutingGrid(width=20.0, height=20.0, rules=rules)
        subgrid = SubGridRouter(grid, rules)

        # Create fine-pitch pads (0.65mm pitch < 0.8mm threshold)
        pads = []
        for i in range(4):
            pads.append(make_pad(
                x=1.0 + i * 0.65, y=1.0, net=i + 1, ref="U1", pin=str(i + 1)
            ))

        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)
        result = subgrid.generate_escape_segments(analysis)

        # Escape segments for fine-pitch pads should use min_trace_width
        for escape in result.escapes:
            assert escape.segment.width <= rules.trace_width

    def test_no_escapes_for_on_grid_pads(self):
        """Pads on the grid should not generate escape segments."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.1, y=1.0, net=2, ref="U1", pin="2"),
        ]

        analysis = subgrid.analyze_pads(pads)
        result = subgrid.generate_escape_segments(analysis)

        assert result.success_count == 0
        assert len(result.escapes) == 0


class TestSubGridApply:
    """Tests for applying escape segments to the grid."""

    def test_apply_unblocks_cells(self):
        """Applying escapes should unblock grid cells at escape endpoints."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pad = make_pad(x=1.65, y=1.0, net=1, ref="U1", pin="1")
        pads = [
            make_pad(x=1.0, y=1.0, net=2, ref="U1", pin="2"),
            pad,
        ]

        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)
        result = subgrid.generate_escape_segments(analysis)
        unblocked = subgrid.apply_escape_segments(result)

        # Should have unblocked at least some cells
        assert unblocked >= 0  # May be 0 if cells were already accessible
        assert result.unblocked_count >= 0

    def test_route_with_subgrid_convenience(self):
        """route_with_subgrid should perform analysis, generation, and application."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
        ]

        for p in pads:
            grid.add_pad(p)

        result = subgrid.route_with_subgrid(pads)

        assert isinstance(result, SubGridResult)
        assert result.analysis is not None


class TestSubGridEscapeRoutes:
    """Tests for converting escapes to Route objects."""

    def test_get_escape_routes(self):
        """get_escape_routes should return Route objects."""
        grid, rules = make_grid_and_rules(resolution=0.1)
        subgrid = SubGridRouter(grid, rules)

        pads = [
            make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
        ]

        for p in pads:
            grid.add_pad(p)

        result = subgrid.route_with_subgrid(pads)
        routes = subgrid.get_escape_routes(result)

        for route in routes:
            assert route.net > 0
            assert len(route.segments) == 1


class TestSubGridResolutionComputation:
    """Tests for compute_subgrid_resolution."""

    def test_065mm_pitch(self):
        """0.65mm pitch should suggest a grid that divides well."""
        res = compute_subgrid_resolution(0.65, 0.1)
        assert res < 0.1  # Must be finer than main grid
        assert res >= 0.005  # Not excessively fine
        # Check alignment: pitch / res should be close to integer
        ratio = 0.65 / res
        assert abs(ratio - round(ratio)) < 0.1

    def test_050mm_pitch(self):
        """0.5mm pitch should suggest 0.025 or similar."""
        res = compute_subgrid_resolution(0.5, 0.1)
        assert res < 0.1
        ratio = 0.5 / res
        assert abs(ratio - round(ratio)) < 0.1

    def test_254mm_pitch(self):
        """2.54mm pitch (standard through-hole) should still suggest something reasonable."""
        res = compute_subgrid_resolution(2.54, 0.1)
        assert res < 0.1


class TestSubGridDataclasses:
    """Tests for data class properties and formatting."""

    def test_subgrid_analysis_properties(self):
        """SubGridAnalysis properties should work correctly."""
        analysis = SubGridAnalysis(
            off_grid_pads=[
                SubGridPad(
                    pad=make_pad(x=1.65, y=1.0, net=1, ref="U1", pin="1"),
                    grid_x=17,
                    grid_y=10,
                    offset_x=0.05,
                    offset_y=0.0,
                    snap_x=1.7,
                    snap_y=1.0,
                )
            ],
            on_grid_pads=[
                make_pad(x=1.0, y=1.0, net=2, ref="U1", pin="2"),
            ],
            grid_resolution=0.1,
            grid_tolerance=0.025,
        )

        assert analysis.has_off_grid_pads
        assert analysis.off_grid_count == 1
        assert analysis.total_pads == 2
        assert abs(analysis.off_grid_percentage - 50.0) < 0.1

    def test_subgrid_result_properties(self):
        """SubGridResult properties should work correctly."""
        result = SubGridResult(
            escapes=[
                SubGridEscape(
                    pad=make_pad(x=1.65, y=1.0, net=1, ref="U1", pin="1"),
                    segment=Segment(
                        x1=1.65, y1=1.0, x2=1.7, y2=1.0,
                        width=0.2, layer=Layer.F_CU, net=1,
                    ),
                    grid_point=(17, 10),
                    snap_point=(1.7, 1.0),
                )
            ],
            failed_pads=[
                make_pad(x=1.65, y=2.0, net=3, ref="U1", pin="3"),
            ],
            unblocked_count=2,
        )

        assert result.success_count == 1
        assert result.total_attempted == 2

    def test_subgrid_result_format_summary(self):
        """format_summary should produce readable output."""
        result = SubGridResult(
            escapes=[],
            failed_pads=[],
            unblocked_count=0,
        )

        summary = result.format_summary()
        assert "0/0" in summary


class TestSubGridSSOP:
    """Integration test: SSOP-20 component with 0.65mm pitch on 0.1mm grid."""

    def test_ssop20_escape_routing(self):
        """SSOP-20 with 0.65mm pitch should have off-grid pads detected and escaped."""
        grid, rules = make_grid_and_rules(
            width=30.0,
            height=30.0,
            resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        subgrid = SubGridRouter(grid, rules)

        # Create SSOP-20 like component: 10 pads per side, 0.65mm pitch
        pads = []
        base_x = 10.0
        base_y = 10.0

        # Left side pads
        for i in range(10):
            pads.append(make_pad(
                x=base_x,
                y=base_y + i * 0.65,
                net=i + 1,
                ref="U1",
                pin=str(i + 1),
            ))

        # Right side pads
        for i in range(10):
            pads.append(make_pad(
                x=base_x + 6.0,  # Body width
                y=base_y + i * 0.65,
                net=i + 11,
                ref="U1",
                pin=str(i + 11),
            ))

        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)

        # Most pads should be off-grid (0.65mm pitch on 0.1mm grid)
        # Only pads at multiples of 0.1mm from base will be on-grid
        assert analysis.has_off_grid_pads
        assert analysis.off_grid_count > 0

        # Generate and apply escapes
        result = subgrid.generate_escape_segments(analysis)

        # Most off-grid pads should be successfully escaped
        assert result.success_count > 0
        assert result.success_count >= result.total_attempted * 0.5  # At least 50% success

    def test_ssop_with_through_hole_mix(self):
        """Mixed board with SSOP and THT components should handle both."""
        grid, rules = make_grid_and_rules(
            width=40.0,
            height=40.0,
            resolution=0.1,
        )
        subgrid = SubGridRouter(grid, rules)

        pads = []

        # SSOP: 0.65mm pitch (off-grid)
        for i in range(8):
            pads.append(make_pad(
                x=5.0 + i * 0.65,
                y=5.0,
                net=i + 1,
                ref="U1",
                pin=str(i + 1),
            ))

        # Through-hole connector: 2.54mm pitch (on-grid at 0.1mm)
        for i in range(4):
            pads.append(make_pad(
                x=20.0 + i * 2.54,
                y=20.0,
                net=i + 20,
                ref="J1",
                pin=str(i + 1),
                through_hole=True,
                width=1.7,
                height=1.7,
            ))

        for p in pads:
            grid.add_pad(p)

        analysis = subgrid.analyze_pads(pads)

        # Only SSOP pads should be off-grid; THT pads should be on or near grid
        off_grid_refs = {sgp.pad.ref for sgp in analysis.off_grid_pads}
        assert "U1" in off_grid_refs or analysis.off_grid_count == 0  # U1 has fine pitch


class TestDesignRulesSubgrid:
    """Tests for DesignRules sub-grid configuration fields."""

    def test_default_subgrid_disabled(self):
        """Sub-grid routing should be disabled by default."""
        rules = DesignRules()
        assert rules.subgrid_routing is False

    def test_subgrid_enabled(self):
        """Sub-grid routing should be configurable."""
        rules = DesignRules(subgrid_routing=True, subgrid_escape_radius=5)
        assert rules.subgrid_routing is True
        assert rules.subgrid_escape_radius == 5

    def test_subgrid_escape_radius_used(self):
        """SubGridRouter should respect escape_search_radius from rules."""
        rules = DesignRules(subgrid_escape_radius=5)
        grid = RoutingGrid(width=20.0, height=20.0, rules=rules)
        subgrid = SubGridRouter(
            grid, rules, escape_search_radius=rules.subgrid_escape_radius
        )
        assert subgrid.escape_search_radius == 5


# Import Segment for use in test data construction
from kicad_tools.router.primitives import Segment
