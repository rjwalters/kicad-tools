"""Tests for netlist query API.

Tests the netlist extraction and connectivity query methods:
- extract_netlist()
- get_net_for_pin()
- pins_on_net()
- are_connected()
"""

from kicad_tools.schematic.models import PinRef, Schematic
from kicad_tools.schematic.models.elements import Junction, Label, Wire
from kicad_tools.schematic.models.pin import Pin
from kicad_tools.schematic.models.symbol import SymbolDef, SymbolInstance


def make_simple_symbol(lib_id: str, pins: list[tuple[str, str, float, float]]) -> SymbolDef:
    """Create a simple symbol definition for testing.

    Args:
        lib_id: Library ID (e.g., "Device:R")
        pins: List of (name, number, x, y) tuples for pin positions
    """
    return SymbolDef(
        lib_id=lib_id,
        name=lib_id.split(":")[-1],
        raw_sexp="",
        pins=[
            Pin(name=name, number=number, x=x, y=y, angle=0, length=2.54, pin_type="passive")
            for name, number, x, y in pins
        ],
    )


class TestPinRef:
    """Tests for PinRef dataclass."""

    def test_pinref_creation(self):
        """Create PinRef with symbol ref and pin."""
        ref = PinRef(symbol_ref="R1", pin="1")
        assert ref.symbol_ref == "R1"
        assert ref.pin == "1"

    def test_pinref_str(self):
        """PinRef string representation."""
        ref = PinRef(symbol_ref="U1", pin="VDD")
        assert str(ref) == "U1.VDD"

    def test_pinref_hashable(self):
        """PinRef is hashable for use in sets/dicts."""
        ref1 = PinRef(symbol_ref="R1", pin="1")
        ref2 = PinRef(symbol_ref="R1", pin="1")
        assert ref1 == ref2
        assert hash(ref1) == hash(ref2)

        # Can use in set
        refs = {ref1, ref2}
        assert len(refs) == 1


