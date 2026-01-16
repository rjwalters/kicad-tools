"""
Tests for pattern validation, adaptation, and schema types.

Includes tests for:
- Schema types (Placement, PlacementRule, PatternSpec)
- Validation (PatternValidator, ValidationViolation)
- Adaptation (PatternAdapter, AdaptedPatternParams)
- Constraint patterns (SPI, UART, Ethernet, Analog, Protection)
"""

import pytest

from kicad_tools.intent import InterfaceCategory
from kicad_tools.patterns import (
    # Validation and adaptation
    AdaptedPatternParams,
    ComponentRequirements,
    PatternAdapter,
    PatternSpec,
    PatternValidationResult,
    PatternValidator,
    PatternViolation,
    ValidationViolation,
    get_component_requirements,
    list_components,
    # Schema types
    Placement,
    PlacementPriority,
    PlacementRule,
    RoutingConstraint,
    # Placement patterns
    BuckPattern,
    CrystalPattern,
    I2CPattern,
    LDOPattern,
    OscillatorPattern,
    USBPattern,
    # Constraint pattern base classes
    ConstraintPlacementRule,
    ConstraintPriority,
    ConstraintRoutingRule,
    IntentPattern,
    # Constraint patterns
    SPIPattern,
    UARTPattern,
    EthernetPattern,
    ADCInputFilter,
    DACOutputFilter,
    OpAmpCircuit,
    SensorInterface,
    ESDProtection,
    OvercurrentProtection,
    OvervoltageProtection,
    ReversePolarityProtection,
    ThermalShutdown,
)


class TestValidationViolation:
    """Tests for PatternViolation dataclass."""

    def test_create_error_violation(self):
        """Test creating an error violation."""
        violation = ValidationViolation(
            severity="error",
            rule="input_cap_distance",
            message="C1 is too far from U1",
            component="C1",
            location=(10.0, 20.0),
            fix_suggestion="Move C1 closer to U1",
        )
        assert violation.severity == "error"
        assert violation.is_error
        assert not violation.is_warning
        assert violation.rule == "input_cap_distance"
        assert violation.component == "C1"
        assert violation.location == (10.0, 20.0)

    def test_create_warning_violation(self):
        """Test creating a warning violation."""
        violation = ValidationViolation(
            severity="warning",
            rule="output_cap_value",
            message="C2 is 1uF, recommended is 10uF",
            component="C2",
        )
        assert violation.severity == "warning"
        assert violation.is_warning
        assert not violation.is_error

    def test_create_info_violation(self):
        """Test creating an info violation."""
        violation = ValidationViolation(
            severity="info",
            rule="thermal_note",
            message="Consider adding thermal vias",
        )
        assert violation.severity == "info"
        assert violation.is_info
        assert not violation.is_error
        assert not violation.is_warning

    def test_invalid_severity_raises_error(self):
        """Test that invalid severity raises ValueError."""
        with pytest.raises(ValueError, match="severity must be"):
            ValidationViolation(
                severity="critical",  # Invalid
                rule="test",
                message="test",
            )

    def test_to_dict(self):
        """Test converting violation to dictionary."""
        violation = ValidationViolation(
            severity="error",
            rule="test_rule",
            message="Test message",
            component="C1",
            location=(1.0, 2.0),
            fix_suggestion="Fix it",
        )
        d = violation.to_dict()
        assert d["severity"] == "error"
        assert d["rule"] == "test_rule"
        assert d["message"] == "Test message"
        assert d["component"] == "C1"
        assert d["location"] == [1.0, 2.0]
        assert d["fix_suggestion"] == "Fix it"


class TestPatternValidationResult:
    """Tests for PatternValidationResult."""

    def test_empty_result_passed(self):
        """Test that empty result passes."""
        result = PatternValidationResult(pattern_type="LDO")
        assert result.passed
        assert result.error_count == 0
        assert result.warning_count == 0
        assert len(result) == 0

    def test_result_with_errors_fails(self):
        """Test that result with errors fails."""
        result = PatternValidationResult(pattern_type="LDO")
        result.add(ValidationViolation(severity="error", rule="test", message="Error"))
        assert not result.passed
        assert result.error_count == 1

    def test_result_with_only_warnings_passes(self):
        """Test that result with only warnings passes."""
        result = PatternValidationResult(pattern_type="LDO")
        result.add(ValidationViolation(severity="warning", rule="test", message="Warning"))
        assert result.passed
        assert result.warning_count == 1

    def test_errors_and_warnings_lists(self):
        """Test filtering by severity."""
        result = PatternValidationResult(pattern_type="Test")
        result.add(ValidationViolation(severity="error", rule="e1", message="E1"))
        result.add(ValidationViolation(severity="warning", rule="w1", message="W1"))
        result.add(ValidationViolation(severity="error", rule="e2", message="E2"))
        result.add(ValidationViolation(severity="info", rule="i1", message="I1"))

        assert len(result.errors) == 2
        assert len(result.warnings) == 1
        assert result.info_count == 1
        assert len(result) == 4

    def test_summary(self):
        """Test summary generation."""
        result = PatternValidationResult(pattern_type="LDO", rules_checked=5)
        result.add(ValidationViolation(severity="error", rule="e1", message="E1"))
        result.add(ValidationViolation(severity="warning", rule="w1", message="W1"))

        summary = result.summary()
        assert "FAILED" in summary
        assert "1 errors" in summary
        assert "1 warnings" in summary
        assert "5 rules checked" in summary

    def test_to_dict(self):
        """Test converting result to dictionary."""
        result = PatternValidationResult(pattern_type="LDO", rules_checked=3)
        result.add(ValidationViolation(severity="error", rule="e1", message="E1"))

        d = result.to_dict()
        assert d["passed"] is False
        assert d["pattern_type"] == "LDO"
        assert d["error_count"] == 1
        assert d["rules_checked"] == 3
        assert len(d["violations"]) == 1


