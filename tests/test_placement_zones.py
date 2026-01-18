"""Tests for zone-based component placement optimization."""

import pytest

from kicad_tools.optim import (
    Component,
    PlacementOptimizer,
    PlacementZone,
    Polygon,
    assign_zone,
    expand_regex_pattern,
)


class TestPlacementZone:
    """Tests for PlacementZone dataclass."""

    def test_basic_creation(self):
        zone = PlacementZone(name="power", x=10, y=20, width=50, height=30)
        assert zone.name == "power"
        assert zone.x == 10
        assert zone.y == 20
        assert zone.width == 50
        assert zone.height == 30

    def test_x_max_y_max(self):
        zone = PlacementZone(name="test", x=10, y=20, width=50, height=30)
        assert zone.x_max == 60  # 10 + 50
        assert zone.y_max == 50  # 20 + 30

    def test_center(self):
        zone = PlacementZone(name="test", x=10, y=20, width=50, height=30)
        cx, cy = zone.center
        assert cx == 35  # 10 + 50/2
        assert cy == 35  # 20 + 30/2

    def test_contains_point(self):
        zone = PlacementZone(name="test", x=10, y=10, width=20, height=20)
        # Inside
        assert zone.contains_point(15, 15)
        assert zone.contains_point(10, 10)  # Edge
        assert zone.contains_point(30, 30)  # Edge
        # Outside
        assert not zone.contains_point(5, 15)
        assert not zone.contains_point(35, 15)
        assert not zone.contains_point(15, 5)
        assert not zone.contains_point(15, 35)

    def test_to_constraint(self):
        zone = PlacementZone(name="test", x=10, y=20, width=50, height=30)
        constraint = zone.to_constraint()
        assert constraint.parameters["x"] == 10
        assert constraint.parameters["y"] == 20
        assert constraint.parameters["width"] == 50
        assert constraint.parameters["height"] == 30

    def test_invalid_width_raises(self):
        with pytest.raises(ValueError, match="width must be positive"):
            PlacementZone(name="bad", x=0, y=0, width=0, height=10)

    def test_invalid_height_raises(self):
        with pytest.raises(ValueError, match="height must be positive"):
            PlacementZone(name="bad", x=0, y=0, width=10, height=-5)


class TestExpandRegexPattern:
    """Tests for regex pattern expansion."""

    def test_simple_pattern(self):
        refs = ["C1", "C2", "C3", "R1", "R2"]
        result = expand_regex_pattern(r"C[0-9]", refs)
        assert result == ["C1", "C2", "C3"]

    def test_range_pattern(self):
        refs = ["C10", "C15", "C20", "C25", "C30"]
        result = expand_regex_pattern(r"C[12][05]", refs)
        assert result == ["C10", "C15", "C20", "C25"]

    def test_complex_range(self):
        # Pattern for C100-C169
        refs = [f"C{i}" for i in range(100, 200)]
        result = expand_regex_pattern(r"C1[0-6][0-9]", refs)
        assert len(result) == 70  # C100-C169
        assert "C100" in result
        assert "C169" in result
        assert "C170" not in result

    def test_wildcard_pattern(self):
        refs = ["R1", "R12", "R123", "C1"]
        result = expand_regex_pattern(r"R[0-9]+", refs)
        assert result == ["R1", "R12", "R123"]

    def test_exact_match(self):
        refs = ["U1", "U10", "U11", "U2"]
        result = expand_regex_pattern(r"U1", refs)
        assert result == ["U1"]

    def test_no_matches(self):
        refs = ["C1", "C2", "R1"]
        result = expand_regex_pattern(r"LED[0-9]+", refs)
        assert result == []

    def test_invalid_regex_raises(self):
        with pytest.raises(ValueError, match="Invalid regex pattern"):
            expand_regex_pattern(r"[invalid", ["C1", "C2"])


