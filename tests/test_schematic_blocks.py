"""Tests for schematic circuit blocks."""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import (
    BarrelJackInput,
    BatteryInput,
    CircuitBlock,
    CrystalOscillator,
    DebugHeader,
    DecouplingCaps,
    LDOBlock,
    LEDIndicator,
    MCUBlock,
    OscillatorBlock,
    Port,
    USBConnector,
    USBPowerInput,
    create_3v3_ldo,
    create_12v_barrel_jack,
    create_jtag_header,
    create_lipo_battery,
    create_mclk_oscillator,
    create_power_led,
    create_status_led,
    create_swd_header,
    create_tag_connect_header,
    create_usb_micro_b,
    create_usb_power,
    create_usb_type_c,
)


class TestPort:
    """Tests for Port dataclass."""

    def test_port_creation(self):
        """Create port with all fields."""
        port = Port(name="VCC", x=10.0, y=20.0, direction="power")
        assert port.name == "VCC"
        assert port.x == 10.0
        assert port.y == 20.0
        assert port.direction == "power"

    def test_port_default_direction(self):
        """Default port direction is passive."""
        port = Port(name="A", x=0, y=0)
        assert port.direction == "passive"

    def test_port_pos(self):
        """Get position as tuple."""
        port = Port(name="VCC", x=10.0, y=20.0)
        assert port.pos() == (10.0, 20.0)


class TestCircuitBlock:
    """Tests for CircuitBlock base class."""

    def test_circuit_block_init(self):
        """Initialize circuit block."""
        block = CircuitBlock()
        assert block.schematic is None
        assert block.x == 0
        assert block.y == 0
        assert block.ports == {}
        assert block.components == {}

    def test_port_lookup(self):
        """Look up port by name."""
        block = CircuitBlock()
        block.ports = {"VCC": (10.0, 20.0), "GND": (10.0, 30.0)}

        assert block.port("VCC") == (10.0, 20.0)
        assert block.port("GND") == (10.0, 30.0)

    def test_port_not_found(self):
        """KeyError when port not found."""
        block = CircuitBlock()
        block.ports = {"VCC": (10.0, 20.0)}

        with pytest.raises(KeyError) as exc:
            block.port("MISSING")
        assert "MISSING" in str(exc.value)
        assert "Available" in str(exc.value)


class TestLEDIndicatorMocked:
    """Tests for LEDIndicator with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()
        # Mock add_symbol to return symbol with pin_position method
        mock_led = Mock()
        mock_led.pin_position.side_effect = lambda name: {"A": (100, 95), "K": (100, 105)}.get(
            name, (0, 0)
        )

        mock_resistor = Mock()
        mock_resistor.pin_position.side_effect = lambda name: {
            "1": (100, 110),
            "2": (100, 120),
        }.get(name, (0, 0))

        def mock_add_symbol(symbol, x, y, ref, *args, **kwargs):
            if "LED" in symbol:
                return mock_led
            else:
                return mock_resistor

        sch.add_symbol = Mock(side_effect=mock_add_symbol)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_led_indicator_creation(self, mock_schematic):
        """Create LED indicator."""
        led = LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")

        assert led.schematic == mock_schematic
        assert led.x == 100
        assert led.y == 100
        assert "VCC" in led.ports
        assert "GND" in led.ports
        assert "LED" in led.components
        assert "R" in led.components

    def test_led_indicator_adds_wire(self, mock_schematic):
        """LED indicator wires LED to resistor."""
        LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")
        # Verify add_wire was called
        assert mock_schematic.add_wire.called

    def test_led_connect_to_rails(self, mock_schematic):
        """Connect LED to power rails."""
        led = LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")
        led.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150)

        # Should add wires to connect to rails
        wire_calls = mock_schematic.add_wire.call_count
        assert wire_calls >= 2  # VCC wire and GND wire

    def test_led_connect_with_junctions(self, mock_schematic):
        """Connect LED adds junctions when requested."""
        led = LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")
        led.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150, add_junctions=True)

        # Should add junctions
        assert mock_schematic.add_junction.called


class TestDecouplingCapsMocked:
    """Tests for DecouplingCaps with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_cap(symbol, x, y, ref, *args, **kwargs):
            cap = Mock()
            cap.pin_position.side_effect = lambda name: {"1": (x, y - 5), "2": (x, y + 5)}.get(
                name, (0, 0)
            )
            return cap

        sch.add_symbol = Mock(side_effect=create_mock_cap)
        sch.add_wire = Mock()
        sch.wire_decoupling_cap = Mock()
        return sch

    def test_decoupling_caps_creation(self, mock_schematic):
        """Create decoupling capacitor bank."""
        caps = DecouplingCaps(mock_schematic, x=100, y=100, values=["10uF", "100nF"], ref_start=1)

        assert len(caps.caps) == 2
        assert "VCC" in caps.ports
        assert "GND" in caps.ports
        assert "VCC_END" in caps.ports
        assert "GND_END" in caps.ports

    def test_decoupling_caps_components(self, mock_schematic):
        """Components dict has all caps."""
        caps = DecouplingCaps(
            mock_schematic, x=100, y=100, values=["10uF", "100nF", "10nF"], ref_start=1
        )

        assert "C1" in caps.components
        assert "C2" in caps.components
        assert "C3" in caps.components

    def test_decoupling_caps_connect_to_rails(self, mock_schematic):
        """Connect caps to rails."""
        caps = DecouplingCaps(mock_schematic, x=100, y=100, values=["10uF", "100nF"], ref_start=1)
        caps.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150)

        # Should wire each cap
        assert mock_schematic.wire_decoupling_cap.call_count == 2