class TestPatternValidator:
    """Tests for PatternValidator."""

    def test_parse_capacitance_uf(self):
        """Test parsing microfarad values."""
        assert PatternValidator._parse_capacitance("10uF") == 10.0
        assert PatternValidator._parse_capacitance("4.7uF") == 4.7
        assert PatternValidator._parse_capacitance("100UF") == 100.0

    def test_parse_capacitance_nf(self):
        """Test parsing nanofarad values."""
        assert PatternValidator._parse_capacitance("100nF") == 0.1
        assert PatternValidator._parse_capacitance("470NF") == 0.47

    def test_parse_capacitance_pf(self):
        """Test parsing picofarad values."""
        assert PatternValidator._parse_capacitance("100pF") == 0.0001
        assert PatternValidator._parse_capacitance("22PF") == 0.000022

    def test_parse_capacitance_invalid(self):
        """Test parsing invalid values."""
        assert PatternValidator._parse_capacitance("") is None
        assert PatternValidator._parse_capacitance("abc") is None

    def test_calculate_distance(self):
        """Test distance calculation."""
        assert PatternValidator._calculate_distance((0, 0), (3, 4)) == 5.0
        assert PatternValidator._calculate_distance((0, 0), (0, 0)) == 0.0

    def test_value_present_exact_match(self):
        """Test value matching with exact match."""
        assert PatternValidator._value_present("10uF", ["10uF", "100nF"])

    def test_value_present_equivalent_values(self):
        """Test value matching with equivalent values."""
        # 100nF == 0.1uF
        assert PatternValidator._value_present("100nF", ["0.1uF"])
        assert PatternValidator._value_present("0.1uF", ["100nF"])


class TestComponentRequirements:
    """Tests for ComponentRequirements."""

    def test_create_ldo_requirements(self):
        """Test creating LDO requirements."""
        reqs = ComponentRequirements(
            mpn="TEST-LDO-3.3",
            component_type="LDO",
            manufacturer="Test Corp",
            input_cap_min_uf=10.0,
            output_cap_min_uf=22.0,
            dropout_voltage=1.0,
        )
        assert reqs.mpn == "TEST-LDO-3.3"
        assert reqs.input_cap_min_uf == 10.0
        assert reqs.output_cap_min_uf == 22.0
        assert reqs.dropout_voltage == 1.0

    def test_to_dict(self):
        """Test converting requirements to dictionary."""
        reqs = ComponentRequirements(
            mpn="TEST",
            component_type="LDO",
            input_cap_min_uf=10.0,
            dropout_voltage=0.5,
        )
        d = reqs.to_dict()
        assert d["mpn"] == "TEST"
        assert "input_cap" in d
        assert d["dropout_voltage"] == 0.5


class TestComponentDatabase:
    """Tests for component database functions."""

    def test_get_ams1117_requirements(self):
        """Test getting AMS1117-3.3 requirements."""
        reqs = get_component_requirements("AMS1117-3.3")
        assert reqs.mpn == "AMS1117-3.3"
        assert reqs.component_type == "LDO"
        assert reqs.input_cap_min_uf == 10.0
        assert reqs.output_cap_min_uf == 10.0
        assert reqs.dropout_voltage == 1.0

    def test_get_lm2596_requirements(self):
        """Test getting LM2596-5.0 requirements."""
        reqs = get_component_requirements("LM2596-5.0")
        assert reqs.mpn == "LM2596-5.0"
        assert reqs.component_type == "BuckConverter"
        assert reqs.inductor_min_uh == 33.0

    def test_get_stm32_requirements(self):
        """Test getting STM32F405RGT6 requirements."""
        reqs = get_component_requirements("STM32F405RGT6")
        assert reqs.mpn == "STM32F405RGT6"
        assert reqs.component_type == "IC"
        assert reqs.num_vdd_pins == 4
        assert "100nF" in reqs.decoupling_caps

    def test_unknown_component_raises_keyerror(self):
        """Test that unknown component raises KeyError."""
        with pytest.raises(KeyError):
            get_component_requirements("UNKNOWN-PART-123")

    def test_case_insensitive_lookup(self):
        """Test case-insensitive component lookup."""
        reqs1 = get_component_requirements("AMS1117-3.3")
        reqs2 = get_component_requirements("ams1117-3.3")
        assert reqs1.mpn == reqs2.mpn

    def test_list_all_components(self):
        """Test listing all components."""
        components = list_components()
        assert len(components) >= 10  # At least 10 built-in components
        assert "AMS1117-3.3" in components
        assert "LM2596-5.0" in components

    def test_list_components_by_type(self):
        """Test listing components filtered by type."""
        ldos = list_components("LDO")
        assert "AMS1117-3.3" in ldos
        assert "LM2596-5.0" not in ldos  # Buck converter

        bucks = list_components("BuckConverter")
        assert "LM2596-5.0" in bucks
        assert "AMS1117-3.3" not in bucks


