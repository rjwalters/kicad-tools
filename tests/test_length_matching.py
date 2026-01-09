"""Tests for length matching and serpentine tuning functionality.

Tests cover:
- LengthConstraint validation
- LengthTracker class methods
- SerpentineGenerator and serpentine tuning
- Match group functionality
"""

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.length import (
    LengthTracker,
    LengthViolation,
    ViolationType,
    create_match_group,
)
from kicad_tools.router.optimizer.serpentine import (
    SerpentineConfig,
    SerpentineGenerator,
    SerpentineStyle,
    add_serpentine,
    tune_match_group,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import LengthConstraint


class TestLengthConstraint:
    """Tests for LengthConstraint dataclass."""

    def test_basic_constraint(self):
        """Test creating a basic length constraint."""
        constraint = LengthConstraint(net_id=100, min_length=10.0, max_length=50.0)
        assert constraint.net_id == 100
        assert constraint.min_length == 10.0
        assert constraint.max_length == 50.0
        assert constraint.match_group is None
        assert constraint.match_tolerance == 0.5

    def test_constraint_with_match_group(self):
        """Test creating a constraint with match group."""
        constraint = LengthConstraint(
            net_id=100,
            match_group="DDR_DATA",
            match_tolerance=0.25,
        )
        assert constraint.match_group == "DDR_DATA"
        assert constraint.match_tolerance == 0.25

    def test_constraint_validation_min_greater_than_max(self):
        """Test that min_length > max_length raises error."""
        with pytest.raises(ValueError, match="min_length.*cannot be greater than"):
            LengthConstraint(net_id=100, min_length=50.0, max_length=10.0)

    def test_constraint_validation_negative_tolerance(self):
        """Test that negative tolerance raises error."""
        with pytest.raises(ValueError, match="match_tolerance must be non-negative"):
            LengthConstraint(net_id=100, match_tolerance=-0.5)

    def test_constraint_min_only(self):
        """Test constraint with only min_length."""
        constraint = LengthConstraint(net_id=100, min_length=20.0)
        assert constraint.min_length == 20.0
        assert constraint.max_length is None

    def test_constraint_max_only(self):
        """Test constraint with only max_length."""
        constraint = LengthConstraint(net_id=100, max_length=100.0)
        assert constraint.min_length is None
        assert constraint.max_length == 100.0


class TestLengthTracker:
    """Tests for LengthTracker class."""

    @pytest.fixture
    def simple_route(self):
        """Create a simple route with known length."""
        # Horizontal segment: 10mm
        seg1 = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        # Vertical segment: 5mm
        seg2 = Segment(x1=10, y1=0, x2=10, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        return Route(net=1, net_name="NET1", segments=[seg1, seg2])

    @pytest.fixture
    def diagonal_route(self):
        """Create a route with a diagonal segment."""
        # Diagonal segment: sqrt(9 + 16) = 5mm
        seg = Segment(x1=0, y1=0, x2=3, y2=4, width=0.2, layer=Layer.F_CU, net=2)
        return Route(net=2, net_name="NET2", segments=[seg])

    def test_calculate_route_length_simple(self, simple_route):
        """Test calculating route length for horizontal/vertical segments."""
        length = LengthTracker.calculate_route_length(simple_route)
        assert abs(length - 15.0) < 0.001  # 10mm + 5mm

    def test_calculate_route_length_diagonal(self, diagonal_route):
        """Test calculating route length for diagonal segment."""
        length = LengthTracker.calculate_route_length(diagonal_route)
        assert abs(length - 5.0) < 0.001  # sqrt(3^2 + 4^2) = 5

    def test_calculate_route_length_empty(self):
        """Test calculating route length for empty route."""
        route = Route(net=1, net_name="NET1", segments=[])
        length = LengthTracker.calculate_route_length(route)
        assert length == 0.0

    def test_record_route(self, simple_route):
        """Test recording a route length."""
        tracker = LengthTracker()
        length = tracker.record_route(1, simple_route)
        assert abs(length - 15.0) < 0.001
        assert abs(tracker.get_length(1) - 15.0) < 0.001

    def test_record_length(self):
        """Test recording a pre-calculated length."""
        tracker = LengthTracker()
        tracker.record_length(100, 25.5)
        assert tracker.get_length(100) == 25.5

    def test_get_length_not_recorded(self):
        """Test getting length for unrecorded net."""
        tracker = LengthTracker()
        assert tracker.get_length(999) is None

    def test_add_constraint(self):
        """Test adding a constraint."""
        tracker = LengthTracker()
        constraint = LengthConstraint(net_id=100, min_length=10.0)
        tracker.add_constraint(constraint)
        assert tracker.get_constraint(100) == constraint

    def test_add_constraint_with_match_group(self):
        """Test adding constraints with match groups."""
        tracker = LengthTracker()
        c1 = LengthConstraint(net_id=100, match_group="BUS")
        c2 = LengthConstraint(net_id=101, match_group="BUS")
        tracker.add_constraint(c1)
        tracker.add_constraint(c2)
        assert 100 in tracker.match_groups["BUS"]
        assert 101 in tracker.match_groups["BUS"]

    def test_get_violations_too_short(self):
        """Test detecting TOO_SHORT violations."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, min_length=20.0))
        tracker.record_length(100, 15.0)  # Too short

        violations = tracker.get_violations()
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.TOO_SHORT
        assert violations[0].net_id == 100
        assert abs(violations[0].delta - 5.0) < 0.001

    def test_get_violations_too_long(self):
        """Test detecting TOO_LONG violations."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, max_length=20.0))
        tracker.record_length(100, 25.0)  # Too long

        violations = tracker.get_violations()
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.TOO_LONG
        assert violations[0].net_id == 100
        assert abs(violations[0].delta - 5.0) < 0.001

    def test_get_violations_mismatch(self):
        """Test detecting MISMATCH violations in match groups."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, match_group="BUS", match_tolerance=0.5))
        tracker.add_constraint(LengthConstraint(net_id=101, match_group="BUS", match_tolerance=0.5))
        tracker.record_length(100, 20.0)
        tracker.record_length(101, 25.0)  # 5mm difference > 0.5mm tolerance

        violations = tracker.get_violations()
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.MISMATCH
        assert violations[0].match_group == "BUS"
        assert abs(violations[0].delta - 5.0) < 0.001

    def test_get_violations_no_violation(self):
        """Test that no violations returned when constraints are met."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, min_length=10.0, max_length=30.0))
        tracker.record_length(100, 20.0)  # Within range

        violations = tracker.get_violations()
        assert len(violations) == 0

    def test_get_violations_match_group_within_tolerance(self):
        """Test that match groups within tolerance have no violations."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, match_group="BUS", match_tolerance=1.0))
        tracker.add_constraint(LengthConstraint(net_id=101, match_group="BUS", match_tolerance=1.0))
        tracker.record_length(100, 20.0)
        tracker.record_length(101, 20.5)  # 0.5mm difference < 1.0mm tolerance

        violations = tracker.get_violations()
        assert len(violations) == 0

    def test_get_match_group_target(self):
        """Test getting target length for match group (longest net)."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, match_group="BUS"))
        tracker.add_constraint(LengthConstraint(net_id=101, match_group="BUS"))
        tracker.record_length(100, 20.0)
        tracker.record_length(101, 25.0)

        target = tracker.get_match_group_target("BUS")
        assert target == 25.0

    def test_get_length_needed(self):
        """Test calculating additional length needed."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, min_length=30.0))
        tracker.record_length(100, 20.0)

        needed = tracker.get_length_needed(100)
        assert abs(needed - 10.0) < 0.001

    def test_get_length_needed_match_group(self):
        """Test calculating length needed for match group."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, match_group="BUS", match_tolerance=0.5))
        tracker.add_constraint(LengthConstraint(net_id=101, match_group="BUS", match_tolerance=0.5))
        tracker.record_length(100, 20.0)
        tracker.record_length(101, 30.0)  # Longest

        needed = tracker.get_length_needed(100)
        assert abs(needed - 10.0) < 0.001  # Need to match 30mm

    def test_get_statistics(self):
        """Test getting length statistics."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, min_length=10.0))
        tracker.add_constraint(LengthConstraint(net_id=101, match_group="BUS"))
        tracker.record_length(100, 15.0)
        tracker.record_length(101, 20.0)
        tracker.record_length(102, 25.0)  # No constraint

        stats = tracker.get_statistics()
        assert stats["total_nets"] == 3
        assert stats["constrained_nets"] == 2
        assert stats["min_length"] == 15.0
        assert stats["max_length"] == 25.0

    def test_clear(self):
        """Test clearing recorded lengths."""
        tracker = LengthTracker()
        tracker.add_constraint(LengthConstraint(net_id=100, min_length=10.0))
        tracker.record_length(100, 15.0)

        tracker.clear()
        assert tracker.get_length(100) is None
        assert tracker.get_constraint(100) is not None  # Constraints kept


class TestCreateMatchGroup:
    """Tests for create_match_group helper function."""

    def test_create_match_group_basic(self):
        """Test creating a basic match group."""
        constraints = create_match_group("DDR_DATA", [100, 101, 102, 103])
        assert len(constraints) == 4
        for c in constraints:
            assert c.match_group == "DDR_DATA"
            assert c.match_tolerance == 0.5  # Default

    def test_create_match_group_custom_tolerance(self):
        """Test creating match group with custom tolerance."""
        constraints = create_match_group("HIGH_SPEED", [200, 201], tolerance=0.1)
        for c in constraints:
            assert c.match_tolerance == 0.1

    def test_create_match_group_with_min_max(self):
        """Test creating match group with min/max lengths."""
        constraints = create_match_group(
            "CLOCK",
            [300, 301],
            min_length=50.0,
            max_length=100.0,
        )
        for c in constraints:
            assert c.min_length == 50.0
            assert c.max_length == 100.0


class TestLengthViolation:
    """Tests for LengthViolation string representation."""

    def test_too_short_str(self):
        """Test string representation for TOO_SHORT violation."""
        v = LengthViolation(
            net_id=100,
            violation_type=ViolationType.TOO_SHORT,
            actual_length=15.0,
            target_length=20.0,
            delta=5.0,
        )
        s = str(v)
        assert "100" in s
        assert "too short" in s
        assert "15.000" in s
        assert "20.000" in s

    def test_too_long_str(self):
        """Test string representation for TOO_LONG violation."""
        v = LengthViolation(
            net_id=100,
            violation_type=ViolationType.TOO_LONG,
            actual_length=25.0,
            target_length=20.0,
            delta=5.0,
        )
        s = str(v)
        assert "too long" in s

    def test_mismatch_str(self):
        """Test string representation for MISMATCH violation."""
        v = LengthViolation(
            net_id="BUS",
            violation_type=ViolationType.MISMATCH,
            actual_length=[20.0, 25.0, 22.0],
            delta=5.0,
            match_group="BUS",
        )
        s = str(v)
        assert "BUS" in s
        assert "mismatch" in s


class TestSerpentineConfig:
    """Tests for SerpentineConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = SerpentineConfig()
        assert config.style == SerpentineStyle.TROMBONE
        assert config.amplitude == 1.0
        assert config.min_spacing == 0.2
        assert config.min_segment_length == 2.0
        assert config.max_iterations == 20

    def test_custom_config(self):
        """Test custom configuration."""
        config = SerpentineConfig(
            style=SerpentineStyle.RECTANGULAR,
            amplitude=0.5,
            min_spacing=0.3,
        )
        assert config.style == SerpentineStyle.RECTANGULAR
        assert config.amplitude == 0.5
        assert config.min_spacing == 0.3


class TestSerpentineGenerator:
    """Tests for SerpentineGenerator class."""

    @pytest.fixture
    def generator(self):
        """Create a default serpentine generator."""
        return SerpentineGenerator()

    @pytest.fixture
    def long_horizontal_route(self):
        """Create a route with a long horizontal segment."""
        seg = Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        return Route(net=1, net_name="NET1", segments=[seg])

    @pytest.fixture
    def short_route(self):
        """Create a route with a short segment."""
        seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        return Route(net=1, net_name="NET1", segments=[seg])

    @pytest.fixture
    def multi_segment_route(self):
        """Create a route with multiple segments."""
        seg1 = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg2 = Segment(x1=5, y1=0, x2=5, y2=10, width=0.2, layer=Layer.F_CU, net=1)
        seg3 = Segment(x1=5, y1=10, x2=15, y2=10, width=0.2, layer=Layer.F_CU, net=1)
        return Route(net=1, net_name="NET1", segments=[seg1, seg2, seg3])

    def test_find_best_segment_prefers_longer(self, generator, multi_segment_route):
        """Test that find_best_segment prefers longer segments."""
        result = generator.find_best_segment(multi_segment_route)
        assert result is not None
        idx, seg = result
        # seg2 is 10mm (longest), seg3 is 10mm, seg1 is 5mm
        # Should prefer middle segments, so seg2 (index 1) or seg3 (index 2)
        assert idx in [1, 2]

    def test_find_best_segment_empty_route(self, generator):
        """Test find_best_segment with empty route."""
        route = Route(net=1, net_name="NET1", segments=[])
        result = generator.find_best_segment(route)
        assert result is None

    def test_find_best_segment_all_too_short(self, generator, short_route):
        """Test find_best_segment when all segments are too short."""
        result = generator.find_best_segment(short_route)
        assert result is None  # 1mm < 2mm min_segment_length

    def test_generate_trombone_adds_length(self, generator):
        """Test that trombone generation adds the requested length."""
        seg = Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        result = generator.generate_trombone(seg, target_length_add=5.0)

        assert result.success
        assert result.length_added > 0
        assert len(result.new_segments) > 1
        assert result.num_loops > 0

    def test_generate_trombone_no_addition_needed(self, generator):
        """Test trombone with zero length addition."""
        seg = Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        result = generator.generate_trombone(seg, target_length_add=0)

        assert result.success
        assert result.length_added == 0
        assert len(result.new_segments) == 1  # Original segment

    def test_generate_trombone_segment_too_short(self, generator):
        """Test trombone with segment too short."""
        seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        result = generator.generate_trombone(seg, target_length_add=5.0)

        assert not result.success
        assert "too short" in result.message.lower()

    def test_generate_trombone_preserves_segment_properties(self, generator):
        """Test that generated segments preserve width, layer, net."""
        seg = Segment(
            x1=0, y1=0, x2=20, y2=0, width=0.3, layer=Layer.B_CU, net=42, net_name="SIGNAL"
        )
        result = generator.generate_trombone(seg, target_length_add=3.0)

        assert result.success
        for s in result.new_segments:
            assert s.width == 0.3
            assert s.layer == Layer.B_CU
            assert s.net == 42
            assert s.net_name == "SIGNAL"

    def test_add_serpentine_increases_length(self, generator, long_horizontal_route):
        """Test that add_serpentine increases route length."""
        original_length = LengthTracker.calculate_route_length(long_horizontal_route)
        target_length = original_length + 10.0

        new_route, result = generator.add_serpentine(long_horizontal_route, target_length)

        assert result.success
        new_length = LengthTracker.calculate_route_length(new_route)
        assert new_length > original_length

    def test_add_serpentine_already_meets_target(self, generator, long_horizontal_route):
        """Test add_serpentine when route already meets target."""
        original_length = LengthTracker.calculate_route_length(long_horizontal_route)
        target_length = original_length - 5.0  # Already exceeds target

        new_route, result = generator.add_serpentine(long_horizontal_route, target_length)

        assert result.success
        assert "already meets" in result.message.lower()

    def test_add_serpentine_no_suitable_segment(self, generator, short_route):
        """Test add_serpentine when no segment is suitable."""
        new_route, result = generator.add_serpentine(short_route, target_length=50.0)

        assert not result.success
        assert "no suitable segment" in result.message.lower()


class TestAddSerpentineFunction:
    """Tests for the add_serpentine convenience function."""

    def test_add_serpentine_function(self):
        """Test the module-level add_serpentine function."""
        seg = Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg])

        new_route, result = add_serpentine(route, target_length=30.0)

        assert result.success
        new_length = LengthTracker.calculate_route_length(new_route)
        assert new_length > 20.0

    def test_add_serpentine_with_custom_config(self):
        """Test add_serpentine with custom configuration."""
        seg = Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg])

        config = SerpentineConfig(amplitude=0.5, min_spacing=0.1)
        new_route, result = add_serpentine(route, target_length=30.0, config=config)

        assert result.success