class TestLDOBlockMocked:
    """Tests for LDOBlock with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            # LDO pins
            if "LDO" in str(symbol) or "Regulator" in str(symbol) or "AP2204" in str(symbol):
                comp.pin_position.side_effect = lambda name: {
                    "VIN": (x - 10, y),
                    "VOUT": (x + 10, y),
                    "GND": (x, y + 10),
                    "EN": (x - 5, y + 5),
                }.get(name, (0, 0))
            else:
                # Capacitor pins
                comp.pin_position.side_effect = lambda name: {"1": (x, y - 5), "2": (x, y + 5)}.get(
                    name, (0, 0)
                )
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.wire_to_rail = Mock()
        sch.wire_decoupling_cap = Mock()
        sch.add_rail = Mock()
        return sch

    def test_ldo_block_creation(self, mock_schematic):
        """Create LDO block."""
        ldo = LDOBlock(mock_schematic, x=100, y=100, ref="U1", value="3.3V")

        assert ldo.schematic == mock_schematic
        assert "VIN" in ldo.ports
        assert "VOUT" in ldo.ports
        assert "GND" in ldo.ports
        assert "EN" in ldo.ports

    def test_ldo_block_components(self, mock_schematic):
        """LDO block has all components."""
        ldo = LDOBlock(mock_schematic, x=100, y=100, ref="U1", output_caps=["10uF", "100nF"])

        assert "LDO" in ldo.components
        assert "C_IN" in ldo.components
        assert "C_OUT1" in ldo.components
        assert "C_OUT2" in ldo.components

    def test_ldo_en_tied_to_vin(self, mock_schematic):
        """EN pin tied to VIN when requested."""
        LDOBlock(mock_schematic, x=100, y=100, ref="U1", en_tied_to_vin=True)

        # Should add wire connecting EN to VIN level
        assert mock_schematic.add_wire.called

    def test_ldo_connect_to_rails(self, mock_schematic):
        """Connect LDO to power rails."""
        ldo = LDOBlock(mock_schematic, x=100, y=100, ref="U1")
        ldo.connect_to_rails(vin_rail_y=50, vout_rail_y=60, gnd_rail_y=150)

        # Should wire LDO pins to rails
        assert mock_schematic.wire_to_rail.called

    def test_ldo_extend_vout_rail(self, mock_schematic):
        """Extend VOUT rail when requested."""
        ldo = LDOBlock(mock_schematic, x=100, y=100, ref="U1")
        ldo.connect_to_rails(vin_rail_y=50, vout_rail_y=60, gnd_rail_y=150, extend_vout_rail_to=200)

        # Should add rail extension
        assert mock_schematic.add_rail.called


class TestOscillatorBlockMocked:
    """Tests for OscillatorBlock with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            if "Oscillator" in str(symbol) or "ASE" in str(symbol):
                comp.pin_position.side_effect = lambda name: {
                    "Vdd": (x, y - 5),
                    "GND": (x, y + 5),
                    "OUT": (x + 10, y),
                    "EN": (x - 10, y),
                }.get(name, (0, 0))
            else:
                comp.pin_position.side_effect = lambda name: {"1": (x, y - 5), "2": (x, y + 5)}.get(
                    name, (0, 0)
                )
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        sch.wire_decoupling_cap = Mock()
        return sch

    def test_oscillator_block_creation(self, mock_schematic):
        """Create oscillator block."""
        osc = OscillatorBlock(mock_schematic, x=100, y=100, ref="Y1", value="24.576MHz")

        assert "VCC" in osc.ports
        assert "GND" in osc.ports
        assert "OUT" in osc.ports
        assert "EN" in osc.ports

    def test_oscillator_block_components(self, mock_schematic):
        """Oscillator block has osc and cap."""
        osc = OscillatorBlock(mock_schematic, x=100, y=100, ref="Y1")

        assert "OSC" in osc.components
        assert "C" in osc.components

    def test_oscillator_connect_to_rails(self, mock_schematic):
        """Connect oscillator to rails."""
        osc = OscillatorBlock(mock_schematic, x=100, y=100, ref="Y1")
        osc.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150)

        # Should add wires for Vdd and GND
        assert mock_schematic.add_wire.call_count >= 2

    def test_oscillator_ties_en_to_vcc(self, mock_schematic):
        """EN tied to VCC when en_tied_to_vcc=True."""
        osc = OscillatorBlock(mock_schematic, x=100, y=100, ref="Y1", en_tied_to_vcc=True)
        osc.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150)

        # Should add wire for EN to VCC rail
        assert mock_schematic.add_wire.call_count >= 3


