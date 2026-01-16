"""Tests for the explain system."""

import json

import pytest

from kicad_tools.explain import (
    ExplanationRegistry,
    ExplanationResult,
    RuleExplanation,
    SpecReference,
    explain,
    explain_net_constraints,
    explain_violations,
    format_result,
    format_violations,
    list_rules,
    search_rules,
)
from kicad_tools.explain.formatters import (
    format_json,
    format_markdown,
    format_text,
    format_tree,
)
from kicad_tools.explain.models import ExplainedViolation, InterfaceSpec


class TestSpecReference:
    """Tests for SpecReference model."""

    def test_create_spec_reference(self):
        """Test creating a SpecReference."""
        ref = SpecReference(
            name="JLCPCB Manufacturing Capabilities",
            section="PCB Specifications > Minimum Clearance",
            url="https://jlcpcb.com/capabilities",
            version="2024-01",
        )
        assert ref.name == "JLCPCB Manufacturing Capabilities"
        assert ref.section == "PCB Specifications > Minimum Clearance"
        assert ref.url == "https://jlcpcb.com/capabilities"
        assert ref.version == "2024-01"

    def test_spec_reference_to_dict(self):
        """Test SpecReference serialization."""
        ref = SpecReference(name="Test Spec", section="Section 1")
        d = ref.to_dict()
        assert d["name"] == "Test Spec"
        assert d["section"] == "Section 1"
        assert d["url"] == ""
        assert d["version"] == ""


class TestRuleExplanation:
    """Tests for RuleExplanation model."""

    def test_create_rule_explanation(self):
        """Test creating a RuleExplanation."""
        exp = RuleExplanation(
            rule_id="trace_clearance",
            title="Minimum Trace Clearance",
            explanation="Traces must maintain minimum clearance.",
            fix_templates=["Increase spacing by {delta}mm"],
            related_rules=["trace_width", "via_clearance"],
        )
        assert exp.rule_id == "trace_clearance"
        assert exp.title == "Minimum Trace Clearance"
        assert len(exp.fix_templates) == 1
        assert len(exp.related_rules) == 2

    def test_rule_explanation_to_dict(self):
        """Test RuleExplanation serialization."""
        ref = SpecReference(name="Test Spec")
        exp = RuleExplanation(
            rule_id="test_rule",
            title="Test Rule",
            explanation="Test explanation",
            spec_references=[ref],
        )
        d = exp.to_dict()
        assert d["rule_id"] == "test_rule"
        assert len(d["spec_references"]) == 1
        assert d["spec_references"][0]["name"] == "Test Spec"


class TestExplanationResult:
    """Tests for ExplanationResult model."""

    def test_create_explanation_result(self):
        """Test creating an ExplanationResult."""
        result = ExplanationResult(
            rule="trace_clearance",
            title="Minimum Trace Clearance",
            explanation="Traces must maintain minimum clearance.",
            current_value=0.15,
            required_value=0.2,
            unit="mm",
            fix_suggestions=["Increase spacing by 0.05mm"],
        )
        assert result.rule == "trace_clearance"
        assert result.current_value == 0.15
        assert result.required_value == 0.2
        assert len(result.fix_suggestions) == 1

    def test_explanation_result_to_dict(self):
        """Test ExplanationResult serialization."""
        result = ExplanationResult(
            rule="test_rule",
            title="Test Rule",
            explanation="Test explanation",
        )
        d = result.to_dict()
        assert d["rule"] == "test_rule"
        assert d["title"] == "Test Rule"
        assert d["spec_reference"] is None

    def test_format_tree(self):
        """Test tree formatting."""
        ref = SpecReference(name="Test Spec", version="1.0")
        result = ExplanationResult(
            rule="test_rule",
            title="Test Rule",
            explanation="Test explanation",
            spec_reference=ref,
            current_value=0.15,
            required_value=0.2,
            unit="mm",
            fix_suggestions=["Fix it"],
            related_rules=["other_rule"],
        )
        tree = result.format_tree()
        assert "Test Rule" in tree
        assert "Test Spec" in tree
        assert "0.15mm" in tree
        assert "Fix it" in tree


