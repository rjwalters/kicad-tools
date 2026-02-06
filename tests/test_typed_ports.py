"""Tests for typed interface ports and connection validation."""

from unittest.mock import Mock

import pytest

from kicad_tools.intent.types import InterfaceCategory
from kicad_tools.schematic.blocks import (
    CircuitBlock,
    ConnectionValidator,
    DataPort,
    Port,
    PowerPort,
    WarningSeverity,
)


class TestPortExtensions:
    """Tests for extended Port dataclass fields."""

    def test_port_backward_compat(self):
        """Port still works with only original fields."""
        port = Port(name="VCC", x=10.0, y=20.0, direction="power")
        assert port.name == "VCC"
        assert port.pos() == (10.0, 20.0)
        assert port.interface is None
        assert port.interface_type is None
        assert port.parameters is None
        assert port.group is None

    def test_port_with_interface_metadata(self):
        """Port accepts optional interface metadata."""
        port = Port(
            name="D+",
            x=50.0,
            y=60.0,
            direction="bidirectional",
            interface=InterfaceCategory.DIFFERENTIAL,
            interface_type="usb2_high_speed",
            parameters={"impedance": 90.0},
            group="usb_data",
        )
        assert port.interface == InterfaceCategory.DIFFERENTIAL
        assert port.interface_type == "usb2_high_speed"
        assert port.parameters == {"impedance": 90.0}
        assert port.group == "usb_data"

    def test_port_pos_unchanged(self):
        """pos() method still works with extended fields."""
        port = Port(
            name="SDA",
            x=100.0,
            y=200.0,
            interface=InterfaceCategory.BUS,
        )
        assert port.pos() == (100.0, 200.0)


class TestPowerPort:
    """Tests for PowerPort subclass."""

    def test_power_port_defaults(self):
        """PowerPort sets interface to POWER automatically."""
        port = PowerPort(name="VCC", x=10.0, y=20.0)
        assert port.interface == InterfaceCategory.POWER
        assert port.direction == "power"
        assert port.voltage_min is None
        assert port.voltage_max is None
        assert port.max_current is None

    def test_power_port_with_voltage(self):
        """PowerPort with voltage range."""
        port = PowerPort(
            name="VOUT",
            x=50.0,
            y=60.0,
            direction="output",
            voltage_min=3.135,
            voltage_max=3.465,
            max_current=0.5,
        )
        assert port.voltage_min == 3.135
        assert port.voltage_max == 3.465
        assert port.max_current == 0.5
        assert port.interface == InterfaceCategory.POWER

    def test_power_port_voltage_overlaps(self):
        """Voltage overlap check between power ports."""
        source = PowerPort(
            name="VOUT",
            x=0,
            y=0,
            voltage_min=3.0,
            voltage_max=3.6,
        )
        target = PowerPort(
            name="VIN",
            x=10,
            y=10,
            voltage_min=3.0,
            voltage_max=3.6,
        )
        assert source.voltage_overlaps(target) is True

    def test_power_port_voltage_no_overlap(self):
        """Voltage ranges that don't overlap."""
        source = PowerPort(
            name="VOUT",
            x=0,
            y=0,
            voltage_min=3.0,
            voltage_max=3.6,
        )
        target = PowerPort(
            name="VIN",
            x=10,
            y=10,
            voltage_min=4.5,
            voltage_max=5.5,
        )
        assert source.voltage_overlaps(target) is False

    def test_power_port_voltage_overlap_partial(self):
        """Partially overlapping voltage ranges."""
        source = PowerPort(
            name="VOUT",
            x=0,
            y=0,
            voltage_min=3.0,
            voltage_max=3.6,
        )
        target = PowerPort(
            name="VIN",
            x=10,
            y=10,
            voltage_min=3.3,
            voltage_max=5.0,
        )
        assert source.voltage_overlaps(target) is True

    def test_power_port_voltage_overlap_none_values(self):
        """Overlap returns True when voltage info is missing."""
        source = PowerPort(name="VOUT", x=0, y=0)
        target = PowerPort(name="VIN", x=10, y=10, voltage_min=3.0, voltage_max=3.6)
        assert source.voltage_overlaps(target) is True

    def test_power_port_is_port(self):
        """PowerPort is a subclass of Port."""
        port = PowerPort(name="VCC", x=0, y=0)
        assert isinstance(port, Port)
        assert port.pos() == (0.0, 0.0)