class TestExtractNetlist:
    """Tests for extract_netlist() method."""

    def test_empty_schematic(self):
        """Empty schematic returns empty netlist."""
        sch = Schematic("Test")
        netlist = sch.extract_netlist()
        assert netlist == {}

    def test_single_wire_between_two_symbols(self):
        """Two symbols connected by a wire."""
        sch = Schematic("Test")

        # Create two resistors with pins
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])

        # Place R1 at (100, 50) and R2 at (110, 50)
        r1 = SymbolInstance(
            symbol_def=r_def,
            x=100,
            y=50,
            rotation=0,
            reference="R1",
            value="10k",
        )
        r2 = SymbolInstance(
            symbol_def=r_def,
            x=110,
            y=50,
            rotation=0,
            reference="R2",
            value="10k",
        )
        sch.symbols.extend([r1, r2])

        # R1 pin 2 at (102.54, 50), R2 pin 1 at (107.46, 50)
        # Wire connecting them
        sch.wires.append(Wire(x1=102.54, y1=50, x2=107.46, y2=50))

        netlist = sch.extract_netlist()

        # Should have 3 nets: R1.1 floating, R1.2-R2.1 connected, R2.2 floating
        assert len(netlist) == 3

        # Find the net with 2 connected pins
        connected_net = None
        for net_name, pins in netlist.items():
            if len(pins) == 2:
                connected_net = net_name
                break

        assert connected_net is not None
        assert connected_net.startswith("Net-(")  # Auto-generated name

        # Check both pins are present in the connected net
        pins = netlist[connected_net]
        pin_strs = {str(p) for p in pins}
        assert "R1.2" in pin_strs
        assert "R2.1" in pin_strs

    def test_labeled_net(self):
        """Net with a label gets the label name."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def,
            x=100,
            y=50,
            rotation=0,
            reference="R1",
            value="10k",
        )
        sch.symbols.append(r1)

        # Wire from pin 2 to label
        sch.wires.append(Wire(x1=102.54, y1=50, x2=110, y2=50))
        sch.labels.append(Label(text="SIG_OUT", x=110, y=50))

        netlist = sch.extract_netlist()

        assert "SIG_OUT" in netlist
        pins = netlist["SIG_OUT"]
        assert len(pins) == 1
        assert pins[0].symbol_ref == "R1"
        assert pins[0].pin == "2"

    def test_power_net(self):
        """Power symbols create named nets."""
        sch = Schematic("Test")

        # IC with VDD pin
        ic_def = make_simple_symbol("MCU:Test", [("VDD", "1", 0, -2.54)])
        ic = SymbolInstance(
            symbol_def=ic_def,
            x=100,
            y=50,
            rotation=0,
            reference="U1",
            value="MCU",
        )
        sch.symbols.append(ic)

        # Power symbol at same position as VDD pin
        from kicad_tools.schematic.models.elements import PowerSymbol

        pwr = PowerSymbol(lib_id="power:+3.3V", x=100, y=47.46, rotation=0)
        sch.power_symbols.append(pwr)

        # Wire connecting IC VDD to power symbol
        sch.wires.append(Wire(x1=100, y1=47.46, x2=100, y2=47.46))

        netlist = sch.extract_netlist()

        assert "+3.3V" in netlist
        pins = netlist["+3.3V"]
        assert len(pins) == 1
        assert pins[0].symbol_ref == "U1"
        assert pins[0].pin == "1"


class TestGetNetForPin:
    """Tests for get_net_for_pin() method."""

    def test_pin_on_named_net(self):
        """Get net name for pin connected to labeled net."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def,
            x=100,
            y=50,
            rotation=0,
            reference="R1",
            value="10k",
        )
        sch.symbols.append(r1)

        # Wire from pin 2 to label
        sch.wires.append(Wire(x1=102.54, y1=50, x2=110, y2=50))
        sch.labels.append(Label(text="DATA", x=110, y=50))

        net = sch.get_net_for_pin("R1", "2")
        assert net == "DATA"

    def test_floating_pin(self):
        """Floating pin returns None."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def,
            x=100,
            y=50,
            rotation=0,
            reference="R1",
            value="10k",
        )
        sch.symbols.append(r1)

        # Pin 1 is floating (no wire)
        net = sch.get_net_for_pin("R1", "1")
        assert net is None

    def test_nonexistent_symbol(self):
        """Nonexistent symbol returns None."""
        sch = Schematic("Test")
        net = sch.get_net_for_pin("R99", "1")
        assert net is None

    def test_nonexistent_pin(self):
        """Nonexistent pin returns None."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def,
            x=100,
            y=50,
            rotation=0,
            reference="R1",
            value="10k",
        )
        sch.symbols.append(r1)

        net = sch.get_net_for_pin("R1", "99")
        assert net is None

    def test_unnamed_net(self):
        """Pin on unnamed net returns auto-generated name."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def,
            x=100,
            y=50,
            rotation=0,
            reference="R1",
            value="10k",
        )
        r2 = SymbolInstance(
            symbol_def=r_def,
            x=110,
            y=50,
            rotation=0,
            reference="R2",
            value="10k",
        )
        sch.symbols.extend([r1, r2])

        # Connect R1 pin 2 to R2 pin 1 (no label)
        sch.wires.append(Wire(x1=102.54, y1=50, x2=107.46, y2=50))

        net = sch.get_net_for_pin("R1", "2")
        assert net is not None
        assert net.startswith("Net-(")


class TestPinsOnNet:
    """Tests for pins_on_net() method."""

    def test_get_all_pins_on_net(self):
        """Get all pins connected to a named net."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])

        # Three resistors connected in series
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        r2 = SymbolInstance(symbol_def=r_def, x=115, y=50, rotation=0, reference="R2", value="10k")
        r3 = SymbolInstance(symbol_def=r_def, x=130, y=50, rotation=0, reference="R3", value="10k")
        sch.symbols.extend([r1, r2, r3])

        # Common net between R1 pin 2 and R2 pin 1, labeled "NODE_A"
        sch.wires.append(Wire(x1=102.54, y1=50, x2=112.46, y2=50))
        sch.labels.append(Label(text="NODE_A", x=107.5, y=50))

        pins = sch.pins_on_net("NODE_A")
        assert len(pins) == 2

        pin_strs = {str(p) for p in pins}
        assert "R1.2" in pin_strs
        assert "R2.1" in pin_strs

    def test_nonexistent_net(self):
        """Nonexistent net returns empty list."""
        sch = Schematic("Test")
        pins = sch.pins_on_net("NONEXISTENT")
        assert pins == []


class TestAreConnected:
    """Tests for are_connected() method."""

    def test_directly_connected_pins(self):
        """Pins connected by a single wire."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        r2 = SymbolInstance(symbol_def=r_def, x=110, y=50, rotation=0, reference="R2", value="10k")
        sch.symbols.extend([r1, r2])

        # Connect R1 pin 2 to R2 pin 1
        sch.wires.append(Wire(x1=102.54, y1=50, x2=107.46, y2=50))

        assert sch.are_connected("R1", "2", "R2", "1") is True
        assert sch.are_connected("R2", "1", "R1", "2") is True  # Order independent

    def test_not_connected_pins(self):
        """Pins that are not connected."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        r2 = SymbolInstance(symbol_def=r_def, x=200, y=50, rotation=0, reference="R2", value="10k")
        sch.symbols.extend([r1, r2])

        # No wire between them
        assert sch.are_connected("R1", "2", "R2", "1") is False

    def test_indirectly_connected_via_junction(self):
        """Pins connected through a junction (T-connection)."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        r2 = SymbolInstance(symbol_def=r_def, x=100, y=70, rotation=0, reference="R2", value="10k")
        r3 = SymbolInstance(symbol_def=r_def, x=120, y=50, rotation=0, reference="R3", value="10k")
        sch.symbols.extend([r1, r2, r3])

        # R1 pin 2 connects to junction at (102.54, 50)
        # R2 pin 1 connects to same junction via vertical wire
        # R3 pin 1 connects to same junction via horizontal wire
        sch.wires.append(Wire(x1=102.54, y1=50, x2=117.46, y2=50))  # Horizontal
        sch.wires.append(Wire(x1=102.54, y1=50, x2=102.54, y2=67.46))  # Vertical to R2 pin 1
        sch.junctions.append(Junction(x=102.54, y=50))

        # Note: R2 pin 1 is at (100-2.54, 70) = (97.46, 70), need to adjust
        # Actually with rotation=0, R2 at (100, 70), pin 1 at (97.46, 70)
        # Let me fix the wire endpoint
        sch.wires[-1] = Wire(x1=102.54, y1=50, x2=97.46, y2=70)  # To R2 pin 1

        # R1 pin 2 should be connected to R3 pin 1 via junction
        assert sch.are_connected("R1", "2", "R3", "1") is True

    def test_nonexistent_symbol(self):
        """Nonexistent symbol returns False."""
        sch = Schematic("Test")
        assert sch.are_connected("R1", "1", "R99", "1") is False

    def test_same_pin(self):
        """Same pin compared to itself is connected."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        sch.symbols.append(r1)

        assert sch.are_connected("R1", "1", "R1", "1") is True