class TestAssignZone:
    """Tests for zone assignment function."""

    def setup_method(self):
        """Create a test optimizer with components."""
        board = Polygon.rectangle(100, 100, 200, 150)
        self.optimizer = PlacementOptimizer(board)

        # Add test components
        for i in range(1, 11):
            comp = Component(ref=f"C{i}", x=10 * i, y=10 * i, width=2, height=1)
            self.optimizer.add_component(comp)

        for i in range(1, 6):
            comp = Component(ref=f"R{i}", x=50 + 5 * i, y=50, width=1.5, height=0.5)
            self.optimizer.add_component(comp)

        for i in range(1, 4):
            comp = Component(ref=f"U{i}", x=80, y=30 * i, width=5, height=5)
            self.optimizer.add_component(comp)

    def test_assign_by_references(self):
        zone = PlacementZone(name="caps", x=10, y=10, width=50, height=50)
        assigned = assign_zone(self.optimizer, zone, references=["C1", "C2", "C3"])
        assert assigned == ["C1", "C2", "C3"]
        assert zone.assigned_components == ["C1", "C2", "C3"]

    def test_assign_by_pattern(self):
        zone = PlacementZone(name="resistors", x=50, y=40, width=30, height=20)
        assigned = assign_zone(self.optimizer, zone, pattern=r"R[0-9]+")
        assert len(assigned) == 5
        assert "R1" in assigned
        assert "R5" in assigned

    def test_assign_combined(self):
        zone = PlacementZone(name="mixed", x=0, y=0, width=100, height=100)
        assigned = assign_zone(
            self.optimizer,
            zone,
            pattern=r"C[1-3]",
            references=["U1", "U2"],
        )
        # Should include U1, U2 first, then C1, C2, C3
        assert "U1" in assigned
        assert "U2" in assigned
        assert "C1" in assigned
        assert "C2" in assigned
        assert "C3" in assigned
        assert len(assigned) == 5

    def test_assign_creates_constraint(self):
        zone = PlacementZone(name="test", x=10, y=10, width=50, height=50)
        assign_zone(self.optimizer, zone, references=["C1", "C2"])

        # Check that grouping constraint was added
        assert len(self.optimizer.grouping_constraints) == 1
        constraint = self.optimizer.grouping_constraints[0]
        assert constraint.name == "zone_test"
        assert "C1" in constraint.members
        assert "C2" in constraint.members

    def test_assign_nonexistent_reference_filtered(self):
        zone = PlacementZone(name="test", x=0, y=0, width=100, height=100)
        assigned = assign_zone(
            self.optimizer,
            zone,
            references=["C1", "NONEXISTENT", "C2"],
        )
        assert assigned == ["C1", "C2"]

    def test_assign_empty_result(self):
        zone = PlacementZone(name="empty", x=0, y=0, width=100, height=100)
        assigned = assign_zone(self.optimizer, zone, pattern=r"LED[0-9]+")
        assert assigned == []

    def test_assign_requires_pattern_or_references(self):
        zone = PlacementZone(name="test", x=0, y=0, width=10, height=10)
        with pytest.raises(ValueError, match="Must specify either"):
            assign_zone(self.optimizer, zone)


