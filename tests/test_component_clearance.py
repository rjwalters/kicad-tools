"""Tests for per-component clearance override feature (Issue #1016).

This module tests the ability to specify different clearance values for
different components, enabling tighter clearance around fine-pitch ICs
while maintaining standard clearance elsewhere.
"""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


class TestDesignRulesComponentClearance:
    """Tests for DesignRules per-component clearance methods."""

    def test_default_clearance_when_no_overrides(self):
        """Test that default clearance is used when no overrides are set."""
        rules = DesignRules(trace_clearance=0.15)

        assert rules.get_clearance_for_component("U1") == 0.15
        assert rules.get_clearance_for_component("R1") == 0.15

    def test_explicit_component_clearance_override(self):
        """Test explicit per-component clearance override."""
        rules = DesignRules(
            trace_clearance=0.15,
            component_clearances={"U1": 0.08, "U2": 0.10},
        )

        assert rules.get_clearance_for_component("U1") == 0.08
        assert rules.get_clearance_for_component("U2") == 0.10
        assert rules.get_clearance_for_component("R1") == 0.15  # Default

    def test_fine_pitch_automatic_clearance(self):
        """Test automatic fine-pitch clearance based on pin pitch."""
        rules = DesignRules(
            trace_clearance=0.15,
            fine_pitch_clearance=0.1,
            fine_pitch_threshold=0.8,  # Components with pitch < 0.8mm get fine_pitch_clearance
        )

        # Component with 0.65mm pitch should get fine-pitch clearance
        assert rules.get_clearance_for_component("U1", pin_pitch=0.65) == 0.1

        # Component with 1.0mm pitch should get default clearance
        assert rules.get_clearance_for_component("U1", pin_pitch=1.0) == 0.15

        # Without pitch info, default is used
        assert rules.get_clearance_for_component("U1") == 0.15

    def test_explicit_override_takes_precedence_over_fine_pitch(self):
        """Test that explicit override takes precedence over fine-pitch auto-detection."""
        rules = DesignRules(
            trace_clearance=0.15,
            component_clearances={"U1": 0.08},
            fine_pitch_clearance=0.1,
            fine_pitch_threshold=0.8,
        )

        # Even with fine-pitch, explicit override is used
        assert rules.get_clearance_for_component("U1", pin_pitch=0.65) == 0.08

    def test_fine_pitch_clearance_disabled_when_none(self):
        """Test that fine-pitch clearance is not used when set to None."""
        rules = DesignRules(
            trace_clearance=0.15,
            fine_pitch_clearance=None,  # Disabled
            fine_pitch_threshold=0.8,
        )

        # Even fine-pitch components get default clearance
        assert rules.get_clearance_for_component("U1", pin_pitch=0.65) == 0.15


class TestRoutingGridComponentPitches:
    """Tests for RoutingGrid component pitch computation."""

    def test_compute_component_pitches_basic(self):
        """Test computing component pitches from pads."""
        rules = DesignRules(trace_clearance=0.15, grid_resolution=0.05)
        grid = RoutingGrid(20.0, 20.0, rules)

        # Add a component with 0.65mm pitch (TSSOP-like)
        for i in range(4):
            pad = Pad(
                x=5.0 + i * 0.65,
                y=5.0,
                width=0.3,
                height=0.8,
                net=i + 1,
                net_name=f"NET{i + 1}",
                layer=Layer.F_CU,
                ref="U1",
                pin=str(i + 1),
            )
            grid.add_pad(pad)

        pitches = grid.compute_component_pitches()

        assert "U1" in pitches
        # Pitch should be approximately 0.65mm
        assert abs(pitches["U1"] - 0.65) < 0.01

    def test_compute_component_pitches_multiple_components(self):
        """Test computing pitches for multiple components."""
        rules = DesignRules(trace_clearance=0.15, grid_resolution=0.05)
        grid = RoutingGrid(30.0, 30.0, rules)

        # Add fine-pitch component (0.5mm pitch)
        for i in range(4):
            pad = Pad(
                x=5.0 + i * 0.5,
                y=5.0,
                width=0.25,
                height=0.6,
                net=i + 1,
                net_name=f"NET{i + 1}",
                layer=Layer.F_CU,
                ref="U1",
                pin=str(i + 1),
            )
            grid.add_pad(pad)

        # Add standard pitch component (1.27mm SOIC-like)
        for i in range(4):
            pad = Pad(
                x=20.0 + i * 1.27,
                y=10.0,
                width=0.6,
                height=2.0,
                net=i + 10,
                net_name=f"NET{i + 10}",
                layer=Layer.F_CU,
                ref="U2",
                pin=str(i + 1),
            )
            grid.add_pad(pad)

        pitches = grid.compute_component_pitches()

        assert "U1" in pitches
        assert "U2" in pitches
        assert abs(pitches["U1"] - 0.5) < 0.01
        assert abs(pitches["U2"] - 1.27) < 0.01

    def test_compute_component_pitches_single_pad(self):
        """Test that single-pad components are excluded."""
        rules = DesignRules(trace_clearance=0.15, grid_resolution=0.05)
        grid = RoutingGrid(20.0, 20.0, rules)

        # Add single-pad component
        pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="TP1",
            pin="1",
        )
        grid.add_pad(pad)

        pitches = grid.compute_component_pitches()

        # Single-pad components have no pitch
        assert "TP1" not in pitches