class TestPatternAdapter:
    """Tests for PatternAdapter."""

    def test_adapt_ldo_pattern(self):
        """Test adapting LDO pattern for AMS1117."""
        adapter = PatternAdapter()
        params = adapter.adapt_ldo_pattern("AMS1117-3.3")

        assert params.pattern_type == "LDO"
        assert params.component_mpn == "AMS1117-3.3"
        assert params.parameters["input_cap"] == "10uF"
        assert "10uF" in params.parameters["output_caps"]

    def test_adapt_buck_pattern(self):
        """Test adapting buck pattern for LM2596."""
        adapter = PatternAdapter()
        params = adapter.adapt_buck_pattern("LM2596-5.0")

        assert params.pattern_type == "BuckConverter"
        assert params.parameters["inductor"] == "33uH"
        assert params.parameters["diode"] == "SS34"

    def test_adapt_decoupling_pattern(self):
        """Test adapting decoupling pattern for STM32."""
        adapter = PatternAdapter()
        params = adapter.adapt_decoupling_pattern("STM32F405RGT6")

        assert params.pattern_type == "Decoupling"
        assert "100nF" in params.parameters["capacitors"]

    def test_adapt_with_overrides(self):
        """Test adapting with parameter overrides."""
        adapter = PatternAdapter()
        params = adapter.adapt_ldo_pattern(
            "AMS1117-3.3",
            input_cap="22uF",
        )
        assert params.parameters["input_cap"] == "22uF"

    def test_adapt_unknown_component_uses_defaults(self):
        """Test that unknown components use default values."""
        adapter = PatternAdapter()
        params = adapter.adapt_ldo_pattern("UNKNOWN-LDO-123")

        # Should still work with defaults
        assert params.pattern_type == "LDO"
        assert "input_cap" in params.parameters
        assert any("not in database" in note for note in params.notes)

    def test_generic_adapt_method(self):
        """Test the generic adapt() method."""
        adapter = PatternAdapter()

        ldo_params = adapter.adapt("LDO", "AMS1117-3.3")
        assert ldo_params.pattern_type == "LDO"

        buck_params = adapter.adapt("BuckConverter", "LM2596-5.0")
        assert buck_params.pattern_type == "BuckConverter"

    def test_adapt_invalid_pattern_type(self):
        """Test that invalid pattern type raises ValueError."""
        adapter = PatternAdapter()
        with pytest.raises(ValueError, match="Unknown pattern type"):
            adapter.adapt("InvalidType", "AMS1117-3.3")

    def test_format_capacitance(self):
        """Test capacitance formatting."""
        assert PatternAdapter._format_capacitance(10.0) == "10uF"
        assert PatternAdapter._format_capacitance(0.1) == "100nF"
        assert PatternAdapter._format_capacitance(0.000022) == "22pF"
        assert PatternAdapter._format_capacitance(4.7) == "4.7uF"


class TestAdaptedPatternParams:
    """Tests for AdaptedPatternParams."""

    def test_to_dict(self):
        """Test converting params to dictionary."""
        params = AdaptedPatternParams(
            pattern_type="LDO",
            component_mpn="TEST-3.3",
            parameters={"input_cap": "10uF", "output_caps": ["22uF"]},
            notes=["Test note"],
        )
        d = params.to_dict()
        assert d["pattern_type"] == "LDO"
        assert d["component_mpn"] == "TEST-3.3"
        assert d["parameters"]["input_cap"] == "10uF"
        assert d["notes"] == ["Test note"]


# ============================================================================
# Schema Types Tests
# ============================================================================


class TestPlacement:
    """Tests for Placement dataclass."""

    def test_placement_creation(self) -> None:
        """Test creating a Placement."""
        placement = Placement(
            position=(50.0, 30.0),
            rotation=90.0,
            rationale="Test placement",
            layer="F.Cu",
        )
        assert placement.position == (50.0, 30.0)
        assert placement.rotation == 90.0
        assert placement.rationale == "Test placement"
        assert placement.layer == "F.Cu"

    def test_placement_defaults(self) -> None:
        """Test Placement default values."""
        placement = Placement(position=(0.0, 0.0))
        assert placement.rotation == 0.0
        assert placement.rationale == ""
        assert placement.layer == "F.Cu"


class TestPlacementRule:
    """Tests for PlacementRule dataclass."""

    def test_rule_creation(self) -> None:
        """Test creating a PlacementRule."""
        rule = PlacementRule(
            component="input_cap",
            relative_to="regulator",
            max_distance_mm=3.0,
            preferred_angle=180.0,
            rationale="Input cap within 3mm of VIN",
        )
        assert rule.component == "input_cap"
        assert rule.relative_to == "regulator"
        assert rule.max_distance_mm == 3.0
        assert rule.preferred_angle == 180.0
        assert rule.rationale == "Input cap within 3mm of VIN"

    def test_rule_defaults(self) -> None:
        """Test PlacementRule default values."""
        rule = PlacementRule(
            component="cap",
            relative_to="ic",
            max_distance_mm=5.0,
        )
        assert rule.min_distance_mm == 0.0
        assert rule.preferred_angle is None
        assert rule.angle_tolerance == 45.0
        assert rule.priority == PlacementPriority.HIGH
        assert rule.same_layer is True


class TestRoutingConstraint:
    """Tests for RoutingConstraint dataclass."""

    def test_constraint_creation(self) -> None:
        """Test creating a RoutingConstraint."""
        constraint = RoutingConstraint(
            net_role="usb_dp",
            min_width_mm=0.15,
            max_length_mm=100.0,
            rationale="USB D+ differential",
        )
        assert constraint.net_role == "usb_dp"
        assert constraint.min_width_mm == 0.15
        assert constraint.max_length_mm == 100.0

    def test_constraint_defaults(self) -> None:
        """Test RoutingConstraint default values."""
        constraint = RoutingConstraint(net_role="test")
        assert constraint.min_width_mm == 0.2
        assert constraint.max_length_mm is None
        assert constraint.via_allowed is True
        assert constraint.plane_connection is False


