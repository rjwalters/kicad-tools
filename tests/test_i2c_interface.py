"""
Unit tests for I2C interface specification.

Tests cover:
- I2CInterfaceSpec implements InterfaceSpec protocol
- All I2C variants defined and registered
- Constraint derivation for capacitance, pull-ups, trace length
- Intent-aware validation messages for common I2C violations
"""

import pytest

from kicad_tools.intent import (
    REGISTRY,
    ConstraintSeverity,
    InterfaceCategory,
    create_intent_declaration,
    derive_constraints,
    validate_intent,
)
from kicad_tools.intent.interfaces.i2c import (
    I2C_VARIANTS,
    I2CInterfaceSpec,
    I2CVariant,
)


class TestI2CVariants:
    """Tests for I2C variant definitions."""

    def test_all_variants_defined(self):
        """Test that all I2C variants are defined."""
        expected_variants = ["i2c_standard", "i2c_fast", "i2c_fast_plus"]
        for variant in expected_variants:
            assert variant in I2C_VARIANTS, f"Missing variant: {variant}"

    def test_variant_dataclass_fields(self):
        """Test I2CVariant has all required fields."""
        variant = I2C_VARIANTS["i2c_standard"]
        assert isinstance(variant, I2CVariant)
        assert hasattr(variant, "freq")
        assert hasattr(variant, "max_capacitance_pf")
        assert hasattr(variant, "rise_time_ns")
        assert hasattr(variant, "pullup_ohms")
        assert hasattr(variant, "max_trace_length_mm")

    def test_i2c_standard_params(self):
        """Test I2C standard mode parameters."""
        variant = I2C_VARIANTS["i2c_standard"]
        assert variant.freq == 100e3
        assert variant.max_capacitance_pf == 400.0
        assert variant.rise_time_ns == 1000.0
        assert variant.pullup_ohms == 4700.0
        assert variant.max_trace_length_mm == 1000.0

    def test_i2c_fast_params(self):
        """Test I2C fast mode parameters."""
        variant = I2C_VARIANTS["i2c_fast"]
        assert variant.freq == 400e3
        assert variant.max_capacitance_pf == 400.0
        assert variant.rise_time_ns == 300.0
        assert variant.pullup_ohms == 2200.0
        assert variant.max_trace_length_mm == 500.0

    def test_i2c_fast_plus_params(self):
        """Test I2C fast mode plus parameters."""
        variant = I2C_VARIANTS["i2c_fast_plus"]
        assert variant.freq == 1e6
        assert variant.max_capacitance_pf == 550.0
        assert variant.rise_time_ns == 120.0
        assert variant.pullup_ohms == 1000.0
        assert variant.max_trace_length_mm == 300.0


class TestI2CInterfaceSpec:
    """Tests for I2CInterfaceSpec class."""

    def test_spec_creation_default_variant(self):
        """Test creating spec with default variant."""
        spec = I2CInterfaceSpec()
        assert spec.name == "i2c_standard"

    def test_spec_creation_with_variant(self):
        """Test creating spec with specific variant."""
        spec = I2CInterfaceSpec("i2c_fast_plus")
        assert spec.name == "i2c_fast_plus"

    def test_spec_creation_invalid_variant(self):
        """Test that invalid variant raises ValueError."""
        with pytest.raises(ValueError, match="Unknown I2C variant"):
            I2CInterfaceSpec("i2c_invalid")

    def test_category_is_bus(self):
        """Test that all I2C specs have BUS category."""
        for variant_name in I2C_VARIANTS:
            spec = I2CInterfaceSpec(variant_name)
            assert spec.category == InterfaceCategory.BUS

    def test_implements_interface_spec_protocol(self):
        """Test that I2CInterfaceSpec implements InterfaceSpec protocol."""
        spec = I2CInterfaceSpec()
        assert hasattr(spec, "name")
        assert hasattr(spec, "category")
        assert hasattr(spec, "validate_nets")
        assert hasattr(spec, "derive_constraints")
        assert hasattr(spec, "get_validation_message")


class TestI2CNetValidation:
    """Tests for I2C net validation."""

    def test_validate_nets_correct_count(self):
        """Test validation passes with 2 nets."""
        spec = I2CInterfaceSpec()
        errors = spec.validate_nets(["I2C_SDA", "I2C_SCL"])
        assert errors == []

    def test_validate_nets_too_few(self):
        """Test validation fails with 1 net."""
        spec = I2CInterfaceSpec()
        errors = spec.validate_nets(["I2C_SDA"])
        assert len(errors) == 1
        assert "exactly 2 nets" in errors[0]

    def test_validate_nets_too_many(self):
        """Test validation fails with 3 nets."""
        spec = I2CInterfaceSpec()
        errors = spec.validate_nets(["I2C_SDA", "I2C_SCL", "I2C_INT"])
        assert len(errors) == 1
        assert "exactly 2 nets" in errors[0]

    def test_validate_nets_empty(self):
        """Test validation fails with no nets."""
        spec = I2CInterfaceSpec()
        errors = spec.validate_nets([])
        assert len(errors) == 1


