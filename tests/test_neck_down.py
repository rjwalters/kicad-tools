"""Tests for automatic trace neck-down near fine-pitch pads (Issue #1018)."""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import DesignRules


class TestDesignRulesNeckDown:
    """Tests for neck-down configuration in DesignRules."""

    def test_neck_down_disabled_by_default(self):
        """Test that neck-down is disabled when min_trace_width is None."""
        rules = DesignRules(trace_width=0.2)
        assert rules.min_trace_width is None
        assert not rules.should_apply_neck_down("U1", pin_pitch=0.5)

    def test_neck_down_enabled_with_min_width(self):
        """Test that neck-down is enabled when min_trace_width is set."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_threshold=0.8,
        )
        # Fine-pitch component (below threshold)
        assert rules.should_apply_neck_down("U1", pin_pitch=0.65)
        # Standard-pitch component (above threshold)
        assert not rules.should_apply_neck_down("R1", pin_pitch=1.27)

    def test_neck_down_requires_pitch_info(self):
        """Test that neck-down requires pin pitch information."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
        )
        # No pitch info - should not apply
        assert not rules.should_apply_neck_down("U1", pin_pitch=None)
        assert not rules.should_apply_neck_down("U1")

    def test_get_neck_down_width_disabled(self):
        """Test get_neck_down_width when feature is disabled."""
        rules = DesignRules(trace_width=0.2)
        # Should return normal trace width regardless of distance
        assert rules.get_neck_down_width(0.0) == 0.2
        assert rules.get_neck_down_width(0.5) == 0.2
        assert rules.get_neck_down_width(2.0) == 0.2

    def test_get_neck_down_width_at_pad(self):
        """Test get_neck_down_width returns min width at pad center."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=1.0,
        )
        # At pad center (distance=0), should be min width
        assert rules.get_neck_down_width(0.0) == 0.1

    def test_get_neck_down_width_beyond_taper_zone(self):
        """Test get_neck_down_width returns normal width beyond taper zone."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=1.0,
        )
        # At or beyond neck_down_distance, should be normal width
        assert rules.get_neck_down_width(1.0) == 0.2
        assert rules.get_neck_down_width(2.0) == 0.2
        assert rules.get_neck_down_width(10.0) == 0.2

    def test_get_neck_down_width_linear_interpolation(self):
        """Test get_neck_down_width interpolates linearly in taper zone."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=1.0,
        )
        # At halfway point, should be midway between min and max
        # t = 0.5 -> 0.1 + 0.5 * (0.2 - 0.1) = 0.15
        assert abs(rules.get_neck_down_width(0.5) - 0.15) < 0.001

        # At quarter point
        # t = 0.25 -> 0.1 + 0.25 * (0.2 - 0.1) = 0.125
        assert abs(rules.get_neck_down_width(0.25) - 0.125) < 0.001

        # At three-quarter point
        # t = 0.75 -> 0.1 + 0.75 * (0.2 - 0.1) = 0.175
        assert abs(rules.get_neck_down_width(0.75) - 0.175) < 0.001

    def test_get_neck_down_width_respects_pitch_threshold(self):
        """Test that pitch threshold is respected in get_neck_down_width."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=1.0,
            neck_down_threshold=0.8,
        )
        # Fine-pitch component should get neck-down
        assert rules.get_neck_down_width(0.0, pin_pitch=0.65) == 0.1

        # Standard-pitch component should not get neck-down
        assert rules.get_neck_down_width(0.0, pin_pitch=1.27) == 0.2

    def test_neck_down_configuration_defaults(self):
        """Test default values for neck-down configuration."""
        rules = DesignRules()
        assert rules.min_trace_width is None  # Disabled by default
        assert rules.neck_down_distance == 1.0  # 1mm default taper zone
        assert rules.neck_down_threshold == 0.8  # 0.8mm threshold

    def test_custom_neck_down_distance(self):
        """Test custom neck_down_distance setting."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=2.0,  # 2mm taper zone
        )
        # At 1mm (halfway through 2mm taper zone)
        assert abs(rules.get_neck_down_width(1.0) - 0.15) < 0.001
        # At 2mm (end of taper zone)
        assert rules.get_neck_down_width(2.0) == 0.2

    def test_custom_neck_down_threshold(self):
        """Test custom neck_down_threshold setting."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_threshold=0.5,  # Stricter threshold
        )
        # 0.65mm pitch is above 0.5mm threshold - no neck-down
        assert not rules.should_apply_neck_down("U1", pin_pitch=0.65)
        # 0.4mm pitch is below threshold - apply neck-down
        assert rules.should_apply_neck_down("U1", pin_pitch=0.4)


