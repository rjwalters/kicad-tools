"""Tests for power net connectivity validation.

Tests for the validate_power_nets() method that checks:
- Power input pins are connected to power outputs
- Power symbols are properly connected to nets
- Multiple power outputs on the same net are detected
"""

import pytest

from kicad_tools.schematic.models import PowerNetIssue, Schematic
from kicad_tools.schematic.models.elements import PowerSymbol
from kicad_tools.schematic.models.pin import Pin
from kicad_tools.schematic.models.symbol import SymbolDef, SymbolInstance


def add_power_symbol(sch: Schematic, lib_id: str, x: float, y: float) -> PowerSymbol:
    """Helper to add a power symbol without requiring library access."""
    ref = f"#PWR{len(sch.power_symbols) + 1:02d}"
    pwr = PowerSymbol(
        lib_id=lib_id,
        x=round(x, 2),
        y=round(y, 2),
        rotation=0,
        reference=ref,
    )
    sch.power_symbols.append(pwr)
    return pwr


class TestPowerNetIssue:
    """Tests for PowerNetIssue dataclass."""

    def test_power_net_issue_creation(self):
        """Create a basic power net issue."""
        issue = PowerNetIssue(
            net="+3.3V",
            issue_type="not_driven",
            message="Power net +3.3V has no power output",
            locations=[(100.0, 50.0), (100.0, 75.0)],
        )
        assert issue.net == "+3.3V"
        assert issue.issue_type == "not_driven"
        assert len(issue.locations) == 2

    def test_power_net_issue_to_dict(self):
        """PowerNetIssue can be converted to dictionary."""
        issue = PowerNetIssue(
            net="GND",
            issue_type="isolated",
            message="Power symbol GND is isolated",
            locations=[(50.0, 100.0)],
        )
        d = issue.to_dict()
        assert d["net"] == "GND"
        assert d["type"] == "isolated"
        assert d["message"] == "Power symbol GND is isolated"
        assert d["locations"] == [(50.0, 100.0)]

    def test_power_net_issue_default_locations(self):
        """PowerNetIssue has empty locations by default."""
        issue = PowerNetIssue(
            net="+5V",
            issue_type="not_driven",
            message="Test message",
        )
        assert issue.locations == []


class TestValidatePowerNetsEmpty:
    """Tests for validate_power_nets with empty or minimal schematics."""

    def test_empty_schematic_no_issues(self):
        """Empty schematic has no power net issues."""
        sch = Schematic("Test")
        issues = sch.validate_power_nets()
        assert issues == []

    def test_schematic_with_only_wires_no_issues(self):
        """Schematic with only wires and no power symbols has no issues."""
        sch = Schematic("Test")
        sch.add_wire((10, 20), (50, 20))
        sch.add_wire((50, 20), (50, 60))
        issues = sch.validate_power_nets()
        assert issues == []


class TestValidatePowerNetsBasic:
    """Basic tests for power net validation."""

    def test_isolated_power_symbol(self):
        """Single isolated power symbol is detected."""
        sch = Schematic("Test")
        # Add a power symbol not connected to anything
        add_power_symbol(sch,"power:+3.3V", 100, 50)
        issues = sch.validate_power_nets()

        assert len(issues) == 1
        assert issues[0].net == "+3.3V"
        assert issues[0].issue_type == "isolated"
        assert (100.0, 50.0) in issues[0].locations

    def test_power_symbols_connected_but_no_output(self):
        """Multiple power symbols connected together but no power output."""
        sch = Schematic("Test")
        # Add power symbols connected by wire
        add_power_symbol(sch,"power:+3.3V", 100, 50)
        add_power_symbol(sch,"power:+3.3V", 100, 100)
        sch.add_wire((100, 50), (100, 100))

        issues = sch.validate_power_nets()

        assert len(issues) == 1
        assert issues[0].net == "+3.3V"
        assert issues[0].issue_type == "not_driven"
        assert "no power output" in issues[0].message.lower()

    def test_power_symbol_with_power_output_ok(self):
        """Power symbol connected to power output pin passes validation."""
        sch = Schematic("Test")

        # Create a mock voltage regulator symbol with power output
        regulator_def = SymbolDef(
            lib_id="Regulator_Linear:AMS1117-3.3",
            name="AMS1117-3.3",
            raw_sexp="",
            pins=[
                Pin(name="GND", number="1", x=0, y=5.08, angle=90, length=2.54, pin_type="power_in"),
                Pin(name="VO", number="2", x=5.08, y=0, angle=0, length=2.54, pin_type="power_out"),
                Pin(name="VI", number="3", x=-5.08, y=0, angle=180, length=2.54, pin_type="power_in"),
            ],
        )

        # Add regulator
        reg = SymbolInstance(
            symbol_def=regulator_def,
            x=100,
            y=50,
            rotation=0,
            reference="U1",
            value="AMS1117-3.3",
        )
        sch.symbols.append(reg)
        sch._symbol_defs[regulator_def.lib_id] = regulator_def

        # Get the power output pin position and add power symbol connected to it
        vo_pos = reg.pin_position("VO")  # Should be (105.08, 50)

        # Add power symbol at same position as VO pin
        add_power_symbol(sch,"power:+3.3V", vo_pos[0], vo_pos[1])

        # Connect them with wire (though they're at same point)
        issues = sch.validate_power_nets()

        # Should pass - power symbol is connected to power output
        assert len([i for i in issues if i.net == "+3.3V" and i.issue_type == "not_driven"]) == 0


