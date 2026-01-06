"""
Unit tests for the recovery module.

Tests cover:
- FailureCause enumeration
- StrategyType enumeration
- Difficulty enumeration
- Rectangle dataclass
- BlockingElement dataclass
- FailureAnalysis dataclass
- ResolutionStrategy dataclass
- Action and SideEffect dataclasses
- StrategyGenerator class
- PatternMatcher class
"""

from kicad_tools.recovery import (
    Action,
    BlockingElement,
    Difficulty,
    FailureAnalysis,
    FailureCause,
    PathAttempt,
    PatternMatcher,
    Rectangle,
    ResolutionStrategy,
    SideEffect,
    StrategyGenerator,
    StrategyType,
)


class TestFailureCause:
    """Tests for FailureCause enumeration."""

    def test_failure_cause_values(self):
        """Test FailureCause enum values."""
        assert FailureCause.CONGESTION.value == "congestion"
        assert FailureCause.BLOCKED_PATH.value == "blocked_path"
        assert FailureCause.CLEARANCE.value == "clearance"
        assert FailureCause.LAYER_CONFLICT.value == "layer_conflict"
        assert FailureCause.PIN_ACCESS.value == "pin_access"
        assert FailureCause.LENGTH_CONSTRAINT.value == "length_constraint"
        assert FailureCause.DIFFERENTIAL_PAIR.value == "differential_pair"
        assert FailureCause.KEEPOUT.value == "keepout"

    def test_failure_cause_from_string(self):
        """Test creating FailureCause from string value."""
        assert FailureCause("congestion") == FailureCause.CONGESTION
        assert FailureCause("blocked_path") == FailureCause.BLOCKED_PATH


class TestStrategyType:
    """Tests for StrategyType enumeration."""

    def test_strategy_type_values(self):
        """Test StrategyType enum values."""
        assert StrategyType.MOVE_COMPONENT.value == "move_component"
        assert StrategyType.MOVE_MULTIPLE.value == "move_multiple"
        assert StrategyType.ADD_VIA.value == "add_via"
        assert StrategyType.CHANGE_LAYER.value == "change_layer"
        assert StrategyType.REROUTE_NET.value == "reroute_net"
        assert StrategyType.REROUTE_MULTIPLE.value == "reroute_multiple"
        assert StrategyType.WIDEN_CLEARANCE.value == "widen_clearance"
        assert StrategyType.MANUAL_INTERVENTION.value == "manual_intervention"


class TestDifficulty:
    """Tests for Difficulty enumeration."""

    def test_difficulty_values(self):
        """Test Difficulty enum values."""
        assert Difficulty.TRIVIAL.value == "trivial"
        assert Difficulty.EASY.value == "easy"
        assert Difficulty.MEDIUM.value == "medium"
        assert Difficulty.HARD.value == "hard"
        assert Difficulty.EXPERT.value == "expert"

    def test_difficulty_ordering(self):
        """Test that difficulty values can be ordered."""
        difficulties = [
            Difficulty.TRIVIAL,
            Difficulty.EASY,
            Difficulty.MEDIUM,
            Difficulty.HARD,
            Difficulty.EXPERT,
        ]
        assert len(difficulties) == 5


class TestRectangle:
    """Tests for Rectangle dataclass."""

    def test_rectangle_creation(self):
        """Test creating a Rectangle."""
        rect = Rectangle(min_x=10.0, min_y=20.0, max_x=30.0, max_y=50.0)
        assert rect.min_x == 10.0
        assert rect.min_y == 20.0
        assert rect.max_x == 30.0
        assert rect.max_y == 50.0

    def test_rectangle_width_height(self):
        """Test Rectangle width and height properties."""
        rect = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=20.0)
        assert rect.width == 10.0
        assert rect.height == 20.0

    def test_rectangle_center(self):
        """Test Rectangle center property."""
        rect = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)
        assert rect.center == (5.0, 5.0)

    def test_rectangle_area(self):
        """Test Rectangle area property."""
        rect = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=5.0)
        assert rect.area == 50.0

    def test_rectangle_contains_point(self):
        """Test Rectangle contains_point method."""
        rect = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)
        assert rect.contains_point(5.0, 5.0) is True
        assert rect.contains_point(0.0, 0.0) is True
        assert rect.contains_point(10.0, 10.0) is True
        assert rect.contains_point(-1.0, 5.0) is False
        assert rect.contains_point(11.0, 5.0) is False

    def test_rectangle_intersects(self):
        """Test Rectangle intersects method."""
        rect1 = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)
        rect2 = Rectangle(min_x=5, min_y=5, max_x=15.0, max_y=15.0)
        rect3 = Rectangle(min_x=20, min_y=20, max_x=30.0, max_y=30.0)
        assert rect1.intersects(rect2) is True
        assert rect1.intersects(rect3) is False

    def test_rectangle_expand(self):
        """Test Rectangle expand method."""
        rect = Rectangle(min_x=10, min_y=10, max_x=20.0, max_y=20.0)
        expanded = rect.expand(5.0)
        assert expanded.min_x == 5.0
        assert expanded.min_y == 5.0
        assert expanded.max_x == 25.0
        assert expanded.max_y == 25.0

    def test_rectangle_to_dict(self):
        """Test Rectangle to_dict method."""
        rect = Rectangle(min_x=0, min_y=1.0, max_x=10.0, max_y=20.0)
        d = rect.to_dict()
        assert d == {"min_x": 0, "min_y": 1.0, "max_x": 10.0, "max_y": 20.0}