class TestDataPort:
    """Tests for DataPort subclass."""

    def test_data_port_usb(self):
        """DataPort for USB sets DIFFERENTIAL category."""
        port = DataPort(
            name="D+",
            x=50.0,
            y=60.0,
            direction="bidirectional",
            protocol="usb2",
            signal_role="d+",
        )
        assert port.protocol == "usb2"
        assert port.signal_role == "d+"
        assert port.interface == InterfaceCategory.DIFFERENTIAL
        assert port.interface_type == "usb2"

    def test_data_port_i2c(self):
        """DataPort for I2C sets BUS category."""
        port = DataPort(
            name="SDA",
            x=100.0,
            y=200.0,
            direction="bidirectional",
            protocol="i2c",
            signal_role="sda",
        )
        assert port.interface == InterfaceCategory.BUS
        assert port.interface_type == "i2c"

    def test_data_port_spi(self):
        """DataPort for SPI sets BUS category."""
        port = DataPort(
            name="MOSI",
            x=100.0,
            y=200.0,
            direction="output",
            protocol="spi",
            signal_role="mosi",
        )
        assert port.interface == InterfaceCategory.BUS

    def test_data_port_no_protocol(self):
        """DataPort without protocol has no interface set."""
        port = DataPort(name="GPIO1", x=0, y=0)
        assert port.protocol is None
        assert port.interface is None

    def test_data_port_explicit_interface_preserved(self):
        """Explicit interface is not overridden by protocol mapping."""
        port = DataPort(
            name="D+",
            x=0,
            y=0,
            interface=InterfaceCategory.SINGLE_ENDED,
            protocol="usb2",
        )
        assert port.interface == InterfaceCategory.SINGLE_ENDED

    def test_data_port_is_port(self):
        """DataPort is a subclass of Port."""
        port = DataPort(name="TX", x=0, y=0, protocol="uart")
        assert isinstance(port, Port)


class TestCircuitBlockTypedPorts:
    """Tests for CircuitBlock typed port support."""

    def test_typed_ports_initialized(self):
        """CircuitBlock has empty typed_ports dict."""
        block = CircuitBlock()
        assert block.typed_ports == {}

    def test_get_typed_port_exists(self):
        """get_typed_port returns typed port when available."""
        block = CircuitBlock()
        tp = PowerPort(name="VCC", x=10.0, y=20.0)
        block.typed_ports["VCC"] = tp
        block.ports["VCC"] = (10.0, 20.0)

        result = block.get_typed_port("VCC")
        assert result is tp
        assert isinstance(result, PowerPort)

    def test_get_typed_port_fallback(self):
        """get_typed_port creates basic Port from position tuple."""
        block = CircuitBlock()
        block.ports["GND"] = (30.0, 40.0)

        result = block.get_typed_port("GND")
        assert isinstance(result, Port)
        assert result.name == "GND"
        assert result.pos() == (30.0, 40.0)
        assert result.interface is None

    def test_get_typed_port_not_found(self):
        """get_typed_port raises KeyError for missing port."""
        block = CircuitBlock()
        with pytest.raises(KeyError, match="MISSING"):
            block.get_typed_port("MISSING")

    def test_backward_compat_port_method(self):
        """Original port() method still works."""
        block = CircuitBlock()
        block.ports = {"VCC": (10.0, 20.0), "GND": (10.0, 30.0)}
        assert block.port("VCC") == (10.0, 20.0)


