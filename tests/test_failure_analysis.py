"""Tests for intelligent failure recovery and root cause analysis."""

import json

import numpy as np
import pytest

from kicad_tools.router.failure_analysis import (
    BlockingElement,
    CongestionMap,
    FailureAnalysis,
    FailureCause,
    PathAttempt,
    Rectangle,
    RootCauseAnalyzer,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def design_rules() -> DesignRules:
    """Create standard design rules for testing."""
    return DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
    )


@pytest.fixture
def two_layer_stack() -> LayerStack:
    """Create a two-layer stack for testing."""
    return LayerStack.two_layer()


@pytest.fixture
def routing_grid(design_rules: DesignRules, two_layer_stack: LayerStack) -> RoutingGrid:
    """Create a routing grid for testing."""
    return RoutingGrid(
        width=50.0,
        height=50.0,
        rules=design_rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=two_layer_stack,
    )


class TestRectangle:
    """Tests for Rectangle helper class."""

    def test_rectangle_basic_properties(self):
        """Test rectangle basic properties."""
        rect = Rectangle(0, 0, 10, 20)
        assert rect.width == 10
        assert rect.height == 20
        assert rect.center == (5.0, 10.0)
        assert rect.area == 200

    def test_rectangle_contains_point(self):
        """Test point containment."""
        rect = Rectangle(0, 0, 10, 10)
        assert rect.contains(5, 5)
        assert rect.contains(0, 0)
        assert rect.contains(10, 10)
        assert not rect.contains(-1, 5)
        assert not rect.contains(5, 11)

    def test_rectangle_intersects(self):
        """Test rectangle intersection."""
        rect1 = Rectangle(0, 0, 10, 10)
        rect2 = Rectangle(5, 5, 15, 15)
        rect3 = Rectangle(20, 20, 30, 30)

        assert rect1.intersects(rect2)
        assert rect2.intersects(rect1)
        assert not rect1.intersects(rect3)
        assert not rect3.intersects(rect1)

    def test_rectangle_expand(self):
        """Test rectangle expansion."""
        rect = Rectangle(10, 10, 20, 20)
        expanded = rect.expand(5)

        assert expanded.min_x == 5
        assert expanded.min_y == 5
        assert expanded.max_x == 25
        assert expanded.max_y == 25


class TestFailureCause:
    """Tests for FailureCause enum."""

    def test_all_causes_have_string_values(self):
        """Test that all causes have string values."""
        for cause in FailureCause:
            assert isinstance(cause.value, str)
            assert len(cause.value) > 0

    def test_expected_causes_exist(self):
        """Test that expected causes are defined."""
        expected_causes = [
            "congestion",
            "blocked_path",
            "clearance",
            "layer_conflict",
            "pin_access",
            "keepout",
        ]
        cause_values = [c.value for c in FailureCause]
        for expected in expected_causes:
            assert expected in cause_values


class TestBlockingElement:
    """Tests for BlockingElement dataclass."""

    def test_blocking_element_creation(self):
        """Test creating a blocking element."""
        bounds = Rectangle(10, 10, 20, 20)
        element = BlockingElement(
            type="component",
            ref="C1",
            net="VCC",
            bounds=bounds,
            movable=True,
            layer=0,
        )

        assert element.type == "component"
        assert element.ref == "C1"
        assert element.net == "VCC"
        assert element.movable is True
        assert element.layer == 0

    def test_blocking_element_to_dict(self):
        """Test converting blocking element to dictionary."""
        bounds = Rectangle(10, 10, 20, 20)
        element = BlockingElement(
            type="trace",
            ref=None,
            net="CLK",
            bounds=bounds,
            movable=True,
        )

        d = element.to_dict()
        assert d["type"] == "trace"
        assert d["ref"] is None
        assert d["net"] == "CLK"
        assert d["movable"] is True
        assert "bounds" in d
        assert d["bounds"]["min_x"] == 10

    def test_blocking_element_repr(self):
        """Test string representation."""
        bounds = Rectangle(0, 0, 10, 10)
        element = BlockingElement(type="component", ref="U1", net=None, bounds=bounds, movable=True)
        repr_str = repr(element)
        assert "component" in repr_str
        assert "U1" in repr_str


class TestPathAttempt:
    """Tests for PathAttempt dataclass."""

    def test_path_attempt_successful(self):
        """Test successful path attempt."""
        attempt = PathAttempt(
            start=(0, 0),
            end=(10, 10),
            layer=0,
            success=True,
            path=[(0, 0), (5, 5), (10, 10)],
            cost=15.0,
        )

        assert attempt.success is True
        assert attempt.length > 0

    def test_path_attempt_failed(self):
        """Test failed path attempt."""
        attempt = PathAttempt(
            start=(0, 0),
            end=(10, 10),
            layer=0,
            success=False,
            blocked_at=(5, 5),
            explored_cells=100,
        )

        assert attempt.success is False
        assert attempt.blocked_at == (5, 5)
        assert attempt.explored_cells == 100

    def test_path_attempt_length_calculation(self):
        """Test path length calculation."""
        # Straight horizontal path
        attempt = PathAttempt(
            start=(0, 0),
            end=(10, 0),
            layer=0,
            success=True,
            path=[(0, 0), (5, 0), (10, 0)],
        )
        assert abs(attempt.length - 10.0) < 0.01

    def test_path_attempt_to_dict(self):
        """Test converting to dictionary."""
        attempt = PathAttempt(
            start=(0, 0),
            end=(10, 10),
            layer=0,
            success=True,
            path=[(0, 0), (10, 10)],
            cost=14.14,
        )

        d = attempt.to_dict()
        assert d["start"] == (0, 0)
        assert d["end"] == (10, 10)
        assert d["success"] is True
        assert d["cost"] == 14.14