class TestPatternSpec:
    """Tests for PatternSpec dataclass."""

    def test_spec_creation(self) -> None:
        """Test creating a PatternSpec."""
        spec = PatternSpec(
            name="test_pattern",
            description="A test pattern",
            components=["ic", "cap"],
        )
        assert spec.name == "test_pattern"
        assert spec.description == "A test pattern"
        assert spec.components == ["ic", "cap"]

    def test_get_rules_for_component(self) -> None:
        """Test getting rules for a specific component."""
        rules = [
            PlacementRule(component="cap1", relative_to="ic", max_distance_mm=3.0),
            PlacementRule(component="cap2", relative_to="ic", max_distance_mm=5.0),
            PlacementRule(component="cap1", relative_to="cap2", max_distance_mm=2.0),
        ]
        spec = PatternSpec(name="test", placement_rules=rules)

        cap1_rules = spec.get_rules_for_component("cap1")
        assert len(cap1_rules) == 2
        assert all(r.component == "cap1" for r in cap1_rules)

        cap2_rules = spec.get_rules_for_component("cap2")
        assert len(cap2_rules) == 1

    def test_get_routing_for_net(self) -> None:
        """Test getting routing constraint for a specific net."""
        constraints = [
            RoutingConstraint(net_role="power", min_width_mm=0.5),
            RoutingConstraint(net_role="signal", min_width_mm=0.15),
        ]
        spec = PatternSpec(name="test", routing_constraints=constraints)

        power_constraint = spec.get_routing_for_net("power")
        assert power_constraint is not None
        assert power_constraint.min_width_mm == 0.5

        missing = spec.get_routing_for_net("nonexistent")
        assert missing is None


class TestPatternViolation:
    """Tests for PatternViolation dataclass."""

    def test_violation_creation(self) -> None:
        """Test creating a PatternViolation."""
        rule = PlacementRule(component="cap", relative_to="ic", max_distance_mm=3.0)
        violation = PatternViolation(
            rule=rule,
            component="cap",
            message="Cap too far from IC",
            severity=PlacementPriority.CRITICAL,
            actual_value=5.0,
            expected_value=3.0,
        )
        assert violation.rule == rule
        assert violation.component == "cap"
        assert violation.actual_value == 5.0
        assert violation.expected_value == 3.0


class TestLDOPattern:
    """Tests for LDOPattern."""

    def test_ldo_creation(self) -> None:
        """Test creating an LDOPattern."""
        pattern = LDOPattern(
            regulator="AMS1117-3.3",
            input_cap="10uF",
            output_caps=["10uF", "100nF"],
        )
        assert pattern.regulator == "AMS1117-3.3"
        assert pattern.input_cap == "10uF"
        assert pattern.output_caps == ["10uF", "100nF"]

    def test_ldo_defaults(self) -> None:
        """Test LDOPattern default values."""
        pattern = LDOPattern()
        assert pattern.regulator == "LDO"
        assert pattern.input_cap == "10uF"
        assert pattern.output_caps == ["10uF", "100nF"]

    def test_ldo_spec(self) -> None:
        """Test LDOPattern generates correct spec."""
        pattern = LDOPattern(output_caps=["22uF", "100nF", "10nF"])
        spec = pattern.spec

        assert spec.name == "ldo_regulator"
        assert "regulator" in spec.components
        assert "input_cap" in spec.components
        assert "output_cap_1" in spec.components
        assert "output_cap_2" in spec.components
        assert "output_cap_3" in spec.components

        # Check placement rules
        input_cap_rules = spec.get_rules_for_component("input_cap")
        assert len(input_cap_rules) == 1
        assert input_cap_rules[0].max_distance_mm == 3.0

    def test_ldo_get_placements(self) -> None:
        """Test LDOPattern.get_placements()."""
        pattern = LDOPattern(
            regulator="AMS1117-3.3",
            input_cap="10uF",
            output_caps=["10uF", "100nF"],
        )

        placements = pattern.get_placements(anchor_at=(50.0, 30.0))

        assert "input_cap" in placements
        assert "output_cap_1" in placements
        assert "output_cap_2" in placements

        # Input cap should be to the left (180 degrees)
        input_cap = placements["input_cap"]
        assert input_cap.position[0] < 50.0  # Left of anchor

        # Output caps should be to the right (0 degrees)
        output_cap_1 = placements["output_cap_1"]
        assert output_cap_1.position[0] > 50.0  # Right of anchor

        # Each placement should have a rationale
        assert "10uF" in input_cap.rationale
        assert "10uF" in output_cap_1.rationale

    def test_ldo_validate_no_component_map(self) -> None:
        """Test LDOPattern.validate() without component map."""
        pattern = LDOPattern()
        violations = pattern.validate("dummy.kicad_pcb")

        assert len(violations) == 1
        assert "component mapping" in violations[0].message.lower()


class TestBuckPattern:
    """Tests for BuckPattern."""

    def test_buck_creation(self) -> None:
        """Test creating a BuckPattern."""
        pattern = BuckPattern(
            controller="MP2359",
            input_cap="10uF",
            output_cap="22uF",
            inductor="4.7uH",
        )
        assert pattern.controller == "MP2359"
        assert pattern.inductor == "4.7uH"

    def test_buck_spec(self) -> None:
        """Test BuckPattern generates correct spec."""
        pattern = BuckPattern()
        spec = pattern.spec

        assert spec.name == "buck_converter"
        assert "controller" in spec.components
        assert "input_cap" in spec.components
        assert "inductor" in spec.components
        assert "output_cap" in spec.components
        assert "bootstrap_cap" in spec.components

        # Check critical hot loop rule
        input_cap_rules = spec.get_rules_for_component("input_cap")
        assert len(input_cap_rules) == 1
        assert input_cap_rules[0].max_distance_mm == 2.0
        assert input_cap_rules[0].priority == PlacementPriority.CRITICAL

        # Check switch node routing constraint
        switch_constraint = spec.get_routing_for_net("switch_node")
        assert switch_constraint is not None
        assert switch_constraint.via_allowed is False

    def test_buck_get_placements(self) -> None:
        """Test BuckPattern.get_placements()."""
        pattern = BuckPattern()
        placements = pattern.get_placements(anchor_at=(40.0, 40.0))

        assert "input_cap" in placements
        assert "inductor" in placements
        assert "output_cap" in placements
        assert "bootstrap_cap" in placements
        assert "feedback_divider" in placements

        # Verify relative positions
        input_cap = placements["input_cap"]
        inductor = placements["inductor"]
        output_cap = placements["output_cap"]

        # Input cap left, inductor right, output cap after inductor
        assert input_cap.position[0] < 40.0
        assert inductor.position[0] > 40.0
        assert output_cap.position[0] > inductor.position[0]


