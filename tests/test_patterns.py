"""Tests for pattern validation and adaptation."""

import pytest

from kicad_tools.patterns import (
    AdaptedPatternParams,
    ComponentRequirements,
    PatternAdapter,
    PatternValidationResult,
    PatternValidator,
    PatternViolation,
    get_component_requirements,
    list_components,
)


class TestPatternViolation:
    """Tests for PatternViolation dataclass."""

    def test_create_error_violation(self):
        """Test creating an error violation."""
        violation = PatternViolation(
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
        violation = PatternViolation(
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
        violation = PatternViolation(
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
            PatternViolation(
                severity="critical",  # Invalid
                rule="test",
                message="test",
            )

    def test_to_dict(self):
        """Test converting violation to dictionary."""
        violation = PatternViolation(
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
        result.add(PatternViolation(severity="error", rule="test", message="Error"))
        assert not result.passed
        assert result.error_count == 1

    def test_result_with_only_warnings_passes(self):
        """Test that result with only warnings passes."""
        result = PatternValidationResult(pattern_type="LDO")
        result.add(PatternViolation(severity="warning", rule="test", message="Warning"))
        assert result.passed
        assert result.warning_count == 1

    def test_errors_and_warnings_lists(self):
        """Test filtering by severity."""
        result = PatternValidationResult(pattern_type="Test")
        result.add(PatternViolation(severity="error", rule="e1", message="E1"))
        result.add(PatternViolation(severity="warning", rule="w1", message="W1"))
        result.add(PatternViolation(severity="error", rule="e2", message="E2"))
        result.add(PatternViolation(severity="info", rule="i1", message="I1"))

        assert len(result.errors) == 2
        assert len(result.warnings) == 1
        assert result.info_count == 1
        assert len(result) == 4

    def test_summary(self):
        """Test summary generation."""
        result = PatternValidationResult(pattern_type="LDO", rules_checked=5)
        result.add(PatternViolation(severity="error", rule="e1", message="E1"))
        result.add(PatternViolation(severity="warning", rule="w1", message="W1"))

        summary = result.summary()
        assert "FAILED" in summary
        assert "1 errors" in summary
        assert "1 warnings" in summary
        assert "5 rules checked" in summary

    def test_to_dict(self):
        """Test converting result to dictionary."""
        result = PatternValidationResult(pattern_type="LDO", rules_checked=3)
        result.add(PatternViolation(severity="error", rule="e1", message="E1"))

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