class TestPlacementOptimizerZoneMethods:
    """Tests for PlacementOptimizer zone methods."""

    def setup_method(self):
        """Create a test optimizer with components."""
        board = Polygon.rectangle(100, 100, 200, 150)
        self.optimizer = PlacementOptimizer(board)

        # Add test components
        for i in range(100, 170):
            comp = Component(ref=f"C{i}", x=10 + (i % 10) * 5, y=10 + (i // 10) * 5)
            self.optimizer.add_component(comp)

        for i in range(1, 5):
            comp = Component(ref=f"Q{i}", x=150, y=30 * i, width=3, height=3)
            self.optimizer.add_component(comp)

    def test_add_zone(self):
        zone = self.optimizer.add_zone("supercaps", x=10, y=10, width=120, height=50)
        assert zone.name == "supercaps"
        assert zone.x == 10
        assert zone.width == 120

    def test_get_zone(self):
        self.optimizer.add_zone("power", x=0, y=0, width=50, height=50)
        zone = self.optimizer.get_zone("power")
        assert zone is not None
        assert zone.name == "power"

    def test_get_zone_not_found(self):
        zone = self.optimizer.get_zone("nonexistent")
        assert zone is None

    def test_get_zones(self):
        self.optimizer.add_zone("zone1", x=0, y=0, width=50, height=50)
        self.optimizer.add_zone("zone2", x=60, y=0, width=50, height=50)
        zones = self.optimizer.get_zones()
        assert len(zones) == 2
        names = [z.name for z in zones]
        assert "zone1" in names
        assert "zone2" in names

    def test_get_zones_empty(self):
        zones = self.optimizer.get_zones()
        assert zones == []

    def test_assign_to_zone(self):
        self.optimizer.add_zone("supercaps", x=10, y=10, width=120, height=50)
        assigned = self.optimizer.assign_to_zone("supercaps", pattern=r"C1[0-6][0-9]")

        # Should match C100-C169
        assert len(assigned) == 70
        assert "C100" in assigned
        assert "C169" in assigned

    def test_assign_to_zone_by_references(self):
        self.optimizer.add_zone("power_output", x=140, y=10, width=20, height=100)
        assigned = self.optimizer.assign_to_zone(
            "power_output",
            references=["Q1", "Q2", "Q3", "Q4"],
        )
        assert assigned == ["Q1", "Q2", "Q3", "Q4"]

    def test_assign_to_zone_not_found(self):
        with pytest.raises(ValueError, match="Zone 'nonexistent' not found"):
            self.optimizer.assign_to_zone("nonexistent", references=["C1"])

    def test_full_workflow(self):
        """Test the complete zone-based placement workflow."""
        # Define zones
        self.optimizer.add_zone("supercaps", x=10, y=10, width=120, height=50)
        self.optimizer.add_zone("control", x=140, y=10, width=50, height=100)
        self.optimizer.add_zone("power_output", x=180, y=10, width=15, height=100)

        # Assign components to zones
        caps_assigned = self.optimizer.assign_to_zone(
            "supercaps",
            pattern=r"C1[0-6][0-9]",
        )
        control_assigned = self.optimizer.assign_to_zone(
            "control",
            references=["C100", "C101"],  # Some overlap for control logic
        )
        power_assigned = self.optimizer.assign_to_zone(
            "power_output",
            pattern=r"Q[1-4]",
        )

        # Verify assignments
        assert len(caps_assigned) == 70
        assert len(power_assigned) == 4

        # Verify constraints were created
        assert len(self.optimizer.grouping_constraints) == 3

        # Run a few optimization steps
        iterations = self.optimizer.run(iterations=10, dt=0.01)
        assert iterations > 0


class TestZoneIntegrationWithConstraints:
    """Test zone integration with the constraint system."""

    def test_zone_constraint_enforcement(self):
        """Test that zone constraints are enforced during optimization."""
        board = Polygon.rectangle(100, 100, 200, 150)
        optimizer = PlacementOptimizer(board)

        # Add components outside the zone
        optimizer.add_component(Component(ref="C1", x=150, y=150, width=2, height=1))
        optimizer.add_component(Component(ref="C2", x=160, y=160, width=2, height=1))

        # Add zone and assign components
        optimizer.add_zone("small_zone", x=10, y=10, width=30, height=30)
        optimizer.assign_to_zone("small_zone", references=["C1", "C2"])

        # Run optimization - components should be pushed toward zone
        optimizer.run(iterations=100, dt=0.01)

        # Components should have moved closer to the zone
        c1 = optimizer.get_component("C1")
        c2 = optimizer.get_component("C2")

        # They should be closer to the zone center (25, 25) than their start
        zone = optimizer.get_zone("small_zone")
        center_x, center_y = zone.center

        # Check that they moved toward the zone (not necessarily inside yet)
        # The force should have pulled them in that direction
        assert c1.x < 150 or c1.y < 150  # Moved toward zone
        assert c2.x < 160 or c2.y < 160