class TestFailureAnalysis:
    """Tests for FailureAnalysis dataclass."""

    def test_failure_analysis_creation(self):
        """Test creating a failure analysis."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.85,
            failure_location=(25.0, 25.0),
            failure_area=Rectangle(20, 20, 30, 30),
            congestion_score=0.92,
        )

        assert analysis.root_cause == FailureCause.CONGESTION
        assert analysis.confidence == 0.85
        assert analysis.congestion_score == 0.92

    def test_failure_analysis_to_dict(self):
        """Test converting to dictionary."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            blocking_elements=[
                BlockingElement(
                    type="component",
                    ref="C3",
                    net=None,
                    bounds=Rectangle(8, 18, 12, 22),
                    movable=True,
                )
            ],
            suggestions=["Move component C3"],
        )

        d = analysis.to_dict()
        assert d["root_cause"] == "blocked_path"
        assert d["confidence"] == 0.9
        assert len(d["blocking_elements"]) == 1
        assert d["blocking_elements"][0]["ref"] == "C3"
        assert "Move component C3" in d["suggestions"]

    def test_failure_analysis_json_serializable(self):
        """Test that to_dict is JSON serializable."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.CLEARANCE,
            confidence=0.75,
            failure_location=(0, 0),
            failure_area=Rectangle(0, 0, 10, 10),
            clearance_margin=0.02,
        )

        # Should not raise
        json_str = json.dumps(analysis.to_dict())
        assert isinstance(json_str, str)

    def test_failure_analysis_str_repr(self):
        """Test string representation."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.85,
            failure_location=(0, 0),
            failure_area=Rectangle(0, 0, 10, 10),
            congestion_score=0.9,
        )

        s = str(analysis)
        assert "congestion" in s
        assert "85%" in s


class TestCongestionMap:
    """Tests for CongestionMap class."""

    def test_congestion_map_creation(self, routing_grid: RoutingGrid):
        """Test creating a congestion map."""
        cmap = CongestionMap(routing_grid, cell_size=1.0)

        assert cmap.cols > 0
        assert cmap.rows > 0
        assert cmap.shape == (cmap.rows, cmap.cols)

    def test_congestion_map_empty_grid(self, routing_grid: RoutingGrid):
        """Test congestion map on empty grid."""
        cmap = CongestionMap(routing_grid)

        # Empty grid should have low/zero congestion
        area = Rectangle(10, 10, 20, 20)
        congestion = cmap.get_congestion(area)
        assert 0 <= congestion <= 1.0

    def test_congestion_map_get_at_point(self, routing_grid: RoutingGrid):
        """Test getting congestion at a point."""
        cmap = CongestionMap(routing_grid)

        congestion = cmap.get_congestion_at(25.0, 25.0)
        assert 0 <= congestion <= 1.0

    def test_congestion_map_hotspots(self, routing_grid: RoutingGrid):
        """Test finding congestion hotspots."""
        cmap = CongestionMap(routing_grid)

        # On empty grid, should find no hotspots with high threshold
        hotspots = cmap.find_congestion_hotspots(threshold=0.9)
        assert isinstance(hotspots, list)

    def test_congestion_map_to_array(self, routing_grid: RoutingGrid):
        """Test getting raw array."""
        cmap = CongestionMap(routing_grid)

        arr = cmap.to_array()
        assert isinstance(arr, np.ndarray)
        assert arr.shape == cmap.shape

    def test_congestion_map_weights(self, routing_grid: RoutingGrid):
        """Test custom congestion weights."""
        cmap = CongestionMap(
            routing_grid,
            component_weight=2.0,
            trace_weight=1.0,
            via_weight=0.5,
        )

        assert cmap.component_weight == 2.0
        assert cmap.trace_weight == 1.0
        assert cmap.via_weight == 0.5