class TestCrystalOscillatorMocked:
    """Tests for CrystalOscillator with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            if "Crystal" in str(symbol):
                # Crystal has pins 1 and 2 on left and right
                comp.pin_position.side_effect = lambda name: {
                    "1": (x - 5, y),
                    "2": (x + 5, y),
                }.get(name, (0, 0))
            else:
                # Capacitor has pins 1 (top) and 2 (bottom)
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5),
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_crystal_oscillator_creation(self, mock_schematic):
        """Create crystal oscillator."""
        xtal = CrystalOscillator(mock_schematic, x=100, y=100, frequency="8MHz", load_caps="20pF")

        assert xtal.schematic == mock_schematic
        assert xtal.x == 100
        assert xtal.y == 100
        assert "IN" in xtal.ports
        assert "OUT" in xtal.ports
        assert "GND" in xtal.ports

    def test_crystal_oscillator_components(self, mock_schematic):
        """Crystal oscillator has crystal and two caps."""
        xtal = CrystalOscillator(mock_schematic, x=100, y=100)

        assert "XTAL" in xtal.components
        assert "C1" in xtal.components
        assert "C2" in xtal.components

    def test_crystal_oscillator_wires_caps_to_crystal(self, mock_schematic):
        """Crystal oscillator wires load caps to crystal."""
        CrystalOscillator(mock_schematic, x=100, y=100)

        # Should add wires for crystal-to-cap and cap-to-cap ground
        assert mock_schematic.add_wire.call_count >= 5

    def test_crystal_oscillator_adds_junctions(self, mock_schematic):
        """Crystal oscillator adds junctions at connection points."""
        CrystalOscillator(mock_schematic, x=100, y=100)

        # Should add junctions at crystal-to-cap connections
        assert mock_schematic.add_junction.call_count >= 2

    def test_crystal_oscillator_different_cap_values(self, mock_schematic):
        """Crystal oscillator supports different cap values."""
        xtal = CrystalOscillator(mock_schematic, x=100, y=100, load_caps=("18pF", "22pF"))

        # Should create both caps
        assert "C1" in xtal.components
        assert "C2" in xtal.components

    def test_crystal_oscillator_custom_ref_prefix(self, mock_schematic):
        """Crystal oscillator uses custom reference prefix."""
        CrystalOscillator(mock_schematic, x=100, y=100, ref_prefix="Y2")

        # Check add_symbol was called with Y2 reference
        calls = mock_schematic.add_symbol.call_args_list
        crystal_call = [c for c in calls if "Crystal" in str(c)]
        assert len(crystal_call) >= 1

    def test_crystal_oscillator_connect_to_rails(self, mock_schematic):
        """Crystal oscillator connects to ground rail."""
        xtal = CrystalOscillator(mock_schematic, x=100, y=100)
        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        xtal.connect_to_rails(gnd_rail_y=150)

        # Should add wire to ground rail
        assert mock_schematic.add_wire.called
        assert mock_schematic.add_junction.called

    def test_crystal_oscillator_connect_without_junction(self, mock_schematic):
        """Crystal oscillator can connect without junction."""
        xtal = CrystalOscillator(mock_schematic, x=100, y=100)
        mock_schematic.add_junction.reset_mock()

        xtal.connect_to_rails(gnd_rail_y=150, add_junction=False)

        # Should not add junction
        assert not mock_schematic.add_junction.called

    def test_crystal_oscillator_port_positions(self, mock_schematic):
        """Crystal oscillator ports have correct positions."""
        xtal = CrystalOscillator(mock_schematic, x=100, y=100)

        # IN and OUT should be tuples
        assert isinstance(xtal.ports["IN"], tuple)
        assert isinstance(xtal.ports["OUT"], tuple)
        assert isinstance(xtal.ports["GND"], tuple)

        # IN should be on left (lower x), OUT on right (higher x)
        assert xtal.ports["IN"][0] < xtal.ports["OUT"][0]


class TestDebugHeaderMocked:
    """Tests for DebugHeader with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic with support for all header types."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            # Generate pin positions based on symbol type
            if "Conn" in symbol or "Connector" in symbol:
                # Header pins
                def header_pin_pos(name):
                    try:
                        pin_num = int(name)
                        return (x - 10, y + (pin_num - 1) * 2.54)
                    except ValueError:
                        return (x, y)

                comp.pin_position = Mock(side_effect=header_pin_pos)
            else:
                # Resistor pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x - 5, y),
                    "2": (x + 5, y),
                }.get(name, (x, y))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_debug_header_swd_10pin(self, mock_schematic):
        """Create 10-pin SWD debug header (default)."""
        header = DebugHeader(mock_schematic, x=100, y=100, ref="J1")

        assert header.interface == "swd"
        assert header.pins == 10
        assert "VCC" in header.ports
        assert "SWDIO" in header.ports
        assert "SWCLK" in header.ports
        assert "GND" in header.ports
        assert "SWO" in header.ports
        assert "NRST" in header.ports
        assert "HEADER" in header.components

    def test_debug_header_swd_6pin(self, mock_schematic):
        """Create 6-pin SWD debug header."""
        header = DebugHeader(mock_schematic, x=100, y=100, interface="swd", pins=6, ref="J1")

        assert header.interface == "swd"
        assert header.pins == 6
        assert "VCC" in header.ports
        assert "SWDIO" in header.ports
        assert "SWCLK" in header.ports
        assert "GND" in header.ports
        assert "NRST" in header.ports
        # 6-pin doesn't have SWO
        assert "SWO" not in header.ports

    def test_debug_header_jtag(self, mock_schematic):
        """Create 20-pin JTAG debug header."""
        header = DebugHeader(mock_schematic, x=100, y=100, interface="jtag", pins=20, ref="J1")

        assert header.interface == "jtag"
        assert header.pins == 20
        assert "VCC" in header.ports
        assert "GND" in header.ports
        assert "TDI" in header.ports
        assert "TDO" in header.ports
        assert "TMS" in header.ports
        assert "TCK" in header.ports
        assert "TRST" in header.ports
        assert "NRST" in header.ports
        assert "RTCK" in header.ports

    def test_debug_header_tag_connect_10pin(self, mock_schematic):
        """Create 10-pin Tag-Connect debug header."""
        header = DebugHeader(
            mock_schematic, x=100, y=100, interface="tag-connect", pins=10, ref="J1"
        )

        assert header.interface == "tag-connect"
        assert header.pins == 10
        assert "VCC" in header.ports
        assert "SWDIO" in header.ports
        assert "SWCLK" in header.ports
        assert "GND" in header.ports

    def test_debug_header_tag_connect_6pin(self, mock_schematic):
        """Create 6-pin Tag-Connect debug header."""
        header = DebugHeader(
            mock_schematic, x=100, y=100, interface="tag-connect", pins=6, ref="J1"
        )

        assert header.interface == "tag-connect"
        assert header.pins == 6

    def test_debug_header_with_series_resistors(self, mock_schematic):
        """Create debug header with series resistors."""
        header = DebugHeader(
            mock_schematic,
            x=100,
            y=100,
            interface="swd",
            pins=10,
            series_resistors=True,
            ref="J1",
        )

        assert header.series_resistors is True
        # Should have resistors for protected signals
        assert len(header.resistors) > 0
        # Components should include resistors
        assert any("R_" in k for k in header.components.keys())
        # Wires should be added to connect resistors to header
        assert mock_schematic.add_wire.called

    def test_debug_header_without_series_resistors(self, mock_schematic):
        """Create debug header without series resistors."""
        header = DebugHeader(
            mock_schematic,
            x=100,
            y=100,
            interface="swd",
            pins=10,
            series_resistors=False,
            ref="J1",
        )

        assert header.series_resistors is False
        assert len(header.resistors) == 0

    def test_debug_header_invalid_interface(self, mock_schematic):
        """Invalid interface raises ValueError."""
        with pytest.raises(ValueError) as exc:
            DebugHeader(mock_schematic, x=100, y=100, interface="invalid", ref="J1")
        assert "Invalid interface" in str(exc.value)

    def test_debug_header_invalid_pins_for_swd(self, mock_schematic):
        """Invalid pin count for SWD raises ValueError."""
        with pytest.raises(ValueError) as exc:
            DebugHeader(mock_schematic, x=100, y=100, interface="swd", pins=20, ref="J1")
        assert "Invalid pin count" in str(exc.value)

    def test_debug_header_invalid_pins_for_jtag(self, mock_schematic):
        """Invalid pin count for JTAG raises ValueError."""
        with pytest.raises(ValueError) as exc:
            DebugHeader(mock_schematic, x=100, y=100, interface="jtag", pins=10, ref="J1")
        assert "Invalid pin count" in str(exc.value)

    def test_debug_header_connect_to_rails(self, mock_schematic):
        """Connect debug header to power rails."""
        header = DebugHeader(mock_schematic, x=100, y=100, ref="J1")
        header.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150)

        # Should add wires for VCC and GND
        assert mock_schematic.add_wire.called
        # Should add junctions by default
        assert mock_schematic.add_junction.called

    def test_debug_header_connect_no_junctions(self, mock_schematic):
        """Connect debug header without junctions."""
        header = DebugHeader(mock_schematic, x=100, y=100, ref="J1")
        mock_schematic.add_junction.reset_mock()
        header.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150, add_junctions=False)

        # Should not add junctions
        assert not mock_schematic.add_junction.called

    def test_debug_header_value_labels(self, mock_schematic):
        """Debug header value labels match interface type."""
        # Create headers of different types to verify they initialize correctly
        DebugHeader(mock_schematic, x=100, y=100, interface="swd", pins=10)
        DebugHeader(mock_schematic, x=100, y=100, interface="jtag", pins=20)
        DebugHeader(mock_schematic, x=100, y=100, interface="tag-connect", pins=10)

        # Verify add_symbol was called for each header
        assert mock_schematic.add_symbol.call_count >= 3

    def test_debug_header_custom_resistor_value(self, mock_schematic):
        """Custom resistor value for series resistors."""
        header = DebugHeader(
            mock_schematic,
            x=100,
            y=100,
            series_resistors=True,
            resistor_value="22R",
            ref="J1",
        )

        # Verify resistors were created
        assert len(header.resistors) > 0


