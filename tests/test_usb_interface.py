"""
Unit tests for USB interface specification.

Tests cover:
- USBInterfaceSpec implements InterfaceSpec protocol
- All USB variants defined and registered
- Constraint derivation for differential pair, length match, impedance
- Intent-aware validation messages for common USB violations
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
from kicad_tools.intent.interfaces.usb import (
    USB_VARIANTS,
    USBInterfaceSpec,
    USBVariant,
)


class TestUSBVariants:
    """Tests for USB variant definitions."""

    def test_all_variants_defined(self):
        """Test that all USB variants are defined."""
        expected_variants = [
            "usb2_low_speed",
            "usb2_full_speed",
            "usb2_high_speed",
            "usb3_gen1",
            "usb3_gen2",
        ]
        for variant in expected_variants:
            assert variant in USB_VARIANTS, f"Missing variant: {variant}"

    def test_variant_dataclass_fields(self):
        """Test USBVariant has all required fields."""
        variant = USB_VARIANTS["usb2_high_speed"]
        assert isinstance(variant, USBVariant)
        assert hasattr(variant, "speed")
        assert hasattr(variant, "impedance")
        assert hasattr(variant, "length_tolerance_mm")
        assert hasattr(variant, "max_trace_length_mm")
        assert hasattr(variant, "min_spacing_multiplier")

    def test_usb2_low_speed_params(self):
        """Test USB 2.0 Low Speed parameters."""
        variant = USB_VARIANTS["usb2_low_speed"]
        assert variant.speed == 1.5e6
        assert variant.impedance is None
        assert variant.length_tolerance_mm is None
        assert variant.min_spacing_multiplier == 2.0

    def test_usb2_full_speed_params(self):
        """Test USB 2.0 Full Speed parameters."""
        variant = USB_VARIANTS["usb2_full_speed"]
        assert variant.speed == 12e6
        assert variant.impedance is None
        assert variant.length_tolerance_mm is None

    def test_usb2_high_speed_params(self):
        """Test USB 2.0 High Speed parameters."""
        variant = USB_VARIANTS["usb2_high_speed"]
        assert variant.speed == 480e6
        assert variant.impedance == 90.0
        assert variant.length_tolerance_mm == 0.5
        assert variant.max_trace_length_mm == 50.0
        assert variant.min_spacing_multiplier == 3.0

    def test_usb3_gen1_params(self):
        """Test USB 3.0 Gen1 parameters."""
        variant = USB_VARIANTS["usb3_gen1"]
        assert variant.speed == 5e9
        assert variant.impedance == 85.0
        assert variant.length_tolerance_mm == 0.25
        assert variant.max_trace_length_mm == 150.0
        assert variant.min_spacing_multiplier == 4.0

    def test_usb3_gen2_params(self):
        """Test USB 3.0 Gen2 parameters."""
        variant = USB_VARIANTS["usb3_gen2"]
        assert variant.speed == 10e9
        assert variant.impedance == 85.0
        assert variant.length_tolerance_mm == 0.25


class TestUSBInterfaceSpec:
    """Tests for USBInterfaceSpec class."""

    def test_spec_creation_default_variant(self):
        """Test creating spec with default variant."""
        spec = USBInterfaceSpec()
        assert spec.name == "usb2_high_speed"

    def test_spec_creation_with_variant(self):
        """Test creating spec with specific variant."""
        spec = USBInterfaceSpec("usb3_gen1")
        assert spec.name == "usb3_gen1"

    def test_spec_creation_invalid_variant(self):
        """Test that invalid variant raises ValueError."""
        with pytest.raises(ValueError, match="Unknown USB variant"):
            USBInterfaceSpec("usb4_invalid")

    def test_category_is_differential(self):
        """Test that all USB specs have DIFFERENTIAL category."""
        for variant_name in USB_VARIANTS:
            spec = USBInterfaceSpec(variant_name)
            assert spec.category == InterfaceCategory.DIFFERENTIAL

    def test_implements_interface_spec_protocol(self):
        """Test that USBInterfaceSpec implements InterfaceSpec protocol."""
        spec = USBInterfaceSpec()
        assert hasattr(spec, "name")
        assert hasattr(spec, "category")
        assert hasattr(spec, "validate_nets")
        assert hasattr(spec, "derive_constraints")
        assert hasattr(spec, "get_validation_message")


class TestUSBNetValidation:
    """Tests for USB net validation."""

    def test_validate_nets_correct_count(self):
        """Test validation passes with 2 nets."""
        spec = USBInterfaceSpec()
        errors = spec.validate_nets(["USB_D+", "USB_D-"])
        assert errors == []

    def test_validate_nets_too_few(self):
        """Test validation fails with 1 net."""
        spec = USBInterfaceSpec()
        errors = spec.validate_nets(["USB_D+"])
        assert len(errors) == 1
        assert "exactly 2 nets" in errors[0]

    def test_validate_nets_too_many(self):
        """Test validation fails with 3 nets."""
        spec = USBInterfaceSpec()
        errors = spec.validate_nets(["USB_D+", "USB_D-", "USB_ID"])
        assert len(errors) == 1
        assert "exactly 2 nets" in errors[0]

    def test_validate_nets_empty(self):
        """Test validation fails with no nets."""
        spec = USBInterfaceSpec()
        errors = spec.validate_nets([])
        assert len(errors) == 1


class TestUSBConstraintDerivation:
    """Tests for USB constraint derivation."""

    def test_usb2_hs_constraints(self):
        """Test USB 2.0 High Speed generates correct constraints."""
        spec = USBInterfaceSpec("usb2_high_speed")
        constraints = spec.derive_constraints(["USB_D+", "USB_D-"], {})

        constraint_types = {c.type for c in constraints}
        assert "differential_pair" in constraint_types
        assert "length_match" in constraint_types
        assert "trace_width" in constraint_types
        assert "max_length" in constraint_types
        assert "clearance" in constraint_types

    def test_usb2_ls_no_impedance_constraints(self):
        """Test USB 2.0 Low Speed has no impedance/length match constraints."""
        spec = USBInterfaceSpec("usb2_low_speed")
        constraints = spec.derive_constraints(["USB_D+", "USB_D-"], {})

        constraint_types = {c.type for c in constraints}
        # Low speed has differential pair but no length matching
        assert "differential_pair" in constraint_types
        assert "length_match" not in constraint_types
        assert "trace_width" not in constraint_types

    def test_differential_pair_constraint_params(self):
        """Test differential pair constraint has correct parameters."""
        spec = USBInterfaceSpec("usb2_high_speed")
        constraints = spec.derive_constraints(["USB_D+", "USB_D-"], {})

        diff_constraint = next(c for c in constraints if c.type == "differential_pair")
        assert diff_constraint.params["nets"] == ["USB_D+", "USB_D-"]
        assert diff_constraint.params["impedance"] == 90.0
        assert diff_constraint.params["tolerance"] == 0.1
        assert diff_constraint.source == "usb:usb2_high_speed"
        assert diff_constraint.severity == ConstraintSeverity.ERROR

    def test_length_match_constraint_params(self):
        """Test length match constraint has correct parameters."""
        spec = USBInterfaceSpec("usb2_high_speed")
        constraints = spec.derive_constraints(["USB_D+", "USB_D-"], {})

        length_constraint = next(c for c in constraints if c.type == "length_match")
        assert length_constraint.params["nets"] == ["USB_D+", "USB_D-"]
        assert length_constraint.params["tolerance_mm"] == 0.5
        assert length_constraint.severity == ConstraintSeverity.ERROR

    def test_usb3_stricter_length_match(self):
        """Test USB 3.x has stricter length matching than USB 2.0."""
        usb2_spec = USBInterfaceSpec("usb2_high_speed")
        usb3_spec = USBInterfaceSpec("usb3_gen1")

        usb2_constraints = usb2_spec.derive_constraints(["D+", "D-"], {})
        usb3_constraints = usb3_spec.derive_constraints(["D+", "D-"], {})

        usb2_length = next(c for c in usb2_constraints if c.type == "length_match")
        usb3_length = next(c for c in usb3_constraints if c.type == "length_match")

        # USB 3.x requires tighter matching
        assert usb3_length.params["tolerance_mm"] < usb2_length.params["tolerance_mm"]

    def test_variant_override_via_params(self):
        """Test variant can be overridden via params."""
        spec = USBInterfaceSpec("usb2_low_speed")
        constraints = spec.derive_constraints(["D+", "D-"], {"variant": "usb2_high_speed"})

        # Should use high speed constraints even though spec is low speed
        constraint_types = {c.type for c in constraints}
        assert "length_match" in constraint_types

        # Source should reflect the override
        diff_constraint = next(c for c in constraints if c.type == "differential_pair")
        assert diff_constraint.source == "usb:usb2_high_speed"

    def test_constraint_source_format(self):
        """Test constraint source uses usb:variant format."""
        spec = USBInterfaceSpec("usb3_gen2")
        constraints = spec.derive_constraints(["D+", "D-"], {})

        for constraint in constraints:
            assert constraint.source == "usb:usb3_gen2"


class TestUSBValidationMessages:
    """Tests for USB validation message formatting."""

    def test_length_mismatch_message(self):
        """Test length mismatch validation message."""
        spec = USBInterfaceSpec("usb2_high_speed")
        msg = spec.get_validation_message({"type": "length_mismatch", "delta": 1.2})

        assert "USB" in msg
        assert "1.2mm" in msg
        assert "length mismatch" in msg.lower()
        assert "signal integrity" in msg.lower()

    def test_impedance_message(self):
        """Test impedance violation message."""
        spec = USBInterfaceSpec("usb2_high_speed")
        msg = spec.get_validation_message({"type": "impedance", "actual": 75})

        assert "75" in msg
        assert "90" in msg  # Target impedance
        assert "impedance" in msg.lower()

    def test_clearance_message(self):
        """Test clearance violation message."""
        spec = USBInterfaceSpec("usb2_high_speed")
        msg = spec.get_validation_message({"type": "clearance", "actual": 0.12, "required": 0.15})

        assert "0.12mm" in msg
        assert "0.15mm" in msg
        assert "clearance" in msg.lower()
        assert "crosstalk" in msg.lower()

    def test_max_length_message(self):
        """Test max length violation message."""
        spec = USBInterfaceSpec("usb2_high_speed")
        msg = spec.get_validation_message({"type": "max_length", "actual": 75})

        assert "75mm" in msg
        assert "50" in msg and "mm" in msg  # Max for USB 2.0 HS (50.0mm)
        assert "signal integrity" in msg.lower()

    def test_unknown_violation_fallback(self):
        """Test fallback for unknown violation types."""
        spec = USBInterfaceSpec()
        msg = spec.get_validation_message({"type": "unknown", "message": "Custom message"})
        assert msg == "Custom message"

    def test_unknown_violation_str_fallback(self):
        """Test str() fallback when no message key."""
        spec = USBInterfaceSpec()
        violation = {"type": "unknown", "value": 42}
        msg = spec.get_validation_message(violation)
        assert "unknown" in msg or "42" in msg


class TestUSBSpeedFormatting:
    """Tests for speed formatting helper."""

    def test_format_kbps(self):
        """Test formatting speeds in kbps."""
        msg = USBInterfaceSpec._format_speed(1.5e6)
        assert msg == "2Mbps" or msg == "1.5Mbps"  # Rounding may vary

    def test_format_mbps(self):
        """Test formatting speeds in Mbps."""
        assert USBInterfaceSpec._format_speed(480e6) == "480Mbps"

    def test_format_gbps(self):
        """Test formatting speeds in Gbps."""
        assert USBInterfaceSpec._format_speed(5e9) == "5Gbps"
        assert USBInterfaceSpec._format_speed(10e9) == "10Gbps"


class TestUSBRegistryIntegration:
    """Tests for USB interface registration in global registry."""

    def test_all_variants_registered(self):
        """Test all USB variants are registered in global REGISTRY."""
        for variant_name in USB_VARIANTS:
            assert variant_name in REGISTRY, f"Variant not registered: {variant_name}"

    def test_registered_spec_is_usb_interface_spec(self):
        """Test registered specs are USBInterfaceSpec instances."""
        spec = REGISTRY.get("usb2_high_speed")
        assert spec is not None
        assert isinstance(spec, USBInterfaceSpec)

    def test_derive_constraints_via_registry(self):
        """Test deriving constraints through the registry."""
        constraints = derive_constraints(
            interface_type="usb2_high_speed",
            nets=["USB_D+", "USB_D-"],
        )
        assert len(constraints) > 0
        assert any(c.type == "differential_pair" for c in constraints)

    def test_validate_intent_via_registry(self):
        """Test validating intent through the registry."""
        errors = validate_intent(
            interface_type="usb2_high_speed",
            nets=["USB_D+", "USB_D-"],
        )
        assert errors == []

    def test_create_intent_declaration_via_registry(self):
        """Test creating intent declaration through the registry."""
        declaration = create_intent_declaration(
            interface_type="usb2_high_speed",
            nets=["USB_D+", "USB_D-"],
            metadata={"connector": "J1"},
        )
        assert declaration.interface_type == "usb2_high_speed"
        assert len(declaration.constraints) > 0

    def test_usb_specs_in_differential_category(self):
        """Test USB specs appear in DIFFERENTIAL category listing."""
        differential = REGISTRY.list_by_category(InterfaceCategory.DIFFERENTIAL)
        for variant_name in USB_VARIANTS:
            assert variant_name in differential


class TestUSBAcceptanceCriteria:
    """Tests verifying issue acceptance criteria."""

    def test_implements_interface_spec_protocol(self):
        """AC: USBInterfaceSpec implements InterfaceSpec protocol."""
        spec = USBInterfaceSpec()
        # Protocol requirements
        assert isinstance(spec.name, str)
        assert isinstance(spec.category, InterfaceCategory)
        assert callable(spec.validate_nets)
        assert callable(spec.derive_constraints)
        assert callable(spec.get_validation_message)

    def test_all_variants_defined(self):
        """AC: All USB variants defined (LS, FS, HS, USB3 Gen1/Gen2)."""
        assert "usb2_low_speed" in USB_VARIANTS
        assert "usb2_full_speed" in USB_VARIANTS
        assert "usb2_high_speed" in USB_VARIANTS
        assert "usb3_gen1" in USB_VARIANTS
        assert "usb3_gen2" in USB_VARIANTS

    def test_constraint_derivation_types(self):
        """AC: Constraint derivation for differential pair, length match, impedance."""
        spec = USBInterfaceSpec("usb2_high_speed")
        constraints = spec.derive_constraints(["D+", "D-"], {})

        types = {c.type for c in constraints}
        assert "differential_pair" in types
        assert "length_match" in types
        assert "trace_width" in types  # For impedance

    def test_intent_aware_messages(self):
        """AC: Intent-aware validation messages for common USB violations."""
        spec = USBInterfaceSpec("usb2_high_speed")

        # Length mismatch message is USB-aware
        msg = spec.get_validation_message({"type": "length_mismatch", "delta": 1.0})
        assert "USB" in msg

        # Impedance message is USB-aware
        msg = spec.get_validation_message({"type": "impedance", "actual": 75})
        assert "USB" in msg or "impedance" in msg.lower()

    def test_registered_in_global_registry(self):
        """AC: Registered in global REGISTRY on module import."""
        for variant in USB_VARIANTS:
            assert REGISTRY.get(variant) is not None