class TestI2CConstraintDerivation:
    """Tests for I2C constraint derivation."""

    def test_i2c_standard_constraints(self):
        """Test I2C standard generates correct constraints."""
        spec = I2CInterfaceSpec("i2c_standard")
        constraints = spec.derive_constraints(["SDA", "SCL"], {})

        constraint_types = {c.type for c in constraints}
        assert "max_capacitance" in constraint_types
        assert "requires_pullup" in constraint_types
        assert "max_length" in constraint_types

    def test_capacitance_constraint_params(self):
        """Test capacitance constraint has correct parameters."""
        spec = I2CInterfaceSpec("i2c_standard")
        constraints = spec.derive_constraints(["SDA", "SCL"], {})

        cap_constraint = next(c for c in constraints if c.type == "max_capacitance")
        assert cap_constraint.params["nets"] == ["SDA", "SCL"]
        assert cap_constraint.params["max_pf"] == 400.0
        assert cap_constraint.source == "i2c:i2c_standard"
        assert cap_constraint.severity == ConstraintSeverity.WARNING

    def test_pullup_constraint_params(self):
        """Test pull-up constraint has correct parameters."""
        spec = I2CInterfaceSpec("i2c_fast")
        constraints = spec.derive_constraints(["SDA", "SCL"], {})

        pullup_constraint = next(c for c in constraints if c.type == "requires_pullup")
        assert pullup_constraint.params["nets"] == ["SDA", "SCL"]
        assert pullup_constraint.params["typical_ohms"] == 2200.0

    def test_max_length_constraint_params(self):
        """Test max length constraint has correct parameters."""
        spec = I2CInterfaceSpec("i2c_fast_plus")
        constraints = spec.derive_constraints(["SDA", "SCL"], {})

        length_constraint = next(c for c in constraints if c.type == "max_length")
        assert length_constraint.params["max_mm"] == 300.0
        assert length_constraint.severity == ConstraintSeverity.WARNING

    def test_fast_plus_higher_capacitance(self):
        """Test I2C Fast+ has higher capacitance limit than standard/fast."""
        standard_spec = I2CInterfaceSpec("i2c_standard")
        fast_plus_spec = I2CInterfaceSpec("i2c_fast_plus")

        standard_constraints = standard_spec.derive_constraints(["SDA", "SCL"], {})
        fast_plus_constraints = fast_plus_spec.derive_constraints(["SDA", "SCL"], {})

        standard_cap = next(c for c in standard_constraints if c.type == "max_capacitance")
        fast_plus_cap = next(c for c in fast_plus_constraints if c.type == "max_capacitance")

        # Fast+ has 550pF vs 400pF for standard/fast
        assert fast_plus_cap.params["max_pf"] > standard_cap.params["max_pf"]

    def test_variant_override_via_params(self):
        """Test variant can be overridden via params."""
        spec = I2CInterfaceSpec("i2c_standard")
        constraints = spec.derive_constraints(["SDA", "SCL"], {"variant": "i2c_fast"})

        # Should use fast constraints
        pullup = next(c for c in constraints if c.type == "requires_pullup")
        assert pullup.params["typical_ohms"] == 2200.0  # Fast value, not standard

    def test_constraint_source_format(self):
        """Test constraint source uses i2c:variant format."""
        spec = I2CInterfaceSpec("i2c_fast")
        constraints = spec.derive_constraints(["SDA", "SCL"], {})

        for constraint in constraints:
            assert constraint.source == "i2c:i2c_fast"


class TestI2CValidationMessages:
    """Tests for I2C validation message formatting."""

    def test_capacitance_message(self):
        """Test capacitance violation message."""
        spec = I2CInterfaceSpec("i2c_standard")
        msg = spec.get_validation_message({"type": "capacitance", "actual": 500})

        assert "I2C" in msg
        assert "500pF" in msg
        assert "400" in msg  # 400 or 400.0
        assert "capacitance" in msg.lower()

    def test_max_length_message(self):
        """Test max length violation message."""
        spec = I2CInterfaceSpec("i2c_fast")
        msg = spec.get_validation_message({"type": "max_length", "actual": 700})

        assert "700mm" in msg
        assert "500" in msg  # Max for fast mode
        assert "capacitance" in msg.lower() or "pull-up" in msg.lower()

    def test_pullup_message(self):
        """Test pull-up requirement message."""
        spec = I2CInterfaceSpec("i2c_standard")
        msg = spec.get_validation_message({"type": "pullup"})

        assert "pull-up" in msg.lower()
        assert "4.7k" in msg  # Standard pull-up value

    def test_rise_time_message(self):
        """Test rise time violation message."""
        spec = I2CInterfaceSpec("i2c_fast")
        msg = spec.get_validation_message({"type": "rise_time", "actual": 400})

        assert "400ns" in msg
        assert "300" in msg  # Fast mode rise time (300 or 300.0)
        assert "pull-up" in msg.lower() or "capacitance" in msg.lower()

    def test_unknown_violation_fallback(self):
        """Test fallback for unknown violation types."""
        spec = I2CInterfaceSpec()
        msg = spec.get_validation_message({"type": "unknown", "message": "Custom message"})
        assert msg == "Custom message"