class TestMCUBlockMocked:
    """Tests for MCUBlock with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic with MCU support."""
        sch = Mock()

        def create_mock_pin(pin_name, pin_number, pin_type):
            """Create a mock pin with proper attribute access."""
            pin = Mock()
            pin.name = pin_name
            pin.number = pin_number
            pin.pin_type = pin_type
            return pin

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()

            # Check if this is an MCU symbol
            if "MCU" in str(symbol) or "STM32" in str(symbol):
                # Create mock symbol_def with pins for MCU
                mock_symbol_def = Mock()
                mock_pins = [
                    create_mock_pin("VDD", "1", "power_in"),
                    create_mock_pin("VDDA", "2", "power_in"),
                    create_mock_pin("GND", "3", "power_in"),
                    create_mock_pin("VSS", "4", "power_in"),
                    create_mock_pin("PA0", "5", "bidirectional"),
                    create_mock_pin("PA1", "6", "bidirectional"),
                    create_mock_pin("PB0", "7", "bidirectional"),
                    create_mock_pin("NRST", "8", "input"),
                    create_mock_pin("BOOT0", "9", "input"),
                ]
                mock_symbol_def.pins = mock_pins
                comp.symbol_def = mock_symbol_def

                comp.pin_position.side_effect = lambda name: {
                    "VDD": (x - 20, y - 10),
                    "VDDA": (x - 20, y - 5),
                    "GND": (x - 20, y + 10),
                    "VSS": (x - 20, y + 5),
                    "PA0": (x + 20, y - 10),
                    "PA1": (x + 20, y - 5),
                    "PB0": (x + 20, y),
                    "NRST": (x - 20, y),
                    "BOOT0": (x - 20, y + 15),
                }.get(name, (x, y))
            else:
                # Capacitor pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5),
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        sch.wire_decoupling_cap = Mock()
        return sch

    def test_mcu_block_creation(self, mock_schematic):
        """Create MCU block."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        assert mcu.schematic == mock_schematic
        assert mcu.x == 100
        assert mcu.y == 100
        assert "VDD" in mcu.ports
        assert "GND" in mcu.ports
        assert "MCU" in mcu.components

    def test_mcu_block_with_custom_bypass_caps(self, mock_schematic):
        """Create MCU block with custom bypass cap values."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            bypass_caps=["100nF", "100nF", "4.7uF"],
            ref="U1",
        )

        assert len(mcu.bypass_caps) == 3
        assert "C1" in mcu.components
        assert "C2" in mcu.components
        assert "C3" in mcu.components

    def test_mcu_block_default_bypass_caps(self, mock_schematic):
        """MCU block has default bypass caps when not specified."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        # Default is 4 x 100nF caps
        assert len(mcu.bypass_caps) == 4

    def test_mcu_block_identifies_power_pins(self, mock_schematic):
        """MCU block identifies VDD and GND pins."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        # Should have found VDD and VDDA
        assert len(mcu.vdd_pins) >= 1
        assert "VDD" in mcu.vdd_pins or "VDDA" in mcu.vdd_pins

        # Should have found GND and VSS
        assert len(mcu.gnd_pins) >= 1
        assert "GND" in mcu.gnd_pins or "VSS" in mcu.gnd_pins

    def test_mcu_block_exposes_gpio_pins(self, mock_schematic):
        """MCU block exposes GPIO pins as ports."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        # GPIO pins should be available as ports
        assert "PA0" in mcu.ports
        assert "PA1" in mcu.ports
        assert "PB0" in mcu.ports
        assert "NRST" in mcu.ports

    def test_mcu_block_wires_bypass_caps(self, mock_schematic):
        """MCU block wires bypass caps together."""
        MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            bypass_caps=["100nF", "100nF"],
            ref="U1",
        )

        # Should add wires between caps
        assert mock_schematic.add_wire.called

    def test_mcu_block_connect_to_rails(self, mock_schematic):
        """Connect MCU and bypass caps to power rails."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )
        mcu.connect_to_rails(vdd_rail_y=50, gnd_rail_y=150)

        # Should wire bypass caps to rails
        assert mock_schematic.wire_decoupling_cap.called

        # Should add wires and junctions for MCU power pins
        assert mock_schematic.add_wire.called
        assert mock_schematic.add_junction.called

    def test_mcu_block_get_gpio_pins(self, mock_schematic):
        """Get list of GPIO pins."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        gpio_pins = mcu.get_gpio_pins()

        # Should include non-power pins
        assert "PA0" in gpio_pins
        assert "PA1" in gpio_pins
        assert "NRST" in gpio_pins

        # Should NOT include power pins
        assert "VDD" not in gpio_pins
        assert "GND" not in gpio_pins
        assert "VSS" not in gpio_pins

    def test_mcu_block_get_power_pins(self, mock_schematic):
        """Get dict of power pins."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        power_pins = mcu.get_power_pins()

        assert "VDD" in power_pins
        assert "GND" in power_pins
        assert len(power_pins["VDD"]) >= 1
        assert len(power_pins["GND"]) >= 1

    def test_mcu_block_with_unit(self, mock_schematic):
        """MCU block supports multi-unit symbols."""
        MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
            unit=2,
        )

        # add_symbol should be called with unit parameter
        add_symbol_calls = mock_schematic.add_symbol.call_args_list
        # First call should be for the MCU
        mcu_call = add_symbol_calls[0]
        assert mcu_call.kwargs.get("unit") == 2 or 2 in mcu_call.args

    def test_mcu_block_custom_cap_positions(self, mock_schematic):
        """MCU block allows custom cap positioning."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
            cap_offset_x=-40,
            cap_offset_y=30,
            cap_spacing=15,
        )

        # Verify caps were created at expected positions
        assert len(mcu.bypass_caps) == 4

    def test_mcu_block_default_value_from_symbol(self, mock_schematic):
        """MCU block uses symbol name as default value."""
        MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        # add_symbol should be called with the symbol name as value
        add_symbol_calls = mock_schematic.add_symbol.call_args_list
        mcu_call = add_symbol_calls[0]
        # Value is the 5th argument (after symbol, x, y, ref)
        assert "STM32F103C8Tx" in str(mcu_call)

    def test_mcu_block_port_lookup(self, mock_schematic):
        """MCU block port() method works."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        # Should be able to look up ports
        vdd_pos = mcu.port("VDD")
        assert isinstance(vdd_pos, tuple)
        assert len(vdd_pos) == 2

        pa0_pos = mcu.port("PA0")
        assert isinstance(pa0_pos, tuple)

    def test_mcu_block_port_not_found(self, mock_schematic):
        """MCU block raises KeyError for unknown port."""
        mcu = MCUBlock(
            mock_schematic,
            x=100,
            y=100,
            mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
            ref="U1",
        )

        with pytest.raises(KeyError) as exc:
            mcu.port("NONEXISTENT")
        assert "NONEXISTENT" in str(exc.value)