class TestTuneMatchGroup:
    """Tests for tune_match_group function."""

    def test_tune_match_group_equalizes_lengths(self):
        """Test that tune_match_group equalizes route lengths."""
        # Create routes of different lengths
        route1 = Route(
            net=100,
            net_name="NET100",
            segments=[Segment(x1=0, y1=0, x2=15, y2=0, width=0.2, layer=Layer.F_CU, net=100)],
        )  # 15mm
        route2 = Route(
            net=101,
            net_name="NET101",
            segments=[Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=101)],
        )  # 20mm (longest)
        route3 = Route(
            net=102,
            net_name="NET102",
            segments=[Segment(x1=0, y1=0, x2=12, y2=0, width=0.2, layer=Layer.F_CU, net=102)],
        )  # 12mm

        routes = {100: route1, 101: route2, 102: route3}
        results = tune_match_group(routes, [100, 101, 102], tolerance=0.5)

        # All routes should be processed
        assert len(results) == 3

        # Routes 100 and 102 should have length added
        assert results[100][1].length_added > 0 or "tolerance" in results[100][1].message.lower()
        assert results[102][1].length_added > 0 or "tolerance" in results[102][1].message.lower()
        # Route 101 was longest, should not need changes
        assert results[101][1].length_added == 0 or "tolerance" in results[101][1].message.lower()

    def test_tune_match_group_already_matched(self):
        """Test tune_match_group when routes are already matched."""
        route1 = Route(
            net=100,
            net_name="NET100",
            segments=[Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=100)],
        )
        route2 = Route(
            net=101,
            net_name="NET101",
            segments=[Segment(x1=0, y1=0, x2=20.3, y2=0, width=0.2, layer=Layer.F_CU, net=101)],
        )  # Within 0.5mm tolerance

        routes = {100: route1, 101: route2}
        results = tune_match_group(routes, [100, 101], tolerance=0.5)

        # Both should report within tolerance
        for net_id, (new_route, result) in results.items():
            assert "tolerance" in result.message.lower()

    def test_tune_match_group_single_route(self):
        """Test tune_match_group with single route (no matching needed)."""
        route = Route(
            net=100,
            net_name="NET100",
            segments=[Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=100)],
        )

        routes = {100: route}
        results = tune_match_group(routes, [100], tolerance=0.5)

        # Should return empty (need at least 2 routes to match)
        assert len(results) == 0

    def test_tune_match_group_missing_routes(self):
        """Test tune_match_group when some routes are missing."""
        route = Route(
            net=100,
            net_name="NET100",
            segments=[Segment(x1=0, y1=0, x2=20, y2=0, width=0.2, layer=Layer.F_CU, net=100)],
        )

        routes = {100: route}  # Only one route, asking for two
        results = tune_match_group(routes, [100, 101], tolerance=0.5)

        # Should return empty (only 1 of 2 routes available)
        assert len(results) == 0
