"""
Unit tests for Power interface specification.

Tests cover:
- PowerInterfaceSpec implements InterfaceSpec protocol
- Constraint derivation for trace width and decoupling
- Current-based trace width calculation
- Intent-aware validation messages for power violations
"""

from kicad_tools.intent import (
    REGISTRY,
    ConstraintSeverity,
    InterfaceCategory,
    create_intent_declaration,
    derive_constraints,
    validate_intent,
)
from kicad_tools.intent.interfaces.power import PowerInterfaceSpec


class TestPowerInterfaceSpec:
    """Tests for PowerInterfaceSpec class."""

    def test_spec_creation(self):
        """Test creating power spec."""
        spec = PowerInterfaceSpec()
        assert spec.name == "power_rail"

    def test_category_is_power(self):
        """Test that power spec has POWER category."""
        spec = PowerInterfaceSpec()
        assert spec.category == InterfaceCategory.POWER

    def test_implements_interface_spec_protocol(self):
        """Test that PowerInterfaceSpec implements InterfaceSpec protocol."""
        spec = PowerInterfaceSpec()
        assert hasattr(spec, "name")
        assert hasattr(spec, "category")
        assert hasattr(spec, "validate_nets")
        assert hasattr(spec, "derive_constraints")
        assert hasattr(spec, "get_validation_message")


class TestPowerNetValidation:
    """Tests for power net validation."""

    def test_validate_nets_correct_count(self):
        """Test validation passes with 1 net."""
        spec = PowerInterfaceSpec()
        errors = spec.validate_nets(["VCC_3V3"])
        assert errors == []

    def test_validate_nets_too_few(self):
        """Test validation fails with 0 nets."""
        spec = PowerInterfaceSpec()
        errors = spec.validate_nets([])
        assert len(errors) == 1
        assert "exactly 1 net" in errors[0]

    def test_validate_nets_too_many(self):
        """Test validation fails with 2 nets."""
        spec = PowerInterfaceSpec()
        errors = spec.validate_nets(["VCC_3V3", "VCC_5V"])
        assert len(errors) == 1
        assert "exactly 1 net" in errors[0]


class TestPowerConstraintDerivation:
    """Tests for power constraint derivation."""

    def test_basic_constraints_generated(self):
        """Test power generates trace width and decoupling constraints."""
        spec = PowerInterfaceSpec()
        constraints = spec.derive_constraints(["VCC"], {"max_current": 0.5})

        constraint_types = {c.type for c in constraints}
        assert "min_trace_width" in constraint_types
        assert "requires_decoupling" in constraint_types

    def test_trace_width_constraint_params(self):
        """Test trace width constraint has correct parameters."""
        spec = PowerInterfaceSpec()
        constraints = spec.derive_constraints(["VCC_3V3"], {"max_current": 0.5})

        width_constraint = next(c for c in constraints if c.type == "min_trace_width")
        assert width_constraint.params["net"] == "VCC_3V3"
        assert width_constraint.params["current"] == 0.5
        assert width_constraint.params["min_mm"] == 0.25  # 0.1 + 0.5*0.3
        assert width_constraint.source == "power_rail"
        assert width_constraint.severity == ConstraintSeverity.ERROR

    def test_decoupling_constraint_params(self):
        """Test decoupling constraint has correct parameters."""
        spec = PowerInterfaceSpec()
        constraints = spec.derive_constraints(["VCC_3V3"], {"voltage": 3.3, "max_current": 0.5})

        decoupling = next(c for c in constraints if c.type == "requires_decoupling")
        assert decoupling.params["net"] == "VCC_3V3"
        assert decoupling.params["voltage"] == 3.3
        assert "capacitors" in decoupling.params
        assert decoupling.severity == ConstraintSeverity.WARNING

    def test_default_current_used(self):
        """Test default current (0.5A) is used when not specified."""
        spec = PowerInterfaceSpec()
        constraints = spec.derive_constraints(["VCC"], {})

        width_constraint = next(c for c in constraints if c.type == "min_trace_width")
        assert width_constraint.params["current"] == 0.5

    def test_voltage_optional(self):
        """Test voltage is optional in params."""
        spec = PowerInterfaceSpec()
        constraints = spec.derive_constraints(["VCC"], {"max_current": 0.2})

        decoupling = next(c for c in constraints if c.type == "requires_decoupling")
        assert "voltage" not in decoupling.params