class TestCrystalPattern:
    """Tests for CrystalPattern."""

    def test_crystal_creation(self) -> None:
        """Test creating a CrystalPattern."""
        pattern = CrystalPattern(crystal="8MHz", load_caps=["18pF", "18pF"])
        assert pattern.crystal == "8MHz"
        assert pattern.load_caps == ["18pF", "18pF"]

    def test_crystal_spec(self) -> None:
        """Test CrystalPattern generates correct spec."""
        pattern = CrystalPattern()
        spec = pattern.spec

        assert spec.name == "crystal_oscillator"
        assert "crystal" in spec.components
        assert "load_cap_1" in spec.components
        assert "load_cap_2" in spec.components

        # Check no vias allowed on oscillator traces
        osc_in = spec.get_routing_for_net("osc_in")
        assert osc_in is not None
        assert osc_in.via_allowed is False

    def test_crystal_get_placements(self) -> None:
        """Test CrystalPattern.get_placements()."""
        pattern = CrystalPattern(crystal="16MHz")
        placements = pattern.get_placements(anchor_at=(30.0, 20.0))

        assert "crystal" in placements
        assert "load_cap_1" in placements
        assert "load_cap_2" in placements

        crystal = placements["crystal"]
        load_cap_1 = placements["load_cap_1"]
        load_cap_2 = placements["load_cap_2"]

        # Load caps should be below crystal
        assert load_cap_1.position[1] > crystal.position[1]
        assert load_cap_2.position[1] > crystal.position[1]


class TestOscillatorPattern:
    """Tests for OscillatorPattern."""

    def test_oscillator_creation(self) -> None:
        """Test creating an OscillatorPattern."""
        pattern = OscillatorPattern(
            oscillator="SIT8008",
            frequency="25MHz",
            decoupling_cap="100nF",
        )
        assert pattern.oscillator == "SIT8008"
        assert pattern.frequency == "25MHz"
        assert pattern.decoupling_cap == "100nF"

    def test_oscillator_get_placements(self) -> None:
        """Test OscillatorPattern.get_placements()."""
        pattern = OscillatorPattern()
        placements = pattern.get_placements(anchor_at=(50.0, 50.0))

        assert "oscillator" in placements
        assert "decoupling_cap" in placements


class TestUSBPattern:
    """Tests for USBPattern."""

    def test_usb_creation(self) -> None:
        """Test creating a USBPattern."""
        pattern = USBPattern(
            connector="USB-C",
            esd_protection=True,
            termination_resistors=True,
        )
        assert pattern.connector == "USB-C"
        assert pattern.esd_protection is True
        assert pattern.termination_resistors is True

    def test_usb_spec_with_esd(self) -> None:
        """Test USBPattern spec includes ESD protection."""
        pattern = USBPattern(esd_protection=True)
        spec = pattern.spec

        assert "esd_protection" in spec.components
        esd_rules = spec.get_rules_for_component("esd_protection")
        assert len(esd_rules) == 1
        assert esd_rules[0].max_distance_mm == 5.0

    def test_usb_spec_without_esd(self) -> None:
        """Test USBPattern spec without ESD protection."""
        pattern = USBPattern(esd_protection=False, termination_resistors=False)
        spec = pattern.spec

        assert "esd_protection" not in spec.components
        assert "term_r_dp" not in spec.components

    def test_usb_get_placements(self) -> None:
        """Test USBPattern.get_placements()."""
        pattern = USBPattern(esd_protection=True, termination_resistors=True)
        placements = pattern.get_placements(anchor_at=(5.0, 30.0))

        assert "vbus_cap" in placements
        assert "esd_protection" in placements
        assert "term_r_dp" in placements
        assert "term_r_dm" in placements


class TestI2CPattern:
    """Tests for I2CPattern."""

    def test_i2c_creation(self) -> None:
        """Test creating an I2CPattern."""
        pattern = I2CPattern(
            bus_speed="fast",
            pull_up_value="4.7k",
            device_count=3,
        )
        assert pattern.bus_speed == "fast"
        assert pattern.pull_up_value == "4.7k"
        assert pattern.device_count == 3

    def test_i2c_spec_speed_affects_length(self) -> None:
        """Test that I2C speed mode affects max trace length."""
        standard = I2CPattern(bus_speed="standard")
        fast = I2CPattern(bus_speed="fast")
        fast_plus = I2CPattern(bus_speed="fast-plus")

        standard_sda = standard.spec.get_routing_for_net("i2c_sda")
        fast_sda = fast.spec.get_routing_for_net("i2c_sda")
        fast_plus_sda = fast_plus.spec.get_routing_for_net("i2c_sda")

        # Faster modes have shorter max length
        assert standard_sda.max_length_mm > fast_sda.max_length_mm
        assert fast_sda.max_length_mm > fast_plus_sda.max_length_mm

    def test_i2c_get_placements(self) -> None:
        """Test I2CPattern.get_placements()."""
        pattern = I2CPattern()
        placements = pattern.get_placements(anchor_at=(20.0, 20.0))

        assert "pullup_sda" in placements
        assert "pullup_scl" in placements