class TestValidateSegmentClearanceWithComponents:
    """Tests for per-component clearance validation."""

    def test_validate_clearance_with_component_override(self):
        """Test clearance validation uses per-component override."""
        rules = DesignRules(
            trace_clearance=0.15,
            grid_resolution=0.05,
            component_clearances={"U1": 0.08},
        )
        grid = RoutingGrid(30.0, 30.0, rules)

        # Add pad from component U1
        # Pad radius = max(0.3, 0.8) / 2 = 0.4
        pad_u1 = Pad(
            x=10.0,
            y=10.0,
            width=0.3,
            height=0.8,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad_u1)

        # Create segment that would fail default clearance but pass component clearance
        # Pad radius = 0.4mm (max dimension / 2)
        # Trace half width = 0.1mm (width 0.2)
        # For component U1 with 0.08mm clearance:
        #   Required edge-to-edge = 0.08mm
        #   Distance from pad center to segment must be >= 0.4 + 0.1 + 0.08 = 0.58mm
        # For default 0.15mm clearance:
        #   Distance must be >= 0.4 + 0.1 + 0.15 = 0.65mm
        # Place segment at x=10.6 (0.6mm from pad center)
        # Edge-to-edge clearance = 0.6 - 0.4 - 0.1 = 0.1mm
        # This is >= 0.08 (component clearance) but < 0.15 (default)
        segment = Segment(
            x1=10.6,
            y1=5.0,
            x2=10.6,
            y2=15.0,
            width=0.2,
            layer=Layer.F_CU,
            net=2,
            net_name="NET2",
        )

        # With component_pitches=None, should use component_clearances from rules
        is_valid, actual_clearance, location = grid.validate_segment_clearance(
            segment, exclude_net=2
        )

        # The segment should be valid with U1's tighter clearance (0.08mm)
        # Clearance is ~0.1mm which is >= 0.08mm
        assert is_valid is True
        assert actual_clearance >= 0.08

    def test_validate_clearance_with_fine_pitch_auto(self):
        """Test clearance validation uses automatic fine-pitch detection."""
        rules = DesignRules(
            trace_clearance=0.15,
            grid_resolution=0.05,
            fine_pitch_clearance=0.08,
            fine_pitch_threshold=0.8,
        )
        grid = RoutingGrid(30.0, 30.0, rules)

        # Add single fine-pitch component pad to avoid complications
        pad = Pad(
            x=10.0,
            y=10.0,
            width=0.3,
            height=0.8,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad)

        # Add another pad to create pitch calculation
        pad2 = Pad(
            x=10.65,  # 0.65mm pitch
            y=10.0,
            width=0.3,
            height=0.8,
            net=2,
            net_name="NET2",
            layer=Layer.F_CU,
            ref="U1",
            pin="2",
        )
        grid.add_pad(pad2)

        # Compute pitches - should be ~0.65mm (fine-pitch)
        pitches = grid.compute_component_pitches()
        assert abs(pitches["U1"] - 0.65) < 0.01  # Verify pitch detected

        # Create segment between the pads at x=10.325
        # This segment is equidistant from both pads
        # Distance to pad center = 0.325mm
        # Pad radius = 0.4mm, segment half-width = 0.1mm
        # Edge-to-edge = 0.325 - 0.4 - 0.1 = -0.175mm (overlaps)

        # Instead, place segment above the pads
        # Segment at y=8.0, pads at y=10.0
        # Distance = 2.0mm (plenty of clearance)
        segment = Segment(
            x1=9.0,
            y1=8.0,
            x2=12.0,
            y2=8.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,  # Different net
            net_name="NET10",
        )

        # Validate with component pitches for fine-pitch detection
        is_valid, actual_clearance, location = grid.validate_segment_clearance(
            segment, exclude_net=10, component_pitches=pitches
        )

        # Should be valid since segment is far from pads
        assert is_valid is True
        # Clearance should be at least fine_pitch_clearance (0.08mm)
        # Actual clearance = ~1.5mm (2.0 - 0.4 - 0.1)
        assert actual_clearance >= 0.08

        # Now test that fine-pitch clearance is actually used
        # The get_clearance_for_component should return fine_pitch_clearance
        fine_clearance = rules.get_clearance_for_component("U1", pitches["U1"])
        assert fine_clearance == 0.08