class TestFactoryFunctions:
    """Tests for circuit block factory functions."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            # Handle different component types
            if "Conn" in symbol or "Connector" in symbol:
                # Header with numbered pins
                def header_pin_pos(name):
                    try:
                        pin_num = int(name)
                        return (x - 10, y + (pin_num - 1) * 2.54)
                    except ValueError:
                        return (x, y)

                comp.pin_position = Mock(side_effect=header_pin_pos)
            else:
                comp.pin_position.return_value = (x, y)
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        return sch

    def test_create_power_led(self, mock_schematic):
        """Create power LED."""
        led = create_power_led(mock_schematic, x=100, y=100, ref="D1")
        assert isinstance(led, LEDIndicator)

    def test_create_status_led(self, mock_schematic):
        """Create status LED."""
        led = create_status_led(mock_schematic, x=100, y=100, ref="D2")
        assert isinstance(led, LEDIndicator)

    def test_create_3v3_ldo(self, mock_schematic):
        """Create 3.3V LDO."""
        ldo = create_3v3_ldo(mock_schematic, x=100, y=100, ref="U1")
        assert isinstance(ldo, LDOBlock)

    def test_create_mclk_oscillator(self, mock_schematic):
        """Create MCLK oscillator."""
        osc = create_mclk_oscillator(mock_schematic, x=100, y=100, ref="Y1")
        assert isinstance(osc, OscillatorBlock)

    def test_create_swd_header(self, mock_schematic):
        """Create SWD debug header."""
        header = create_swd_header(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(header, DebugHeader)
        assert header.interface == "swd"
        assert header.pins == 10  # default

    def test_create_swd_header_6pin(self, mock_schematic):
        """Create 6-pin SWD debug header."""
        header = create_swd_header(mock_schematic, x=100, y=100, ref="J1", pins=6)
        assert isinstance(header, DebugHeader)
        assert header.pins == 6

    def test_create_swd_header_with_protection(self, mock_schematic):
        """Create SWD debug header with protection resistors."""
        header = create_swd_header(mock_schematic, x=100, y=100, ref="J1", with_protection=True)
        assert isinstance(header, DebugHeader)
        assert header.series_resistors is True

    def test_create_jtag_header(self, mock_schematic):
        """Create JTAG debug header."""
        header = create_jtag_header(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(header, DebugHeader)
        assert header.interface == "jtag"
        assert header.pins == 20

    def test_create_jtag_header_with_protection(self, mock_schematic):
        """Create JTAG debug header with protection resistors."""
        header = create_jtag_header(mock_schematic, x=100, y=100, ref="J1", with_protection=True)
        assert isinstance(header, DebugHeader)
        assert header.series_resistors is True

    def test_create_tag_connect_header(self, mock_schematic):
        """Create Tag-Connect debug header."""
        header = create_tag_connect_header(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(header, DebugHeader)
        assert header.interface == "tag-connect"
        assert header.pins == 10  # default

    def test_create_tag_connect_header_6pin(self, mock_schematic):
        """Create 6-pin Tag-Connect debug header."""
        header = create_tag_connect_header(mock_schematic, x=100, y=100, ref="J1", pins=6)
        assert isinstance(header, DebugHeader)
        assert header.pins == 6

    def test_create_tag_connect_header_with_protection(self, mock_schematic):
        """Create Tag-Connect debug header with protection resistors."""
        header = create_tag_connect_header(
            mock_schematic, x=100, y=100, ref="J1", with_protection=True
        )
        assert isinstance(header, DebugHeader)
        assert header.series_resistors is True


class TestBlockIntegration:
    """Integration tests for circuit blocks."""

    @pytest.fixture
    def mock_schematic(self):
        """Create a more complete mock schematic."""
        sch = Mock()

        wires = []
        junctions = []

        def mock_add_symbol(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            comp.x = x
            comp.y = y
            comp.ref = ref

            # Generic pin_position that returns relative positions
            def pin_pos(name):
                if name in ["VCC", "VIN", "Vdd", "A", "1"]:
                    return (x, y - 5)
                elif name in ["GND", "VSS", "K", "2"]:
                    return (x, y + 5)
                elif name in ["OUT", "VOUT"]:
                    return (x + 10, y)
                elif name in ["EN", "IN"]:
                    return (x - 10, y)
                else:
                    return (x, y)

            comp.pin_position = Mock(side_effect=pin_pos)
            return comp

        sch.add_symbol = Mock(side_effect=mock_add_symbol)
        sch.add_wire = Mock(side_effect=lambda *args: wires.append(args))
        sch.add_junction = Mock(side_effect=lambda *args: junctions.append(args))
        sch.wire_to_rail = Mock()
        sch.wire_decoupling_cap = Mock()
        sch.add_rail = Mock()

        sch._wires = wires
        sch._junctions = junctions

        return sch

    def test_multiple_leds(self, mock_schematic):
        """Create multiple LEDs."""
        led1 = LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")
        led2 = LEDIndicator(mock_schematic, x=120, y=100, ref_prefix="D2")

        # Both should have ports
        assert "VCC" in led1.ports
        assert "VCC" in led2.ports

    def test_ldo_with_caps(self, mock_schematic):
        """LDO creates input and output caps."""
        ldo = LDOBlock(
            mock_schematic, x=100, y=100, ref="U1", input_cap="10uF", output_caps=["10uF", "100nF"]
        )

        # Should have 3 capacitors total
        assert ldo.input_cap is not None
        assert len(ldo.output_caps) == 2

    def test_block_port_coordinates(self, mock_schematic):
        """Block ports have correct coordinates."""
        led = LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")

        # VCC port should be at LED anode position
        vcc_pos = led.ports["VCC"]
        assert isinstance(vcc_pos, tuple)
        assert len(vcc_pos) == 2


class TestBarrelJackInputMocked:
    """Tests for BarrelJackInput with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            if "Barrel" in str(symbol):
                # Barrel jack pins
                comp.pin_position.side_effect = lambda name: {
                    "Tip": (x - 10, y),
                    "Sleeve": (x - 10, y + 10),
                    "Switch": (x - 10, y + 5),
                }.get(name, (0, 0))
            elif "PMOS" in str(symbol) or "Q_PMOS" in str(symbol):
                # P-FET pins
                comp.pin_position.side_effect = lambda name: {
                    "G": (x, y + 5),
                    "S": (x - 5, y),
                    "D": (x + 5, y),
                }.get(name, (0, 0))
            elif "Schottky" in str(symbol):
                # Diode pins
                comp.pin_position.side_effect = lambda name: {
                    "A": (x - 5, y),
                    "K": (x + 5, y),
                }.get(name, (0, 0))
            else:
                # Capacitor pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5),
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_barrel_jack_creation_pfet(self, mock_schematic):
        """Create barrel jack with P-FET protection."""
        jack = BarrelJackInput(
            mock_schematic, x=100, y=100, voltage="12V", protection="pfet"
        )

        assert jack.schematic == mock_schematic
        assert jack.x == 100
        assert jack.y == 100
        assert "VIN" in jack.ports
        assert "VOUT" in jack.ports
        assert "GND" in jack.ports
        assert "JACK" in jack.components
        assert "Q" in jack.components
        assert "C_FILT" in jack.components

    def test_barrel_jack_creation_diode(self, mock_schematic):
        """Create barrel jack with diode protection."""
        jack = BarrelJackInput(
            mock_schematic, x=100, y=100, voltage="9V", protection="diode"
        )

        assert "JACK" in jack.components
        assert "D" in jack.components
        assert "C_FILT" in jack.components
        assert "Q" not in jack.components

    def test_barrel_jack_creation_no_protection(self, mock_schematic):
        """Create barrel jack without protection."""
        jack = BarrelJackInput(
            mock_schematic, x=100, y=100, voltage="5V", protection="none"
        )

        assert "JACK" in jack.components
        assert "C_FILT" in jack.components
        assert "Q" not in jack.components
        assert "D" not in jack.components

    def test_barrel_jack_adds_wires(self, mock_schematic):
        """Barrel jack wires components together."""
        BarrelJackInput(mock_schematic, x=100, y=100, protection="pfet")
        # Should add wires for: jack to pfet, gate to gnd, pfet to cap
        assert mock_schematic.add_wire.call_count >= 3

    def test_barrel_jack_connect_to_rails(self, mock_schematic):
        """Connect barrel jack to ground rail."""
        jack = BarrelJackInput(mock_schematic, x=100, y=100, protection="pfet")
        jack.connect_to_rails(gnd_rail_y=150)

        # Should add wires for cap negative and jack sleeve to GND rail
        wire_count = mock_schematic.add_wire.call_count
        jack.connect_to_rails(gnd_rail_y=150)
        assert mock_schematic.add_wire.call_count > wire_count

    def test_barrel_jack_connect_with_junctions(self, mock_schematic):
        """Connect barrel jack adds junctions when requested."""
        jack = BarrelJackInput(mock_schematic, x=100, y=100, protection="pfet")
        jack.connect_to_rails(gnd_rail_y=150, add_junctions=True)

        assert mock_schematic.add_junction.called