class TestPCBPatternHelpers:
    """Tests for PCBPattern helper methods."""

    def test_calculate_position(self) -> None:
        """Test position calculation at various angles."""
        pattern = LDOPattern()

        # Right (0 degrees)
        pos = pattern._calculate_position((0.0, 0.0), 10.0, 0.0)
        assert abs(pos[0] - 10.0) < 0.001
        assert abs(pos[1] - 0.0) < 0.001

        # Down (90 degrees)
        pos = pattern._calculate_position((0.0, 0.0), 10.0, 90.0)
        assert abs(pos[0] - 0.0) < 0.001
        assert abs(pos[1] - 10.0) < 0.001

        # Left (180 degrees)
        pos = pattern._calculate_position((0.0, 0.0), 10.0, 180.0)
        assert abs(pos[0] - (-10.0)) < 0.001
        assert abs(pos[1] - 0.0) < 0.001

        # Up (270 degrees)
        pos = pattern._calculate_position((0.0, 0.0), 10.0, 270.0)
        assert abs(pos[0] - 0.0) < 0.001
        assert abs(pos[1] - (-10.0)) < 0.001

    def test_measure_distance(self) -> None:
        """Test distance measurement."""
        pattern = LDOPattern()

        # Horizontal distance
        dist = pattern._measure_distance((0.0, 0.0), (3.0, 0.0))
        assert abs(dist - 3.0) < 0.001

        # Vertical distance
        dist = pattern._measure_distance((0.0, 0.0), (0.0, 4.0))
        assert abs(dist - 4.0) < 0.001

        # Diagonal (3-4-5 triangle)
        dist = pattern._measure_distance((0.0, 0.0), (3.0, 4.0))
        assert abs(dist - 5.0) < 0.001

    def test_validate_placement_rule_passes(self) -> None:
        """Test validation when rule is satisfied."""
        pattern = LDOPattern()
        rule = PlacementRule(
            component="cap",
            relative_to="ic",
            max_distance_mm=5.0,
        )

        violation = pattern._validate_placement_rule(
            rule,
            component_pos=(3.0, 0.0),
            anchor_pos=(0.0, 0.0),
        )
        assert violation is None

    def test_validate_placement_rule_fails_max_distance(self) -> None:
        """Test validation when max distance is exceeded."""
        pattern = LDOPattern()
        rule = PlacementRule(
            component="cap",
            relative_to="ic",
            max_distance_mm=5.0,
            rationale="Test rule",
        )

        violation = pattern._validate_placement_rule(
            rule,
            component_pos=(10.0, 0.0),
            anchor_pos=(0.0, 0.0),
        )
        assert violation is not None
        assert violation.component == "cap"
        assert violation.actual_value == 10.0
        assert violation.expected_value == 5.0
        assert "too far" in violation.message

    def test_validate_placement_rule_fails_min_distance(self) -> None:
        """Test validation when min distance is not met."""
        pattern = LDOPattern()
        rule = PlacementRule(
            component="fb_divider",
            relative_to="ic",
            max_distance_mm=10.0,
            min_distance_mm=3.0,
        )

        violation = pattern._validate_placement_rule(
            rule,
            component_pos=(1.0, 0.0),
            anchor_pos=(0.0, 0.0),
        )
        assert violation is not None
        assert "too close" in violation.message

# =============================================================================
# Constraint Pattern Base Class Tests
# =============================================================================


class TestConstraintPatternBase:
    """Tests for the IntentPattern base class and related types."""

    def test_constraint_priority_values(self):
        """Test that ConstraintPriority enum has expected values."""
        assert ConstraintPriority.CRITICAL.value == "critical"
        assert ConstraintPriority.RECOMMENDED.value == "recommended"
        assert ConstraintPriority.OPTIONAL.value == "optional"

    def test_constraint_placement_rule_creation(self):
        """Test ConstraintPlacementRule dataclass creation."""
        rule = ConstraintPlacementRule(
            name="test_rule",
            description="A test placement rule",
            priority=ConstraintPriority.CRITICAL,
            component_refs=["U1", "C1"],
            params={"max_distance_mm": 5.0},
        )
        assert rule.name == "test_rule"
        assert rule.priority == ConstraintPriority.CRITICAL
        assert rule.component_refs == ["U1", "C1"]
        assert rule.params["max_distance_mm"] == 5.0

    def test_constraint_routing_rule_creation(self):
        """Test ConstraintRoutingRule dataclass creation."""
        rule = ConstraintRoutingRule(
            name="test_routing",
            description="A test routing rule",
            net_pattern="SPI_*",
            params={"max_mm": 100.0},
        )
        assert rule.name == "test_routing"
        assert rule.net_pattern == "SPI_*"
        assert rule.params["max_mm"] == 100.0


# =============================================================================
# SPI Pattern Tests
# =============================================================================