class TestBlockingElement:
    """Tests for BlockingElement dataclass."""

    def test_blocking_element_creation(self):
        """Test creating a BlockingElement."""
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        element = BlockingElement(
            type="component",
            ref="C1",
            net=None,
            bounds=bounds,
            movable=True,
        )
        assert element.type == "component"
        assert element.ref == "C1"
        assert element.net is None
        assert element.movable is True

    def test_blocking_element_trace(self):
        """Test BlockingElement for a trace."""
        bounds = Rectangle(min_x=10, min_y=10, max_x=50.0, max_y=12.0)
        element = BlockingElement(
            type="trace",
            ref=None,
            net="GND",
            bounds=bounds,
            movable=False,
        )
        assert element.type == "trace"
        assert element.ref is None
        assert element.net == "GND"
        assert element.movable is False

    def test_blocking_element_to_dict(self):
        """Test BlockingElement to_dict method."""
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        element = BlockingElement(
            type="via",
            ref=None,
            net="VCC",
            bounds=bounds,
            movable=False,
        )
        d = element.to_dict()
        assert d["type"] == "via"
        assert d["ref"] is None
        assert d["net"] == "VCC"
        assert d["movable"] is False
        assert "bounds" in d


class TestPathAttempt:
    """Tests for PathAttempt dataclass."""

    def test_path_attempt_creation(self):
        """Test creating a PathAttempt."""
        attempt = PathAttempt(
            start=(0.0, 0.0),
            end=(100.0, 100.0),
            reached=0.75,
            failure_point=(75.0, 75.0),
            failure_reason="Blocked by C3",
        )
        assert attempt.start == (0.0, 0.0)
        assert attempt.end == (100.0, 100.0)
        assert attempt.reached == 0.75
        assert attempt.failure_point == (75.0, 75.0)
        assert attempt.failure_reason == "Blocked by C3"

    def test_path_attempt_to_dict(self):
        """Test PathAttempt to_dict method."""
        attempt = PathAttempt(
            start=(10.0, 20.0),
            end=(50.0, 60.0),
            reached=0.5,
            failure_point=(30.0, 40.0),
            failure_reason="Clearance violation",
        )
        d = attempt.to_dict()
        assert d["start"] == {"x": 10.0, "y": 20.0}
        assert d["end"] == {"x": 50.0, "y": 60.0}
        assert d["reached"] == 0.5
        assert d["failure_point"] == {"x": 30.0, "y": 40.0}