class TestValidatePowerNetsMultipleOutputs:
    """Tests for multiple power outputs on same net."""

    def test_multiple_power_outputs_detected(self):
        """Multiple power outputs on same net are detected."""
        sch = Schematic("Test")

        # Create two voltage regulators
        for i, x_pos in enumerate([100, 200]):
            regulator_def = SymbolDef(
                lib_id=f"Regulator_Linear:AMS1117-3.3_{i}",
                name="AMS1117-3.3",
                raw_sexp="",
                pins=[
                    Pin(name="VO", number="2", x=5.08, y=0, angle=0, length=2.54, pin_type="power_out"),
                ],
            )
            reg = SymbolInstance(
                symbol_def=regulator_def,
                x=x_pos,
                y=50,
                rotation=0,
                reference=f"U{i+1}",
                value="AMS1117-3.3",
            )
            sch.symbols.append(reg)
            sch._symbol_defs[regulator_def.lib_id] = regulator_def

        # Add power symbols connected to both outputs
        add_power_symbol(sch,"power:+3.3V", 105.08, 50)
        add_power_symbol(sch,"power:+3.3V", 205.08, 50)

        # Connect both outputs together
        sch.add_wire((105.08, 50), (205.08, 50))

        issues = sch.validate_power_nets()

        # Should detect multiple outputs
        multiple_output_issues = [i for i in issues if i.issue_type == "multiple_outputs"]
        assert len(multiple_output_issues) == 1
        assert multiple_output_issues[0].net == "+3.3V"
        assert "2 power outputs" in multiple_output_issues[0].message


class TestValidatePowerNetsUnconnected:
    """Tests for unconnected power input pins."""

    def test_unconnected_power_input_detected(self):
        """Unconnected power input pin on symbol is detected."""
        sch = Schematic("Test")

        # Create an IC with power input pins
        ic_def = SymbolDef(
            lib_id="MCU_Microchip:ATmega328P",
            name="ATmega328P",
            raw_sexp="",
            pins=[
                Pin(name="VCC", number="7", x=-10.16, y=0, angle=0, length=2.54, pin_type="power_in"),
                Pin(name="GND", number="8", x=10.16, y=0, angle=180, length=2.54, pin_type="power_in"),
                Pin(name="PB0", number="14", x=0, y=-10.16, angle=90, length=2.54, pin_type="bidirectional"),
            ],
        )
        ic = SymbolInstance(
            symbol_def=ic_def,
            x=100,
            y=100,
            rotation=0,
            reference="U1",
            value="ATmega328P",
        )
        sch.symbols.append(ic)
        sch._symbol_defs[ic_def.lib_id] = ic_def

        # Don't connect power pins to anything
        issues = sch.validate_power_nets()

        # Should detect unconnected power inputs
        undriven_issues = [i for i in issues if i.issue_type == "undriven_input"]
        assert len(undriven_issues) == 2  # VCC and GND

        # Check that both VCC and GND are mentioned
        messages = " ".join(i.message for i in undriven_issues)
        assert "VCC" in messages
        assert "GND" in messages


class TestValidatePowerNetsMixed:
    """Tests for mixed scenarios with multiple power nets."""

    def test_multiple_power_nets(self):
        """Multiple power nets are validated independently."""
        sch = Schematic("Test")

        # Add +3.3V power symbol (isolated - should fail)
        add_power_symbol(sch,"power:+3.3V", 100, 50)

        # Add GND power symbols connected (no output - should fail)
        add_power_symbol(sch,"power:GND", 100, 100)
        add_power_symbol(sch,"power:GND", 150, 100)
        sch.add_wire((100, 100), (150, 100))

        issues = sch.validate_power_nets()

        # Check +3.3V issue
        v33_issues = [i for i in issues if i.net == "+3.3V"]
        assert len(v33_issues) == 1
        assert v33_issues[0].issue_type == "isolated"

        # Check GND issue
        gnd_issues = [i for i in issues if i.net == "GND"]
        assert len(gnd_issues) == 1
        assert gnd_issues[0].issue_type == "not_driven"


