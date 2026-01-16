"""
Unit tests for the patterns module.

Tests cover:
- Schema types (Placement, PlacementRule, PatternSpec)
- PCBPattern base class functionality
- Power patterns (LDOPattern, BuckPattern)
- Timing patterns (CrystalPattern, OscillatorPattern)
- Interface patterns (USBPattern, I2CPattern)
"""

from kicad_tools.patterns import (
    BuckPattern,
    CrystalPattern,
    I2CPattern,
    LDOPattern,
    OscillatorPattern,
    PatternSpec,
    PatternViolation,
    Placement,
    PlacementPriority,
    PlacementRule,
    RoutingConstraint,
    USBPattern,
)


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