class TestPowerTraceWidthCalculation:
    """Tests for trace width calculation."""

    def test_width_for_100ma(self):
        """Test trace width for 100mA."""
        width = PowerInterfaceSpec._width_for_current(0.1)
        assert width == 0.13

    def test_width_for_500ma(self):
        """Test trace width for 500mA."""
        width = PowerInterfaceSpec._width_for_current(0.5)
        assert width == 0.25

    def test_width_for_1a(self):
        """Test trace width for 1A."""
        width = PowerInterfaceSpec._width_for_current(1.0)
        assert width == 0.4

    def test_width_for_2a(self):
        """Test trace width for 2A."""
        width = PowerInterfaceSpec._width_for_current(2.0)
        assert width == 0.7

    def test_width_increases_with_current(self):
        """Test that width increases proportionally with current."""
        width_low = PowerInterfaceSpec._width_for_current(0.5)
        width_high = PowerInterfaceSpec._width_for_current(1.0)
        assert width_high > width_low


class TestPowerDecouplingSpec:
    """Tests for decoupling capacitor recommendations."""

    def test_decoupling_for_low_current(self):
        """Test decoupling for <100mA."""
        spec = PowerInterfaceSpec()
        caps = spec._decoupling_spec(0.05)
        assert len(caps) == 1
        assert caps[0]["value"] == "100nF"

    def test_decoupling_for_100ma(self):
        """Test decoupling for 100mA threshold."""
        spec = PowerInterfaceSpec()
        caps = spec._decoupling_spec(0.1)
        values = [c["value"] for c in caps]
        assert "100nF" in values

    def test_decoupling_for_500ma(self):
        """Test decoupling for 500mA threshold."""
        spec = PowerInterfaceSpec()
        caps = spec._decoupling_spec(0.5)
        values = [c["value"] for c in caps]
        assert "100nF" in values
        assert "10uF" in values

    def test_decoupling_for_1a(self):
        """Test decoupling for 1A threshold."""
        spec = PowerInterfaceSpec()
        caps = spec._decoupling_spec(1.0)
        values = [c["value"] for c in caps]
        assert "100nF" in values
        assert "10uF" in values
        assert "47uF" in values

    def test_decoupling_for_2a(self):
        """Test decoupling for 2A threshold."""
        spec = PowerInterfaceSpec()
        caps = spec._decoupling_spec(2.0)
        values = [c["value"] for c in caps]
        assert "100nF" in values
        assert "10uF" in values
        assert "47uF" in values
        assert "100uF" in values

    def test_decoupling_increases_with_current(self):
        """Test that more capacitors are recommended for higher current."""
        spec = PowerInterfaceSpec()
        caps_low = spec._decoupling_spec(0.05)
        caps_high = spec._decoupling_spec(2.0)
        assert len(caps_high) > len(caps_low)


class TestPowerValidationMessages:
    """Tests for power validation message formatting."""

    def test_trace_width_message(self):
        """Test trace width violation message."""
        spec = PowerInterfaceSpec()
        msg = spec.get_validation_message(
            {
                "type": "trace_width",
                "actual": 0.15,
                "required": 0.25,
                "current": 0.5,
            }
        )

        assert "0.15mm" in msg
        assert "0.25mm" in msg
        assert "0.5A" in msg
        assert "voltage drop" in msg.lower() or "overheating" in msg.lower()

    def test_decoupling_message_with_missing(self):
        """Test decoupling violation message with missing caps."""
        spec = PowerInterfaceSpec()
        msg = spec.get_validation_message(
            {
                "type": "decoupling",
                "missing": ["100nF", "10uF"],
            }
        )

        assert "decoupling" in msg.lower()
        assert "100nF" in msg
        assert "10uF" in msg

    def test_decoupling_message_generic(self):
        """Test generic decoupling message."""
        spec = PowerInterfaceSpec()
        msg = spec.get_validation_message({"type": "decoupling"})

        assert "decoupling" in msg.lower()
        assert "capacitor" in msg.lower()

    def test_voltage_drop_message(self):
        """Test voltage drop violation message."""
        spec = PowerInterfaceSpec()
        msg = spec.get_validation_message(
            {
                "type": "voltage_drop",
                "drop": 0.15,
                "max_allowed": 0.1,
            }
        )

        assert "0.15V" in msg
        assert "0.1V" in msg
        assert "trace width" in msg.lower() or "power plane" in msg.lower()

    def test_unknown_violation_fallback(self):
        """Test fallback for unknown violation types."""
        spec = PowerInterfaceSpec()
        msg = spec.get_validation_message({"type": "unknown", "message": "Custom message"})
        assert msg == "Custom message"


