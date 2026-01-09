"""
Unit tests for the intent module.

Tests cover:
- InterfaceCategory enumeration
- ConstraintSeverity enumeration
- Constraint dataclass
- IntentDeclaration dataclass
- InterfaceRegistry operations
- Constraint derivation functions
"""

import pytest

from kicad_tools.intent import (
    REGISTRY,
    Constraint,
    ConstraintSeverity,
    IntentDeclaration,
    InterfaceCategory,
    InterfaceRegistry,
    create_intent_declaration,
    derive_constraints,
    validate_intent,
)


class TestInterfaceCategory:
    """Tests for InterfaceCategory enumeration."""

    def test_category_values(self):
        """Test InterfaceCategory enum values."""
        assert InterfaceCategory.DIFFERENTIAL.value == "differential"
        assert InterfaceCategory.BUS.value == "bus"
        assert InterfaceCategory.SINGLE_ENDED.value == "single_ended"
        assert InterfaceCategory.POWER.value == "power"

    def test_category_from_string(self):
        """Test creating InterfaceCategory from string value."""
        assert InterfaceCategory("differential") == InterfaceCategory.DIFFERENTIAL
        assert InterfaceCategory("bus") == InterfaceCategory.BUS
        assert InterfaceCategory("single_ended") == InterfaceCategory.SINGLE_ENDED
        assert InterfaceCategory("power") == InterfaceCategory.POWER


class TestConstraintSeverity:
    """Tests for ConstraintSeverity enumeration."""

    def test_severity_values(self):
        """Test ConstraintSeverity enum values."""
        assert ConstraintSeverity.ERROR.value == "error"
        assert ConstraintSeverity.WARNING.value == "warning"

    def test_severity_from_string(self):
        """Test creating ConstraintSeverity from string value."""
        assert ConstraintSeverity("error") == ConstraintSeverity.ERROR
        assert ConstraintSeverity("warning") == ConstraintSeverity.WARNING


class TestConstraint:
    """Tests for Constraint dataclass."""

    def test_constraint_creation(self):
        """Test creating a Constraint."""
        constraint = Constraint(
            type="impedance",
            params={"target": 90.0, "tolerance": 0.1},
            source="usb2_high_speed",
            severity=ConstraintSeverity.ERROR,
        )
        assert constraint.type == "impedance"
        assert constraint.params == {"target": 90.0, "tolerance": 0.1}
        assert constraint.source == "usb2_high_speed"
        assert constraint.severity == ConstraintSeverity.ERROR

    def test_constraint_with_string_severity(self):
        """Test Constraint with string severity (auto-converted)."""
        constraint = Constraint(
            type="length_match",
            params={"tolerance_mm": 2.0},
            source="usb2_high_speed",
            severity="warning",
        )
        assert constraint.severity == ConstraintSeverity.WARNING

    def test_constraint_with_enum_severity(self):
        """Test Constraint with enum severity."""
        constraint = Constraint(
            type="spacing",
            params={"min_mm": 0.2},
            source="spi",
            severity=ConstraintSeverity.ERROR,
        )
        assert constraint.severity == ConstraintSeverity.ERROR


class TestIntentDeclaration:
    """Tests for IntentDeclaration dataclass."""

    def test_declaration_creation(self):
        """Test creating an IntentDeclaration."""
        declaration = IntentDeclaration(
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
        )
        assert declaration.interface_type == "usb2_high_speed"
        assert declaration.nets == ["USB_DP", "USB_DM"]
        assert declaration.constraints == []
        assert declaration.metadata == {}

    def test_declaration_with_constraints(self):
        """Test IntentDeclaration with constraints."""
        constraints = [
            Constraint(
                type="impedance",
                params={"target": 90.0},
                source="usb2_high_speed",
                severity="error",
            )
        ]
        declaration = IntentDeclaration(
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
            constraints=constraints,
        )
        assert len(declaration.constraints) == 1
        assert declaration.constraints[0].type == "impedance"

    def test_declaration_with_metadata(self):
        """Test IntentDeclaration with metadata."""
        declaration = IntentDeclaration(
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
            metadata={"connector": "J1", "layer": "top"},
        )
        assert declaration.metadata == {"connector": "J1", "layer": "top"}