class TestUSBPowerInputMocked:
    """Tests for USBPowerInput with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            if "Polyfuse" in str(symbol) or "Fuse" in str(symbol):
                # Fuse pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x - 5, y),
                    "2": (x + 5, y),
                }.get(name, (0, 0))
            else:
                # Capacitor pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5),
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_usb_power_creation_fuse(self, mock_schematic):
        """Create USB power input with fuse protection."""
        usb = USBPowerInput(
            mock_schematic, x=100, y=100, protection="fuse", filter_cap="10uF"
        )

        assert usb.schematic == mock_schematic
        assert "VBUS_IN" in usb.ports
        assert "V5" in usb.ports
        assert "GND" in usb.ports
        assert "F" in usb.components
        assert "C_FILT" in usb.components

    def test_usb_power_creation_polyfuse(self, mock_schematic):
        """Create USB power input with polyfuse protection."""
        usb = USBPowerInput(mock_schematic, x=100, y=100, protection="polyfuse")

        assert "F" in usb.components
        assert "C_FILT" in usb.components

    def test_usb_power_creation_no_protection(self, mock_schematic):
        """Create USB power input without protection."""
        usb = USBPowerInput(mock_schematic, x=100, y=100, protection="none")

        assert "F" not in usb.components
        assert "C_FILT" in usb.components

    def test_usb_power_adds_wires(self, mock_schematic):
        """USB power wires components together."""
        USBPowerInput(mock_schematic, x=100, y=100, protection="fuse")
        # Should add wires for: input to fuse, fuse to cap
        assert mock_schematic.add_wire.call_count >= 2

    def test_usb_power_connect_to_rails(self, mock_schematic):
        """Connect USB power to ground rail."""
        usb = USBPowerInput(mock_schematic, x=100, y=100, protection="fuse")
        usb.connect_to_rails(gnd_rail_y=150)

        # Should add wire for cap negative to GND rail
        assert mock_schematic.add_wire.call_count >= 3

    def test_usb_power_connect_with_junctions(self, mock_schematic):
        """Connect USB power adds junctions when requested."""
        usb = USBPowerInput(mock_schematic, x=100, y=100, protection="fuse")
        usb.connect_to_rails(gnd_rail_y=150, add_junctions=True)

        assert mock_schematic.add_junction.called


class TestBatteryInputMocked:
    """Tests for BatteryInput with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            if "Conn" in str(symbol):
                # Connector pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x - 10, y - 2.5),
                    "2": (x - 10, y + 2.5),
                }.get(name, (0, 0))
            elif "PMOS" in str(symbol) or "Q_PMOS" in str(symbol):
                # P-FET pins
                comp.pin_position.side_effect = lambda name: {
                    "G": (x, y + 5),
                    "S": (x - 5, y),
                    "D": (x + 5, y),
                }.get(name, (0, 0))
            elif "Schottky" in str(symbol):
                # Diode pins
                comp.pin_position.side_effect = lambda name: {
                    "A": (x - 5, y),
                    "K": (x + 5, y),
                }.get(name, (0, 0))
            else:
                # Capacitor pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5),
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_battery_input_creation_pfet(self, mock_schematic):
        """Create battery input with P-FET protection."""
        batt = BatteryInput(
            mock_schematic,
            x=100,
            y=100,
            voltage="3.7V",
            connector="JST-PH",
            protection="pfet",
        )

        assert batt.schematic == mock_schematic
        assert batt.voltage == "3.7V"
        assert batt.connector_type == "JST-PH"
        assert "VBAT_IN" in batt.ports
        assert "VBAT" in batt.ports
        assert "GND" in batt.ports
        assert "CONN" in batt.components
        assert "Q" in batt.components
        assert "C_FILT" in batt.components

    def test_battery_input_creation_diode(self, mock_schematic):
        """Create battery input with diode protection."""
        batt = BatteryInput(
            mock_schematic, x=100, y=100, voltage="7.4V", protection="diode"
        )

        assert "CONN" in batt.components
        assert "D" in batt.components
        assert "C_FILT" in batt.components
        assert "Q" not in batt.components

    def test_battery_input_creation_no_protection(self, mock_schematic):
        """Create battery input without protection."""
        batt = BatteryInput(mock_schematic, x=100, y=100, protection="none")

        assert "CONN" in batt.components
        assert "C_FILT" in batt.components
        assert "Q" not in batt.components
        assert "D" not in batt.components

    def test_battery_input_adds_wires(self, mock_schematic):
        """Battery input wires components together."""
        BatteryInput(mock_schematic, x=100, y=100, protection="pfet")
        # Should add wires for: conn to pfet, gate to gnd, pfet to cap
        assert mock_schematic.add_wire.call_count >= 3

    def test_battery_input_connect_to_rails(self, mock_schematic):
        """Connect battery input to ground rail."""
        batt = BatteryInput(mock_schematic, x=100, y=100, protection="pfet")
        batt.connect_to_rails(gnd_rail_y=150)

        # Should add wires for cap negative and connector negative to GND rail
        assert mock_schematic.add_wire.call_count >= 5

    def test_battery_input_connect_with_junctions(self, mock_schematic):
        """Connect battery input adds junctions when requested."""
        batt = BatteryInput(mock_schematic, x=100, y=100, protection="pfet")
        batt.connect_to_rails(gnd_rail_y=150, add_junctions=True)

        assert mock_schematic.add_junction.called