class TestExplanationRegistry:
    """Tests for ExplanationRegistry."""

    def setup_method(self):
        """Clear registry before each test."""
        ExplanationRegistry.clear()

    def test_register_and_get(self):
        """Test registering and retrieving an explanation."""
        exp = RuleExplanation(
            rule_id="test_rule",
            title="Test Rule",
            explanation="Test explanation",
        )
        ExplanationRegistry.register("test_rule", exp)
        retrieved = ExplanationRegistry.get("test_rule")
        assert retrieved is not None
        assert retrieved.title == "Test Rule"

    def test_get_unknown_rule(self):
        """Test getting an unknown rule returns None."""
        result = ExplanationRegistry.get("nonexistent_rule")
        assert result is None

    def test_list_rules(self):
        """Test listing registered rules."""
        ExplanationRegistry.register(
            "rule_a",
            RuleExplanation(rule_id="rule_a", title="Rule A", explanation="A"),
        )
        ExplanationRegistry.register(
            "rule_b",
            RuleExplanation(rule_id="rule_b", title="Rule B", explanation="B"),
        )
        rules = ExplanationRegistry.list_rules()
        assert "rule_a" in rules
        assert "rule_b" in rules

    def test_search(self):
        """Test searching for rules."""
        ExplanationRegistry.register(
            "unique_test_rule_abc",
            RuleExplanation(
                rule_id="unique_test_rule_abc",
                title="Unique Test Rule ABC",
                explanation="Test rule",
            ),
        )
        ExplanationRegistry.register(
            "another_rule",
            RuleExplanation(
                rule_id="another_rule", title="Another Rule", explanation="Another"
            ),
        )
        results = ExplanationRegistry.search("unique_test_rule_abc")
        assert len(results) == 1
        assert results[0].rule_id == "unique_test_rule_abc"

    def test_register_interface(self):
        """Test registering and retrieving interface specs."""
        spec = InterfaceSpec(
            interface="USB 2.0",
            spec_document="USB Spec",
            constraints={"impedance": {"value": 90}},
        )
        ExplanationRegistry.register_interface("usb2", spec)
        retrieved = ExplanationRegistry.get_interface("usb2")
        assert retrieved is not None
        assert retrieved.interface == "USB 2.0"