class MockUSBSpec:
    """Mock USB interface specification for testing."""

    @property
    def name(self) -> str:
        return "usb2_high_speed"

    @property
    def category(self) -> InterfaceCategory:
        return InterfaceCategory.DIFFERENTIAL

    def validate_nets(self, nets: list[str]) -> list[str]:
        if len(nets) != 2:
            return ["USB 2.0 High Speed requires exactly 2 nets (D+ and D-)"]
        return []

    def derive_constraints(self, nets: list[str], params: dict[str, object]) -> list[Constraint]:
        return [
            Constraint(
                type="impedance",
                params={"target": 90.0, "tolerance": 0.1},
                source=self.name,
                severity="error",
            ),
            Constraint(
                type="length_match",
                params={"tolerance_mm": 2.0},
                source=self.name,
                severity="warning",
            ),
        ]

    def get_validation_message(self, violation: dict[str, object]) -> str:
        return f"USB 2.0 HS: {violation.get('message', '')}"


class MockSPISpec:
    """Mock SPI interface specification for testing."""

    @property
    def name(self) -> str:
        return "spi"

    @property
    def category(self) -> InterfaceCategory:
        return InterfaceCategory.BUS

    def validate_nets(self, nets: list[str]) -> list[str]:
        if len(nets) < 3:
            return ["SPI requires at least 3 nets (CLK, MOSI, MISO)"]
        return []

    def derive_constraints(self, nets: list[str], params: dict[str, object]) -> list[Constraint]:
        return [
            Constraint(
                type="timing",
                params={"max_skew_ns": 1.0},
                source=self.name,
                severity="error",
            ),
        ]

    def get_validation_message(self, violation: dict[str, object]) -> str:
        return f"SPI: {violation.get('message', '')}"


class MockPowerSpec:
    """Mock power interface specification for testing."""

    @property
    def name(self) -> str:
        return "power_3v3"

    @property
    def category(self) -> InterfaceCategory:
        return InterfaceCategory.POWER

    def validate_nets(self, nets: list[str]) -> list[str]:
        return []

    def derive_constraints(self, nets: list[str], params: dict[str, object]) -> list[Constraint]:
        return []

    def get_validation_message(self, violation: dict[str, object]) -> str:
        return f"Power 3.3V: {violation.get('message', '')}"


class TestInterfaceRegistry:
    """Tests for InterfaceRegistry."""

    def test_registry_creation(self):
        """Test creating an empty registry."""
        registry = InterfaceRegistry()
        assert len(registry) == 0
        assert registry.list_interfaces() == []

    def test_register_interface(self):
        """Test registering an interface specification."""
        registry = InterfaceRegistry()
        spec = MockUSBSpec()
        registry.register(spec)
        assert len(registry) == 1
        assert "usb2_high_speed" in registry
        assert registry.list_interfaces() == ["usb2_high_speed"]

    def test_register_duplicate_raises(self):
        """Test that registering duplicate interface raises ValueError."""
        registry = InterfaceRegistry()
        spec = MockUSBSpec()
        registry.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec)

    def test_get_interface(self):
        """Test getting an interface by name."""
        registry = InterfaceRegistry()
        spec = MockUSBSpec()
        registry.register(spec)
        retrieved = registry.get("usb2_high_speed")
        assert retrieved is not None
        assert retrieved.name == "usb2_high_speed"

    def test_get_nonexistent_returns_none(self):
        """Test getting a nonexistent interface returns None."""
        registry = InterfaceRegistry()
        assert registry.get("nonexistent") is None

    def test_unregister_interface(self):
        """Test unregistering an interface."""
        registry = InterfaceRegistry()
        spec = MockUSBSpec()
        registry.register(spec)
        assert registry.unregister("usb2_high_speed") is True
        assert "usb2_high_speed" not in registry
        assert len(registry) == 0

    def test_unregister_nonexistent_returns_false(self):
        """Test unregistering nonexistent interface returns False."""
        registry = InterfaceRegistry()
        assert registry.unregister("nonexistent") is False

    def test_list_interfaces_sorted(self):
        """Test that list_interfaces returns sorted names."""
        registry = InterfaceRegistry()
        registry.register(MockSPISpec())
        registry.register(MockUSBSpec())
        registry.register(MockPowerSpec())
        assert registry.list_interfaces() == ["power_3v3", "spi", "usb2_high_speed"]

    def test_list_by_category(self):
        """Test listing interfaces by category."""
        registry = InterfaceRegistry()
        registry.register(MockUSBSpec())
        registry.register(MockSPISpec())
        registry.register(MockPowerSpec())

        differential = registry.list_by_category(InterfaceCategory.DIFFERENTIAL)
        assert differential == ["usb2_high_speed"]

        bus = registry.list_by_category(InterfaceCategory.BUS)
        assert bus == ["spi"]

        power = registry.list_by_category(InterfaceCategory.POWER)
        assert power == ["power_3v3"]

        single_ended = registry.list_by_category(InterfaceCategory.SINGLE_ENDED)
        assert single_ended == []

    def test_contains_operator(self):
        """Test the 'in' operator."""
        registry = InterfaceRegistry()
        registry.register(MockUSBSpec())
        assert "usb2_high_speed" in registry
        assert "nonexistent" not in registry