class TestConnectionValidator:
    """Tests for ConnectionValidator."""

    def setup_method(self):
        self.validator = ConnectionValidator()

    def test_untyped_ports_no_warnings(self):
        """Untyped ports skip validation."""
        source = Port(name="A", x=0, y=0)
        target = Port(name="B", x=10, y=10)
        warnings = self.validator.validate_connection(source, target)
        assert warnings == []

    def test_same_protocol_compatible(self):
        """Same protocol is compatible."""
        source = DataPort(name="SDA1", x=0, y=0, protocol="i2c", signal_role="sda")
        target = DataPort(name="SDA2", x=10, y=10, protocol="i2c", signal_role="sda")
        warnings = self.validator.validate_connection(source, target)
        assert warnings == []

    def test_different_protocol_error(self):
        """Different protocols produce an error."""
        source = DataPort(name="D+", x=0, y=0, protocol="usb2", signal_role="d+")
        target = DataPort(name="MOSI", x=10, y=10, protocol="spi", signal_role="mosi")
        warnings = self.validator.validate_connection(source, target)
        # Should have category mismatch (DIFFERENTIAL vs BUS)
        assert len(warnings) >= 1
        assert any(w.severity == WarningSeverity.ERROR for w in warnings)

    def test_usb2_usb3_compatible(self):
        """USB2 and USB3 are compatible protocols."""
        source = DataPort(name="D+", x=0, y=0, protocol="usb2", signal_role="d+")
        target = DataPort(name="D+", x=10, y=10, protocol="usb3", signal_role="d+")
        warnings = self.validator.validate_connection(source, target)
        assert warnings == []

    def test_i2c_to_spi_error(self):
        """I2C to SPI connection produces protocol error."""
        source = DataPort(name="SDA", x=0, y=0, protocol="i2c", signal_role="sda")
        target = DataPort(name="MOSI", x=10, y=10, protocol="spi", signal_role="mosi")
        warnings = self.validator.validate_connection(source, target)
        assert len(warnings) >= 1
        error_msgs = [w.message for w in warnings if w.severity == WarningSeverity.ERROR]
        assert any("Protocol mismatch" in msg or "mismatch" in msg.lower() for msg in error_msgs)

    def test_voltage_compatible(self):
        """Compatible voltage ranges produce no warnings."""
        source = PowerPort(name="VOUT", x=0, y=0, voltage_min=3.0, voltage_max=3.6)
        target = PowerPort(name="VIN", x=10, y=10, voltage_min=3.0, voltage_max=3.6)
        warnings = self.validator.validate_connection(source, target)
        assert warnings == []

    def test_voltage_mismatch_error(self):
        """Incompatible voltage ranges produce an error."""
        source = PowerPort(name="VOUT", x=0, y=0, voltage_min=3.0, voltage_max=3.6)
        target = PowerPort(name="VIN", x=10, y=10, voltage_min=4.5, voltage_max=5.5)
        warnings = self.validator.validate_connection(source, target)
        assert len(warnings) == 1
        assert warnings[0].severity == WarningSeverity.ERROR
        assert "Voltage mismatch" in warnings[0].message

    def test_power_to_data_category_mismatch(self):
        """Connecting power port to data port produces category mismatch error."""
        source = PowerPort(name="VOUT", x=0, y=0, voltage_min=3.0, voltage_max=3.6)
        target = DataPort(name="SDA", x=10, y=10, protocol="i2c", signal_role="sda")
        warnings = self.validator.validate_connection(source, target)
        # POWER vs BUS category mismatch is caught as error
        errors = [w for w in warnings if w.severity == WarningSeverity.ERROR]
        assert len(errors) >= 1
        assert "category mismatch" in errors[0].message.lower()

    def test_data_to_power_category_mismatch(self):
        """Connecting data port to power port produces category mismatch error."""
        source = DataPort(name="D+", x=0, y=0, protocol="usb2", signal_role="d+")
        target = PowerPort(name="VCC", x=10, y=10, voltage_min=3.0, voltage_max=3.6)
        warnings = self.validator.validate_connection(source, target)
        errors = [w for w in warnings if w.severity == WarningSeverity.ERROR]
        assert len(errors) >= 1

    def test_power_to_data_warning_same_category(self):
        """Power-to-data warning when both have same interface category."""
        source = PowerPort(
            name="VOUT",
            x=0,
            y=0,
            voltage_min=3.0,
            voltage_max=3.6,
            interface=InterfaceCategory.SINGLE_ENDED,
        )
        target = DataPort(
            name="GPIO",
            x=10,
            y=10,
            interface=InterfaceCategory.SINGLE_ENDED,
        )
        warnings = self.validator.validate_connection(source, target)
        warning_msgs = [w for w in warnings if w.severity == WarningSeverity.WARNING]
        assert len(warning_msgs) >= 1
        assert "Power port connected to data port" in warning_msgs[0].message

    def test_one_typed_one_untyped_no_error(self):
        """Mixed typed/untyped skips category check if one is None."""
        source = DataPort(name="D+", x=0, y=0, protocol="usb2")
        target = Port(name="PIN1", x=10, y=10)
        warnings = self.validator.validate_connection(source, target)
        # No category mismatch when target has no interface
        assert not any(
            w.severity == WarningSeverity.ERROR and "category" in w.message.lower()
            for w in warnings
        )

    def test_connection_warning_fields(self):
        """ConnectionWarning stores port references."""
        source = PowerPort(name="VOUT", x=0, y=0, voltage_min=3.0, voltage_max=3.6)
        target = PowerPort(name="VIN", x=10, y=10, voltage_min=5.0, voltage_max=5.5)
        warnings = self.validator.validate_connection(source, target)
        assert len(warnings) == 1
        assert warnings[0].source_port is source
        assert warnings[0].target_port is target