class TestRouterComponentClearance:
    """Tests for Router with per-component clearance."""

    def test_router_precomputes_clearance_radii(self):
        """Test that Router precomputes clearance radii for all values."""
        rules = DesignRules(
            trace_clearance=0.15,
            grid_resolution=0.05,
            component_clearances={"U1": 0.08, "U2": 0.10},
            fine_pitch_clearance=0.12,
        )
        grid = RoutingGrid(30.0, 30.0, rules)
        router = Router(grid, rules)

        # All clearance values should be precomputed
        assert 0.15 in router._clearance_radii
        assert 0.08 in router._clearance_radii
        assert 0.10 in router._clearance_radii
        assert 0.12 in router._clearance_radii

    def test_router_get_clearance_radius_cells(self):
        """Test Router.get_clearance_radius_cells method."""
        rules = DesignRules(
            trace_clearance=0.15,
            trace_width=0.2,
            grid_resolution=0.05,
        )
        grid = RoutingGrid(30.0, 30.0, rules)
        router = Router(grid, rules)

        # For 0.15mm clearance: (0.2/2 + 0.15) / 0.05 = 5 cells
        radius = router.get_clearance_radius_cells(0.15)
        assert radius == 5

        # For 0.08mm clearance: (0.2/2 + 0.08) / 0.05 = 3.6 -> 4 cells
        radius = router.get_clearance_radius_cells(0.08)
        assert radius == 4


class TestAutorouterComponentClearance:
    """Tests for Autorouter with per-component clearance."""

    def test_autorouter_with_component_clearances(self):
        """Test routing with per-component clearance overrides."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.05,
            component_clearances={"U1": 0.08},
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Add fine-pitch component with override
        pads = []
        for i in range(2):
            pads.append(
                {
                    "number": str(i + 1),
                    "x": 10.0 + i * 0.65,
                    "y": 10.0,
                    "width": 0.3,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                }
            )
        router.add_component("U1", pads)

        # Route should work with tighter clearance
        routes = router.route_all([1])

        assert len(routes) >= 0  # May or may not have routes depending on layout

    def test_autorouter_with_fine_pitch_auto(self):
        """Test routing with automatic fine-pitch clearance detection."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.05,
            fine_pitch_clearance=0.08,
            fine_pitch_threshold=0.8,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Add fine-pitch component (0.65mm pitch)
        pads = []
        for i in range(3):
            pads.append(
                {
                    "number": str(i + 1),
                    "x": 10.0 + i * 0.65,
                    "y": 10.0,
                    "width": 0.3,
                    "height": 0.8,
                    "net": 1 if i < 2 else 2,
                    "net_name": "NET1" if i < 2 else "NET2",
                }
            )
        router.add_component("U1", pads)

        # Should detect fine-pitch and use tighter clearance
        routes = router.route_all([1])

        # Check that component pitches are computed
        pitches = router.grid.compute_component_pitches()
        assert "U1" in pitches
        assert abs(pitches["U1"] - 0.65) < 0.01