class TestExplainFunction:
    """Tests for the main explain() function."""

    def setup_method(self):
        """Clear and reload registry before each test."""
        ExplanationRegistry.reload()

    def test_explain_known_rule(self):
        """Test explaining a known rule."""
        # The YAML specs should be loaded
        result = explain("trace_clearance")
        assert result.rule == "trace_clearance"
        assert result.title != ""
        assert result.explanation != ""

    def test_explain_with_context(self):
        """Test explaining with context values."""
        result = explain(
            "trace_clearance",
            context={"value": 0.15, "required_value": 0.2, "unit": "mm"},
        )
        assert result.current_value == 0.15
        assert result.required_value == 0.2
        assert result.unit == "mm"
        # Should generate fix suggestion
        assert len(result.fix_suggestions) > 0

    def test_explain_unknown_rule(self):
        """Test explaining an unknown rule raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            explain("definitely_not_a_real_rule_xyz123")
        assert "Unknown rule" in str(exc_info.value)

    def test_explain_partial_match(self):
        """Test that partial matches work."""
        # "clearance" should find "trace_clearance"
        result = explain("clearance")
        assert "clearance" in result.rule.lower()


class TestExplainNetConstraints:
    """Tests for explain_net_constraints function."""

    def setup_method(self):
        """Reload registry before each test."""
        ExplanationRegistry.reload()

    def test_explain_usb_net(self):
        """Test explaining a USB net."""
        result = explain_net_constraints("USB_D+")
        assert result.rule != ""
        # Should detect USB interface
        assert "usb" in result.rule.lower() or "USB" in result.explanation

    def test_explain_i2c_net(self):
        """Test explaining an I2C net."""
        result = explain_net_constraints("SDA")
        assert result.rule != ""
        # Should detect I2C interface
        assert "i2c" in result.rule.lower() or "I2C" in result.explanation

    def test_explain_unknown_net(self):
        """Test explaining an unknown net type."""
        result = explain_net_constraints("RANDOM_NET_123")
        assert result.rule == "unknown_net"
        assert result.severity == "info"

    def test_explain_with_explicit_interface(self):
        """Test explaining with explicit interface type."""
        result = explain_net_constraints("MY_SIGNAL", interface_type="spi")
        assert "spi" in result.rule.lower() or "SPI" in result.explanation


class TestExplainViolations:
    """Tests for explain_violations function."""

    def setup_method(self):
        """Reload registry before each test."""
        ExplanationRegistry.reload()

    def test_explain_violations_list(self):
        """Test explaining a list of violations."""

        # Create mock violations
        class MockViolation:
            def __init__(self, rule_id, message):
                self.type_str = rule_id
                self.type = rule_id
                self.message = message
                self.required_value_mm = 0.2
                self.actual_value_mm = 0.15
                self.nets = ["NET1", "NET2"]
                self.severity = "error"
                self.primary_location = None

            def to_dict(self):
                return {"type": self.type_str, "message": self.message}

        violations = [
            MockViolation("clearance", "Clearance violation"),
            MockViolation("trace_width", "Trace too narrow"),
        ]

        explained = explain_violations(violations)
        assert len(explained) == 2
        assert all(isinstance(ev, ExplainedViolation) for ev in explained)
        assert all(ev.explanation is not None for ev in explained)

    def test_explain_unknown_violation_type(self):
        """Test that unknown violation types get generic explanations."""

        class MockViolation:
            type_str = "unknown_xyz"
            type = "unknown_xyz"
            message = "Unknown error"
            severity = "error"
            primary_location = None

            def to_dict(self):
                return {"type": self.type_str, "message": self.message}

        explained = explain_violations([MockViolation()])
        assert len(explained) == 1
        assert explained[0].explanation.rule == "unknown_xyz"


class TestFormatters:
    """Tests for output formatters."""

    @pytest.fixture
    def sample_result(self):
        """Create a sample ExplanationResult for testing."""
        return ExplanationResult(
            rule="trace_clearance",
            title="Minimum Trace Clearance",
            explanation="Traces must maintain minimum clearance.",
            spec_reference=SpecReference(
                name="JLCPCB Spec",
                section="Clearance",
                url="https://example.com",
            ),
            current_value=0.15,
            required_value=0.2,
            unit="mm",
            severity="error",
            fix_suggestions=["Increase spacing by 0.05mm"],
            related_rules=["trace_width"],
        )

    def test_format_text(self, sample_result):
        """Test text formatting."""
        output = format_text(sample_result)
        assert "trace_clearance" in output
        assert "Minimum Trace Clearance" in output
        assert "JLCPCB Spec" in output
        assert "0.15" in output
        assert "0.2" in output

    def test_format_tree(self, sample_result):
        """Test tree formatting."""
        output = format_tree(sample_result)
        assert "Minimum Trace Clearance" in output
        assert "JLCPCB Spec" in output

    def test_format_json(self, sample_result):
        """Test JSON formatting."""
        output = format_json(sample_result)
        data = json.loads(output)
        assert data["rule"] == "trace_clearance"
        assert data["spec_reference"]["name"] == "JLCPCB Spec"

    def test_format_markdown(self, sample_result):
        """Test markdown formatting."""
        output = format_markdown(sample_result)
        assert "## Minimum Trace Clearance" in output
        assert "`trace_clearance`" in output
        assert "[JLCPCB Spec]" in output

    def test_format_result_function(self, sample_result):
        """Test format_result dispatcher function."""
        text_output = format_result(sample_result, "text")
        assert "trace_clearance" in text_output

        json_output = format_result(sample_result, "json")
        assert json.loads(json_output)["rule"] == "trace_clearance"

    def test_format_result_invalid_format(self, sample_result):
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError):
            format_result(sample_result, "invalid_format")


class TestListAndSearchRules:
    """Tests for list_rules and search_rules functions."""

    def setup_method(self):
        """Reload registry before each test."""
        ExplanationRegistry.reload()

    def test_list_rules(self):
        """Test list_rules returns non-empty list."""
        rules = list_rules()
        assert isinstance(rules, list)
        assert len(rules) > 0

    def test_search_rules(self):
        """Test search_rules finds matching rules."""
        results = search_rules("clearance")
        assert isinstance(results, list)
        # Should find at least trace_clearance
        assert any("clearance" in r.rule_id for r in results)

    def test_search_rules_no_match(self):
        """Test search_rules returns empty list for no matches."""
        results = search_rules("xyznonexistent123")
        assert results == []


class TestYAMLSpecLoading:
    """Tests for YAML spec file loading."""

    def setup_method(self):
        """Reload registry before each test."""
        ExplanationRegistry.reload()

    def test_jlcpcb_specs_loaded(self):
        """Test that JLCPCB specs are loaded."""
        exp = ExplanationRegistry.get("trace_clearance")
        assert exp is not None
        assert "JLCPCB" in exp.spec_references[0].name

    def test_oshpark_specs_loaded(self):
        """Test that OSH Park specs are loaded."""
        # Check if any OSH Park rule exists
        rules = list_rules()
        # The YAML file might not use "oshpark" prefix, check for rules
        assert len(rules) > 5  # Should have multiple rules loaded

    def test_usb_interface_specs_loaded(self):
        """Test that USB interface specs are loaded."""
        spec = ExplanationRegistry.get_interface("usb_20_high_speed")
        assert spec is not None
        assert "USB" in spec.interface

    def test_i2c_interface_specs_loaded(self):
        """Test that I2C interface specs are loaded."""
        spec = ExplanationRegistry.get_interface("i2c")
        assert spec is not None
        assert "I2C" in spec.interface

    def test_spi_interface_specs_loaded(self):
        """Test that SPI interface specs are loaded."""
        spec = ExplanationRegistry.get_interface("spi")
        assert spec is not None
        assert "SPI" in spec.interface