class TestPowerRegistryIntegration:
    """Tests for power interface registration in global registry."""

    def test_power_rail_registered(self):
        """Test power_rail is registered in global REGISTRY."""
        assert "power_rail" in REGISTRY

    def test_registered_spec_is_power_interface_spec(self):
        """Test registered spec is PowerInterfaceSpec instance."""
        spec = REGISTRY.get("power_rail")
        assert spec is not None
        assert isinstance(spec, PowerInterfaceSpec)

    def test_derive_constraints_via_registry(self):
        """Test deriving constraints through the registry."""
        constraints = derive_constraints(
            interface_type="power_rail",
            nets=["VCC_3V3"],
            params={"max_current": 0.5},
        )
        assert len(constraints) > 0
        assert any(c.type == "min_trace_width" for c in constraints)

    def test_validate_intent_via_registry(self):
        """Test validating intent through the registry."""
        errors = validate_intent(
            interface_type="power_rail",
            nets=["VCC"],
        )
        assert errors == []

    def test_create_intent_declaration_via_registry(self):
        """Test creating intent declaration through the registry."""
        declaration = create_intent_declaration(
            interface_type="power_rail",
            nets=["VCC_5V"],
            params={"voltage": 5.0, "max_current": 1.0},
            metadata={"regulator": "U2"},
        )
        assert declaration.interface_type == "power_rail"
        assert len(declaration.constraints) > 0

    def test_power_spec_in_power_category(self):
        """Test power spec appears in POWER category listing."""
        power_interfaces = REGISTRY.list_by_category(InterfaceCategory.POWER)
        assert "power_rail" in power_interfaces


class TestPowerAcceptanceCriteria:
    """Tests verifying issue acceptance criteria."""

    def test_implements_interface_spec_protocol(self):
        """AC: PowerInterfaceSpec implements InterfaceSpec protocol."""
        spec = PowerInterfaceSpec()
        assert isinstance(spec.name, str)
        assert isinstance(spec.category, InterfaceCategory)
        assert callable(spec.validate_nets)
        assert callable(spec.derive_constraints)
        assert callable(spec.get_validation_message)

    def test_current_based_trace_width(self):
        """AC: PowerInterfaceSpec with current-based trace width calculation."""
        spec = PowerInterfaceSpec()

        # Test that trace width varies with current
        constraints_low = spec.derive_constraints(["VCC"], {"max_current": 0.1})
        constraints_high = spec.derive_constraints(["VCC"], {"max_current": 2.0})

        width_low = next(c for c in constraints_low if c.type == "min_trace_width").params["min_mm"]
        width_high = next(c for c in constraints_high if c.type == "min_trace_width").params[
            "min_mm"
        ]

        assert width_high > width_low

    def test_intent_aware_messages(self):
        """AC: Intent-aware validation messages for power violations."""
        spec = PowerInterfaceSpec()

        msg = spec.get_validation_message(
            {
                "type": "trace_width",
                "actual": 0.1,
                "required": 0.4,
                "current": 1.0,
            }
        )
        assert "trace" in msg.lower() or "width" in msg.lower()

        msg = spec.get_validation_message({"type": "decoupling"})
        assert "decoupling" in msg.lower()

    def test_registered_in_global_registry(self):
        """AC: Registered in global REGISTRY on module import."""
        assert REGISTRY.get("power_rail") is not None