class TestUSBConnectorTypedPorts:
    """Tests for USBConnector typed port annotations."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic for USB connector."""
        sch = Mock()

        def make_mock_symbol(symbol, x, y, ref, *args, **kwargs):
            mock = Mock()
            # Simulate connector pin positions
            pin_map = {
                "VBUS": (x, y - 20),
                "GND": (x, y + 20),
                "D+": (x + 5, y - 10),
                "D-": (x + 5, y + 10),
                "CC1": (x + 5, y - 5),
                "CC2": (x + 5, y + 5),
                "SHIELD": (x, y + 30),
                "A": (x + 20, y - 10),
                "K": (x + 20, y + 10),
            }
            mock.pin_position.side_effect = lambda name: pin_map.get(name, (0, 0))
            return mock

        sch.add_symbol = Mock(side_effect=make_mock_symbol)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_usb_typed_ports_exist(self, mock_schematic):
        """USBConnector creates typed ports."""
        from kicad_tools.schematic.blocks import USBConnector

        usb = USBConnector(mock_schematic, x=50, y=100, esd_protection=False)
        assert len(usb.typed_ports) > 0

    def test_usb_data_ports_are_data_port(self, mock_schematic):
        """USB D+/D- ports are DataPort instances."""
        from kicad_tools.schematic.blocks import USBConnector

        usb = USBConnector(mock_schematic, x=50, y=100, esd_protection=False)
        dp = usb.typed_ports.get("D+")
        dm = usb.typed_ports.get("D-")
        assert isinstance(dp, DataPort)
        assert isinstance(dm, DataPort)
        assert dp.protocol == "usb2"
        assert dm.protocol == "usb2"
        assert dp.group == "usb_data"

    def test_usb_vbus_is_power_port(self, mock_schematic):
        """USB VBUS port is a PowerPort."""
        from kicad_tools.schematic.blocks import USBConnector

        usb = USBConnector(mock_schematic, x=50, y=100, esd_protection=False)
        vbus = usb.typed_ports.get("VBUS")
        assert isinstance(vbus, PowerPort)
        assert vbus.voltage_min == 4.75
        assert vbus.voltage_max == 5.25

    def test_usb_backward_compat(self, mock_schematic):
        """USBConnector still exposes tuple ports."""
        from kicad_tools.schematic.blocks import USBConnector

        usb = USBConnector(mock_schematic, x=50, y=100, esd_protection=False)
        pos = usb.port("D+")
        assert isinstance(pos, tuple)
        assert len(pos) == 2


class TestI2CPullupsTypedPorts:
    """Tests for I2CPullups typed port annotations."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic for I2C block."""
        sch = Mock()

        def make_mock_symbol(symbol, x, y, ref, *args, **kwargs):
            mock = Mock()
            pin_map = {
                "1": (x, y - 5),
                "2": (x, y + 5),
            }
            mock.pin_position.side_effect = lambda name: pin_map.get(name, (0, 0))
            return mock

        sch.add_symbol = Mock(side_effect=make_mock_symbol)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_i2c_typed_ports_exist(self, mock_schematic):
        """I2CPullups creates typed ports."""
        from kicad_tools.schematic.blocks import I2CPullups

        i2c = I2CPullups(mock_schematic, x=100, y=50)
        assert len(i2c.typed_ports) > 0

    def test_i2c_sda_scl_are_data_ports(self, mock_schematic):
        """I2C SDA/SCL ports are DataPort instances."""
        from kicad_tools.schematic.blocks import I2CPullups

        i2c = I2CPullups(mock_schematic, x=100, y=50)
        sda = i2c.typed_ports.get("SDA")
        scl = i2c.typed_ports.get("SCL")
        assert isinstance(sda, DataPort)
        assert isinstance(scl, DataPort)
        assert sda.protocol == "i2c"
        assert scl.protocol == "i2c"
        assert sda.signal_role == "sda"
        assert scl.signal_role == "scl"

    def test_i2c_vcc_is_power_port(self, mock_schematic):
        """I2C VCC port is a PowerPort."""
        from kicad_tools.schematic.blocks import I2CPullups

        i2c = I2CPullups(mock_schematic, x=100, y=50)
        vcc = i2c.typed_ports.get("VCC")
        assert isinstance(vcc, PowerPort)


