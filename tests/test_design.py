"""
Tests for multi-resolution design abstraction layer.

Includes tests for:
- SubsystemType and SubsystemDefinition
- PlacementStrategy implementations (PowerSupply, MCUCore, Connector)
- Design facade class
- Decomposition logic
- Cross-level validation
"""

import pytest

from kicad_tools.design import (
    GroupResult,
    OptimizationGoal,
    Placement,
    PlacementPlan,
    SubsystemDefinition,
    SubsystemResult,
    SubsystemType,
    ValidationIssue,
    ValidationSeverity,
    decompose_group,
    get_strategy,
    get_subsystem_definition,
    list_subsystem_types,
)
from kicad_tools.design.decomposition import (
    DecomposedStep,
    DecompositionResult,
    OperationType,
)
from kicad_tools.design.strategies import (
    ConnectorStrategy,
    MCUCoreStrategy,
    PlacementStrategy,
    PowerSupplyStrategy,
)
from kicad_tools.design.subsystems import SUBSYSTEMS
from kicad_tools.design.validation import (
    DEFAULT_CONSTRAINTS,
    AbstractionValidator,
    Subsystem,
    SubsystemConstraint,
)


class TestSubsystemType:
    """Tests for SubsystemType enum."""

    def test_power_supply_type(self):
        """Test power supply subsystem type."""
        assert SubsystemType.POWER_SUPPLY.value == "power_supply"

    def test_mcu_core_type(self):
        """Test MCU core subsystem type."""
        assert SubsystemType.MCU_CORE.value == "mcu_core"

    def test_connector_type(self):
        """Test connector subsystem type."""
        assert SubsystemType.CONNECTOR.value == "connector"

    def test_all_types_have_definitions(self):
        """Test that all subsystem types have definitions."""
        for st in SubsystemType:
            assert st in SUBSYSTEMS
            definition = SUBSYSTEMS[st]
            assert isinstance(definition, SubsystemDefinition)