class TestI2CFormatting:
    """Tests for formatting helpers."""

    def test_format_freq_khz(self):
        """Test formatting frequencies in kHz."""
        assert I2CInterfaceSpec._format_freq(100e3) == "100kHz"
        assert I2CInterfaceSpec._format_freq(400e3) == "400kHz"

    def test_format_freq_mhz(self):
        """Test formatting frequencies in MHz."""
        assert I2CInterfaceSpec._format_freq(1e6) == "1MHz"

    def test_format_resistance_kohms(self):
        """Test formatting resistance in kΩ."""
        assert I2CInterfaceSpec._format_resistance(4700.0) == "4.7kΩ"
        assert I2CInterfaceSpec._format_resistance(2200.0) == "2.2kΩ"
        assert I2CInterfaceSpec._format_resistance(1000.0) == "1kΩ"

    def test_format_resistance_ohms(self):
        """Test formatting resistance in Ω."""
        assert I2CInterfaceSpec._format_resistance(470.0) == "470Ω"


class TestI2CRegistryIntegration:
    """Tests for I2C interface registration in global registry."""

    def test_all_variants_registered(self):
        """Test all I2C variants are registered in global REGISTRY."""
        for variant_name in I2C_VARIANTS:
            assert variant_name in REGISTRY, f"Variant not registered: {variant_name}"

    def test_registered_spec_is_i2c_interface_spec(self):
        """Test registered specs are I2CInterfaceSpec instances."""
        spec = REGISTRY.get("i2c_standard")
        assert spec is not None
        assert isinstance(spec, I2CInterfaceSpec)

    def test_derive_constraints_via_registry(self):
        """Test deriving constraints through the registry."""
        constraints = derive_constraints(
            interface_type="i2c_fast",
            nets=["SDA", "SCL"],
        )
        assert len(constraints) > 0
        assert any(c.type == "max_capacitance" for c in constraints)

    def test_validate_intent_via_registry(self):
        """Test validating intent through the registry."""
        errors = validate_intent(
            interface_type="i2c_standard",
            nets=["SDA", "SCL"],
        )
        assert errors == []

    def test_create_intent_declaration_via_registry(self):
        """Test creating intent declaration through the registry."""
        declaration = create_intent_declaration(
            interface_type="i2c_fast_plus",
            nets=["SDA", "SCL"],
            metadata={"device": "U1"},
        )
        assert declaration.interface_type == "i2c_fast_plus"
        assert len(declaration.constraints) > 0

    def test_i2c_specs_in_bus_category(self):
        """Test I2C specs appear in BUS category listing."""
        bus_interfaces = REGISTRY.list_by_category(InterfaceCategory.BUS)
        for variant_name in I2C_VARIANTS:
            assert variant_name in bus_interfaces


class TestI2CAcceptanceCriteria:
    """Tests verifying issue acceptance criteria."""

    def test_implements_interface_spec_protocol(self):
        """AC: I2CInterfaceSpec implements InterfaceSpec protocol."""
        spec = I2CInterfaceSpec()
        assert isinstance(spec.name, str)
        assert isinstance(spec.category, InterfaceCategory)
        assert callable(spec.validate_nets)
        assert callable(spec.derive_constraints)
        assert callable(spec.get_validation_message)

    def test_three_speed_variants(self):
        """AC: I2CInterfaceSpec with Standard/Fast/Fast+ variants."""
        assert "i2c_standard" in I2C_VARIANTS
        assert "i2c_fast" in I2C_VARIANTS
        assert "i2c_fast_plus" in I2C_VARIANTS
        assert len(I2C_VARIANTS) == 3

    def test_intent_aware_messages(self):
        """AC: Intent-aware validation messages for I2C violations."""
        spec = I2CInterfaceSpec("i2c_fast")

        msg = spec.get_validation_message({"type": "capacitance", "actual": 500})
        assert "I2C" in msg

        msg = spec.get_validation_message({"type": "pullup"})
        assert "pull-up" in msg.lower()

    def test_registered_in_global_registry(self):
        """AC: Registered in global REGISTRY on module import."""
        for variant in I2C_VARIANTS:
            assert REGISTRY.get(variant) is not None
