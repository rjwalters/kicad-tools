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

        # IC with VDD pin.  Pin coordinates are library Y-UP (#738/#2129):
        # VDD at library (0, +2.54) lands at schematic (100, 47.46) after
        # the rotate-then-negate-Y transform in SymbolInstance.pin_position.
        ic_def = make_simple_symbol("MCU:Test", [("VDD", "1", 0, 2.54)])
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

        # IC with multiple power pins.  Pin coordinates are library Y-UP
        # (#738/#2129): VDD at library (0, +5.08) is the TOP pin and lands
        # at schematic (100, 44.92) after the rotate-then-negate-Y
        # transform; VSS at library (0, -5.08) lands at (100, 55.08).
        ic_def = make_simple_symbol(
            "MCU:Test",
            [
                ("VDD", "1", 0, 5.08),
                ("VSS", "2", 0, -5.08),
                ("IO", "3", 5.08, 0),
            ],
        )
        ic = SymbolInstance(symbol_def=ic_def, x=100, y=50, rotation=0, reference="U1", value="MCU")
        sch.symbols.append(ic)

        # Decoupling capacitor: pin 1 at library (0, +2.54) -> schematic
        # (90, 47.46); pin 2 at library (0, -2.54) -> schematic (90, 52.54).
        c_def = make_simple_symbol("Device:C", [("~", "1", 0, 2.54), ("~", "2", 0, -2.54)])
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


