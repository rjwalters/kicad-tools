"""Tests for intelligent failure recovery and root cause analysis."""

import json

import numpy as np
import pytest

from kicad_tools.router.failure_analysis import (
    ActionableSuggestion,
    BlockingElement,
    CongestionMap,
    FailureAnalysis,
    FailureCause,
    PadAccessBlocker,
    PathAttempt,
    Rectangle,
    RootCauseAnalyzer,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
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
            "via_blocked",
            "routing_order",
        ]
        cause_values = [c.value for c in FailureCause]
        for expected in expected_causes:
            assert expected in cause_values

    def test_cause_descriptions(self):
        """Test that all causes have human-readable descriptions."""
        for cause in FailureCause:
            assert hasattr(cause, "description")
            assert isinstance(cause.description, str)
            assert len(cause.description) > 0
            # Description should not just be the enum value
            assert cause.description != cause.value


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

    def test_failure_analysis_has_movable_blockers(self):
        """Test has_movable_blockers property."""
        # Analysis with movable blocker
        analysis_with_movable = FailureAnalysis(
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
        )
        assert analysis_with_movable.has_movable_blockers is True

        # Analysis without movable blockers
        analysis_without_movable = FailureAnalysis(
            root_cause=FailureCause.KEEPOUT,
            confidence=0.95,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            blocking_elements=[
                BlockingElement(
                    type="keepout",
                    ref=None,
                    net=None,
                    bounds=Rectangle(8, 18, 12, 22),
                    movable=False,
                )
            ],
        )
        assert analysis_without_movable.has_movable_blockers is False

        # Analysis with no blockers
        analysis_empty = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.8,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            blocking_elements=[],
        )
        assert analysis_empty.has_movable_blockers is False

    def test_failure_analysis_has_reroutable_nets(self):
        """Test has_reroutable_nets property."""
        # Analysis with reroutable trace from different net
        analysis_with_reroutable = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            blocking_elements=[
                BlockingElement(
                    type="trace",
                    ref=None,
                    net="GND",
                    bounds=Rectangle(8, 18, 12, 22),
                    movable=True,
                )
            ],
            net="CLK",
        )
        assert analysis_with_reroutable.has_reroutable_nets is True

        # Analysis with trace from same net (not reroutable)
        analysis_same_net = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            blocking_elements=[
                BlockingElement(
                    type="trace",
                    ref=None,
                    net="CLK",
                    bounds=Rectangle(8, 18, 12, 22),
                    movable=True,
                )
            ],
            net="CLK",
        )
        assert analysis_same_net.has_reroutable_nets is False

        # Analysis with non-trace blocker
        analysis_no_traces = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            blocking_elements=[
                BlockingElement(
                    type="component",
                    ref="U1",
                    net=None,
                    bounds=Rectangle(8, 18, 12, 22),
                    movable=True,
                )
            ],
            net="CLK",
        )
        assert analysis_no_traces.has_reroutable_nets is False

    def test_failure_analysis_net_field(self):
        """Test net field in FailureAnalysis."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(10, 20),
            failure_area=Rectangle(5, 15, 15, 25),
            net="CLK_NET",
        )
        assert analysis.net == "CLK_NET"

        # Test to_dict includes net
        d = analysis.to_dict()
        assert d["net"] == "CLK_NET"


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


class TestPadAccessBlocker:
    """Tests for PadAccessBlocker dataclass."""

    def test_pad_access_blocker_creation(self):
        """Test creating a PadAccessBlocker."""
        blocker = PadAccessBlocker(
            pad_ref="U1.13",
            blocking_net=15,
            blocking_net_name="SC_POS_PLUS",
            blocking_type="trace",
            distance=0.12,
            suggested_clearance=0.10,
        )

        assert blocker.pad_ref == "U1.13"
        assert blocker.blocking_net == 15
        assert blocker.blocking_net_name == "SC_POS_PLUS"
        assert blocker.blocking_type == "trace"
        assert blocker.distance == 0.12
        assert blocker.suggested_clearance == 0.10

    def test_pad_access_blocker_to_dict(self):
        """Test converting PadAccessBlocker to dictionary."""
        blocker = PadAccessBlocker(
            pad_ref="U2.5",
            blocking_net=4,
            blocking_net_name="+3.3V",
            blocking_type="via",
            distance=0.08,
            suggested_clearance=0.06,
        )

        d = blocker.to_dict()
        assert d["pad_ref"] == "U2.5"
        assert d["blocking_net"] == 4
        assert d["blocking_net_name"] == "+3.3V"
        assert d["blocking_type"] == "via"
        assert d["distance"] == 0.08
        assert d["suggested_clearance"] == 0.06

    def test_pad_access_blocker_str(self):
        """Test string representation of PadAccessBlocker."""
        blocker = PadAccessBlocker(
            pad_ref="U1.13",
            blocking_net=15,
            blocking_net_name="SC_POS_PLUS",
            blocking_type="trace",
            distance=0.12,
            suggested_clearance=0.10,
        )

        s = str(blocker)
        assert "U1.13" in s
        assert "SC_POS_PLUS" in s
        assert "trace" in s
        assert "0.12" in s


class TestBlockerGeometryClassifier:
    """Issue #2858: geometry-based classification of trace vs via blockers.

    The pre-fix cascade at ``failure_analysis.py:1539-1546`` defaulted to
    ``"via"`` whenever the first three predicates fell through, which
    misclassified *trace clearance* cells (which have the exact same
    ``Cell`` state shape as via clearance cells:
    ``pad_blocked=False``, ``usage_count=0``, ``original_net=0``).
    The post-fix path delegates the else branch to
    ``_classify_blocker_geometry``, which inspects ``grid.routes`` for
    the cell's net and picks ``"trace"`` vs ``"via"`` by checking which
    geometric envelope the cell falls inside.

    These tests pin both the new behaviour and the cascade-order
    invariants so regressions surface before they reach a real board.
    """

    def test_trace_clearance_classified_as_trace(self, routing_grid: RoutingGrid) -> None:
        """A cell inside a segment's clearance envelope -> ``"trace"``.

        Fails on main: the pre-fix cascade falls through to ``"via"``.
        Passes after fix: ``_classify_blocker_geometry`` inspects
        ``grid.routes`` and matches the segment's clearance halo.
        """
        # Target pad (the victim we'll analyse) at (25, 25).
        victim = Pad(
            x=25.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(victim)

        # Lay down a horizontal segment from a different net (net=2) just
        # outside the segment centerline but well inside its clearance halo.
        # ``_mark_segment`` halo radius =
        #     seg.width / 2 + trace_clearance + safety_margin (1 cell)
        # With default rules (trace_width=0.2, trace_clearance=0.15), that's
        # 0.1 + 0.15 + 0.1 = 0.35 mm.  Place the segment 0.3 mm above the
        # victim so the cell at the pad's location is inside the halo but
        # outside the centerline.
        rules = routing_grid.rules
        offset = rules.trace_width / 2 + rules.trace_clearance + 0.5 * routing_grid.resolution
        seg = Segment(
            x1=20.0,
            y1=25.0 + offset,
            x2=30.0,
            y2=25.0 + offset,
            width=rules.trace_width,
            layer=Layer.F_CU,
            net=2,
            net_name="OTHER",
        )
        route = Route(net=2, net_name="OTHER", segments=[seg])
        routing_grid.mark_route(route)

        analyzer = RootCauseAnalyzer()
        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "OTHER"},
        )

        other_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(other_blockers) >= 1, (
            f"Expected the OTHER segment's clearance halo to register as a "
            f"blocker on the VICTIM pad, got: {blockers!r}"
        )
        assert any(b.blocking_type == "trace" for b in other_blockers), (
            "Issue #2858: a segment clearance cell must classify as "
            f"'trace', got: {[b.blocking_type for b in other_blockers]!r}"
        )

    def test_via_clearance_classified_as_via(self, routing_grid: RoutingGrid) -> None:
        """A cell inside a via's clearance ring -> ``"via"``.

        Regression guard: must still pass after the Issue #2858 fix.
        Passes on main as well (the pre-fix default already returns
        ``"via"`` here); the new path resolves the same answer by
        geometric inspection.
        """
        victim = Pad(
            x=25.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(victim)

        # Place a via from net 2 just inside its own clearance ring of
        # the victim pad's cell.  ``_mark_via`` radius =
        #     via.diameter/2 + via_clearance + trace_width/2 + safety_margin
        # = 0.3 + 0.2 + 0.1 + 0.1 = 0.7 mm.  Put the via at ~0.6 mm so the
        # victim pad center is well inside the halo.
        rules = routing_grid.rules
        via_offset = (
            0.6 / 2 + rules.via_clearance + rules.trace_width / 2 - 0.5 * routing_grid.resolution
        )
        offending_via = Via(
            x=25.0 + via_offset,
            y=25.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=2,
            net_name="OTHER",
        )
        # Route the via through ``mark_route`` so it lands in ``grid.routes``
        # (the new classifier inspects ``grid.routes`` rather than scanning
        # all blocked cells).
        route = Route(net=2, net_name="OTHER", vias=[offending_via])
        routing_grid.mark_route(route)

        analyzer = RootCauseAnalyzer()
        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "OTHER"},
        )

        via_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(via_blockers) >= 1, (
            f"Expected the OTHER via's clearance ring to register as a "
            f"blocker on the VICTIM pad, got: {blockers!r}"
        )
        assert all(b.blocking_type == "via" for b in via_blockers), (
            "Issue #2858 regression guard: an isolated via must still "
            f"classify as 'via', got: {[b.blocking_type for b in via_blockers]!r}"
        )

    def test_both_blockers_pick_closest(self, routing_grid: RoutingGrid) -> None:
        """When both segment and via from the same net block a pad, the
        closest cell wins; the classifier returns the type matching the
        closest cell's geometric envelope.

        Geometry: place the segment closer than the via.  The closest
        blocking cell will be inside the segment's clearance halo, so the
        net_closest map should record ``"trace"`` for net=2.
        """
        victim = Pad(
            x=25.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(victim)

        rules = routing_grid.rules
        # Segment trace 0.4 mm above the pad (closest cell ~0.05 mm
        # above the pad metal edge).
        seg_offset = rules.trace_width / 2 + rules.trace_clearance + 0.5 * routing_grid.resolution
        close_seg = Segment(
            x1=20.0,
            y1=25.0 + seg_offset,
            x2=30.0,
            y2=25.0 + seg_offset,
            width=rules.trace_width,
            layer=Layer.F_CU,
            net=2,
            net_name="OTHER",
        )
        # Via 1.5 mm to the east, just inside its own clearance ring but
        # farther from the pad than the segment.
        far_via = Via(
            x=25.0 + 1.5,
            y=25.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=2,
            net_name="OTHER",
        )
        route = Route(net=2, net_name="OTHER", segments=[close_seg], vias=[far_via])
        routing_grid.mark_route(route)

        analyzer = RootCauseAnalyzer()
        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "OTHER"},
        )

        other_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(other_blockers) == 1, (
            "Expected exactly one entry for OTHER (closest cell wins per net), "
            f"got: {other_blockers!r}"
        )
        # The closest blocking cell sits inside the segment halo, so the
        # geometry classifier should return 'trace'.
        assert other_blockers[0].blocking_type == "trace", (
            "Closest cell is inside the segment clearance halo; expected "
            f"'trace' classification, got: {other_blockers[0].blocking_type!r}"
        )

    def test_pad_blocked_takes_precedence(self, routing_grid: RoutingGrid) -> None:
        """``pad_blocked`` must win over geometry inspection (cascade order).

        Even if a route from the same blocking net's geometry is also
        present, the ``pad_blocked`` predicate fires first.
        """
        victim = Pad(
            x=25.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        # Place a foreign pad whose metal cell overlaps the search radius
        # of the victim pad.  ``_block_pad`` sets ``pad_blocked=True`` on
        # the metal cells, and that predicate is first in the cascade.
        blocker_pad = Pad(
            x=25.5,
            y=25.0,
            width=0.5,
            height=0.5,
            net=2,
            net_name="OTHER",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(victim)
        routing_grid.add_pad(blocker_pad)

        # Also add a Route from net=2 to ensure geometry inspection has
        # something to find -- the predicate cascade must NOT delegate to
        # geometry when pad_blocked is True.
        far_seg = Segment(
            x1=30.0,
            y1=30.0,
            x2=35.0,
            y2=30.0,
            width=routing_grid.rules.trace_width,
            layer=Layer.F_CU,
            net=2,
            net_name="OTHER",
        )
        routing_grid.mark_route(Route(net=2, net_name="OTHER", segments=[far_seg]))

        analyzer = RootCauseAnalyzer()
        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "OTHER"},
        )

        other_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(other_blockers) >= 1
        # The metal cell of the foreign pad must dominate the closest cell
        # search, and the cascade short-circuits at ``pad_blocked`` -> 'pad'.
        assert other_blockers[0].blocking_type == "pad", (
            "Issue #2858 cascade-order guard: ``pad_blocked`` must take "
            f"precedence over geometry inspection, got: "
            f"{other_blockers[0].blocking_type!r}"
        )

    def test_pad_clearance_takes_precedence(self, routing_grid: RoutingGrid) -> None:
        """``original_net != 0 and == cell.net`` must win over geometry.

        Reproduces the Issue #2810 pad-clearance test scenario but also
        adds a foreign-net route so the geometry classifier *would*
        return ``"trace"`` if invoked.  The cascade must short-circuit
        at ``pad_clearance`` before reaching the geometry check.
        """
        victim = Pad(
            x=25.0,
            y=25.0,
            width=0.2,
            height=0.2,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        neighbor = Pad(
            x=25.8,
            y=25.0,
            width=0.2,
            height=0.2,
            net=2,
            net_name="NEIGHBOR",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(victim)
        routing_grid.add_pad(neighbor)

        # Also add a segment from the same NEIGHBOR net so geometry would
        # match if invoked.  The expected behaviour: the pad_clearance
        # predicate fires first (because the neighbor's pad-clearance
        # cells have ``original_net == net == 2``), so the geometry
        # classifier is NOT consulted for those cells.
        rules = routing_grid.rules
        seg_offset = rules.trace_width / 2 + rules.trace_clearance + 0.5 * routing_grid.resolution
        far_seg = Segment(
            x1=22.0,
            y1=25.0 - seg_offset,
            x2=23.0,
            y2=25.0 - seg_offset,
            width=rules.trace_width,
            layer=Layer.F_CU,
            net=2,
            net_name="NEIGHBOR",
        )
        routing_grid.mark_route(Route(net=2, net_name="NEIGHBOR", segments=[far_seg]))

        analyzer = RootCauseAnalyzer()
        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "NEIGHBOR"},
        )

        neighbor_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(neighbor_blockers) >= 1
        # The closest blocking cell must be in the neighbor's pad
        # clearance envelope (not the more distant segment halo).
        assert neighbor_blockers[0].blocking_type == "pad_clearance", (
            "Issue #2858 cascade-order guard: ``pad_clearance`` predicate "
            f"must take precedence over geometry inspection, got: "
            f"{neighbor_blockers[0].blocking_type!r}"
        )


class TestAnalyzePadAccessBlockers:
    """Tests for analyze_pad_access_blockers method."""

    def test_analyze_pad_access_blockers_empty_grid(self, routing_grid: RoutingGrid):
        """Test analysis on empty grid returns no blockers."""
        analyzer = RootCauseAnalyzer()

        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "TestNet"},
        )

        # Empty grid should have no blockers
        assert blockers == []

    def test_analyze_pad_access_blockers_with_blocking_pad(self, routing_grid: RoutingGrid):
        """Test analysis finds blocking pad from different net."""
        # Add a pad from net 2 near the target location
        blocking_pad = Pad(
            x=25.5,  # Very close to 25.0
            y=25.0,
            width=1.0,
            height=1.0,
            net=2,
            net_name="BlockingNet",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(blocking_pad)

        analyzer = RootCauseAnalyzer()

        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "TestNet", 2: "BlockingNet"},
        )

        # Should find the blocking pad's clearance zone
        assert len(blockers) >= 1
        # The blocking element should be from net 2
        assert any(b.blocking_net == 2 for b in blockers)

    def test_analyze_pad_access_blockers_same_net_not_blocked(self, routing_grid: RoutingGrid):
        """Test that same net elements don't count as blockers."""
        # Add a pad from same net
        same_net_pad = Pad(
            x=25.5,
            y=25.0,
            width=1.0,
            height=1.0,
            net=1,  # Same net as the target
            net_name="TestNet",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(same_net_pad)

        analyzer = RootCauseAnalyzer()

        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "TestNet"},
        )

        # Same net should not be reported as a blocker
        assert not any(b.blocking_net == 1 for b in blockers)

    def test_analyze_pad_access_blockers_distinguishes_pad_clearance_from_via(
        self, routing_grid: RoutingGrid
    ):
        """Issue #2810: pad-clearance zones must classify as ``pad_clearance``.

        Reproduces the TQFP fine-pitch scenario from board 03 (XTAL2): two
        small SMD pads on different nets at a pitch such that the neighbor's
        clearance zone (not its metal area) is the closest blocker to the
        victim pad's center. Before #2810 the classifier mislabelled these
        cells as ``"via"`` because the only ``else`` branch caught them;
        after the fix the ``original_net == cell.net`` discriminator
        distinguishes the case.

        Geometry note: pads are 0.2 mm square at 0.8 mm pitch so that the
        neighbor's metal area falls JUST outside the pad-access search
        radius (~0.6 mm with the default test rules), while the neighbor's
        clearance envelope (~0.25 mm beyond the metal edge) reaches well
        into the search radius. The closest blocking cell is therefore a
        clearance-zone cell, not a metal cell -- exactly the misclassified
        case the fix targets.
        """
        victim = Pad(
            x=25.0,
            y=25.0,
            width=0.2,
            height=0.2,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        neighbor = Pad(
            x=25.8,
            y=25.0,
            width=0.2,
            height=0.2,
            net=2,
            net_name="NEIGHBOR",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(victim)
        routing_grid.add_pad(neighbor)

        analyzer = RootCauseAnalyzer()

        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "NEIGHBOR"},
        )

        # The neighbor pad's clearance zone must be flagged as a blocker,
        # and the classifier must report ``pad_clearance`` -- NOT ``via``.
        neighbor_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(neighbor_blockers) >= 1, (
            "Expected at least one blocker from the neighbor pad's clearance "
            f"zone, got: {blockers!r}"
        )
        assert any(b.blocking_type == "pad_clearance" for b in neighbor_blockers), (
            "Expected blocking_type='pad_clearance' for the neighbor pad's "
            f"clearance zone, got: {[b.blocking_type for b in neighbor_blockers]!r}"
        )

    def test_analyze_pad_access_blockers_via_remains_via(self, routing_grid: RoutingGrid):
        """Issue #2810 regression guard: real vias must still classify as ``via``.

        Places only a via (no pads in the search radius) and confirms the new
        ``pad_clearance`` branch does NOT swallow the ``via`` case.
        ``_mark_via`` never sets ``cell.original_net``, so the new
        discriminator (``original_net != 0 and original_net == cell.net``)
        evaluates False and the cell correctly falls through to ``"via"``.
        """
        # Place a target pad we'll analyse for blockers.
        target = Pad(
            x=25.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="VICTIM",
            layer=Layer.F_CU,
        )
        routing_grid.add_pad(target)

        # Place an isolated via from a different net nearby (no pad near it).
        # The via's clearance ring (radius ~= drill/2 + via_clearance +
        # trace_width/2) will extend cells into the pad-access search
        # radius (~0.6 mm with the default test rules).
        offending_via = Via(
            x=26.0,
            y=25.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=2,
            net_name="OTHER",
        )
        routing_grid._mark_via(offending_via)

        analyzer = RootCauseAnalyzer()

        blockers = analyzer.analyze_pad_access_blockers(
            grid=routing_grid,
            pad_x=25.0,
            pad_y=25.0,
            pad_ref="U1.1",
            pad_net=1,
            layer=0,
            net_names={1: "VICTIM", 2: "OTHER"},
        )

        # The via must be reported as a blocker AND classified as "via",
        # not "pad_clearance" (regression guard for the new branch).
        via_blockers = [b for b in blockers if b.blocking_net == 2]
        assert len(via_blockers) >= 1, (
            f"Expected at least one blocker from the offending via, got: {blockers!r}"
        )
        assert all(b.blocking_type == "via" for b in via_blockers), (
            "Expected blocking_type='via' for the isolated via, got: "
            f"{[b.blocking_type for b in via_blockers]!r}"
        )

    def test_failure_analysis_includes_pad_access_blockers(self):
        """Test that FailureAnalysis includes pad_access_blockers field."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.PIN_ACCESS,
            confidence=0.9,
            failure_location=(25, 25),
            failure_area=Rectangle(20, 20, 30, 30),
            pad_access_blockers=[
                PadAccessBlocker(
                    pad_ref="U1.13",
                    blocking_net=15,
                    blocking_net_name="SC_POS_PLUS",
                    blocking_type="trace",
                    distance=0.12,
                    suggested_clearance=0.10,
                )
            ],
        )

        assert len(analysis.pad_access_blockers) == 1
        assert analysis.pad_access_blockers[0].pad_ref == "U1.13"

        # Test to_dict includes pad_access_blockers
        d = analysis.to_dict()
        assert "pad_access_blockers" in d
        assert len(d["pad_access_blockers"]) == 1
        assert d["pad_access_blockers"][0]["pad_ref"] == "U1.13"


class TestActionableSuggestion:
    """Tests for ActionableSuggestion dataclass."""

    def test_actionable_suggestion_creation(self):
        """Test creating an ActionableSuggestion."""
        suggestion = ActionableSuggestion(
            category="placement",
            priority=1,
            summary="Move U3 0.5mm east to create routing channel",
            details="The +3.3V trace blocks MCLK_DAC near U3 pin 15",
            affected_component="U3",
            suggested_action="move",
            direction="east",
            distance_mm=0.5,
        )

        assert suggestion.category == "placement"
        assert suggestion.priority == 1
        assert "U3" in suggestion.summary
        assert suggestion.affected_component == "U3"
        assert suggestion.direction == "east"
        assert suggestion.distance_mm == 0.5

    def test_actionable_suggestion_to_dict(self):
        """Test converting ActionableSuggestion to dictionary."""
        suggestion = ActionableSuggestion(
            category="design_rules",
            priority=2,
            summary="Reduce clearance to 0.1mm",
            parameter_name="trace_clearance",
            current_value=0.15,
            suggested_value=0.10,
            suggested_action="reduce",
        )

        d = suggestion.to_dict()
        assert d["category"] == "design_rules"
        assert d["priority"] == 2
        assert d["parameter_name"] == "trace_clearance"
        assert d["current_value"] == 0.15
        assert d["suggested_value"] == 0.10

    def test_actionable_suggestion_str(self):
        """Test string representation of ActionableSuggestion."""
        suggestion = ActionableSuggestion(
            category="routing_order",
            priority=1,
            summary="Route MCLK_DAC before +3.3V",
        )

        s = str(suggestion)
        assert "Route MCLK_DAC before +3.3V" in s

    def test_actionable_suggestion_optional_fields(self):
        """Test that optional fields are only included when set."""
        suggestion = ActionableSuggestion(
            category="layer_stack",
            priority=1,
            summary="Add more layers",
        )

        d = suggestion.to_dict()
        assert "affected_component" not in d
        assert "direction" not in d
        assert "distance_mm" not in d


class TestFailureAnalysisFormatSummary:
    """Tests for FailureAnalysis.format_summary method."""

    def test_format_summary_routing_order(self):
        """Test format_summary for routing order failure."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.ROUTING_ORDER,
            confidence=0.9,
            failure_location=(32.5, 28.0),
            failure_area=Rectangle(30, 25, 35, 31),
            blocking_net_name="+3.3V",
            nearby_component="U3",
            nearby_pin="15",
            suggestions=["Try routing MCLK_DAC before +3.3V, or move U3 0.5mm east"],
        )

        summary = analysis.format_summary("MCLK_DAC")
        assert "MCLK_DAC" in summary
        assert "+3.3V" in summary
        assert "U3" in summary
        assert "pin 15" in summary
        assert "Suggestion:" in summary

    def test_format_summary_pin_access(self):
        """Test format_summary for pin access failure."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.PIN_ACCESS,
            confidence=0.85,
            failure_location=(10, 10),
            failure_area=Rectangle(8, 8, 12, 12),
            nearby_component="U1",
            nearby_pin="4",
            suggestions=["Use finer grid (0.05mm) or neck-down traces"],
        )

        summary = analysis.format_summary("NRST")
        assert "NRST" in summary
        assert "Pin escape blocked" in summary
        assert "U1" in summary

    def test_format_summary_congestion(self):
        """Test format_summary for congestion failure."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(32.5, 28.0),
            failure_area=Rectangle(10, 18, 55, 38),
            congestion_score=0.95,
            suggestions=["Consider 6-layer stackup or placement adjustment"],
        )

        summary = analysis.format_summary("SPI_MOSI")
        assert "SPI_MOSI" in summary
        assert "No path exists" in summary
        assert "Blocked area" in summary
        # Should show dimensions
        assert "45" in summary  # width
        assert "20" in summary  # height

    def test_format_summary_to_dict_includes_description(self):
        """Test that to_dict includes root_cause_description."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(0, 0),
            failure_area=Rectangle(0, 0, 10, 10),
        )

        d = analysis.to_dict()
        assert "root_cause_description" in d
        assert d["root_cause_description"] == FailureCause.CONGESTION.description


class TestFailureAnalysisEnhancements:
    """Tests for enhanced FailureAnalysis features."""

    def test_failure_analysis_with_actionable_suggestions(self):
        """Test FailureAnalysis with actionable suggestions."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.85,
            failure_location=(25, 25),
            failure_area=Rectangle(20, 20, 30, 30),
            suggestions=["Move U1 east"],
            actionable_suggestions=[
                ActionableSuggestion(
                    category="placement",
                    priority=1,
                    summary="Move U1 east",
                    affected_component="U1",
                    direction="east",
                    distance_mm=0.5,
                )
            ],
        )

        assert len(analysis.actionable_suggestions) == 1
        assert analysis.actionable_suggestions[0].affected_component == "U1"

        d = analysis.to_dict()
        assert "actionable_suggestions" in d
        assert len(d["actionable_suggestions"]) == 1
        assert d["actionable_suggestions"][0]["direction"] == "east"

    def test_failure_analysis_with_blocking_net(self):
        """Test FailureAnalysis with blocking net information."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.ROUTING_ORDER,
            confidence=0.9,
            failure_location=(25, 25),
            failure_area=Rectangle(20, 20, 30, 30),
            blocking_net_name="VCC",
            nearby_component="U2",
            nearby_pin="3",
        )

        assert analysis.blocking_net_name == "VCC"
        assert analysis.nearby_component == "U2"
        assert analysis.nearby_pin == "3"

        d = analysis.to_dict()
        assert d["blocking_net_name"] == "VCC"
        assert d["nearby_component"] == "U2"
        assert d["nearby_pin"] == "3"

    def test_failure_area_dimensions_in_dict(self):
        """Test that failure_area includes width and height in to_dict."""
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(25, 25),
            failure_area=Rectangle(10, 20, 55, 45),
        )

        d = analysis.to_dict()
        assert "failure_area" in d
        assert d["failure_area"]["width"] == 45  # 55 - 10
        assert d["failure_area"]["height"] == 25  # 45 - 20
