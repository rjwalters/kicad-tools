"""
Unit tests for placement-routing feedback loop.

Tests cover:
- StrategyApplicator class
- ApplicationResult dataclass
- PlacementFeedbackLoop class
- PlacementFeedbackResult dataclass
- PlacementAdjustment dataclass
"""

from kicad_tools.recovery import (
    Action,
    ApplicationResult,
    Difficulty,
    Rectangle,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.router import (
    PlacementAdjustment,
    PlacementFeedbackResult,
)


class MockFootprint:
    """Mock footprint for testing."""

    def __init__(self, reference: str, x: float, y: float):
        self.reference = reference
        self.position = (x, y)
        self.pads = []


class MockGraphicItem:
    """Mock graphic item for testing board bounds."""

    def __init__(self, layer: str, start: tuple, end: tuple):
        self.layer = layer
        self.start = start
        self.end = end


class MockPCB:
    """Mock PCB for testing."""

    def __init__(self):
        self.footprints = []
        self.graphic_items = []
        self.segments = []
        self.vias = []
        self.zones = []
        self.layers = {}
        self.nets = {}


class TestApplicationResult:
    """Tests for ApplicationResult dataclass."""

    def test_application_result_creation(self):
        """Test creating an ApplicationResult."""
        result = ApplicationResult(
            success=True,
            components_moved=["C1", "C2"],
            message="Moved 2 components",
            conflicts_created=0,
        )
        assert result.success is True
        assert result.components_moved == ["C1", "C2"]
        assert result.message == "Moved 2 components"
        assert result.conflicts_created == 0

    def test_application_result_failure(self):
        """Test ApplicationResult for failed application."""
        result = ApplicationResult(
            success=False,
            components_moved=[],
            message="Component not found",
        )
        assert result.success is False
        assert result.components_moved == []


class TestStrategyApplicator:
    """Tests for StrategyApplicator class."""

    def test_applicator_creation(self):
        """Test creating a StrategyApplicator."""
        applicator = StrategyApplicator()
        assert applicator is not None
        assert applicator.BOARD_EDGE_MARGIN == 1.0
        assert applicator.MAX_MOVE_DISTANCE == 10.0

    def test_apply_move_component_strategy(self):
        """Test applying a single component move strategy."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 20.0))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 15.0, "y": 25.0})],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is True
        assert result.components_moved == ["C1"]
        assert "C1" in result.message
        assert pcb.footprints[0].position == (15.0, 25.0)

    def test_apply_move_multiple_strategy(self):
        """Test applying a multi-component move strategy."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 20.0))
        pcb.footprints.append(MockFootprint("C2", 30.0, 40.0))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_MULTIPLE,
            difficulty=Difficulty.HARD,
            confidence=0.7,
            actions=[
                Action(type="move", target="C1", params={"x": 12.0, "y": 22.0}),
                Action(type="move", target="C2", params={"x": 32.0, "y": 42.0}),
            ],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is True
        assert set(result.components_moved) == {"C1", "C2"}
        assert pcb.footprints[0].position == (12.0, 22.0)
        assert pcb.footprints[1].position == (32.0, 42.0)

    def test_apply_strategy_component_not_found(self):
        """Test applying strategy for non-existent component."""
        applicator = StrategyApplicator()
        pcb = MockPCB()

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C99", params={"x": 15.0, "y": 25.0})],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is False
        assert "not found" in result.message

    def test_apply_non_placement_strategy(self):
        """Test that non-placement strategies are rejected."""
        applicator = StrategyApplicator()
        pcb = MockPCB()

        strategy = ResolutionStrategy(
            type=StrategyType.ADD_VIA,
            difficulty=Difficulty.MEDIUM,
            confidence=0.75,
            actions=[Action(type="add_via", target="CLK", params={"x": 10.0, "y": 10.0})],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is False
        assert "cannot be applied to placement" in result.message

    def test_is_safe_to_apply_valid(self):
        """Test safety check for valid strategy."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 50.0, 50.0))
        # Add board outline
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (100.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 0.0), (100.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 100.0), (0.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 100.0), (0.0, 0.0)))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 55.0, "y": 55.0})],
        )

        assert applicator.is_safe_to_apply(strategy, pcb) is True

    def test_is_safe_to_apply_out_of_bounds(self):
        """Test safety check rejects out-of-bounds moves."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 50.0, 50.0))
        # Add board outline
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (100.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 0.0), (100.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 100.0), (0.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 100.0), (0.0, 0.0)))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 150.0, "y": 50.0})],
        )

        assert applicator.is_safe_to_apply(strategy, pcb) is False

    def test_is_safe_to_apply_excessive_distance(self):
        """Test safety check rejects excessive move distances."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 50.0, 50.0))
        # Add large board outline
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (200.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (200.0, 0.0), (200.0, 200.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (200.0, 200.0), (0.0, 200.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 200.0), (0.0, 0.0)))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            # Move > 10mm (default MAX_MOVE_DISTANCE)
            actions=[Action(type="move", target="C1", params={"x": 100.0, "y": 100.0})],
        )

        assert applicator.is_safe_to_apply(strategy, pcb) is False

    def test_calculate_move_vector(self):
        """Test calculating move vector for a component."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 10.0))

        failure_area = Rectangle(min_x=5.0, min_y=5.0, max_x=15.0, max_y=15.0)

        vector = applicator.calculate_move_vector(pcb, "C1", failure_area)
        assert vector is not None
        dx, dy = vector
        # Component at (10,10), center at (10,10), should get arbitrary direction
        # Just verify we get some non-zero vector
        assert abs(dx) > 0 or abs(dy) > 0

    def test_calculate_move_vector_with_direction(self):
        """Test calculating move vector with specified direction."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 10.0))

        failure_area = Rectangle(min_x=5.0, min_y=5.0, max_x=15.0, max_y=15.0)

        # Specify direction to the right
        vector = applicator.calculate_move_vector(pcb, "C1", failure_area, direction=(1.0, 0.0))
        assert vector is not None
        dx, dy = vector
        assert dx > 0  # Should move right
        assert abs(dy) < 0.01  # Should not move vertically

    def test_calculate_spread_vector(self):
        """Test calculating spread vector for multi-component spreading."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 12.0, 15.0))

        center = (10.0, 10.0)
        vector = applicator.calculate_spread_vector(pcb, "C1", center)
        assert vector is not None
        dx, dy = vector
        # Component should spread away from center
        # C1 is at (12, 15), center at (10, 10), so should move right and up
        assert dx > 0  # Moving right (away from center)
        assert dy > 0  # Moving up (away from center)

    def test_simulate_placement_change(self):
        """Test simulating placement changes without applying them."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 20.0))
        pcb.footprints.append(MockFootprint("C2", 30.0, 40.0))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_MULTIPLE,
            difficulty=Difficulty.HARD,
            confidence=0.7,
            actions=[
                Action(type="move", target="C1", params={"x": 15.0, "y": 25.0}),
                Action(type="move", target="C2", params={"x": 35.0, "y": 45.0}),
            ],
        )

        positions = applicator.simulate_placement_change(pcb, strategy)

        # Verify simulation returns expected positions
        assert "C1" in positions
        assert "C2" in positions
        assert positions["C1"] == (15.0, 25.0)
        assert positions["C2"] == (35.0, 45.0)

        # Verify original PCB is not modified
        assert pcb.footprints[0].position == (10.0, 20.0)
        assert pcb.footprints[1].position == (30.0, 40.0)


class TestPlacementAdjustment:
    """Tests for PlacementAdjustment dataclass."""

    def test_placement_adjustment_creation(self):
        """Test creating a PlacementAdjustment."""
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 15.0, "y": 25.0})],
        )
        result = ApplicationResult(
            success=True,
            components_moved=["C1"],
            message="Moved C1",
        )
        adjustment = PlacementAdjustment(
            iteration=0,
            strategy=strategy,
            result=result,
            failed_nets_before=[1, 2, 3],
            failed_nets_after=[2],
        )
        assert adjustment.iteration == 0
        assert adjustment.strategy == strategy
        assert adjustment.result == result
        assert len(adjustment.failed_nets_before) == 3
        assert len(adjustment.failed_nets_after) == 1


class TestPlacementFeedbackResult:
    """Tests for PlacementFeedbackResult dataclass."""

    def test_feedback_result_creation(self):
        """Test creating a PlacementFeedbackResult."""
        result = PlacementFeedbackResult(
            success=True,
            routes=[],
            iterations=2,
            adjustments=[],
            failed_nets=[],
            total_components_moved=3,
        )
        assert result.success is True
        assert result.iterations == 2
        assert result.total_components_moved == 3

    def test_feedback_result_summary(self):
        """Test PlacementFeedbackResult summary method."""
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 15.0, "y": 25.0})],
        )
        app_result = ApplicationResult(
            success=True,
            components_moved=["C1"],
            message="Moved C1",
        )
        adjustment = PlacementAdjustment(
            iteration=0,
            strategy=strategy,
            result=app_result,
            failed_nets_before=[1, 2, 3],
            failed_nets_after=[2],
        )
        result = PlacementFeedbackResult(
            success=False,
            routes=[],
            iterations=2,
            adjustments=[adjustment],
            failed_nets=[2],
            total_components_moved=1,
        )
        summary = result.summary()
        assert "Placement-Routing Feedback Result" in summary
        assert "Success: False" in summary
        assert "Iterations: 2" in summary
        assert "Components moved: 1" in summary
        assert "Failed nets: 1" in summary
        assert "Adjustments:" in summary


class TestModuleExports:
    """Tests for module exports."""

    def test_recovery_exports(self):
        """Test that new classes are exported from recovery module."""
        from kicad_tools.recovery import (
            ApplicationResult,
            StrategyApplicator,
        )

        assert ApplicationResult is not None
        assert StrategyApplicator is not None

    def test_router_exports(self):
        """Test that new classes are exported from router module."""
        from kicad_tools.router import (
            PlacementAdjustment,
            PlacementFeedbackLoop,
            PlacementFeedbackResult,
        )

        assert PlacementAdjustment is not None
        assert PlacementFeedbackLoop is not None
        assert PlacementFeedbackResult is not None