class TestPinEndpointOnlyWireAttach:
    """Pins attach to wires at endpoints or junctions only (PR #4003).

    Pins the KiCad connection semantics adopted for the board-05
    phantom-LVS-short fix: a symbol/power pin connects to a wire only
    when a wire *endpoint* lands on the pin position or a junction dot
    sits there.  A wire passing straight *through* a pin position
    mid-span does NOT connect (KiCad's netlister keeps them separate),
    while labels keep the mid-span attach (a label placed anywhere
    along a wire names that wire).
    """

    def _crossing_schematic(self) -> Schematic:
        """R1 pin 2 crossed mid-span by a vertical wire ending on R2 pin 1.

        R1 at (100, 50) puts pin 2 at (102.54, 50).  The vertical wire
        runs (102.54, 40) -> (102.54, 60), passing straight through the
        pin position mid-span.  Its top endpoint (102.54, 40) lands
        exactly on R2 pin 1 (R2 at (105.08, 40), pin 1 offset -2.54).
        """
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        r2 = SymbolInstance(
            symbol_def=r_def, x=105.08, y=40, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([r1, r2])
        sch.wires.append(Wire(x1=102.54, y1=40, x2=102.54, y2=60))
        return sch

    def test_wire_crossing_pin_mid_span_does_not_connect(self):
        """A wire passing through a pin position mid-span is NOT a connection.

        This was the board-05 false short: a motor-phase wire drawn
        across U10's SWDIO pin fused two unrelated nets even though the
        KiCad netlister keeps them separate.
        """
        sch = self._crossing_schematic()
        assert sch.are_connected("R1", "2", "R2", "1") is False

    def test_junction_at_pin_position_connects(self):
        """A junction dot at the pin position restores the connection.

        The junction shares the pin's rounded coordinate key, so the
        junction loop unions the crossing wire into the pin's point even
        though the pin itself only attaches at wire endpoints.
        """
        sch = self._crossing_schematic()
        sch.junctions.append(Junction(x=102.54, y=50))
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_wire_endpoint_on_pin_connects(self):
        """A wire *ending* exactly on the pin position still connects."""
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        r2 = SymbolInstance(
            symbol_def=r_def, x=105.08, y=40, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([r1, r2])
        # Wire endpoint (102.54, 50) lands ON R1 pin 2; the other
        # endpoint (102.54, 40) lands on R2 pin 1.
        sch.wires.append(Wire(x1=102.54, y1=50, x2=102.54, y2=40))
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_label_mid_span_still_names_net(self):
        """Labels keep the mid-span attach: they name the wire they sit on."""
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        sch.symbols.append(r1)
        # Wire starts ON R1 pin 2 and runs east; the label sits mid-span.
        sch.wires.append(Wire(x1=102.54, y1=50, x2=110, y2=50))
        sch.labels.append(Label(text="SIG_MID", x=106, y=50))

        netlist = sch.extract_netlist()
        assert "SIG_MID" in netlist
        assert {str(p) for p in netlist["SIG_MID"]} == {"R1.2"}

    def test_power_symbol_mid_span_does_not_connect(self):
        """Power symbols follow the same endpoint-or-junction pin rule."""
        from kicad_tools.schematic.models.elements import PowerSymbol

        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(symbol_def=r_def, x=100, y=50, rotation=0, reference="R1", value="10k")
        sch.symbols.append(r1)
        # Wire starts ON R1 pin 2; the GND symbol's pin sits mid-span.
        sch.wires.append(Wire(x1=102.54, y1=50, x2=110, y2=50))
        sch.power_symbols.append(PowerSymbol(lib_id="power:GND", x=106, y=50, rotation=0))

        netlist = sch.extract_netlist()
        gnd_pins = {str(p) for p in netlist.get("GND", [])}
        assert "R1.2" not in gnd_pins


def _build_dual_unit_symdef() -> SymbolDef:
    """Build a SymbolDef mimicking a dual-unit LM393 comparator.

    Follows the ``LM393_<unit>_<style>`` parser tagging convention:

    - Unit 1 (channel A): IN- (pin 2), IN+ (pin 3), OUT_A (pin 1).
    - Unit 2 (channel B): IN- (pin 6), IN+ (pin 5), OUT_B (pin 7).
    - Common package power (unit 0): V+ (pin 8), V- (pin 4).

    Pins carry non-zero symbol-local ``x`` offsets so a placed instance's
    ``pin_position`` differs from the instance origin — this lets the
    regression tests wire unit 2's pins at *different sheet coordinates*
    than unit 1's, which is exactly the geometry that exposed the
    phantom-net bug (issue #4020).
    """
    return SymbolDef(
        lib_id="Comparator:LM393",
        name="LM393",
        raw_sexp="",
        pins=[
            # Unit 1 (channel A)
            Pin(
                name="-",
                number="2",
                x=-2.54,
                y=2.54,
                angle=0,
                length=2.54,
                pin_type="input",
                unit=1,
            ),
            Pin(
                name="+",
                number="3",
                x=-2.54,
                y=-2.54,
                angle=0,
                length=2.54,
                pin_type="input",
                unit=1,
            ),
            Pin(
                name="OUT_A",
                number="1",
                x=2.54,
                y=0,
                angle=0,
                length=2.54,
                pin_type="open_collector",
                unit=1,
            ),
            # Unit 2 (channel B)
            Pin(
                name="-",
                number="6",
                x=-2.54,
                y=2.54,
                angle=0,
                length=2.54,
                pin_type="input",
                unit=2,
            ),
            Pin(
                name="+",
                number="5",
                x=-2.54,
                y=-2.54,
                angle=0,
                length=2.54,
                pin_type="input",
                unit=2,
            ),
            Pin(
                name="OUT_B",
                number="7",
                x=2.54,
                y=0,
                angle=0,
                length=2.54,
                pin_type="open_collector",
                unit=2,
            ),
            # Unit 0 (common package power)
            Pin(
                name="V+",
                number="8",
                x=0,
                y=2.54,
                angle=0,
                length=2.54,
                pin_type="power_in",
                unit=0,
            ),
            Pin(
                name="V-",
                number="4",
                x=0,
                y=-2.54,
                angle=0,
                length=2.54,
                pin_type="power_in",
                unit=0,
            ),
        ],
    )


class TestMultiUnitNetResolution:
    """Multi-unit symbols resolve nets per placed unit (issue #4020).

    A multi-unit symbol (e.g. LM393) shares one ``symbol_def.pins`` list
    across several placed ``SymbolInstance`` rows that share a
    ``reference`` but differ in ``unit``.  Connectivity must attribute
    each pin to the instance that actually owns it; otherwise a
    unit-2-only pin gets positioned at a unit-1 instance's anchor and
    produces phantom nets, false ``are_connected`` results, and duplicate
    ``PinRef`` entries.
    """

    def _dual_unit_schematic(self) -> Schematic:
        """Place unit 1 @ (100,100) and unit 2 @ (200,200) — different coords.

        Unit 1's OUT_A (pin 1, +2.54 x offset) lands at (102.54, 100) and
        is wired to a net labelled ``A_OUT``.  Unit 2's OUT_B (pin 7) is
        left unwired at (202.54, 200).  Under the old buggy resolution,
        pin 7 would be registered at unit 1's geometry and appear on
        ``A_OUT``; the fix keeps it floating.
        """
        sch = Schematic("Test")
        sym_def = _build_dual_unit_symdef()
        u1 = SymbolInstance(
            symbol_def=sym_def, x=100, y=100, rotation=0, reference="U1", value="LM393", unit=1
        )
        u2 = SymbolInstance(
            symbol_def=sym_def, x=200, y=200, rotation=0, reference="U1", value="LM393", unit=2
        )
        sch.symbols.extend([u1, u2])
        # Wire unit 1's OUT_A (102.54, 100) east and name it A_OUT.
        sch.wires.append(Wire(x1=102.54, y1=100, x2=110, y2=100))
        sch.labels.append(Label(text="A_OUT", x=110, y=100))
        return sch

    def test_get_net_for_pin_resolves_against_owning_unit(self):
        """Unit 1's OUT_A resolves to its real net, not a phantom."""
        sch = self._dual_unit_schematic()
        assert sch.get_net_for_pin("U1", "1") == "A_OUT"

    def test_get_net_for_pin_unwired_unit2_pin_is_floating(self):
        """Unit 2's OUT_B is unwired -> None, despite unit 1 being wired.

        The core phantom-net bug: pin 7 lives on unit 2 at (202.54, 200)
        with no wire, so it must read as floating even though unit 1's
        similarly-shaped geometry is wired to A_OUT.
        """
        sch = self._dual_unit_schematic()
        assert sch.get_net_for_pin("U1", "7") is None

    def test_get_net_for_pin_wired_unit2_pin_uses_unit2_wiring(self):
        """A wire at unit 2's real position names unit 2's pin, not unit 1's."""
        sch = self._dual_unit_schematic()
        # Wire unit 2's OUT_B at its actual sheet position (202.54, 200).
        sch.wires.append(Wire(x1=202.54, y1=200, x2=210, y2=200))
        sch.labels.append(Label(text="B_OUT", x=210, y=200))
        assert sch.get_net_for_pin("U1", "7") == "B_OUT"
        # Unit 1's pin 1 is still on its own net, unchanged.
        assert sch.get_net_for_pin("U1", "1") == "A_OUT"

    def test_are_connected_false_for_unwired_cross_unit_pins(self):
        """Two pins on different units, not wired together -> False.

        Under the old resolution both pins collapsed onto unit 1's
        geometry and reported a false connection.
        """
        sch = self._dual_unit_schematic()
        assert sch.are_connected("U1", "1", "U1", "7") is False

    def test_are_connected_true_when_units_actually_wired_together(self):
        """Cross-unit pins wired to the same net still report connected."""
        sch = self._dual_unit_schematic()
        # Wire unit 2's OUT_B (202.54, 200) back to the A_OUT net node so
        # both units genuinely share a net.
        sch.wires.append(Wire(x1=202.54, y1=200, x2=110, y2=100))
        assert sch.are_connected("U1", "1", "U1", "7") is True

    def test_extract_netlist_no_duplicate_unit_specific_pinrefs(self):
        """Unit-specific pins register once — no per-instance phantom dupes.

        Before the fix, every placed unit re-registered *every* unit's
        pins at its own anchor, so unit-specific pins like OUT_A (pin 1)
        and OUT_B (pin 7) appeared twice each.  After the fix each is
        attributed to its owning unit exactly once.

        Note: unit-0 *common* pins (V+/V-, pins 8/4) legitimately appear
        once per placed instance — each at its own resolved coordinate —
        because connectivity is being computed, not a one-shot flag
        (issue #4020 guidance).  They are excluded from this dedup check.
        """
        sch = self._dual_unit_schematic()
        netlist = sch.extract_netlist()

        common_pins = {"8", "4"}  # unit-0 V+ / V-
        unit_specific_refs = [
            str(p) for pins in netlist.values() for p in pins if p.pin not in common_pins
        ]
        assert len(unit_specific_refs) == len(set(unit_specific_refs)), (
            f"duplicate unit-specific PinRefs: {unit_specific_refs}"
        )
        # A_OUT carries exactly unit 1's OUT_A once (not twice from both
        # placed instances).
        assert [str(p) for p in netlist["A_OUT"]] == ["U1.1"]

    def test_extract_netlist_no_phantom_cross_unit_pin_on_wired_net(self):
        """The wired net (A_OUT) does not gain unit 2's pin as a phantom.

        This is the direct netlist-level symptom of the bug: unit 2's
        OUT_B (pin 7), positioned against unit 1's geometry under the old
        resolution, would union onto A_OUT.  It must not appear there.
        """
        sch = self._dual_unit_schematic()
        netlist = sch.extract_netlist()
        a_out = {str(p) for p in netlist["A_OUT"]}
        assert "U1.7" not in a_out

    def test_pins_on_net_no_phantom_cross_unit_pin(self):
        """pins_on_net for unit 1's net does not include unit 2's pin."""
        sch = self._dual_unit_schematic()
        pins = {str(p) for p in sch.pins_on_net("A_OUT")}
        assert pins == {"U1.1"}
        assert "U1.7" not in pins

    def test_common_pin_connects_across_all_placed_units(self):
        """Unit-0 common pins (V+/V-) still connect through any unit.

        Common package-power pins are shared by every unit; wiring them
        at one placed instance's position must still resolve — the fix
        must not over-restrict unit-0 commons.
        """
        sch = self._dual_unit_schematic()
        # V+ (pin 8) on unit 1 resolves to (100, 97.46) after the
        # schematic Y-flip; wire it there and name the net VCC.
        assert sch.symbols[0].pin_position("8") == (100.0, 97.46)
        sch.wires.append(Wire(x1=100, y1=97.46, x2=100, y2=90))
        sch.labels.append(Label(text="VCC", x=100, y=90))
        assert sch.get_net_for_pin("U1", "8") == "VCC"


class TestWireToWireCollinearUnion:
    """Wire-to-wire collinear overlap and T-touch union (issue #4143).

    KiCad merges the nets of any two touching/overlapping collinear wire
    segments, not only wires that share an exact endpoint.  The connectivity
    graph historically unioned wires only at their two endpoints, silently
    diverging from KiCad on the stub-wire+label idiom that generated the
    softstart rev-B ``+3.3V``/``GND`` short.  These tests lock in the fix.

    NOTE: this is the mirror image of :class:`TestPinEndpointOnlyWireAttach`.
    Pins still attach to wires endpoint-only (#4020/#4003); only *wire-to-
    wire* unions gain collinear-overlap / T-touch semantics.
    """

    def _labeled_nets(self, sch: Schematic, net_a: str, net_b: str):
        """Return the (net_a pins, net_b pins) sets from a fresh netlist."""
        netlist = sch.extract_netlist()
        a = {str(p) for p in netlist.get(net_a, [])}
        b = {str(p) for p in netlist.get(net_b, [])}
        return netlist, a, b

    def _two_stub_schematic(
        self,
        a_span: tuple[float, float],
        b_span: tuple[float, float],
        y: float = 100.0,
        junction: bool = True,
    ) -> Schematic:
        """Two horizontal stub wires on ``y`` with a label on each.

        Each stub carries a symbol pin at its far end plus a net label, so
        the two labels' merge status is observable in the netlist.

        When ``junction`` is True (default) a junction dot is placed inside
        the collinear-overlap sub-segment, representing the *intentional*
        merge a real generator would emit (KiCad requires a dot to merge two
        overlapping wires — issue #4226).  Pass ``junction=False`` to model a
        dot-less incidental graze, which KiCad does NOT merge.
        """
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        # R1 pin 2 lands on the start of stub A; R2 pin 1 on the end of B.
        r1 = SymbolInstance(
            symbol_def=r_def, x=a_span[0] - 2.54, y=y, rotation=0, reference="R1", value="10k"
        )
        r2 = SymbolInstance(
            symbol_def=r_def, x=b_span[1] + 2.54, y=y, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([r1, r2])
        sch.wires.append(Wire(x1=a_span[0], y1=y, x2=a_span[1], y2=y))
        sch.wires.append(Wire(x1=b_span[0], y1=y, x2=b_span[1], y2=y))
        sch.labels.append(Label(text="NET_A", x=a_span[0], y=y))
        sch.labels.append(Label(text="NET_B", x=b_span[1], y=y))
        if junction:
            # Junction dot at the midpoint of the shared overlap sub-segment,
            # marking the intended wire-to-wire merge (issue #4226/#4143).
            lo = max(a_span[0], b_span[0])
            hi = min(a_span[1], b_span[1])
            sch.junctions.append(Junction(x=(lo + hi) / 2.0, y=y))
        return sch

    def test_partial_overlap_neither_contains_the_other(self):
        """A=[100,109], B=[103,112] share [103,109]; must union.

        This is the exact softstart repro geometry and the case the old
        ``_is_collinear_overlap`` (full-containment only) does NOT catch.
        """
        sch = self._two_stub_schematic((100.0, 109.0), (103.0, 112.0))
        netlist, a, b = self._labeled_nets(sch, "NET_A", "NET_B")
        # Post-fix: both stubs merge into ONE net — extract_netlist collapses
        # to a single named net carrying both R1.2 and R2.1.
        merged = a or b
        assert "R1.2" in merged and "R2.1" in merged
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_full_containment(self):
        """A=[100,112] fully contains B=[103,109]; must union."""
        sch = self._two_stub_schematic((100.0, 112.0), (103.0, 109.0))
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_repro_sketch_r1_r2_collinear_stubs(self):
        """Issue repro: R1/R2 ~8mm apart, overlapping collinear stubs.

        R1 at x=100, R2 at x=108; stub A reaches x=109, stub B reaches back
        to x=103 — overlapping on y=100.  Pre-fix NET_A/NET_B were distinct;
        post-fix they are one net (KiCad merges).
        """
        sch = self._two_stub_schematic((100.0, 109.0), (103.0, 112.0))
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_mid_segment_t_touch(self):
        """B's endpoint lands in the interior of A; must union.

        A runs horizontally [100,112] on y=100.  B is vertical from
        (106,100) up to (106,90): B's lower endpoint (106,100) sits on A's
        interior — a T-touch KiCad treats as connected.
        """
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def, x=97.46, y=100, rotation=0, reference="R1", value="10k"
        )
        r2 = SymbolInstance(symbol_def=r_def, x=106, y=85, rotation=0, reference="R2", value="10k")
        sch.symbols.extend([r1, r2])
        # A: horizontal, R1 pin 2 at (100,100) on its start.
        sch.wires.append(Wire(x1=100, y1=100, x2=112, y2=100))
        # B: vertical, lower end T-touches A's interior at (106,100); upper
        # end (106,90) lands on R2 pin 1 (R2 at (106,85), pin1 y-offset).
        sch.wires.append(Wire(x1=106, y1=100, x2=106, y2=90))
        # Junction dot at the T-touch point marks the intentional connection
        # (KiCad merges a mid-segment T-touch only where a dot sits — #4226).
        sch.junctions.append(Junction(x=106, y=100))
        # R2 pin 1 sits at (103.46, 85)?  Recompute: place R2 so pin1 lands
        # on the wire's free end instead — put R2 pin1 at (106,90).
        r2b = SymbolInstance(
            symbol_def=r_def, x=108.54, y=90, rotation=0, reference="R3", value="10k"
        )
        sch.symbols.append(r2b)  # R3 pin1 at (106,90)
        assert sch.symbols[2].pin_position("1") == (106.0, 90.0)
        assert sch.are_connected("R1", "2", "R3", "1") is True

    def test_same_endpoint_still_connects(self):
        """The pre-existing shared-endpoint case is unchanged."""
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def, x=97.46, y=100, rotation=0, reference="R1", value="10k"
        )
        r2 = SymbolInstance(
            symbol_def=r_def, x=114.54, y=100, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([r1, r2])
        sch.wires.append(Wire(x1=100, y1=100, x2=106, y2=100))
        sch.wires.append(Wire(x1=106, y1=100, x2=112, y2=100))
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_no_union_when_parallel_but_not_collinear(self):
        """Two parallel wires on different Y do NOT union."""
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def, x=97.46, y=100, rotation=0, reference="R1", value="10k"
        )
        r2 = SymbolInstance(
            symbol_def=r_def, x=97.46, y=110, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([r1, r2])
        sch.wires.append(Wire(x1=100, y1=100, x2=112, y2=100))
        sch.wires.append(Wire(x1=100, y1=110, x2=112, y2=110))  # 10mm below
        assert sch.are_connected("R1", "2", "R2", "1") is False

    def test_are_connected_and_get_net_for_pin_reflect_fix(self):
        """are_connected() and get_net_for_pin() inherit the fix (no patch)."""
        sch = self._two_stub_schematic((100.0, 109.0), (103.0, 112.0))
        # Both pins land on the merged net; get_net_for_pin returns the same
        # named net for both.
        net1 = sch.get_net_for_pin("R1", "2")
        net2 = sch.get_net_for_pin("R2", "1")
        assert net1 == net2
        assert net1 in ("NET_A", "NET_B")

    def test_softstart_power_vs_power_merge_pattern(self):
        """Interleaved decoupling-cap stubs merge two power rails (class a).

        Synthetic reproduction of the softstart +3.3V/GND geometric class:
        two power-symbol stubs on the same Y with different X extents that
        overlap collinearly.  Post-fix the two power nets are unioned.
        """
        from kicad_tools.schematic.models.elements import PowerSymbol

        sch = Schematic("Test")
        # +3.3V stub [100,108], GND stub [104,112] overlap on [104,108].
        sch.wires.append(Wire(x1=100, y1=100, x2=108, y2=100))
        sch.wires.append(Wire(x1=104, y1=100, x2=112, y2=100))
        # Junction dot inside the [104,108] overlap marks the intended merge
        # (KiCad requires a dot to union two overlapping wires — #4226).
        sch.junctions.append(Junction(x=106, y=100))
        sch.power_symbols.append(PowerSymbol(lib_id="power:+3.3V", x=100, y=100, rotation=0))
        sch.power_symbols.append(PowerSymbol(lib_id="power:GND", x=112, y=100, rotation=0))
        # Add a pin on each end so the merged component surfaces in netlist.
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        sch.symbols.append(
            SymbolInstance(symbol_def=r_def, x=97.46, y=100, rotation=0, reference="R1", value="1k")
        )
        sch.symbols.append(
            SymbolInstance(
                symbol_def=r_def, x=114.54, y=100, rotation=0, reference="R2", value="1k"
            )
        )
        # R1.1 (at 94.92) floats; R1.2 at (100,100) and R2.1 at (112,100)
        # land on the two overlapping stubs and therefore merge.
        assert sch.are_connected("R1", "2", "R2", "1") is True

    def test_same_net_overlap_no_conflict(self):
        """Two overlapping stubs with the SAME label merge uneventfully."""
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def, x=97.46, y=100, rotation=0, reference="R1", value="10k"
        )
        r2 = SymbolInstance(
            symbol_def=r_def, x=114.54, y=100, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([r1, r2])
        sch.wires.append(Wire(x1=100, y1=100, x2=109, y2=100))
        sch.wires.append(Wire(x1=103, y1=100, x2=112, y2=100))
        # Junction inside the [103,109] overlap marks the intended merge.
        sch.junctions.append(Junction(x=106, y=100))
        sch.labels.append(Label(text="VBUS", x=100, y=100))
        sch.labels.append(Label(text="VBUS", x=112, y=100))
        netlist = sch.extract_netlist()
        assert "VBUS" in netlist
        assert {str(p) for p in netlist["VBUS"]} == {"R1.2", "R2.1"}

    def test_dotless_collinear_overlap_does_not_merge(self):
        """A dot-less collinear graze must NOT merge nets (issue #4226).

        This is the board-05 false-short pattern: two differently-labelled
        stubs overlap collinearly with NO junction dot at the overlap.
        KiCad keeps them separate; the junction-gated predicate must too.
        """
        sch = self._two_stub_schematic((100.0, 109.0), (103.0, 112.0), junction=False)
        assert sch.are_connected("R1", "2", "R2", "1") is False
        net1 = sch.get_net_for_pin("R1", "2")
        net2 = sch.get_net_for_pin("R2", "1")
        assert {net1, net2} == {"NET_A", "NET_B"}

    def test_dotless_t_touch_does_not_merge(self):
        """A dot-less mid-segment T-touch must NOT merge nets (issue #4226)."""
        sch = Schematic("Test")
        r_def = make_simple_symbol("Device:R", [("~", "1", -2.54, 0), ("~", "2", 2.54, 0)])
        r1 = SymbolInstance(
            symbol_def=r_def, x=97.46, y=100, rotation=0, reference="R1", value="10k"
        )
        r3 = SymbolInstance(
            symbol_def=r_def, x=108.54, y=90, rotation=0, reference="R3", value="10k"
        )
        sch.symbols.extend([r1, r3])
        sch.wires.append(Wire(x1=100, y1=100, x2=112, y2=100))
        # B's lower end (106,100) T-touches A's interior, but NO junction dot.
        sch.wires.append(Wire(x1=106, y1=100, x2=106, y2=90))
        sch.labels.append(Label(text="NET_A", x=100, y=100))
        sch.labels.append(Label(text="NET_B", x=106, y=90))
        assert sch.are_connected("R1", "2", "R3", "1") is False