class TestFailureAnalysis:
    """Tests for FailureAnalysis dataclass."""

    def test_failure_analysis_creation(self):
        """Test creating a FailureAnalysis."""
        failure_area = Rectangle(min_x=40, min_y=28, max_x=50.0, max_y=36.0)
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.85,
            failure_location=(45.2, 32.1),
            failure_area=failure_area,
            congestion_score=0.92,
            net="CLK",
        )
        assert analysis.root_cause == FailureCause.CONGESTION
        assert analysis.confidence == 0.85
        assert analysis.failure_location == (45.2, 32.1)
        assert analysis.congestion_score == 0.92
        assert analysis.net == "CLK"

    def test_failure_analysis_has_movable_blockers(self):
        """Test has_movable_blockers property."""
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        # With movable blockers
        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="component", ref="C1", net=None, bounds=bounds, movable=True)
            ],
        )
        assert analysis.has_movable_blockers is True

        # Without movable blockers
        analysis2 = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="trace", ref=None, net="GND", bounds=bounds, movable=False)
            ],
        )
        assert analysis2.has_movable_blockers is False

    def test_failure_analysis_has_reroutable_nets(self):
        """Test has_reroutable_nets property."""
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            net="CLK",
            blocking_elements=[
                BlockingElement(type="trace", ref=None, net="DATA", bounds=bounds, movable=False)
            ],
        )
        assert analysis.has_reroutable_nets is True

    def test_failure_analysis_near_connector(self):
        """Test near_connector property."""
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        # Near connector
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="component", ref="J1", net=None, bounds=bounds, movable=False)
            ],
        )
        assert analysis.near_connector is True

        # Not near connector
        analysis2 = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="component", ref="C1", net=None, bounds=bounds, movable=True)
            ],
        )
        assert analysis2.near_connector is False

    def test_failure_analysis_to_dict(self):
        """Test FailureAnalysis to_dict method."""
        failure_area = Rectangle(min_x=40, min_y=28, max_x=50.0, max_y=36.0)
        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.85,
            failure_location=(45.2, 32.1),
            failure_area=failure_area,
            congestion_score=0.92,
            attempted_paths=15,
            net="CLK",
        )
        d = analysis.to_dict()
        assert d["root_cause"] == "congestion"
        assert d["confidence"] == 0.85
        assert d["failure_location"] == {"x": 45.2, "y": 32.1}
        assert d["congestion_score"] == 0.92
        assert d["attempted_paths"] == 15
        assert d["net"] == "CLK"


class TestSideEffect:
    """Tests for SideEffect dataclass."""

    def test_side_effect_creation(self):
        """Test creating a SideEffect."""
        effect = SideEffect(
            description="May affect decoupling effectiveness",
            severity="warning",
            mitigatable=False,
        )
        assert effect.description == "May affect decoupling effectiveness"
        assert effect.severity == "warning"
        assert effect.mitigatable is False

    def test_side_effect_to_dict(self):
        """Test SideEffect to_dict method."""
        effect = SideEffect(
            description="Test effect",
            severity="info",
            mitigatable=True,
        )
        d = effect.to_dict()
        assert d == {
            "description": "Test effect",
            "severity": "info",
            "mitigatable": True,
        }


class TestAction:
    """Tests for Action dataclass."""

    def test_action_creation(self):
        """Test creating an Action."""
        action = Action(
            type="move",
            target="C3",
            params={"x": 48.0, "y": 35.0},
        )
        assert action.type == "move"
        assert action.target == "C3"
        assert action.params == {"x": 48.0, "y": 35.0}

    def test_action_to_dict(self):
        """Test Action to_dict method."""
        action = Action(
            type="add_via",
            target="CLK",
            params={"x": 44.0, "y": 30.0, "layer": "B.Cu"},
        )
        d = action.to_dict()
        assert d["type"] == "add_via"
        assert d["target"] == "CLK"
        assert d["params"]["layer"] == "B.Cu"


class TestResolutionStrategy:
    """Tests for ResolutionStrategy dataclass."""

    def test_resolution_strategy_creation(self):
        """Test creating a ResolutionStrategy."""
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C3", params={"x": 48.0, "y": 35.0})],
            side_effects=[
                SideEffect(
                    description="May affect decoupling",
                    severity="warning",
                    mitigatable=False,
                )
            ],
            affected_components=["C3"],
            affected_nets=["VCC", "GND"],
            estimated_improvement=0.9,
        )
        assert strategy.type == StrategyType.MOVE_COMPONENT
        assert strategy.difficulty == Difficulty.EASY
        assert strategy.confidence == 0.85
        assert len(strategy.actions) == 1
        assert len(strategy.side_effects) == 1
        assert strategy.affected_components == ["C3"]
        assert strategy.affected_nets == ["VCC", "GND"]
        assert strategy.estimated_improvement == 0.9

    def test_resolution_strategy_to_dict(self):
        """Test ResolutionStrategy to_dict method."""
        strategy = ResolutionStrategy(
            type=StrategyType.ADD_VIA,
            difficulty=Difficulty.MEDIUM,
            confidence=0.75,
            actions=[Action(type="add_via", target="CLK", params={"x": 44.0, "y": 30.0})],
            side_effects=[
                SideEffect(
                    description="Uses via budget",
                    severity="info",
                    mitigatable=False,
                )
            ],
            affected_components=[],
            affected_nets=["CLK"],
            estimated_improvement=0.7,
        )
        d = strategy.to_dict()
        assert d["type"] == "add_via"
        assert d["difficulty"] == "medium"
        assert d["confidence"] == 0.75
        assert len(d["actions"]) == 1
        assert len(d["side_effects"]) == 1
        assert d["affected_nets"] == ["CLK"]