class TestLDOBlockTypedPorts:
    """Tests for LDOBlock typed port annotations."""

    @pytest.fixture
    def mock_schematic(self):
        """Create mock schematic for LDO block."""
        sch = Mock()

        def make_mock_symbol(symbol, x, y, ref, *args, **kwargs):
            mock = Mock()
            pin_map = {
                "VIN": (x - 10, y),
                "VOUT": (x + 10, y),
                "GND": (x, y + 10),
                "EN": (x - 5, y + 5),
                "1": (x, y - 5),
                "2": (x, y + 5),
            }
            mock.pin_position.side_effect = lambda name: pin_map.get(name, (0, 0))
            return mock

        sch.add_symbol = Mock(side_effect=make_mock_symbol)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        return sch

    def test_ldo_typed_ports_exist(self, mock_schematic):
        """LDOBlock creates typed ports."""
        from kicad_tools.schematic.blocks import LDOBlock

        ldo = LDOBlock(mock_schematic, x=100, y=50)
        assert len(ldo.typed_ports) > 0

    def test_ldo_ports_are_power_ports(self, mock_schematic):
        """LDO VIN/VOUT/GND ports are PowerPort instances."""
        from kicad_tools.schematic.blocks import LDOBlock

        ldo = LDOBlock(mock_schematic, x=100, y=50)
        assert isinstance(ldo.typed_ports["VIN"], PowerPort)
        assert isinstance(ldo.typed_ports["VOUT"], PowerPort)
        assert isinstance(ldo.typed_ports["GND"], PowerPort)

    def test_ldo_port_directions(self, mock_schematic):
        """LDO typed ports have correct directions."""
        from kicad_tools.schematic.blocks import LDOBlock

        ldo = LDOBlock(mock_schematic, x=100, y=50)
        assert ldo.typed_ports["VIN"].direction == "input"
        assert ldo.typed_ports["VOUT"].direction == "output"

    def test_ldo_backward_compat(self, mock_schematic):
        """LDOBlock still exposes tuple ports."""
        from kicad_tools.schematic.blocks import LDOBlock

        ldo = LDOBlock(mock_schematic, x=100, y=50)
        pos = ldo.port("VIN")
        assert isinstance(pos, tuple)


class TestCrossBockValidation:
    """Integration tests: validate connections between different blocks."""

    def test_usb_to_i2c_detected(self):
        """Connecting USB data to I2C data produces error."""
        validator = ConnectionValidator()
        usb_dp = DataPort(name="D+", x=0, y=0, protocol="usb2", signal_role="d+")
        i2c_sda = DataPort(name="SDA", x=10, y=10, protocol="i2c", signal_role="sda")

        warnings = validator.validate_connection(usb_dp, i2c_sda)
        errors = [w for w in warnings if w.severity == WarningSeverity.ERROR]
        assert len(errors) >= 1

    def test_i2c_to_i2c_valid(self):
        """Connecting I2C to I2C produces no errors."""
        validator = ConnectionValidator()
        sda1 = DataPort(name="SDA", x=0, y=0, protocol="i2c", signal_role="sda")
        sda2 = DataPort(name="SDA", x=10, y=10, protocol="i2c", signal_role="sda")

        warnings = validator.validate_connection(sda1, sda2)
        assert warnings == []

    def test_3v3_to_5v_voltage_mismatch(self):
        """Connecting 3.3V output to 5V input produces error."""
        validator = ConnectionValidator()
        vout_3v3 = PowerPort(
            name="VOUT",
            x=0,
            y=0,
            voltage_min=3.135,
            voltage_max=3.465,
        )
        vin_5v = PowerPort(
            name="VIN",
            x=10,
            y=10,
            voltage_min=4.5,
            voltage_max=5.5,
        )

        warnings = validator.validate_connection(vout_3v3, vin_5v)
        errors = [w for w in warnings if w.severity == WarningSeverity.ERROR]
        assert len(errors) == 1
        assert "Voltage mismatch" in errors[0].message

    def test_3v3_to_3v3_compatible(self):
        """Connecting matching voltage ranges produces no errors."""
        validator = ConnectionValidator()
        source = PowerPort(
            name="VOUT",
            x=0,
            y=0,
            voltage_min=3.0,
            voltage_max=3.6,
        )
        target = PowerPort(
            name="VIN",
            x=10,
            y=10,
            voltage_min=2.7,
            voltage_max=3.6,
        )

        warnings = validator.validate_connection(source, target)
        assert warnings == []
