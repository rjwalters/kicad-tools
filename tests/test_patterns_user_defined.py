"""
Unit tests for user-defined patterns module.

Tests cover:
- Validation checks (checks.py)
- Pattern registry (registry.py)
- YAML pattern loader (loader.py)
- Pattern definition DSL (dsl.py)
"""

from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from kicad_tools.patterns.checks import (
    CheckContext,
    ComponentDistanceCheck,
    ComponentPresentCheck,
    TraceLengthCheck,
    ValidationCheck,
    ValueMatchCheck,
    ValueRangeCheck,
    create_check,
    get_check,
    register_check,
)
from kicad_tools.patterns.dsl import (
    DSLPattern,
    define_pattern,
    get_pattern_from_class,
    get_pattern_name_from_class,
    placement_rule,
    routing_constraint,
)
from kicad_tools.patterns.loader import PatternLoader, YAMLPattern
from kicad_tools.patterns.registry import PatternRegistry, register_pattern
from kicad_tools.patterns.schema import PlacementPriority, PlacementRule


class TestCheckContext:
    """Tests for CheckContext."""

    def test_context_creation(self) -> None:
        """Test creating a CheckContext."""
        context = CheckContext(
            component_positions={"R1": (10.0, 20.0), "C1": (15.0, 20.0)},
            component_values={"R1": "10k", "C1": "100nF"},
            component_footprints={"R1": "0603", "C1": "0402"},
            net_lengths={"VCC": 25.0, "GND": 30.0},
        )
        assert context.component_positions["R1"] == (10.0, 20.0)
        assert context.component_values["C1"] == "100nF"
        assert context.net_lengths["VCC"] == 25.0