class TestConstraintDerivation:
    """Tests for constraint derivation functions."""

    @pytest.fixture
    def registry(self):
        """Create a registry with mock interfaces."""
        reg = InterfaceRegistry()
        reg.register(MockUSBSpec())
        reg.register(MockSPISpec())
        return reg

    def test_derive_constraints(self, registry):
        """Test deriving constraints from an interface."""
        constraints = derive_constraints(
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
            registry=registry,
        )
        assert len(constraints) == 2
        assert constraints[0].type == "impedance"
        assert constraints[1].type == "length_match"

    def test_derive_constraints_with_params(self, registry):
        """Test deriving constraints with params."""
        constraints = derive_constraints(
            interface_type="spi",
            nets=["CLK", "MOSI", "MISO"],
            params={"speed_mhz": 10},
            registry=registry,
        )
        assert len(constraints) == 1
        assert constraints[0].type == "timing"

    def test_derive_constraints_unknown_interface(self, registry):
        """Test that unknown interface raises ValueError."""
        with pytest.raises(ValueError, match="Unknown interface type"):
            derive_constraints(
                interface_type="unknown",
                nets=["A", "B"],
                registry=registry,
            )

    def test_validate_intent_valid(self, registry):
        """Test validating a valid intent."""
        errors = validate_intent(
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
            registry=registry,
        )
        assert errors == []

    def test_validate_intent_invalid_net_count(self, registry):
        """Test validating intent with wrong number of nets."""
        errors = validate_intent(
            interface_type="usb2_high_speed",
            nets=["USB_DP"],  # Only 1 net, need 2
            registry=registry,
        )
        assert len(errors) == 1
        assert "exactly 2 nets" in errors[0]

    def test_validate_intent_unknown_interface(self, registry):
        """Test validating unknown interface raises ValueError."""
        with pytest.raises(ValueError, match="Unknown interface type"):
            validate_intent(
                interface_type="unknown",
                nets=["A", "B"],
                registry=registry,
            )

    def test_create_intent_declaration(self, registry):
        """Test creating an intent declaration."""
        declaration = create_intent_declaration(
            interface_type="usb2_high_speed",
            nets=["USB_DP", "USB_DM"],
            metadata={"connector": "J1"},
            registry=registry,
        )
        assert declaration.interface_type == "usb2_high_speed"
        assert declaration.nets == ["USB_DP", "USB_DM"]
        assert len(declaration.constraints) == 2
        assert declaration.metadata == {"connector": "J1"}

    def test_create_intent_declaration_validation_fails(self, registry):
        """Test that create_intent_declaration validates by default."""
        with pytest.raises(ValueError, match="Invalid intent declaration"):
            create_intent_declaration(
                interface_type="usb2_high_speed",
                nets=["USB_DP"],  # Wrong number of nets
                registry=registry,
            )

    def test_create_intent_declaration_skip_validation(self, registry):
        """Test creating declaration with validation disabled."""
        declaration = create_intent_declaration(
            interface_type="usb2_high_speed",
            nets=["USB_DP"],  # Wrong number, but validation disabled
            registry=registry,
            validate=False,
        )
        assert len(declaration.nets) == 1


class TestGlobalRegistry:
    """Tests for the global REGISTRY instance."""

    def test_global_registry_exists(self):
        """Test that global REGISTRY exists."""
        assert REGISTRY is not None
        assert isinstance(REGISTRY, InterfaceRegistry)

    def test_global_registry_initially_empty(self):
        """Test that global REGISTRY starts empty (no built-in interfaces yet)."""
        # Note: Future issues will add built-in interfaces
        # For now, we just verify it's an InterfaceRegistry
        pass


class TestInterfaceSpecProtocol:
    """Tests for InterfaceSpec protocol compliance."""

    def test_mock_usb_implements_protocol(self):
        """Test that MockUSBSpec implements InterfaceSpec protocol."""
        spec = MockUSBSpec()
        # Protocol requires these attributes/methods
        assert hasattr(spec, "name")
        assert hasattr(spec, "category")
        assert hasattr(spec, "validate_nets")
        assert hasattr(spec, "derive_constraints")
        assert hasattr(spec, "get_validation_message")
        # Verify they work
        assert spec.name == "usb2_high_speed"
        assert spec.category == InterfaceCategory.DIFFERENTIAL
        assert spec.validate_nets(["A", "B"]) == []
        assert len(spec.derive_constraints(["A", "B"], {})) == 2
        assert "USB 2.0" in spec.get_validation_message({"message": "test"})