class TestPowerInputFactoryFunctions:
    """Tests for power input factory functions."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            comp.pin_position.return_value = (x, y)
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        return sch

    def test_create_12v_barrel_jack(self, mock_schematic):
        """Create 12V barrel jack."""
        jack = create_12v_barrel_jack(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(jack, BarrelJackInput)

    def test_create_usb_power(self, mock_schematic):
        """Create USB power input."""
        usb = create_usb_power(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(usb, USBPowerInput)

    def test_create_lipo_battery(self, mock_schematic):
        """Create LiPo battery input."""
        batt = create_lipo_battery(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(batt, BatteryInput)


class TestUSBConnectorMocked:
    """Tests for USBConnector with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            if "USB" in str(symbol) or "Connector" in str(symbol):
                # USB connector pins
                comp.pin_position.side_effect = lambda name: {
                    "VBUS": (x - 10, y - 10),
                    "GND": (x - 10, y + 10),
                    "D+": (x - 10, y - 5),
                    "D-": (x - 10, y),
                    "CC1": (x - 10, y + 5),
                    "CC2": (x - 10, y + 7),
                    "ID": (x - 10, y + 5),
                    "SHIELD": (x - 10, y + 15),
                }.get(name, (x, y))
            elif "TVS" in str(symbol) or "D_TVS" in str(symbol):
                # TVS diode pins
                comp.pin_position.side_effect = lambda name: {
                    "A": (x - 5, y),
                    "K": (x + 5, y),
                }.get(name, (x, y))
            else:
                comp.pin_position.return_value = (x, y)
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_usb_connector_type_c_creation(self, mock_schematic):
        """Create USB Type-C connector."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=True,
        )

        assert usb.schematic == mock_schematic
        assert usb.x == 100
        assert usb.y == 100
        assert usb.connector_type == "type-c"
        assert "CONN" in usb.components
        assert "VBUS" in usb.ports
        assert "GND" in usb.ports
        assert "D+" in usb.ports
        assert "D-" in usb.ports
        assert "CC1" in usb.ports
        assert "CC2" in usb.ports

    def test_usb_connector_micro_b_creation(self, mock_schematic):
        """Create USB Micro-B connector."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="micro-b",
            esd_protection=True,
        )

        assert usb.connector_type == "micro-b"
        assert "VBUS" in usb.ports
        assert "GND" in usb.ports
        assert "D+" in usb.ports
        assert "D-" in usb.ports
        assert "ID" in usb.ports
        # Micro-B should not have CC pins
        assert "CC1" not in usb.ports
        assert "CC2" not in usb.ports

    def test_usb_connector_mini_b_creation(self, mock_schematic):
        """Create USB Mini-B connector."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="mini-b",
            esd_protection=False,
        )

        assert usb.connector_type == "mini-b"
        assert "ID" in usb.ports

    def test_usb_connector_type_a_creation(self, mock_schematic):
        """Create USB Type-A connector."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-a",
            esd_protection=False,
        )

        assert usb.connector_type == "type-a"
        assert "VBUS" in usb.ports
        assert "GND" in usb.ports
        assert "D+" in usb.ports
        assert "D-" in usb.ports
        # Type-A should not have CC or ID pins
        assert "CC1" not in usb.ports
        assert "ID" not in usb.ports

    def test_usb_connector_with_esd_protection(self, mock_schematic):
        """Create USB connector with ESD protection."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=True,
        )

        assert usb.esd_protection is True
        assert "TVS_ESD" in usb.components
        assert "ESD" in usb.tvs_diodes
        # Should add wires for D+ and D- to TVS
        assert mock_schematic.add_wire.called

    def test_usb_connector_without_esd_protection(self, mock_schematic):
        """Create USB connector without ESD protection."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
        )

        assert usb.esd_protection is False
        assert "TVS_ESD" not in usb.components
        assert len(usb.tvs_diodes) == 0

    def test_usb_connector_with_vbus_protection(self, mock_schematic):
        """Create USB connector with VBUS protection."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
            vbus_protection=True,
        )

        assert usb.vbus_protection is True
        assert "TVS_VBUS" in usb.components
        assert "VBUS" in usb.tvs_diodes

    def test_usb_connector_with_both_protections(self, mock_schematic):
        """Create USB connector with both ESD and VBUS protection."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=True,
            vbus_protection=True,
        )

        assert "TVS_ESD" in usb.components
        assert "TVS_VBUS" in usb.components
        assert len(usb.tvs_diodes) == 2

    def test_usb_connector_invalid_type(self, mock_schematic):
        """Invalid connector type raises ValueError."""
        with pytest.raises(ValueError) as exc:
            USBConnector(
                mock_schematic,
                x=100,
                y=100,
                connector_type="invalid",
            )
        assert "Invalid connector type" in str(exc.value)

    def test_usb_connector_connect_to_rails(self, mock_schematic):
        """Connect USB connector to power rails."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
        )
        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        usb.connect_to_rails(vbus_rail_y=50, gnd_rail_y=150)

        # Should add wires for VBUS and GND to rails
        assert mock_schematic.add_wire.call_count >= 2
        assert mock_schematic.add_junction.called

    def test_usb_connector_connect_to_rails_no_junctions(self, mock_schematic):
        """Connect USB connector without junctions."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
        )
        mock_schematic.add_junction.reset_mock()

        usb.connect_to_rails(vbus_rail_y=50, gnd_rail_y=150, add_junctions=False)

        assert not mock_schematic.add_junction.called

    def test_usb_connector_connect_vbus_only(self, mock_schematic):
        """Connect only VBUS rail."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
        )
        mock_schematic.add_wire.reset_mock()

        usb.connect_to_rails(vbus_rail_y=50)

        # Should only connect VBUS
        assert mock_schematic.add_wire.call_count == 1

    def test_usb_connector_has_cc_pins(self, mock_schematic):
        """Type-C connector has CC pins."""
        usb_c = USBConnector(
            mock_schematic, x=100, y=100, connector_type="type-c", esd_protection=False
        )
        usb_micro = USBConnector(
            mock_schematic, x=100, y=100, connector_type="micro-b", esd_protection=False
        )

        assert usb_c.has_cc_pins() is True
        assert usb_micro.has_cc_pins() is False

    def test_usb_connector_has_id_pin(self, mock_schematic):
        """Micro-B/Mini-B connectors have ID pin."""
        usb_c = USBConnector(
            mock_schematic, x=100, y=100, connector_type="type-c", esd_protection=False
        )
        usb_micro = USBConnector(
            mock_schematic, x=100, y=100, connector_type="micro-b", esd_protection=False
        )

        assert usb_c.has_id_pin() is False
        assert usb_micro.has_id_pin() is True

    def test_usb_connector_custom_tvs_values(self, mock_schematic):
        """Custom TVS part values."""
        USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=True,
            esd_tvs_value="TPD2E001",
            vbus_protection=True,
            vbus_tvs_value="SMBJ6.0A",
        )

        # Verify add_symbol was called with custom values
        calls = mock_schematic.add_symbol.call_args_list
        tvs_calls = [c for c in calls if "TVS" in str(c) or "TPD2E001" in str(c) or "SMBJ6.0A" in str(c)]
        assert len(tvs_calls) >= 1

    def test_usb_connector_port_lookup(self, mock_schematic):
        """USB connector port() method works."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
        )

        vbus_pos = usb.port("VBUS")
        assert isinstance(vbus_pos, tuple)
        assert len(vbus_pos) == 2

        dp_pos = usb.port("D+")
        assert isinstance(dp_pos, tuple)

    def test_usb_connector_port_not_found(self, mock_schematic):
        """USB connector raises KeyError for unknown port."""
        usb = USBConnector(
            mock_schematic,
            x=100,
            y=100,
            connector_type="type-c",
            esd_protection=False,
        )

        with pytest.raises(KeyError) as exc:
            usb.port("NONEXISTENT")
        assert "NONEXISTENT" in str(exc.value)