class TestSPIPattern:
    """Tests for SPIPattern."""

    def test_create_standard_spi(self):
        """Test creating a standard speed SPI pattern."""
        spi = SPIPattern(speed="standard", cs_count=1)
        assert spi.name == "spi_standard"
        assert spi.category == InterfaceCategory.BUS

    def test_create_high_speed_spi(self):
        """Test creating a high-speed SPI pattern."""
        spi = SPIPattern(speed="high", cs_count=2)
        assert spi.name == "spi_high"
        assert spi.category == InterfaceCategory.BUS

    def test_spi_invalid_speed(self):
        """Test that invalid speed raises ValueError."""
        with pytest.raises(ValueError, match="Invalid speed"):
            SPIPattern(speed="ultra")

    def test_spi_invalid_cs_count(self):
        """Test that invalid CS count raises ValueError."""
        with pytest.raises(ValueError, match="cs_count must be 1-8"):
            SPIPattern(speed="standard", cs_count=10)

    def test_spi_placement_rules(self):
        """Test SPI placement rules generation."""
        spi = SPIPattern(speed="high", cs_count=1)
        rules = spi.get_placement_rules()
        assert len(rules) >= 2
        assert any(r.name == "clock_near_master" for r in rules)
        assert any(r.name == "decoupling_near_slave" for r in rules)

    def test_spi_routing_rules(self):
        """Test SPI routing rules generation."""
        spi = SPIPattern(speed="high", cs_count=1)
        rules = spi.get_routing_rules()
        assert len(rules) >= 2
        assert any(r.name == "max_trace_length" for r in rules)

    def test_spi_validate(self):
        """Test SPI pattern validation."""
        spi = SPIPattern(speed="standard", cs_count=2)
        errors = spi.validate(nets=["CLK", "MOSI", "MISO", "CS0", "CS1"])
        assert len(errors) == 0
        errors = spi.validate(nets=["CLK", "MOSI"])
        assert len(errors) > 0

    def test_spi_derive_constraints(self):
        """Test SPI constraint derivation."""
        spi = SPIPattern(speed="high", cs_count=1)
        nets = ["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"]
        constraints = spi.derive_constraints(nets)
        assert len(constraints) >= 2
        assert any(c.type == "max_length" for c in constraints)


# =============================================================================
# UART Pattern Tests
# =============================================================================


class TestUARTPattern:
    """Tests for UARTPattern."""

    def test_create_standard_uart(self):
        """Test creating a standard UART pattern."""
        uart = UARTPattern(baud_rate=115200)
        assert uart.name == "uart_115200"
        assert uart.category == InterfaceCategory.SINGLE_ENDED

    def test_uart_placement_rules(self):
        """Test UART placement rules generation."""
        uart = UARTPattern(baud_rate=921600)
        rules = uart.get_placement_rules()
        assert len(rules) >= 1

    def test_uart_routing_rules(self):
        """Test UART routing rules generation."""
        uart = UARTPattern(baud_rate=115200)
        rules = uart.get_routing_rules()
        assert len(rules) >= 2

    def test_uart_validate(self):
        """Test UART pattern validation."""
        uart = UARTPattern(baud_rate=115200)
        errors = uart.validate(nets=["TX", "RX"])
        assert len(errors) == 0

    def test_uart_derive_constraints(self):
        """Test UART constraint derivation."""
        uart = UARTPattern(baud_rate=115200)
        constraints = uart.derive_constraints(["UART_TX", "UART_RX"])
        assert len(constraints) >= 1


# =============================================================================
# Ethernet Pattern Tests
# =============================================================================


class TestEthernetPattern:
    """Tests for EthernetPattern."""

    def test_create_100base_tx(self):
        """Test creating 100BASE-TX Ethernet pattern."""
        eth = EthernetPattern(speed="100base_tx")
        assert eth.name == "ethernet_100base_tx"
        assert eth.category == InterfaceCategory.DIFFERENTIAL

    def test_ethernet_invalid_speed(self):
        """Test that invalid speed raises ValueError."""
        with pytest.raises(ValueError, match="Invalid speed"):
            EthernetPattern(speed="10gbase_t")

    def test_ethernet_placement_rules(self):
        """Test Ethernet placement rules generation."""
        eth = EthernetPattern(speed="100base_tx")
        rules = eth.get_placement_rules()
        assert len(rules) >= 4

    def test_ethernet_routing_rules(self):
        """Test Ethernet routing rules generation."""
        eth = EthernetPattern(speed="1000base_t")
        rules = eth.get_routing_rules()
        assert len(rules) >= 4

    def test_ethernet_validate(self):
        """Test Ethernet pattern validation."""
        eth = EthernetPattern(speed="100base_tx")
        errors = eth.validate(nets=["TXP", "TXN", "RXP", "RXN"])
        assert len(errors) == 0

    def test_ethernet_derive_constraints(self):
        """Test Ethernet constraint derivation."""
        eth = EthernetPattern(speed="100base_tx")
        nets = ["ETH_TXP", "ETH_TXN", "ETH_RXP", "ETH_RXN"]
        constraints = eth.derive_constraints(nets)
        assert len(constraints) >= 3


# =============================================================================
# ADC Input Filter Pattern Tests
# =============================================================================


class TestADCInputFilter:
    """Tests for ADCInputFilter pattern."""

    def test_create_rc_filter(self):
        """Test creating an RC filter pattern."""
        adc = ADCInputFilter(cutoff_hz=10000, order=1, topology="rc")
        assert "adc_filter_rc" in adc.name

    def test_adc_invalid_cutoff(self):
        """Test that invalid cutoff frequency raises ValueError."""
        with pytest.raises(ValueError, match="cutoff_hz must be positive"):
            ADCInputFilter(cutoff_hz=-1000)

    def test_adc_placement_rules(self):
        """Test ADC filter placement rules."""
        adc = ADCInputFilter(cutoff_hz=10000, topology="active")
        rules = adc.get_placement_rules()
        assert len(rules) >= 2

    def test_adc_derive_constraints(self):
        """Test ADC filter constraint derivation."""
        adc = ADCInputFilter(cutoff_hz=10000)
        constraints = adc.derive_constraints(["ADC_IN"])
        assert len(constraints) >= 2