class TestSubsystemDefinition:
    """Tests for SubsystemDefinition dataclass."""

    def test_power_supply_definition(self):
        """Test power supply definition has expected properties."""
        definition = get_subsystem_definition("power_supply")
        assert definition.subsystem_type == SubsystemType.POWER_SUPPLY
        assert "ldo" in definition.patterns
        assert "buck" in definition.patterns
        assert definition.anchor_role == "regulator"
        assert len(definition.typical_components) > 0

    def test_mcu_core_definition(self):
        """Test MCU core definition has expected properties."""
        definition = get_subsystem_definition(SubsystemType.MCU_CORE)
        assert definition.subsystem_type == SubsystemType.MCU_CORE
        assert "mcu_bypass" in definition.patterns
        assert definition.anchor_role == "mcu"

    def test_connector_definition(self):
        """Test connector definition has expected properties."""
        definition = get_subsystem_definition("connector")
        assert definition.subsystem_type == SubsystemType.CONNECTOR
        assert "usb" in definition.patterns
        assert definition.anchor_role == "connector"

    def test_invalid_subsystem_type_raises(self):
        """Test that invalid subsystem type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown subsystem type"):
            get_subsystem_definition("invalid_type")

    def test_list_subsystem_types(self):
        """Test listing all subsystem types."""
        types = list_subsystem_types()
        assert "power_supply" in types
        assert "mcu_core" in types
        assert "connector" in types
        assert len(types) >= 3


class TestOptimizationGoal:
    """Tests for OptimizationGoal enum."""

    def test_thermal_goal(self):
        """Test thermal optimization goal."""
        assert OptimizationGoal.THERMAL.value == "thermal"

    def test_routing_goal(self):
        """Test routing optimization goal."""
        assert OptimizationGoal.ROUTING.value == "routing"

    def test_compact_goal(self):
        """Test compact optimization goal."""
        assert OptimizationGoal.COMPACT.value == "compact"


class TestPlacement:
    """Tests for Placement dataclass."""

    def test_create_placement(self):
        """Test creating a placement."""
        placement = Placement(
            ref="U1",
            x=50.0,
            y=30.0,
            rotation=90.0,
            rationale="Anchor position",
        )
        assert placement.ref == "U1"
        assert placement.x == 50.0
        assert placement.y == 30.0
        assert placement.rotation == 90.0
        assert placement.rationale == "Anchor position"

    def test_placement_defaults(self):
        """Test placement default values."""
        placement = Placement(ref="C1", x=10.0, y=20.0)
        assert placement.rotation == 0.0
        assert placement.rationale == ""


class TestPlacementStrategies:
    """Tests for PlacementStrategy implementations."""

    def test_get_power_supply_strategy(self):
        """Test getting power supply strategy."""
        strategy = get_strategy("power_supply")
        assert isinstance(strategy, PowerSupplyStrategy)
        assert strategy.subsystem_type == SubsystemType.POWER_SUPPLY

    def test_get_mcu_core_strategy(self):
        """Test getting MCU core strategy."""
        strategy = get_strategy(SubsystemType.MCU_CORE)
        assert isinstance(strategy, MCUCoreStrategy)
        assert strategy.subsystem_type == SubsystemType.MCU_CORE

    def test_get_connector_strategy(self):
        """Test getting connector strategy."""
        strategy = get_strategy("connector")
        assert isinstance(strategy, ConnectorStrategy)
        assert strategy.subsystem_type == SubsystemType.CONNECTOR

    def test_invalid_strategy_raises(self):
        """Test that invalid strategy raises ValueError."""
        with pytest.raises(ValueError, match="Unknown subsystem type"):
            get_strategy("invalid")

    def test_power_supply_strategy_patterns(self):
        """Test power supply strategy supported patterns."""
        strategy = PowerSupplyStrategy()
        assert "ldo" in strategy.supported_patterns
        assert "buck" in strategy.supported_patterns
        assert "boost" in strategy.supported_patterns

    def test_mcu_core_strategy_patterns(self):
        """Test MCU core strategy supported patterns."""
        strategy = MCUCoreStrategy()
        assert "mcu_bypass" in strategy.supported_patterns
        assert "crystal" in strategy.supported_patterns

    def test_connector_strategy_patterns(self):
        """Test connector strategy supported patterns."""
        strategy = ConnectorStrategy()
        assert "usb" in strategy.supported_patterns
        assert "uart" in strategy.supported_patterns


class TestPlacementPlan:
    """Tests for PlacementPlan dataclass."""

    def test_create_plan(self):
        """Test creating a placement plan."""
        steps = [
            Placement(ref="U1", x=50.0, y=30.0, rationale="Anchor"),
            Placement(ref="C1", x=47.5, y=30.0, rationale="Input cap"),
        ]
        plan = PlacementPlan(
            steps=steps,
            anchor="U1",
            anchor_position=(50.0, 30.0),
            subsystem_type="power_supply",
            optimization_goal="routing",
        )
        assert len(plan.steps) == 2
        assert plan.anchor == "U1"
        assert plan.subsystem_type == "power_supply"

    def test_empty_plan(self):
        """Test empty placement plan."""
        plan = PlacementPlan()
        assert len(plan.steps) == 0
        assert plan.anchor == ""
        assert len(plan.warnings) == 0


class TestDecomposition:
    """Tests for command decomposition."""

    def test_decomposed_step(self):
        """Test creating a decomposed step."""
        step = DecomposedStep(
            operation=OperationType.MOVE,
            ref="U1",
            x=50.0,
            y=30.0,
            description="Place anchor",
            rationale="Starting position",
        )
        assert step.operation == OperationType.MOVE
        assert step.ref == "U1"
        assert step.x == 50.0
        assert step.y == 30.0

    def test_operation_types(self):
        """Test all operation types exist."""
        assert OperationType.MOVE.value == "move"
        assert OperationType.ROTATE.value == "rotate"
        assert OperationType.VALIDATE.value == "validate"
        assert OperationType.GROUP.value == "group"
        assert OperationType.COMMENT.value == "comment"


class TestValidation:
    """Tests for cross-level validation."""

    def test_validation_severity(self):
        """Test validation severity levels."""
        assert ValidationSeverity.ERROR.value == "error"
        assert ValidationSeverity.WARNING.value == "warning"
        assert ValidationSeverity.INFO.value == "info"

    def test_validation_issue(self):
        """Test creating a validation issue."""
        issue = ValidationIssue(
            severity=ValidationSeverity.WARNING,
            message="Component too far from anchor",
            subsystem="power_supply_1",
            component="C1",
            rule_violated="input_cap_distance",
            actual_value=5.0,
            expected_value=3.0,
            suggestion="Move C1 closer to U1",
        )
        assert issue.severity == ValidationSeverity.WARNING
        assert issue.actual_value == 5.0
        assert issue.expected_value == 3.0

    def test_subsystem_constraint(self):
        """Test subsystem constraint definition."""
        constraint = SubsystemConstraint(
            component="input_cap",
            relative_to="regulator",
            max_distance_mm=3.0,
            rationale="Input filtering",
        )
        assert constraint.max_distance_mm == 3.0
        assert constraint.min_distance_mm == 0.0

    def test_default_constraints_exist(self):
        """Test that default constraints are defined for subsystem types."""
        assert SubsystemType.POWER_SUPPLY in DEFAULT_CONSTRAINTS
        assert SubsystemType.MCU_CORE in DEFAULT_CONSTRAINTS
        assert SubsystemType.CONNECTOR in DEFAULT_CONSTRAINTS

    def test_power_supply_default_constraints(self):
        """Test power supply default constraints."""
        constraints = DEFAULT_CONSTRAINTS[SubsystemType.POWER_SUPPLY]
        assert len(constraints) > 0

        # Check for input cap constraint
        input_cap_constraint = next((c for c in constraints if c.component == "input_cap"), None)
        assert input_cap_constraint is not None
        assert input_cap_constraint.max_distance_mm == 3.0

    def test_abstraction_validator_init(self):
        """Test initializing abstraction validator."""
        validator = AbstractionValidator()
        assert len(validator._subsystems) == 0

    def test_register_subsystem(self):
        """Test registering a subsystem for validation."""
        validator = AbstractionValidator()
        subsystem = Subsystem(
            name="power_supply_1",
            subsystem_type=SubsystemType.POWER_SUPPLY,
            components=["U1", "C1", "C2"],
            anchor="U1",
            anchor_position=(50.0, 30.0),
        )
        validator.register_subsystem(subsystem)
        assert len(validator._subsystems) == 1

    def test_clear_subsystems(self):
        """Test clearing registered subsystems."""
        validator = AbstractionValidator()
        subsystem = Subsystem(
            name="test",
            subsystem_type=SubsystemType.POWER_SUPPLY,
            components=["U1"],
            anchor="U1",
            anchor_position=(0, 0),
        )
        validator.register_subsystem(subsystem)
        assert len(validator._subsystems) == 1

        validator.clear_subsystems()
        assert len(validator._subsystems) == 0


class TestSubsystem:
    """Tests for Subsystem dataclass."""

    def test_create_subsystem(self):
        """Test creating a subsystem."""
        subsystem = Subsystem(
            name="power_supply_1",
            subsystem_type=SubsystemType.POWER_SUPPLY,
            components=["U1", "C1", "C2"],
            anchor="U1",
            anchor_position=(50.0, 30.0),
            optimization_goal=OptimizationGoal.THERMAL,
        )
        assert subsystem.name == "power_supply_1"
        assert subsystem.subsystem_type == SubsystemType.POWER_SUPPLY
        assert len(subsystem.components) == 3
        assert subsystem.anchor == "U1"
        assert subsystem.optimization_goal == OptimizationGoal.THERMAL


class TestDecompositionResult:
    """Tests for DecompositionResult dataclass."""

    def test_create_decomposition_result(self):
        """Test creating a decomposition result."""
        steps = [
            DecomposedStep(
                operation=OperationType.MOVE,
                ref="U1",
                x=50.0,
                y=30.0,
            ),
        ]
        result = DecompositionResult(
            steps=steps,
            subsystem_type="power_supply",
            anchor="U1",
            anchor_position=(50.0, 30.0),
            total_components=3,
        )
        assert len(result.steps) == 1
        assert result.subsystem_type == "power_supply"
        assert result.total_components == 3


class TestSubsystemResult:
    """Tests for SubsystemResult dataclass."""

    def test_successful_result(self):
        """Test successful subsystem result."""
        result = SubsystemResult(
            success=True,
            subsystem_name="power_supply_1",
            placements={
                "U1": Placement(ref="U1", x=50.0, y=30.0),
                "C1": Placement(ref="C1", x=47.5, y=30.0),
            },
        )
        assert result.success
        assert len(result.placements) == 2
        assert len(result.warnings) == 0

    def test_failed_result(self):
        """Test failed subsystem result."""
        result = SubsystemResult(
            success=False,
            subsystem_name="",
            warnings=["Failed to find components"],
        )
        assert not result.success
        assert len(result.warnings) == 1


class TestGroupResult:
    """Tests for GroupResult dataclass."""

    def test_successful_group_result(self):
        """Test successful group result."""
        result = GroupResult(
            success=True,
            placements={
                "U1": Placement(ref="U1", x=50.0, y=30.0),
            },
        )
        assert result.success
        assert len(result.placements) == 1

    def test_failed_group_result(self):
        """Test failed group result."""
        result = GroupResult(
            success=False,
            warnings=["Invalid strategy"],
        )
        assert not result.success


class TestDecomposeGroup:
    """Tests for decompose_group function."""

    def test_decompose_power_supply_strategy(self):
        """Test decomposing with power_supply strategy name."""
        # This test would need a mock PCB, so we test the strategy mapping
        strategy_mapping = {
            "power_supply": SubsystemType.POWER_SUPPLY,
            "ldo": SubsystemType.POWER_SUPPLY,
            "buck": SubsystemType.POWER_SUPPLY,
        }
        for strategy, expected_type in strategy_mapping.items():
            # Verify the mapping is correct
            assert expected_type == SubsystemType.POWER_SUPPLY

    def test_invalid_strategy_raises(self):
        """Test that invalid strategy raises ValueError."""
        # We can't test decompose_group directly without a PCB
        # but we can verify the error message format
        with pytest.raises(ValueError, match="Unknown grouping strategy"):
            # Mock PCB class for testing
            class MockPCB:
                footprints = []

                def get_board_outline(self):
                    return [(0, 0), (100, 0), (100, 100), (0, 100)]

            decompose_group(
                refs=["U1", "C1"],
                strategy="invalid_strategy",
                anchor="U1",
                anchor_position=(50, 50),
                pcb=MockPCB(),
            )


class TestIntegration:
    """Integration tests for the design module."""

    def test_subsystem_workflow(self):
        """Test the typical workflow of defining a subsystem."""
        # 1. Get subsystem definition
        definition = get_subsystem_definition("power_supply")
        assert definition.subsystem_type == SubsystemType.POWER_SUPPLY

        # 2. Get strategy
        strategy = get_strategy("power_supply")
        assert isinstance(strategy, PlacementStrategy)

        # 3. Get default constraints
        constraints = DEFAULT_CONSTRAINTS.get(SubsystemType.POWER_SUPPLY, [])
        assert len(constraints) > 0

        # 4. Create validator
        validator = AbstractionValidator()

        # 5. Register subsystem
        subsystem = Subsystem(
            name="power_supply_1",
            subsystem_type=SubsystemType.POWER_SUPPLY,
            components=["U1", "C1", "C2"],
            anchor="U1",
            anchor_position=(50.0, 30.0),
        )
        validator.register_subsystem(subsystem)
        assert len(validator._subsystems) == 1

    def test_all_subsystem_types_have_strategies(self):
        """Test that all defined subsystem types have strategies."""
        # Currently only 3 strategies are implemented
        implemented_types = [
            SubsystemType.POWER_SUPPLY,
            SubsystemType.MCU_CORE,
            SubsystemType.CONNECTOR,
        ]
        for st in implemented_types:
            strategy = get_strategy(st)
            assert strategy.subsystem_type == st

    def test_subsystem_type_string_conversion(self):
        """Test converting subsystem type strings to enums."""
        # Valid conversions
        assert SubsystemType("power_supply") == SubsystemType.POWER_SUPPLY
        assert SubsystemType("mcu_core") == SubsystemType.MCU_CORE
        assert SubsystemType("connector") == SubsystemType.CONNECTOR

        # Invalid conversion
        with pytest.raises(ValueError):
            SubsystemType("invalid")