class TestUSBConnectorFactoryFunctions:
    """Tests for USB connector factory functions."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            comp.pin_position.side_effect = lambda name: {
                "VBUS": (x - 10, y - 10),
                "GND": (x - 10, y + 10),
                "D+": (x - 10, y - 5),
                "D-": (x - 10, y),
                "CC1": (x - 10, y + 5),
                "CC2": (x - 10, y + 7),
                "ID": (x - 10, y + 5),
                "A": (x - 5, y),
                "K": (x + 5, y),
            }.get(name, (x, y))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_create_usb_type_c(self, mock_schematic):
        """Create USB Type-C connector via factory."""
        usb = create_usb_type_c(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(usb, USBConnector)
        assert usb.connector_type == "type-c"

    def test_create_usb_type_c_with_esd(self, mock_schematic):
        """Create USB Type-C with ESD protection."""
        usb = create_usb_type_c(mock_schematic, x=100, y=100, ref="J1", with_esd=True)
        assert usb.esd_protection is True

    def test_create_usb_type_c_without_esd(self, mock_schematic):
        """Create USB Type-C without ESD protection."""
        usb = create_usb_type_c(mock_schematic, x=100, y=100, ref="J1", with_esd=False)
        assert usb.esd_protection is False

    def test_create_usb_type_c_with_vbus_protection(self, mock_schematic):
        """Create USB Type-C with VBUS protection."""
        usb = create_usb_type_c(
            mock_schematic, x=100, y=100, ref="J1", with_vbus_protection=True
        )
        assert usb.vbus_protection is True

    def test_create_usb_micro_b(self, mock_schematic):
        """Create USB Micro-B connector via factory."""
        usb = create_usb_micro_b(mock_schematic, x=100, y=100, ref="J1")
        assert isinstance(usb, USBConnector)
        assert usb.connector_type == "micro-b"

    def test_create_usb_micro_b_with_esd(self, mock_schematic):
        """Create USB Micro-B with ESD protection."""
        usb = create_usb_micro_b(mock_schematic, x=100, y=100, ref="J1", with_esd=True)
        assert usb.esd_protection is True

    def test_create_usb_micro_b_with_vbus_protection(self, mock_schematic):
        """Create USB Micro-B with VBUS protection."""
        usb = create_usb_micro_b(
            mock_schematic, x=100, y=100, ref="J1", with_vbus_protection=True
        )
        assert usb.vbus_protection is True