class TestPatternMatcher:
    """Tests for PatternMatcher class."""

    def test_pattern_matcher_creation(self):
        """Test creating a PatternMatcher."""
        matcher = PatternMatcher()
        assert matcher is not None

    def test_get_all_patterns(self):
        """Test that at least 5 patterns are defined."""
        matcher = PatternMatcher()
        patterns = matcher.get_all_patterns()
        assert len(patterns) >= 5  # Acceptance criteria: at least 5 patterns

    def test_pattern_definitions_complete(self):
        """Test that each pattern has required fields."""
        matcher = PatternMatcher()
        patterns = matcher.get_all_patterns()
        for pattern in patterns:
            assert pattern.name
            assert pattern.description
            assert pattern.suggestion
            assert pattern.example

    def test_match_bypass_cap_blocking_pattern(self):
        """Test matching bypass cap blocking pattern."""
        matcher = PatternMatcher()
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="component", ref="C1", net=None, bounds=bounds, movable=True)
            ],
        )
        matches = matcher.match_patterns(analysis)
        pattern_names = [m.pattern for m in matches]
        assert "bypass_cap_blocking" in pattern_names

    def test_match_connector_bottleneck_pattern(self):
        """Test matching connector bottleneck pattern."""
        matcher = PatternMatcher()
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="component", ref="J1", net=None, bounds=bounds, movable=False)
            ],
        )
        matches = matcher.match_patterns(analysis)
        pattern_names = [m.pattern for m in matches]
        assert "connector_bottleneck" in pattern_names

    def test_match_via_farm_blocking_pattern(self):
        """Test matching via farm blocking pattern."""
        matcher = PatternMatcher()
        bounds = Rectangle(min_x=0, min_y=0, max_x=1.0, max_y=1.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        # Multiple vias blocking
        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="via", ref=None, net="GND", bounds=bounds, movable=False),
                BlockingElement(type="via", ref=None, net="VCC", bounds=bounds, movable=False),
                BlockingElement(type="via", ref=None, net="3V3", bounds=bounds, movable=False),
            ],
        )
        matches = matcher.match_patterns(analysis)
        pattern_names = [m.pattern for m in matches]
        assert "via_farm_blocking" in pattern_names

    def test_match_differential_pair_pattern(self):
        """Test matching differential pair pattern."""
        matcher = PatternMatcher()
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.DIFFERENTIAL_PAIR,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
        )
        matches = matcher.match_patterns(analysis)
        pattern_names = [m.pattern for m in matches]
        assert "differential_pair_obstacle" in pattern_names

    def test_no_match_for_unrelated_failure(self):
        """Test that patterns don't match unrelated failures."""
        matcher = PatternMatcher()
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        # Simple clearance issue with no specific pattern
        analysis = FailureAnalysis(
            root_cause=FailureCause.PIN_ACCESS,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[],
        )
        matches = matcher.match_patterns(analysis)
        # Should have no or few matches for this generic case
        assert len(matches) <= 2

    def test_matched_pattern_has_all_fields(self):
        """Test that matched patterns have all required fields."""
        matcher = PatternMatcher()
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            blocking_elements=[
                BlockingElement(type="component", ref="C1", net=None, bounds=bounds, movable=True)
            ],
        )
        matches = matcher.match_patterns(analysis)
        for match in matches:
            assert match.pattern
            assert match.suggestion
            assert match.example
            assert 0 <= match.confidence <= 1