class TestComponentDistanceCheck:
    """Tests for ComponentDistanceCheck."""

    def test_check_passes_when_within_distance(self) -> None:
        """Test check passes when components are close enough."""
        check = ComponentDistanceCheck(
            from_component="R1",
            to_component="C1",
            max_mm=10.0,
        )
        context = CheckContext(
            component_positions={"R1": (0.0, 0.0), "C1": (5.0, 0.0)},
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is None

    def test_check_fails_when_too_far(self) -> None:
        """Test check fails when components are too far apart."""
        check = ComponentDistanceCheck(
            from_component="R1",
            to_component="C1",
            max_mm=3.0,
            rationale="Test rationale",
        )
        context = CheckContext(
            component_positions={"R1": (0.0, 0.0), "C1": (5.0, 0.0)},
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "too far" in violation.message
        assert "Test rationale" in violation.message
        assert violation.actual_value == 5.0
        assert violation.expected_value == 3.0

    def test_check_fails_when_too_close(self) -> None:
        """Test check fails when components are too close."""
        check = ComponentDistanceCheck(
            from_component="R1",
            to_component="C1",
            max_mm=10.0,
            min_mm=3.0,
        )
        context = CheckContext(
            component_positions={"R1": (0.0, 0.0), "C1": (1.0, 0.0)},
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "too close" in violation.message

    def test_check_fails_when_component_missing(self) -> None:
        """Test check fails when component is not found."""
        check = ComponentDistanceCheck(
            from_component="R1",
            to_component="C1",
            max_mm=10.0,
        )
        context = CheckContext(
            component_positions={"R1": (0.0, 0.0)},  # C1 missing
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "not found" in violation.message


class TestValueMatchCheck:
    """Tests for ValueMatchCheck."""

    def test_check_passes_when_values_match(self) -> None:
        """Test check passes when values are equal."""
        check = ValueMatchCheck(component="R1", equals="R2")
        context = CheckContext(
            component_positions={},
            component_values={"R1": "10k", "R2": "10k"},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is None

    def test_check_fails_when_values_differ(self) -> None:
        """Test check fails when values don't match."""
        check = ValueMatchCheck(component="R1", equals="R2", rationale="Should match")
        context = CheckContext(
            component_positions={},
            component_values={"R1": "10k", "R2": "4.7k"},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "does not match" in violation.message

    def test_check_passes_within_tolerance(self) -> None:
        """Test check passes when values are within tolerance."""
        check = ValueMatchCheck(component="R1", equals="R2", tolerance_percent=10.0)
        context = CheckContext(
            component_positions={},
            component_values={"R1": "10k", "R2": "9.5k"},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is None

    def test_value_parsing(self) -> None:
        """Test parsing of values with SI prefixes."""
        check = ValueMatchCheck(component="C1", equals="C2")

        # Test parsing individual values (use pytest.approx for float comparison)
        assert check._parse_value("100nF") == pytest.approx(100e-9, rel=1e-9)
        assert check._parse_value("10uF") == pytest.approx(10e-6, rel=1e-9)
        assert check._parse_value("4.7k") == pytest.approx(4700.0, rel=1e-9)
        assert check._parse_value("1M") == pytest.approx(1e6, rel=1e-9)


class TestValueRangeCheck:
    """Tests for ValueRangeCheck."""

    def test_check_passes_when_in_range(self) -> None:
        """Test check passes when value is within range."""
        check = ValueRangeCheck(
            component="C1",
            min_value="100n",
            max_value="1u",
        )
        context = CheckContext(
            component_positions={},
            component_values={"C1": "470nF"},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is None

    def test_check_fails_when_below_min(self) -> None:
        """Test check fails when value is below minimum."""
        check = ValueRangeCheck(
            component="C1",
            min_value="100n",
            max_value="1u",
            rationale="Filter cap range",
        )
        context = CheckContext(
            component_positions={},
            component_values={"C1": "10nF"},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "below minimum" in violation.message

    def test_check_fails_when_above_max(self) -> None:
        """Test check fails when value is above maximum."""
        check = ValueRangeCheck(
            component="C1",
            min_value="100n",
            max_value="1u",
        )
        context = CheckContext(
            component_positions={},
            component_values={"C1": "10uF"},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "above maximum" in violation.message


class TestTraceLengthCheck:
    """Tests for TraceLengthCheck."""

    def test_check_passes_when_within_limits(self) -> None:
        """Test check passes when trace length is within limits."""
        check = TraceLengthCheck(net="CLK", max_mm=50.0, min_mm=5.0)
        context = CheckContext(
            component_positions={},
            component_values={},
            component_footprints={},
            net_lengths={"CLK": 25.0},
        )

        violation = check.validate(context)
        assert violation is None

    def test_check_fails_when_too_long(self) -> None:
        """Test check fails when trace is too long."""
        check = TraceLengthCheck(net="CLK", max_mm=50.0, rationale="Keep clock short")
        context = CheckContext(
            component_positions={},
            component_values={},
            component_footprints={},
            net_lengths={"CLK": 75.0},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "too long" in violation.message

    def test_check_fails_when_too_short(self) -> None:
        """Test check fails when trace is too short."""
        check = TraceLengthCheck(net="DELAY", min_mm=20.0)
        context = CheckContext(
            component_positions={},
            component_values={},
            component_footprints={},
            net_lengths={"DELAY": 10.0},
        )

        violation = check.validate(context)
        assert violation is not None
        assert "too short" in violation.message


class TestComponentPresentCheck:
    """Tests for ComponentPresentCheck."""

    def test_check_passes_when_present(self) -> None:
        """Test check passes when component exists."""
        check = ComponentPresentCheck(component="R1")
        context = CheckContext(
            component_positions={"R1": (0.0, 0.0)},
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is None

    def test_check_fails_when_required_missing(self) -> None:
        """Test check fails when required component is missing."""
        check = ComponentPresentCheck(
            component="R1",
            optional=False,
            rationale="Resistor is required",
        )
        context = CheckContext(
            component_positions={},
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert violation.severity == PlacementPriority.CRITICAL
        assert "Required component" in violation.message

    def test_check_warns_when_optional_missing(self) -> None:
        """Test check warns when optional component is missing."""
        check = ComponentPresentCheck(
            component="D1",
            optional=True,
            rationale="ESD protection recommended",
        )
        context = CheckContext(
            component_positions={},
            component_values={},
            component_footprints={},
            net_lengths={},
        )

        violation = check.validate(context)
        assert violation is not None
        assert violation.severity == PlacementPriority.LOW
        assert "Optional component" in violation.message


class TestCheckRegistry:
    """Tests for check registry functions."""

    def test_get_check(self) -> None:
        """Test getting a check by name."""
        check_class = get_check("component_distance")
        assert check_class == ComponentDistanceCheck

    def test_get_check_unknown(self) -> None:
        """Test getting an unknown check raises error."""
        with pytest.raises(KeyError, match="Unknown validation check"):
            get_check("nonexistent_check")

    def test_create_check(self) -> None:
        """Test creating a check instance from name and params."""
        check = create_check(
            "component_distance",
            {"from_component": "R1", "to_component": "C1", "max_mm": 5.0},
        )
        assert isinstance(check, ComponentDistanceCheck)
        assert check.from_component == "R1"
        assert check.max_mm == 5.0

    def test_register_custom_check(self) -> None:
        """Test registering a custom check."""

        class CustomCheck(ValidationCheck):
            name = "test_custom_check"

            def validate(self, context: CheckContext):
                return None

        try:
            register_check(CustomCheck)
            assert get_check("test_custom_check") == CustomCheck
        finally:
            # Clean up
            from kicad_tools.patterns.checks import VALIDATION_CHECKS

            del VALIDATION_CHECKS["test_custom_check"]


class TestPatternRegistry:
    """Tests for PatternRegistry."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        PatternRegistry.clear()

    def test_register_and_get_instance(self) -> None:
        """Test registering and retrieving a pattern instance."""
        from kicad_tools.patterns import LDOPattern

        pattern = LDOPattern()
        PatternRegistry.register_instance("test_ldo", pattern, category="power")

        retrieved = PatternRegistry.get("test_ldo")
        assert retrieved is pattern

    def test_register_and_get_class(self) -> None:
        """Test registering and retrieving a pattern class."""
        from kicad_tools.patterns import LDOPattern

        PatternRegistry.register("ldo_class", LDOPattern, category="power")

        # Get creates a new instance
        pattern = PatternRegistry.get("ldo_class", regulator="AMS1117")
        assert isinstance(pattern, LDOPattern)

    def test_list_patterns(self) -> None:
        """Test listing registered patterns."""
        from kicad_tools.patterns import BuckPattern, LDOPattern

        PatternRegistry.register("ldo", LDOPattern, category="power")
        PatternRegistry.register("buck", BuckPattern, category="power")

        names = PatternRegistry.list()
        assert "ldo" in names
        assert "buck" in names

    def test_list_patterns_by_category(self) -> None:
        """Test listing patterns filtered by category."""
        from kicad_tools.patterns import CrystalPattern, LDOPattern

        PatternRegistry.register("ldo", LDOPattern, category="power")
        PatternRegistry.register("crystal", CrystalPattern, category="timing")

        power_patterns = PatternRegistry.list(category="power")
        assert "ldo" in power_patterns
        assert "crystal" not in power_patterns

    def test_has_pattern(self) -> None:
        """Test checking if pattern exists."""
        from kicad_tools.patterns import LDOPattern

        PatternRegistry.register("test", LDOPattern)

        assert PatternRegistry.has("test") is True
        assert PatternRegistry.has("nonexistent") is False

    def test_unregister_pattern(self) -> None:
        """Test removing a pattern."""
        from kicad_tools.patterns import LDOPattern

        PatternRegistry.register("test", LDOPattern)
        assert PatternRegistry.has("test")

        PatternRegistry.unregister("test")
        assert not PatternRegistry.has("test")

    def test_register_duplicate_raises(self) -> None:
        """Test registering duplicate name raises error."""
        from kicad_tools.patterns import LDOPattern

        PatternRegistry.register("test", LDOPattern)

        with pytest.raises(ValueError, match="already registered"):
            PatternRegistry.register("test", LDOPattern)

    def test_register_pattern_decorator(self) -> None:
        """Test @register_pattern decorator."""
        from kicad_tools.patterns import PCBPattern

        @register_pattern("decorated_pattern", category="test")
        class TestPattern(PCBPattern):
            def _build_spec(self):
                from kicad_tools.patterns import PatternSpec

                return PatternSpec(name="test")

            def get_placements(self, anchor_at):
                return {}

            def validate(self, pcb_path):
                return []

        assert PatternRegistry.has("decorated_pattern")
        meta = PatternRegistry.get_metadata("decorated_pattern")
        assert meta["category"] == "test"


class TestPatternLoader:
    """Tests for PatternLoader."""

    def test_load_yaml_pattern(self) -> None:
        """Test loading a pattern from YAML."""
        yaml_content = """
name: test_pattern
description: A test pattern
category: test

components:
  - role: resistor
    reference_prefix: R
  - role: capacitor
    reference_prefix: C
    optional: true

placement_rules:
  - component: capacitor
    relative_to: resistor
    max_distance_mm: 5
    rationale: "Keep cap close to resistor"

validation:
  - check: component_distance
    params:
      from_component: resistor
      to_component: capacitor
      max_mm: 5
"""
        with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            loader = PatternLoader()
            pattern, metadata = loader.load(f.name)

            assert isinstance(pattern, YAMLPattern)
            assert metadata["name"] == "test_pattern"
            assert metadata["description"] == "A test pattern"
            assert metadata["category"] == "test"

            # Check pattern spec
            spec = pattern.spec
            assert "resistor" in spec.components
            assert "capacitor" in spec.components

            # Check placement rules
            assert len(spec.placement_rules) == 1
            assert spec.placement_rules[0].max_distance_mm == 5

            Path(f.name).unlink()

    def test_load_string(self) -> None:
        """Test loading pattern from YAML string."""
        yaml_string = """
name: string_pattern
components:
  - role: led
"""
        loader = PatternLoader()
        pattern, metadata = loader.load_string(yaml_string)

        assert metadata["name"] == "string_pattern"
        assert len(pattern.components) == 1
        assert pattern.components[0].role == "led"

    def test_yaml_pattern_get_placements(self) -> None:
        """Test YAML pattern placement calculation."""
        yaml_string = """
name: placement_test
components:
  - role: anchor
  - role: nearby

placement_rules:
  - component: nearby
    relative_to: anchor
    max_distance_mm: 10
    min_distance_mm: 2
    preferred_angle: 90
"""
        loader = PatternLoader()
        pattern, _ = loader.load_string(yaml_string)

        placements = pattern.get_placements(anchor_at=(50.0, 50.0))

        assert "nearby" in placements
        # Should be at angle 90 (down), distance 6mm (midpoint)
        pos = placements["nearby"].position
        assert pos[0] == pytest.approx(50.0, abs=0.1)
        assert pos[1] == pytest.approx(56.0, abs=0.1)


class TestPatternDSL:
    """Tests for pattern definition DSL."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        PatternRegistry.clear()

    def test_placement_rule_helper(self) -> None:
        """Test placement_rule helper function."""
        rule = placement_rule(
            "cap",
            relative_to="ic",
            max_distance_mm=5.0,
            rationale="Test rule",
            priority="critical",
        )

        assert isinstance(rule, PlacementRule)
        assert rule.component == "cap"
        assert rule.relative_to == "ic"
        assert rule.max_distance_mm == 5.0
        assert rule.priority == PlacementPriority.CRITICAL

    def test_routing_constraint_helper(self) -> None:
        """Test routing_constraint helper function."""
        from kicad_tools.patterns.schema import RoutingConstraint

        constraint = routing_constraint(
            "clock",
            min_width_mm=0.15,
            max_length_mm=50.0,
            via_allowed=False,
        )

        assert isinstance(constraint, RoutingConstraint)
        assert constraint.net_role == "clock"
        assert constraint.min_width_mm == 0.15
        assert constraint.max_length_mm == 50.0
        assert constraint.via_allowed is False

    def test_define_pattern_decorator(self) -> None:
        """Test @define_pattern decorator."""

        @define_pattern
        class TestSensorPattern:
            """A test sensor pattern."""

            components = ["sensor", "cap"]

            placement_rules = [
                placement_rule("cap", relative_to="sensor", max_distance_mm=5.0),
            ]

        # Check pattern was created
        pattern = get_pattern_from_class(TestSensorPattern)
        assert pattern is not None
        assert isinstance(pattern, DSLPattern)

        # Check pattern name was extracted
        name = get_pattern_name_from_class(TestSensorPattern)
        assert name == "test_sensor"

        # Check registered
        assert PatternRegistry.has("test_sensor")

    def test_define_pattern_with_custom_name(self) -> None:
        """Test @define_pattern with custom name."""

        @define_pattern(name="my_custom_name", register=True)
        class AnotherPattern:
            components = ["a", "b"]

        assert get_pattern_name_from_class(AnotherPattern) == "my_custom_name"
        assert PatternRegistry.has("my_custom_name")

    def test_define_pattern_no_register(self) -> None:
        """Test @define_pattern with register=False."""

        @define_pattern(register=False)
        class UnregisteredPattern:
            components = ["x"]

        assert get_pattern_from_class(UnregisteredPattern) is not None
        assert not PatternRegistry.has("unregistered")

    def test_dsl_pattern_get_placements(self) -> None:
        """Test DSL pattern placement calculation."""

        @define_pattern(register=False)
        class PlacementTestPattern:
            components = ["anchor", "follower"]

            placement_rules = [
                placement_rule(
                    "follower",
                    relative_to="anchor",
                    max_distance_mm=10.0,
                    min_distance_mm=4.0,
                    preferred_angle=180.0,  # Left
                ),
            ]

        pattern = get_pattern_from_class(PlacementTestPattern)
        placements = pattern.get_placements(anchor_at=(50.0, 50.0))

        assert "follower" in placements
        pos = placements["follower"].position
        # Should be at 180 degrees (left), distance 7mm (midpoint)
        assert pos[0] == pytest.approx(43.0, abs=0.1)
        assert pos[1] == pytest.approx(50.0, abs=0.1)

    def test_dsl_pattern_with_custom_validate(self) -> None:
        """Test DSL pattern with custom validate method."""

        @define_pattern(register=False)
        class CustomValidatePattern:
            components = ["test"]

            def validate(self, pcb_path):
                from kicad_tools.patterns.schema import PatternViolation

                return [
                    PatternViolation(
                        rule=None,
                        component="test",
                        message="Custom validation",
                    )
                ]

        pattern = get_pattern_from_class(CustomValidatePattern)
        violations = pattern.validate("dummy.kicad_pcb")

        assert len(violations) == 1
        assert violations[0].message == "Custom validation"


class TestExamplePatterns:
    """Tests for example YAML patterns."""

    @pytest.fixture
    def examples_dir(self) -> Path:
        """Get the examples/patterns directory."""
        return Path(__file__).parent.parent / "examples" / "patterns"

    def test_temperature_sensor_pattern_loads(self, examples_dir: Path) -> None:
        """Test temperature_sensor.yaml loads correctly."""
        yaml_file = examples_dir / "temperature_sensor.yaml"
        if not yaml_file.exists():
            pytest.skip("Example pattern not found")

        loader = PatternLoader()
        pattern, metadata = loader.load(yaml_file)

        assert metadata["name"] == "temperature_sensor"
        assert len(pattern.components) >= 3  # thermistor, bias_resistor, filter_cap

    def test_voltage_divider_pattern_loads(self, examples_dir: Path) -> None:
        """Test voltage_divider_feedback.yaml loads correctly."""
        yaml_file = examples_dir / "voltage_divider_feedback.yaml"
        if not yaml_file.exists():
            pytest.skip("Example pattern not found")

        loader = PatternLoader()
        pattern, metadata = loader.load(yaml_file)

        assert metadata["name"] == "voltage_divider_feedback"
        assert metadata["category"] == "power"

    def test_led_indicator_pattern_loads(self, examples_dir: Path) -> None:
        """Test led_indicator.yaml loads correctly."""
        yaml_file = examples_dir / "led_indicator.yaml"
        if not yaml_file.exists():
            pytest.skip("Example pattern not found")

        loader = PatternLoader()
        pattern, metadata = loader.load(yaml_file)

        assert metadata["name"] == "led_indicator"

    def test_rc_lowpass_filter_pattern_loads(self, examples_dir: Path) -> None:
        """Test rc_lowpass_filter.yaml loads correctly."""
        yaml_file = examples_dir / "rc_lowpass_filter.yaml"
        if not yaml_file.exists():
            pytest.skip("Example pattern not found")

        loader = PatternLoader()
        pattern, metadata = loader.load(yaml_file)

        assert metadata["name"] == "rc_lowpass_filter"
        assert metadata["category"] == "analog"