class TestRootCauseAnalyzer:
    """Tests for RootCauseAnalyzer class."""

    def test_analyzer_creation(self):
        """Test creating an analyzer."""
        analyzer = RootCauseAnalyzer()
        assert analyzer.congestion_threshold == 0.7

    def test_analyzer_custom_thresholds(self):
        """Test analyzer with custom thresholds."""
        analyzer = RootCauseAnalyzer(
            congestion_threshold=0.8,
            clearance_margin_threshold=0.1,
        )
        assert analyzer.congestion_threshold == 0.8
        assert analyzer.clearance_margin_threshold == 0.1

    def test_analyze_routing_failure(self, routing_grid: RoutingGrid):
        """Test analyzing a routing failure."""
        analyzer = RootCauseAnalyzer()

        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 5.0),
            end=(45.0, 45.0),
            net="TestNet",
            layer=0,
        )

        assert isinstance(analysis, FailureAnalysis)
        assert isinstance(analysis.root_cause, FailureCause)
        assert 0 <= analysis.confidence <= 1.0
        assert isinstance(analysis.failure_area, Rectangle)

    def test_analyze_routing_failure_with_attempts(self, routing_grid: RoutingGrid):
        """Test analyzing with path attempts."""
        analyzer = RootCauseAnalyzer()

        attempts = [
            PathAttempt(
                start=(5, 5),
                end=(45, 45),
                layer=0,
                success=False,
                blocked_at=(25, 25),
                explored_cells=500,
            )
        ]

        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 5.0),
            end=(45.0, 45.0),
            net="TestNet",
            attempts=attempts,
        )

        assert analysis.attempted_paths == 1
        # Failure location should be at blocked_at from attempt
        assert analysis.failure_location == (25, 25)

    def test_analyze_placement_failure(self, routing_grid: RoutingGrid):
        """Test analyzing a placement failure."""
        analyzer = RootCauseAnalyzer()

        analysis = analyzer.analyze_placement_failure(
            grid=routing_grid,
            ref="U1",
            target_pos=(25.0, 25.0),
            component_bounds=Rectangle(20, 20, 30, 30),
        )

        assert isinstance(analysis, FailureAnalysis)
        assert analysis.failure_location == (25.0, 25.0)

    def test_suggestions_generated(self, routing_grid: RoutingGrid):
        """Test that suggestions are generated."""
        analyzer = RootCauseAnalyzer()

        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 5.0),
            end=(45.0, 45.0),
            net="TestNet",
        )

        # Should generate some suggestions
        assert isinstance(analysis.suggestions, list)

    def test_compute_corridor(self):
        """Test corridor computation."""
        analyzer = RootCauseAnalyzer()

        corridor = analyzer._compute_corridor(
            start=(10, 10),
            end=(30, 40),
            margin=5.0,
        )

        assert corridor.min_x == 5  # 10 - 5
        assert corridor.min_y == 5  # 10 - 5
        assert corridor.max_x == 35  # 30 + 5
        assert corridor.max_y == 45  # 40 + 5


class TestIntegration:
    """Integration tests combining multiple components."""

    def test_full_analysis_workflow(self, routing_grid: RoutingGrid):
        """Test complete analysis workflow."""
        # Add some pads to the grid
        pad1 = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="TestNet",
            layer=Layer.F_CU,
        )
        pad2 = Pad(
            x=45.0,
            y=45.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="TestNet",
            layer=Layer.F_CU,
        )

        routing_grid.add_pad(pad1)
        routing_grid.add_pad(pad2)

        # Create congestion map
        cmap = CongestionMap(routing_grid)
        assert cmap.shape[0] > 0

        # Analyze failure
        analyzer = RootCauseAnalyzer()
        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(pad1.x, pad1.y),
            end=(pad2.x, pad2.y),
            net="TestNet",
        )

        # Verify analysis structure
        assert analysis.root_cause is not None
        assert analysis.confidence > 0
        assert len(analysis.failure_area.center) == 2

    def test_json_roundtrip(self, routing_grid: RoutingGrid):
        """Test that analysis can be serialized and parsed."""
        analyzer = RootCauseAnalyzer()
        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 5.0),
            end=(45.0, 45.0),
            net="TestNet",
        )

        # Serialize
        json_str = json.dumps(analysis.to_dict())

        # Parse back
        data = json.loads(json_str)

        # Verify key fields
        assert data["root_cause"] in [c.value for c in FailureCause]
        assert 0 <= data["confidence"] <= 1.0
        assert "failure_location" in data
        assert "failure_area" in data


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_attempts_list(self, routing_grid: RoutingGrid):
        """Test analysis with empty attempts list."""
        analyzer = RootCauseAnalyzer()

        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 5.0),
            end=(45.0, 45.0),
            net="TestNet",
            attempts=[],
        )

        assert analysis.attempted_paths == 0
        assert analysis.best_attempt is None

    def test_zero_area_corridor(self, routing_grid: RoutingGrid):
        """Test with start and end at same point."""
        analyzer = RootCauseAnalyzer()

        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(25.0, 25.0),
            end=(25.0, 25.0),
            net="TestNet",
        )

        # Should still produce valid analysis
        assert isinstance(analysis, FailureAnalysis)

    def test_point_outside_grid(self, routing_grid: RoutingGrid):
        """Test congestion query outside grid bounds."""
        cmap = CongestionMap(routing_grid)

        # Point outside grid
        congestion = cmap.get_congestion_at(-100.0, -100.0)
        assert congestion == 0.0

    def test_area_outside_grid(self, routing_grid: RoutingGrid):
        """Test congestion area outside grid bounds."""
        cmap = CongestionMap(routing_grid)

        # Area outside grid
        area = Rectangle(-100, -100, -50, -50)
        congestion = cmap.get_congestion(area)
        assert congestion == 0.0