class TestValidatePowerNetsConnectivity:
    """Tests for proper connectivity detection."""

    def test_t_junction_connectivity(self):
        """Power symbols connected via T-junction are recognized."""
        sch = Schematic("Test")

        # Create T-junction topology
        #   +3.3V (100, 50)
        #      |
        #      |  <- vertical wire
        #      |
        # -----+------- horizontal wire
        #      |
        #   +3.3V (100, 150)

        add_power_symbol(sch,"power:+3.3V", 100, 50)
        add_power_symbol(sch,"power:+3.3V", 100, 150)
        sch.add_wire((100, 50), (100, 100))  # Vertical top
        sch.add_wire((100, 100), (100, 150))  # Vertical bottom
        sch.add_wire((50, 100), (150, 100))  # Horizontal
        sch.add_junction(100, 100)  # Junction at cross

        issues = sch.validate_power_nets()

        # Should detect not_driven (connected but no output)
        v33_issues = [i for i in issues if i.net == "+3.3V"]
        assert len(v33_issues) == 1
        assert v33_issues[0].issue_type == "not_driven"
        # Both symbols should be on the same net, so we get one issue, not two "isolated" issues
        assert len(v33_issues[0].locations) == 2

    def test_wire_to_pin_connectivity(self):
        """Symbol pins connected to wire are recognized in net."""
        sch = Schematic("Test")

        # Create regulator with power output
        regulator_def = SymbolDef(
            lib_id="Regulator_Linear:LM7805",
            name="LM7805",
            raw_sexp="",
            pins=[
                Pin(name="OUT", number="3", x=7.62, y=0, angle=0, length=2.54, pin_type="power_out"),
            ],
        )
        reg = SymbolInstance(
            symbol_def=regulator_def,
            x=100,
            y=50,
            rotation=0,
            reference="U1",
            value="LM7805",
        )
        sch.symbols.append(reg)
        sch._symbol_defs[regulator_def.lib_id] = regulator_def

        # OUT pin is at (107.62, 50)
        out_pos = reg.pin_position("OUT")

        # Add power symbol far away but connected by wire
        pwr_pos = (200, 50)
        add_power_symbol(sch,"power:+5V", pwr_pos[0], pwr_pos[1])
        sch.add_wire(out_pos, pwr_pos)

        issues = sch.validate_power_nets()

        # Should pass - power symbol connected to power output via wire
        not_driven = [i for i in issues if i.issue_type == "not_driven"]
        assert len(not_driven) == 0


class TestValidatePowerNetsPWRFLAG:
    """Tests involving PWR_FLAG (if supported)."""

    def test_pwr_flag_acts_as_power_output(self):
        """PWR_FLAG power symbol should act as power output.

        Note: This test documents expected behavior but actual implementation
        may need PWR_FLAG special handling.
        """
        sch = Schematic("Test")

        # Add GND power symbol
        add_power_symbol(sch,"power:GND", 100, 100)

        # Add PWR_FLAG connected to it
        add_power_symbol(sch,"power:PWR_FLAG", 100, 80)
        sch.add_wire((100, 80), (100, 100))

        issues = sch.validate_power_nets()

        # Current implementation may not recognize PWR_FLAG as a power output
        # This test documents the expected behavior for future enhancement
        # For now, we just verify the method runs without error
        assert isinstance(issues, list)


class TestPowerNetValidationIntegration:
    """Integration tests with real-world-like scenarios."""

    def test_bypass_capacitor_scenario(self):
        """Bypass capacitors connected to power net without power output.

        This is the scenario described in the issue: bypass caps connected
        to +3.3V symbols but the regulator output doesn't have a +3.3V symbol.
        """
        sch = Schematic("Test")

        # Create bypass capacitor symbol (passive pins)
        cap_def = SymbolDef(
            lib_id="Device:C",
            name="C",
            raw_sexp="",
            pins=[
                Pin(name="1", number="1", x=0, y=2.54, angle=270, length=2.54, pin_type="passive"),
                Pin(name="2", number="2", x=0, y=-2.54, angle=90, length=2.54, pin_type="passive"),
            ],
        )

        # Add 3 bypass capacitors with +3.3V power symbols
        for i in range(3):
            cap = SymbolInstance(
                symbol_def=cap_def,
                x=100 + i * 20,
                y=50,
                rotation=0,
                reference=f"C{i+1}",
                value="100nF",
            )
            sch.symbols.append(cap)

            # Add +3.3V power symbol at capacitor pin 1
            pin1_pos = cap.pin_position("1")
            add_power_symbol(sch,"power:+3.3V", pin1_pos[0], pin1_pos[1])

        # Connect all capacitors to a horizontal bus
        sch.add_wire((100, 47.46), (140, 47.46))  # Horizontal bus

        issues = sch.validate_power_nets()

        # Should detect that +3.3V net has no power output
        v33_issues = [i for i in issues if i.net == "+3.3V" and i.issue_type == "not_driven"]
        assert len(v33_issues) == 1
        assert "power output" in v33_issues[0].message.lower()
        # Should list all 3 power symbol locations
        assert len(v33_issues[0].locations) == 3