# =============================================================================
# Op-Amp Circuit Pattern Tests
# =============================================================================


class TestOpAmpCircuit:
    """Tests for OpAmpCircuit pattern."""

    def test_create_buffer(self):
        """Test creating a buffer op-amp pattern."""
        opamp = OpAmpCircuit(topology="buffer")
        assert opamp.name == "opamp_buffer"

    def test_opamp_invalid_topology(self):
        """Test that invalid topology raises ValueError."""
        with pytest.raises(ValueError, match="Invalid topology"):
            OpAmpCircuit(topology="integrator")

    def test_opamp_placement_rules(self):
        """Test op-amp placement rules."""
        opamp = OpAmpCircuit(topology="non_inverting", gain=100.0)
        rules = opamp.get_placement_rules()
        assert len(rules) >= 2


# =============================================================================
# Sensor Interface Pattern Tests
# =============================================================================


class TestSensorInterface:
    """Tests for SensorInterface pattern."""

    def test_create_thermistor(self):
        """Test creating a thermistor interface pattern."""
        sensor = SensorInterface(sensor_type="thermistor")
        assert sensor.name == "sensor_thermistor"

    def test_sensor_invalid_type(self):
        """Test that invalid sensor type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid sensor_type"):
            SensorInterface(sensor_type="accelerometer")


# =============================================================================
# DAC Output Filter Pattern Tests
# =============================================================================


class TestDACOutputFilter:
    """Tests for DACOutputFilter pattern."""

    def test_create_dac_filter(self):
        """Test creating a DAC output filter pattern."""
        dac = DACOutputFilter(cutoff_hz=20000, order=2, topology="rc")
        assert "dac_filter" in dac.name

    def test_dac_invalid_cutoff(self):
        """Test that invalid cutoff raises ValueError."""
        with pytest.raises(ValueError, match="cutoff_hz must be positive"):
            DACOutputFilter(cutoff_hz=0)


# =============================================================================
# ESD Protection Pattern Tests
# =============================================================================


class TestESDProtection:
    """Tests for ESDProtection pattern."""

    def test_create_basic_esd(self):
        """Test creating a basic ESD protection pattern."""
        esd = ESDProtection(lines=["USB_DP", "USB_DM"], protection_level="basic")
        assert "esd_basic" in esd.name

    def test_esd_empty_lines(self):
        """Test that empty lines raises ValueError."""
        with pytest.raises(ValueError, match="lines cannot be empty"):
            ESDProtection(lines=[])

    def test_esd_placement_rules(self):
        """Test ESD protection placement rules."""
        esd = ESDProtection(lines=["D+", "D-"], protection_level="enhanced")
        rules = esd.get_placement_rules()
        assert len(rules) >= 3


# =============================================================================
# Overcurrent Protection Pattern Tests
# =============================================================================


class TestOvercurrentProtection:
    """Tests for OvercurrentProtection pattern."""

    def test_create_fuse_protection(self):
        """Test creating a fuse-based protection pattern."""
        ocp = OvercurrentProtection(topology="fuse", max_current=2.0)
        assert "overcurrent_fuse" in ocp.name

    def test_ocp_invalid_topology(self):
        """Test that invalid topology raises ValueError."""
        with pytest.raises(ValueError, match="Invalid topology"):
            OvercurrentProtection(topology="circuit_breaker", max_current=1.0)

    def test_ocp_placement_rules(self):
        """Test overcurrent protection placement rules."""
        ocp = OvercurrentProtection(topology="efuse", max_current=3.0)
        rules = ocp.get_placement_rules()
        assert len(rules) >= 2


# =============================================================================
# Reverse Polarity Protection Pattern Tests
# =============================================================================


class TestReversePolarityProtection:
    """Tests for ReversePolarityProtection pattern."""

    def test_create_diode_protection(self):
        """Test creating a diode-based protection pattern."""
        rprot = ReversePolarityProtection(topology="diode", max_current=2.0)
        assert "reverse_polarity_diode" in rprot.name

    def test_rprot_invalid_topology(self):
        """Test that invalid topology raises ValueError."""
        with pytest.raises(ValueError, match="Invalid topology"):
            ReversePolarityProtection(topology="npn", max_current=1.0)


# =============================================================================
# Overvoltage Protection Pattern Tests
# =============================================================================


class TestOvervoltageProtection:
    """Tests for OvervoltageProtection pattern."""

    def test_create_tvs_protection(self):
        """Test creating a TVS-based protection pattern."""
        ovp = OvervoltageProtection(topology="tvs", clamp_voltage=6.0)
        assert "overvoltage_tvs" in ovp.name

    def test_ovp_invalid_topology(self):
        """Test that invalid topology raises ValueError."""
        with pytest.raises(ValueError, match="Invalid topology"):
            OvervoltageProtection(topology="spark_gap", clamp_voltage=5.0)

    def test_ovp_validate(self):
        """Test overvoltage protection validation."""
        ovp = OvervoltageProtection(topology="tvs", clamp_voltage=5.4)
        errors = ovp.validate(nominal_voltage=5.0)
        assert len(errors) > 0


# =============================================================================
# Thermal Shutdown Pattern Tests
# =============================================================================


class TestThermalShutdown:
    """Tests for ThermalShutdown pattern."""

    def test_create_ntc_thermal(self):
        """Test creating an NTC-based thermal pattern."""
        thermal = ThermalShutdown(sensor_type="ntc", shutdown_temp_c=85.0)
        assert "thermal_shutdown_ntc" in thermal.name

    def test_thermal_invalid_sensor(self):
        """Test that invalid sensor type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid sensor_type"):
            ThermalShutdown(sensor_type="rtd")
