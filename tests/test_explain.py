"""
Unit tests for the explain system.

Tests cover:
- ExplanationResult and other data models
- ExplanationRegistry loading and lookup
- Main explain API functions
- Formatters (text, JSON, markdown)
- YAML spec loading
- MCP tool functions
"""

import json

import pytest

from kicad_tools.explain import (
    ExplainedViolation,
    ExplanationRegistry,
    ExplanationResult,
    InterfaceSpec,
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


class TestSpecReference:
    """Tests for SpecReference dataclass."""

    def test_spec_reference_creation(self):
        """Test creating a SpecReference."""
        ref = SpecReference(
            name="JLCPCB Manufacturing Capabilities",
            section="PCB Specifications > Minimum Clearance",
            url="https://jlcpcb.com/capabilities/pcb-capabilities",
            version="2024-01",
        )
        assert ref.name == "JLCPCB Manufacturing Capabilities"
        assert ref.section == "PCB Specifications > Minimum Clearance"
        assert ref.url == "https://jlcpcb.com/capabilities/pcb-capabilities"
        assert ref.version == "2024-01"

    def test_spec_reference_to_dict(self):
        """Test SpecReference.to_dict() method."""
        ref = SpecReference(name="Test Spec", section="Section 1", url="http://example.com")
        d = ref.to_dict()
        assert d["name"] == "Test Spec"
        assert d["section"] == "Section 1"
        assert d["url"] == "http://example.com"

    def test_spec_reference_defaults(self):
        """Test SpecReference default values."""
        ref = SpecReference(name="Minimal Spec")
        assert ref.section == ""
        assert ref.url == ""
        assert ref.version == ""


class TestRuleExplanation:
    """Tests for RuleExplanation dataclass."""

    def test_rule_explanation_creation(self):
        """Test creating a RuleExplanation."""
        exp = RuleExplanation(
            rule_id="trace_clearance",
            title="Minimum Trace Clearance",
            explanation="Traces must maintain minimum clearance.",
            spec_references=[SpecReference(name="JLCPCB")],
            fix_templates=["Increase spacing by {delta}{unit}"],
            related_rules=["trace_width"],
            severity="error",
        )
        assert exp.rule_id == "trace_clearance"
        assert exp.title == "Minimum Trace Clearance"
        assert len(exp.spec_references) == 1
        assert exp.severity == "error"

    def test_rule_explanation_to_dict(self):
        """Test RuleExplanation.to_dict() method."""
        exp = RuleExplanation(
            rule_id="test",
            title="Test Rule",
            explanation="Test explanation",
        )
        d = exp.to_dict()
        assert d["rule_id"] == "test"
        assert d["title"] == "Test Rule"
        assert d["explanation"] == "Test explanation"
        assert "spec_references" in d
        assert "fix_templates" in d


class TestExplanationResult:
    """Tests for ExplanationResult dataclass."""

    def test_explanation_result_creation(self):
        """Test creating an ExplanationResult."""
        result = ExplanationResult(
            rule="trace_clearance",
            title="Minimum Trace Clearance",
            explanation="Test explanation",
            current_value=0.15,
            required_value=0.2,
            unit="mm",
            severity="error",
            fix_suggestions=["Increase spacing"],
        )
        assert result.rule == "trace_clearance"
        assert result.current_value == 0.15
        assert result.required_value == 0.2
        assert result.severity == "error"

    def test_explanation_result_to_dict(self):
        """Test ExplanationResult.to_dict() method."""
        result = ExplanationResult(
            rule="test",
            title="Test",
            explanation="Test explanation",
        )
        d = result.to_dict()
        assert d["rule"] == "test"
        assert d["title"] == "Test"
        assert "fix_suggestions" in d
        assert "context" in d

    def test_explanation_result_format_tree(self):
        """Test ExplanationResult.format_tree() method."""
        result = ExplanationResult(
            rule="trace_clearance",
            title="Minimum Trace Clearance",
            explanation="Traces must maintain spacing",
            spec_reference=SpecReference(name="JLCPCB", section="Clearance"),
            current_value=0.15,
            required_value=0.2,
            unit="mm",
            fix_suggestions=["Increase spacing"],
        )
        tree = result.format_tree()
        assert "Minimum Trace Clearance" in tree
        assert "JLCPCB" in tree
        assert "0.15mm" in tree


class TestExplanationRegistry:
    """Tests for ExplanationRegistry."""

    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """Reset registry before each test."""
        ExplanationRegistry.reload()
        yield

    def test_registry_loads_specs(self):
        """Test that registry loads YAML specs on first access."""
        rules = ExplanationRegistry.list_rules()
        assert len(rules) > 0

    def test_registry_get_existing_rule(self):
        """Test getting an existing rule from registry."""
        exp = ExplanationRegistry.get("trace_clearance")
        assert exp is not None
        assert exp.rule_id == "trace_clearance"
        assert exp.title == "Minimum Trace Clearance"

    def test_registry_get_nonexistent_rule(self):
        """Test getting a nonexistent rule returns None."""
        exp = ExplanationRegistry.get("nonexistent_rule_xyz")
        assert exp is None

    def test_registry_search(self):
        """Test searching for rules."""
        results = ExplanationRegistry.search("clearance")
        assert len(results) > 0
        rule_ids = [r.rule_id for r in results]
        assert "trace_clearance" in rule_ids

    def test_registry_list_interfaces(self):
        """Test listing registered interfaces."""
        interfaces = ExplanationRegistry.list_interfaces()
        assert len(interfaces) > 0
        assert any("usb" in i.lower() for i in interfaces)

    def test_registry_get_interface(self):
        """Test getting an interface specification."""
        spec = ExplanationRegistry.get_interface("usb_20_high_speed")
        assert spec is not None
        assert "USB" in spec.interface
        assert "differential_impedance" in spec.constraints


class TestExplainFunction:
    """Tests for the main explain() function."""

    def test_explain_known_rule(self):
        """Test explaining a known rule."""
        result = explain("trace_clearance")
        assert result.rule == "trace_clearance"
        assert result.title == "Minimum Trace Clearance"
        assert len(result.explanation) > 0

    def test_explain_with_context(self):
        """Test explaining with context values."""
        result = explain("trace_clearance", {
            "value": 0.15,
            "required_value": 0.2,
            "net1": "USB_D+",
            "net2": "GND",
        })
        assert result.current_value == 0.15
        assert result.required_value == 0.2
        assert len(result.fix_suggestions) > 0

    def test_explain_unknown_rule_raises(self):
        """Test that explaining an unknown rule raises ValueError."""
        with pytest.raises(ValueError, match="Unknown rule"):
            explain("completely_unknown_rule_xyz123")

    def test_explain_with_spec_reference(self):
        """Test that explain results include spec references."""
        result = explain("trace_clearance")
        assert result.spec_reference is not None
        assert "JLCPCB" in result.spec_reference.name


class TestExplainNetConstraints:
    """Tests for explain_net_constraints() function."""

    def test_explain_usb_net(self):
        """Test explaining USB net constraints."""
        result = explain_net_constraints("USB_D+")
        assert "USB" in result.title or "usb" in result.rule
        assert len(result.explanation) > 0

    def test_explain_i2c_net(self):
        """Test explaining I2C net constraints."""
        result = explain_net_constraints("SDA")
        assert "I2C" in result.title or "i2c" in result.rule.lower()

    def test_explain_spi_net(self):
        """Test explaining SPI net constraints."""
        result = explain_net_constraints("MOSI")
        assert "SPI" in result.title or "spi" in result.rule.lower()

    def test_explain_unknown_net(self):
        """Test explaining unknown net type."""
        result = explain_net_constraints("RANDOM_NET_123")
        assert result is not None
        assert result.severity == "info"


class TestListAndSearchRules:
    """Tests for list_rules() and search_rules() functions."""

    def test_list_rules_returns_sorted_list(self):
        """Test that list_rules returns sorted list."""
        rules = list_rules()
        assert len(rules) > 0
        assert rules == sorted(rules)

    def test_list_rules_has_expected_rules(self):
        """Test that list_rules includes expected rules."""
        rules = list_rules()
        assert "trace_clearance" in rules
        assert "via_drill" in rules

    def test_search_rules_finds_matches(self):
        """Test that search_rules finds matching rules."""
        results = search_rules("via")
        assert len(results) > 0
        for r in results:
            assert "via" in r.rule_id.lower() or "via" in r.title.lower()

    def test_search_rules_no_results(self):
        """Test search_rules with no matches."""
        results = search_rules("xyznonexistent123")
        assert len(results) == 0


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
                name="JLCPCB",
                section="Clearance",
                url="https://jlcpcb.com",
            ),
            current_value=0.15,
            required_value=0.2,
            unit="mm",
            severity="error",
            fix_suggestions=["Increase spacing by 0.05mm"],
            related_rules=["trace_width"],
        )

    def test_format_text(self, sample_result):
        """Test text formatter."""
        output = format_result(sample_result, "text")
        assert "trace_clearance" in output
        assert "Minimum Trace Clearance" in output
        assert "JLCPCB" in output
        assert "0.15" in output

    def test_format_json(self, sample_result):
        """Test JSON formatter."""
        output = format_result(sample_result, "json")
        data = json.loads(output)
        assert data["rule"] == "trace_clearance"
        assert data["current_value"] == 0.15
        assert data["spec_reference"]["name"] == "JLCPCB"

    def test_format_markdown(self, sample_result):
        """Test markdown formatter."""
        output = format_result(sample_result, "markdown")
        assert "## Minimum Trace Clearance" in output
        assert "**Rule ID:**" in output
        assert "[JLCPCB]" in output

    def test_format_tree(self, sample_result):
        """Test tree formatter."""
        output = format_result(sample_result, "tree")
        assert "Minimum Trace Clearance" in output

    def test_format_invalid_raises(self, sample_result):
        """Test that invalid format type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown format"):
            format_result(sample_result, "invalid_format")


class TestYAMLLoading:
    """Tests for YAML spec file loading."""

    def test_jlcpcb_rules_loaded(self):
        """Test that JLCPCB rules are loaded from YAML."""
        ExplanationRegistry.reload()
        rules = list_rules()
        expected_rules = [
            "trace_clearance",
            "trace_width",
            "via_drill",
            "via_annular_ring",
            "via_clearance",
            "edge_clearance",
        ]
        for rule in expected_rules:
            assert rule in rules, f"Expected rule '{rule}' not found"

    def test_interface_specs_loaded(self):
        """Test that interface specs are loaded from YAML."""
        ExplanationRegistry.reload()
        interfaces = ExplanationRegistry.list_interfaces()
        interface_names = [i.lower() for i in interfaces]
        assert any("usb" in i for i in interface_names)
        assert any("i2c" in i for i in interface_names)
        assert any("spi" in i for i in interface_names)

    def test_rule_count_minimum(self):
        """Test that we have at least 20 rules (acceptance criteria)."""
        rules = list_rules()
        assert len(rules) >= 20, f"Expected at least 20 rules, got {len(rules)}"


class TestMCPTools:
    """Tests for MCP tool functions."""

    def test_explain_rule_tool(self):
        """Test explain_rule MCP tool function."""
        from kicad_tools.mcp.tools.explain import explain_rule

        result = explain_rule("trace_clearance", current_value=0.15, required_value=0.2)
        assert "rule" in result
        assert result["rule"] == "trace_clearance"
        assert "fix_suggestions" in result

    def test_explain_rule_unknown(self):
        """Test explain_rule with unknown rule returns error."""
        from kicad_tools.mcp.tools.explain import explain_rule

        result = explain_rule("totally_unknown_rule_xyz")
        assert "error" in result
        assert "available_rules" in result

    def test_explain_net_tool(self):
        """Test explain_net MCP tool function."""
        from kicad_tools.mcp.tools.explain import explain_net

        result = explain_net("USB_D+")
        assert "rule" in result
        assert "explanation" in result

    def test_list_available_rules_tool(self):
        """Test list_available_rules MCP tool function."""
        from kicad_tools.mcp.tools.explain import list_available_rules

        result = list_available_rules()
        assert "total" in result
        assert result["total"] > 0
        assert "rules" in result
        assert "categories" in result

    def test_search_available_rules_tool(self):
        """Test search_available_rules MCP tool function."""
        from kicad_tools.mcp.tools.explain import search_available_rules

        result = search_available_rules("clearance")
        assert "query" in result
        assert "matches" in result
        assert result["total"] > 0
