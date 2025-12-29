"""Tests for schematic circuit blocks."""

import pytest
from unittest.mock import Mock, MagicMock, patch

from kicad_tools.schematic.blocks import (
    Port,
    CircuitBlock,
    LEDIndicator,
    DecouplingCaps,
    LDOBlock,
    OscillatorBlock,
    DebugHeader,
    create_power_led,
    create_status_led,
    create_3v3_ldo,
    create_mclk_oscillator,
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
        mock_led.pin_position.side_effect = lambda name: {
            "A": (100, 95),
            "K": (100, 105)
        }.get(name, (0, 0))

        mock_resistor = Mock()
        mock_resistor.pin_position.side_effect = lambda name: {
            "1": (100, 110),
            "2": (100, 120)
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
        led = LEDIndicator(mock_schematic, x=100, y=100, ref_prefix="D1")
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
            cap.pin_position.side_effect = lambda name: {
                "1": (x, y - 5),
                "2": (x, y + 5)
            }.get(name, (0, 0))
            return cap

        sch.add_symbol = Mock(side_effect=create_mock_cap)
        sch.add_wire = Mock()
        sch.wire_decoupling_cap = Mock()
        return sch

    def test_decoupling_caps_creation(self, mock_schematic):
        """Create decoupling capacitor bank."""
        caps = DecouplingCaps(
            mock_schematic,
            x=100, y=100,
            values=["10uF", "100nF"],
            ref_start=1
        )

        assert len(caps.caps) == 2
        assert "VCC" in caps.ports
        assert "GND" in caps.ports
        assert "VCC_END" in caps.ports
        assert "GND_END" in caps.ports

    def test_decoupling_caps_components(self, mock_schematic):
        """Components dict has all caps."""
        caps = DecouplingCaps(
            mock_schematic,
            x=100, y=100,
            values=["10uF", "100nF", "10nF"],
            ref_start=1
        )

        assert "C1" in caps.components
        assert "C2" in caps.components
        assert "C3" in caps.components

    def test_decoupling_caps_connect_to_rails(self, mock_schematic):
        """Connect caps to rails."""
        caps = DecouplingCaps(
            mock_schematic,
            x=100, y=100,
            values=["10uF", "100nF"],
            ref_start=1
        )
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
                    "EN": (x - 5, y + 5)
                }.get(name, (0, 0))
            else:
                # Capacitor pins
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5)
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.wire_to_rail = Mock()
        sch.wire_decoupling_cap = Mock()
        sch.add_rail = Mock()
        return sch

    def test_ldo_block_creation(self, mock_schematic):
        """Create LDO block."""
        ldo = LDOBlock(
            mock_schematic,
            x=100, y=100,
            ref="U1",
            value="3.3V"
        )

        assert ldo.schematic == mock_schematic
        assert "VIN" in ldo.ports
        assert "VOUT" in ldo.ports
        assert "GND" in ldo.ports
        assert "EN" in ldo.ports

    def test_ldo_block_components(self, mock_schematic):
        """LDO block has all components."""
        ldo = LDOBlock(
            mock_schematic,
            x=100, y=100,
            ref="U1",
            output_caps=["10uF", "100nF"]
        )

        assert "LDO" in ldo.components
        assert "C_IN" in ldo.components
        assert "C_OUT1" in ldo.components
        assert "C_OUT2" in ldo.components

    def test_ldo_en_tied_to_vin(self, mock_schematic):
        """EN pin tied to VIN when requested."""
        ldo = LDOBlock(
            mock_schematic,
            x=100, y=100,
            ref="U1",
            en_tied_to_vin=True
        )

        # Should add wire connecting EN to VIN level
        assert mock_schematic.add_wire.called

    def test_ldo_connect_to_rails(self, mock_schematic):
        """Connect LDO to power rails."""
        ldo = LDOBlock(mock_schematic, x=100, y=100, ref="U1")
        ldo.connect_to_rails(
            vin_rail_y=50,
            vout_rail_y=60,
            gnd_rail_y=150
        )

        # Should wire LDO pins to rails
        assert mock_schematic.wire_to_rail.called

    def test_ldo_extend_vout_rail(self, mock_schematic):
        """Extend VOUT rail when requested."""
        ldo = LDOBlock(mock_schematic, x=100, y=100, ref="U1")
        ldo.connect_to_rails(
            vin_rail_y=50,
            vout_rail_y=60,
            gnd_rail_y=150,
            extend_vout_rail_to=200
        )

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
                    "EN": (x - 10, y)
                }.get(name, (0, 0))
            else:
                comp.pin_position.side_effect = lambda name: {
                    "1": (x, y - 5),
                    "2": (x, y + 5)
                }.get(name, (0, 0))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        sch.wire_decoupling_cap = Mock()
        return sch

    def test_oscillator_block_creation(self, mock_schematic):
        """Create oscillator block."""
        osc = OscillatorBlock(
            mock_schematic,
            x=100, y=100,
            ref="Y1",
            value="24.576MHz"
        )

        assert "VCC" in osc.ports
        assert "GND" in osc.ports
        assert "OUT" in osc.ports
        assert "EN" in osc.ports

    def test_oscillator_block_components(self, mock_schematic):
        """Oscillator block has osc and cap."""
        osc = OscillatorBlock(
            mock_schematic,
            x=100, y=100,
            ref="Y1"
        )

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
        osc = OscillatorBlock(
            mock_schematic,
            x=100, y=100,
            ref="Y1",
            en_tied_to_vcc=True
        )
        osc.connect_to_rails(vcc_rail_y=50, gnd_rail_y=150)

        # Should add wire for EN to VCC rail
        assert mock_schematic.add_wire.call_count >= 3


class TestDebugHeaderMocked:
    """Tests for DebugHeader with mocked schematic."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic."""
        sch = Mock()

        def create_mock_header(symbol, x, y, ref, *args, **kwargs):
            header = Mock()
            header.pin_position.side_effect = lambda name: {
                "1": (x - 10, y - 7.5),
                "2": (x - 10, y - 2.5),
                "3": (x - 10, y + 2.5),
                "4": (x - 10, y + 7.5)
            }.get(name, (0, 0))
            return header

        sch.add_symbol = Mock(side_effect=create_mock_header)
        return sch

    def test_debug_header_creation(self, mock_schematic):
        """Create debug header."""
        header = DebugHeader(
            mock_schematic,
            x=100, y=100,
            ref="J1",
            value="SWD"
        )

        assert "VCC" in header.ports
        assert "SWDIO" in header.ports
        assert "SWCLK" in header.ports
        assert "GND" in header.ports

    def test_debug_header_components(self, mock_schematic):
        """Debug header has header component."""
        header = DebugHeader(mock_schematic, x=100, y=100, ref="J1")
        assert "HEADER" in header.components


class TestFactoryFunctions:
    """Tests for circuit block factory functions."""

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
            mock_schematic,
            x=100, y=100,
            ref="U1",
            input_cap="10uF",
            output_caps=["10uF", "100nF"]
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