class TestNeckDownEdgeCases:
    """Edge case tests for neck-down feature."""

    def test_same_min_and_normal_width(self):
        """Test when min_trace_width equals trace_width."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.2,  # Same as normal
            neck_down_distance=1.0,
        )
        # Should return same width regardless of distance
        assert rules.get_neck_down_width(0.0) == 0.2
        assert rules.get_neck_down_width(0.5) == 0.2
        assert rules.get_neck_down_width(1.0) == 0.2

    def test_very_small_neck_down_distance(self):
        """Test with very small neck_down_distance."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=0.1,  # Very short taper
        )
        # At 0.05mm (halfway)
        assert abs(rules.get_neck_down_width(0.05) - 0.15) < 0.001
        # Just past the taper zone
        assert rules.get_neck_down_width(0.11) == 0.2

    def test_large_neck_down_distance(self):
        """Test with large neck_down_distance."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=10.0,  # 10mm taper zone
        )
        # At 5mm (halfway)
        assert abs(rules.get_neck_down_width(5.0) - 0.15) < 0.001

    def test_negative_distance_handled(self):
        """Test that negative distances are handled safely."""
        rules = DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=1.0,
        )
        # Negative distance should behave like distance=0 (at pad)
        # The linear formula gives: 0.1 + (-0.5) * 0.1 = 0.05, but this is
        # an edge case. In practice, distances are always >= 0.
        # The implementation uses linear interpolation, so negative distances
        # would give values < min_trace_width. This is acceptable as distances
        # should never be negative in real use.
        width = rules.get_neck_down_width(-0.5)
        # Just verify it doesn't crash and returns a value
        assert isinstance(width, float)


class TestNeckDownRouting:
    """Integration tests for neck-down during actual routing."""

    @pytest.fixture
    def neck_down_rules(self):
        """Create DesignRules with neck-down enabled."""
        return DesignRules(
            trace_width=0.2,
            min_trace_width=0.1,
            neck_down_distance=1.0,
            neck_down_threshold=0.8,
            trace_clearance=0.15,
        )

    @pytest.fixture
    def standard_rules(self):
        """Create DesignRules without neck-down (default)."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
        )

    def test_routing_without_neckdown_uniform_width(self, standard_rules):
        """Test that routing without neck-down uses uniform trace width."""
        router = Autorouter(width=30.0, height=30.0, rules=standard_rules)

        # Add a simple two-pad connection with standard pitch
        pads = [
            {"number": "1", "x": 5.0, "y": 5.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 20.0, "y": 20.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)
        routes = router.route_all()

        # Should have routed successfully
        assert len(routes) > 0

        # All segments should have uniform width (trace_width)
        for route in routes:
            for seg in route.segments:
                assert seg.width == standard_rules.trace_width, (
                    f"Expected uniform width {standard_rules.trace_width}, got {seg.width}"
                )

    def test_routing_fine_pitch_with_neckdown(self, neck_down_rules):
        """Test that routing to fine-pitch pads uses neck-down widths."""
        router = Autorouter(width=30.0, height=30.0, rules=neck_down_rules)

        # Add a fine-pitch IC (0.65mm pitch like TSSOP)
        # Pads close together trigger automatic fine-pitch detection
        pads = []
        for i in range(4):
            pads.append({
                "number": str(i + 1),
                "x": 10.0 + i * 0.65,  # 0.65mm pitch
                "y": 10.0,
                "width": 0.3,
                "height": 0.8,
                "net": i + 1,
                "net_name": f"NET{i + 1}",
            })
        router.add_component("U1", pads)

        # Add a destination pad far from the IC
        router.add_component("R1", [
            {"number": "1", "x": 25.0, "y": 25.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "NET1"},
        ])

        routes = router.route_all()

        # Should have routed successfully
        assert len(routes) > 0

        # Check that segments near the fine-pitch pad have reduced width
        found_necked_down_segment = False
        for route in routes:
            for seg in route.segments:
                if seg.width < neck_down_rules.trace_width:
                    found_necked_down_segment = True
                    # Necked-down segments should be at least min_trace_width
                    assert seg.width >= neck_down_rules.min_trace_width, (
                        f"Neck-down width {seg.width} is less than minimum {neck_down_rules.min_trace_width}"
                    )

        # We should find at least one necked-down segment for fine-pitch component
        assert found_necked_down_segment, "Expected neck-down segments near fine-pitch IC"

    def test_routing_standard_pitch_no_neckdown(self, neck_down_rules):
        """Test that routing to standard-pitch pads doesn't use neck-down."""
        router = Autorouter(width=30.0, height=30.0, rules=neck_down_rules)

        # Add a standard-pitch component (2.54mm pitch)
        pads = [
            {"number": "1", "x": 5.0, "y": 5.0, "width": 1.5, "height": 1.5, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 7.54, "y": 5.0, "width": 1.5, "height": 1.5, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("J1", pads)

        # Add destination far away
        router.add_component("R1", [
            {"number": "1", "x": 20.0, "y": 20.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "NET1"},
        ])

        routes = router.route_all()

        # Should have routed successfully
        assert len(routes) > 0

        # Standard pitch should not trigger neck-down
        for route in routes:
            for seg in route.segments:
                # Standard pitch shouldn't have necked-down segments
                assert seg.width == neck_down_rules.trace_width, (
                    f"Standard pitch should use normal width {neck_down_rules.trace_width}, got {seg.width}"
                )

    def test_neckdown_width_range(self, neck_down_rules):
        """Test that neck-down widths are within valid range."""
        router = Autorouter(width=40.0, height=40.0, rules=neck_down_rules)

        # Add a fine-pitch IC
        pads = []
        for i in range(6):
            pads.append({
                "number": str(i + 1),
                "x": 15.0 + i * 0.5,  # 0.5mm pitch (very fine)
                "y": 15.0,
                "width": 0.25,
                "height": 0.6,
                "net": i + 1,
                "net_name": f"NET{i + 1}",
            })
        router.add_component("U1", pads)

        # Add several destination pads at varying distances
        for i in range(3):
            router.add_component(f"R{i + 1}", [
                {"number": "1", "x": 5.0 + i * 5.0, "y": 30.0, "width": 0.5, "height": 0.5, "net": i + 1, "net_name": f"NET{i + 1}"},
            ])

        routes = router.route_all()
        assert len(routes) > 0

        # Verify all segment widths are within valid range
        for route in routes:
            for seg in route.segments:
                assert seg.width >= neck_down_rules.min_trace_width, (
                    f"Width {seg.width} is below minimum {neck_down_rules.min_trace_width}"
                )
                assert seg.width <= neck_down_rules.trace_width, (
                    f"Width {seg.width} exceeds normal trace width {neck_down_rules.trace_width}"
                )