class TestComplexNetlist:
    """Integration tests with more complex schematics."""

    def test_power_distribution(self):
        """Multiple components on a power net."""
        sch = Schematic("Test")

        # IC with multiple power pins
        ic_def = make_simple_symbol(
            "MCU:Test",
            [
                ("VDD", "1", 0, -5.08),
                ("VSS", "2", 0, 5.08),
                ("IO", "3", 5.08, 0),
            ],
        )
        ic = SymbolInstance(symbol_def=ic_def, x=100, y=50, rotation=0, reference="U1", value="MCU")
        sch.symbols.append(ic)

        # Decoupling capacitor
        c_def = make_simple_symbol("Device:C", [("~", "1", 0, -2.54), ("~", "2", 0, 2.54)])
        cap = SymbolInstance(symbol_def=c_def, x=90, y=50, rotation=0, reference="C1", value="100n")
        sch.symbols.append(cap)

        # Power symbols
        from kicad_tools.schematic.models.elements import PowerSymbol

        vdd = PowerSymbol(lib_id="power:+3.3V", x=90, y=40, rotation=0)
        gnd = PowerSymbol(lib_id="power:GND", x=90, y=60, rotation=180)
        sch.power_symbols.extend([vdd, gnd])

        # Wiring
        # VDD rail: power symbol -> C1 pin 1 -> U1 VDD
        sch.wires.append(Wire(x1=90, y1=40, x2=90, y2=47.46))  # Power to C1 pin 1
        sch.wires.append(Wire(x1=90, y1=47.46, x2=100, y2=47.46))  # C1 to junction
        sch.wires.append(Wire(x1=100, y1=47.46, x2=100, y2=44.92))  # Junction to U1 VDD
        sch.junctions.append(Junction(x=90, y=47.46))

        # GND rail: power symbol -> C1 pin 2 -> U1 VSS
        sch.wires.append(Wire(x1=90, y1=60, x2=90, y2=52.54))  # Power to C1 pin 2
        sch.wires.append(Wire(x1=90, y1=52.54, x2=100, y2=52.54))  # C1 to junction
        sch.wires.append(Wire(x1=100, y1=52.54, x2=100, y2=55.08))  # Junction to U1 VSS
        sch.junctions.append(Junction(x=90, y=52.54))

        netlist = sch.extract_netlist()

        # Check +3.3V net
        assert "+3.3V" in netlist
        vdd_pins = netlist["+3.3V"]
        vdd_pin_strs = {str(p) for p in vdd_pins}
        assert "U1.1" in vdd_pin_strs
        assert "C1.1" in vdd_pin_strs

        # Check GND net
        assert "GND" in netlist
        gnd_pins = netlist["GND"]
        gnd_pin_strs = {str(p) for p in gnd_pins}
        assert "U1.2" in gnd_pin_strs
        assert "C1.2" in gnd_pin_strs

        # Verify connectivity
        assert sch.are_connected("U1", "1", "C1", "1") is True
        assert sch.are_connected("U1", "2", "C1", "2") is True
        assert sch.are_connected("U1", "1", "C1", "2") is False  # Different nets

    def test_global_label_connectivity(self):
        """Global labels connect nets across the schematic."""
        sch = Schematic("Test")

        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])

        # R1 with global label on pin 2
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        sch.symbols.append(r1)
        sch.wires.append(Wire(x1=102.54, y1=50, x2=110, y2=50))

        from kicad_tools.schematic.models.elements import GlobalLabel

        gl1 = GlobalLabel(text="I2C_SDA", x=110, y=50, shape="bidirectional")
        sch.global_labels.append(gl1)

        netlist = sch.extract_netlist()
        assert "I2C_SDA" in netlist
        pins = netlist["I2C_SDA"]
        assert len(pins) == 1
        assert pins[0].symbol_ref == "R1"