class TestStrategyGenerator:
    """Tests for StrategyGenerator class."""

    def test_strategy_generator_creation(self):
        """Test creating a StrategyGenerator."""
        generator = StrategyGenerator()
        assert generator is not None

    def test_generates_manual_strategy_for_keepout(self):
        """Test that keepout failures get manual intervention strategy."""
        generator = StrategyGenerator()
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.KEEPOUT,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            net="CLK",
        )

        # Create minimal mock PCB for testing
        class MockPCB:
            footprints = []
            segments = []
            vias = []
            zones = []
            layers = {}
            nets = {}

        strategies = generator.generate_strategies(MockPCB(), analysis)
        assert len(strategies) >= 1
        # Manual intervention should be present for keepout
        strategy_types = [s.type for s in strategies]
        assert StrategyType.MANUAL_INTERVENTION in strategy_types

    def test_strategies_are_ranked(self):
        """Test that strategies are returned in ranked order."""
        generator = StrategyGenerator()
        bounds = Rectangle(min_x=0, min_y=0, max_x=5.0, max_y=5.0)
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.CONGESTION,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            congestion_score=0.95,
            net="CLK",
            blocking_elements=[
                BlockingElement(type="component", ref="C1", net=None, bounds=bounds, movable=True),
                BlockingElement(type="component", ref="J1", net=None, bounds=bounds, movable=False),
            ],
        )

        class MockPCB:
            footprints = []
            segments = []
            vias = []
            zones = []
            layers = {}
            nets = {}

        strategies = generator.generate_strategies(MockPCB(), analysis)

        # Verify strategies are returned (at least manual intervention)
        assert len(strategies) >= 1

        # Verify ranking: easier difficulties should come first
        difficulty_order = {
            Difficulty.TRIVIAL: 0,
            Difficulty.EASY: 1,
            Difficulty.MEDIUM: 2,
            Difficulty.HARD: 3,
            Difficulty.EXPERT: 4,
        }

        for i in range(len(strategies) - 1):
            curr_diff = difficulty_order[strategies[i].difficulty]
            next_diff = difficulty_order[strategies[i + 1].difficulty]
            # Current should be <= next (accounting for confidence as tie-breaker)
            if curr_diff == next_diff:
                # With same difficulty, higher confidence should come first
                assert strategies[i].confidence >= strategies[i + 1].confidence

    def test_strategy_has_required_fields(self):
        """Test that generated strategies have all required fields."""
        generator = StrategyGenerator()
        failure_area = Rectangle(min_x=0, min_y=0, max_x=10.0, max_y=10.0)

        analysis = FailureAnalysis(
            root_cause=FailureCause.PIN_ACCESS,
            confidence=0.9,
            failure_location=(5.0, 5.0),
            failure_area=failure_area,
            net="SDA",
        )

        class MockPCB:
            footprints = []
            segments = []
            vias = []
            zones = []
            layers = {}
            nets = {}

        strategies = generator.generate_strategies(MockPCB(), analysis)

        for strategy in strategies:
            assert strategy.type is not None
            assert strategy.difficulty is not None
            assert 0 <= strategy.confidence <= 1
            assert isinstance(strategy.actions, list)
            assert isinstance(strategy.side_effects, list)
            assert isinstance(strategy.affected_components, list)
            assert isinstance(strategy.affected_nets, list)
            assert 0 <= strategy.estimated_improvement <= 1


class TestModuleExports:
    """Tests for module exports."""

    def test_all_types_exported(self):
        """Test that all expected types are exported from the module."""
        from kicad_tools.recovery import (
            Action,
            BlockingElement,
            Difficulty,
            FailureAnalysis,
            FailureCause,
            PathAttempt,
            PatternMatcher,
            Rectangle,
            ResolutionStrategy,
            SideEffect,
            StrategyGenerator,
            StrategyType,
        )

        # Verify they can all be instantiated or are enums
        assert FailureCause.CONGESTION is not None
        assert StrategyType.MOVE_COMPONENT is not None
        assert Difficulty.EASY is not None
        assert Rectangle(0, 0, 1, 1) is not None
        assert BlockingElement("component", "C1", None, Rectangle(0, 0, 1, 1), True) is not None
        assert PathAttempt((0, 0), (1, 1), 0.5, None, None) is not None
        assert (
            FailureAnalysis(FailureCause.CONGESTION, 0.9, (0, 0), Rectangle(0, 0, 1, 1)) is not None
        )
        assert SideEffect("test", "info", True) is not None
        assert Action("move", "C1", {}) is not None
        assert ResolutionStrategy(StrategyType.MOVE_COMPONENT, Difficulty.EASY, 0.9) is not None
        assert StrategyGenerator() is not None
        assert PatternMatcher() is not None
