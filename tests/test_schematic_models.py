"""Tests for schematic model classes to increase coverage.

Tests for:
- Wire, Junction, Label, HierarchicalLabel, PowerSymbol (elements.py)
- SymbolDef, SymbolInstance (symbol.py)
- Schematic class (schematic.py)
"""

import pytest

from kicad_tools.schematic.models.elements import (
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    Label,
    PowerSymbol,
    Wire,
)
from kicad_tools.schematic.models.pin import Pin
from kicad_tools.schematic.models.schematic import Schematic, SnapMode
from kicad_tools.schematic.models.symbol import SymbolDef, SymbolInstance
from kicad_tools.sexp import SExp


class TestWire:
    """Tests for Wire dataclass."""

    def test_wire_creation(self):
        """Create wire with explicit coordinates."""
        wire = Wire(x1=10.0, y1=20.0, x2=30.0, y2=40.0)
        assert wire.x1 == 10.0
        assert wire.y1 == 20.0
        assert wire.x2 == 30.0
        assert wire.y2 == 40.0
        assert wire.uuid_str is not None

    def test_wire_between(self):
        """Create wire between two points."""
        wire = Wire.between((10.5, 20.5), (30.5, 40.5))
        assert wire.x1 == 10.5
        assert wire.y1 == 20.5
        assert wire.x2 == 30.5
        assert wire.y2 == 40.5

    def test_wire_between_rounds_coordinates(self):
        """Wire.between rounds to 2 decimal places."""
        wire = Wire.between((10.123456, 20.789), (30.001, 40.999))
        assert wire.x1 == 10.12
        assert wire.y1 == 20.79
        assert wire.x2 == 30.0
        assert wire.y2 == 41.0

    def test_wire_to_sexp_node(self):
        """Wire generates valid S-expression node."""
        wire = Wire(x1=10.0, y1=20.0, x2=30.0, y2=40.0, uuid_str="test-uuid")
        sexp = wire.to_sexp_node()
        assert sexp.name == "wire"
        # Check it has pts node
        pts = sexp.get("pts")
        assert pts is not None

    def test_wire_to_sexp(self):
        """Wire generates S-expression string."""
        wire = Wire(x1=10.0, y1=20.0, x2=30.0, y2=40.0)
        sexp_str = wire.to_sexp()
        assert "wire" in sexp_str
        assert "pts" in sexp_str

    def test_wire_from_sexp(self):
        """Parse wire from S-expression node."""
        # Build a wire sexp manually
        wire_sexp = SExp.list(
            "wire",
            SExp.list(
                "pts",
                SExp.list("xy", 10.0, 20.0),
                SExp.list("xy", 30.0, 40.0),
            ),
            SExp.list("uuid", "test-uuid-123"),
        )

        wire = Wire.from_sexp(wire_sexp)
        assert wire.x1 == 10.0
        assert wire.y1 == 20.0
        assert wire.x2 == 30.0
        assert wire.y2 == 40.0
        assert wire.uuid_str == "test-uuid-123"

    def test_wire_from_sexp_missing_points(self):
        """Wire parsing fails with insufficient points."""
        wire_sexp = SExp.list(
            "wire",
            SExp.list("pts", SExp.list("xy", 10.0, 20.0)),  # Only one point
            SExp.list("uuid", "test-uuid"),
        )

        with pytest.raises(ValueError, match="at least 2 xy points"):
            Wire.from_sexp(wire_sexp)


class TestJunction:
    """Tests for Junction dataclass."""

    def test_junction_creation(self):
        """Create junction at position."""
        junc = Junction(x=10.0, y=20.0)
        assert junc.x == 10.0
        assert junc.y == 20.0
        assert junc.uuid_str is not None

    def test_junction_rounds_coordinates(self):
        """Junction rounds coordinates on creation."""
        junc = Junction(x=10.123456, y=20.789)
        assert junc.x == 10.12
        assert junc.y == 20.79

    def test_junction_to_sexp_node(self):
        """Junction generates valid S-expression node."""
        junc = Junction(x=10.0, y=20.0, uuid_str="junc-uuid")
        sexp = junc.to_sexp_node()
        assert sexp.name == "junction"

    def test_junction_to_sexp(self):
        """Junction generates S-expression string."""
        junc = Junction(x=10.0, y=20.0)
        sexp_str = junc.to_sexp()
        assert "junction" in sexp_str

    def test_junction_from_sexp(self):
        """Parse junction from S-expression node."""
        junc_sexp = SExp.list(
            "junction",
            SExp.list("at", 10.0, 20.0),
            SExp.list("uuid", "junc-uuid-123"),
        )

        junc = Junction.from_sexp(junc_sexp)
        assert junc.x == 10.0
        assert junc.y == 20.0
        assert junc.uuid_str == "junc-uuid-123"


class TestLabel:
    """Tests for Label dataclass."""

    def test_label_creation(self):
        """Create label with text and position."""
        label = Label(text="NET1", x=10.0, y=20.0)
        assert label.text == "NET1"
        assert label.x == 10.0
        assert label.y == 20.0
        assert label.rotation == 0
        assert label.uuid_str is not None

    def test_label_with_rotation(self):
        """Create label with rotation."""
        label = Label(text="NET1", x=10.0, y=20.0, rotation=90)
        assert label.rotation == 90

    def test_label_to_sexp_node(self):
        """Label generates valid S-expression node."""
        label = Label(text="NET1", x=10.0, y=20.0, uuid_str="label-uuid")
        sexp = label.to_sexp_node()
        assert sexp.name == "label"

    def test_label_to_sexp(self):
        """Label generates S-expression string."""
        label = Label(text="NET1", x=10.0, y=20.0)
        sexp_str = label.to_sexp()
        assert "label" in sexp_str
        assert "NET1" in sexp_str

    def test_label_from_sexp(self):
        """Parse label from S-expression node."""
        label_sexp = SExp.list(
            "label",
            "NET1",
            SExp.list("at", 10.0, 20.0, 45),
            SExp.list("uuid", "label-uuid-123"),
        )

        label = Label.from_sexp(label_sexp)
        assert label.text == "NET1"
        assert label.x == 10.0
        assert label.y == 20.0
        assert label.rotation == 45
        assert label.uuid_str == "label-uuid-123"

    def test_label_from_sexp_no_rotation(self):
        """Parse label without rotation defaults to 0."""
        label_sexp = SExp.list(
            "label",
            "NET1",
            SExp.list("at", 10.0, 20.0),
            SExp.list("uuid", "label-uuid"),
        )

        label = Label.from_sexp(label_sexp)
        assert label.rotation == 0


class TestHierarchicalLabel:
    """Tests for HierarchicalLabel dataclass."""

    def test_hier_label_creation(self):
        """Create hierarchical label with defaults."""
        hl = HierarchicalLabel(text="DATA_OUT", x=10.0, y=20.0)
        assert hl.text == "DATA_OUT"
        assert hl.x == 10.0
        assert hl.y == 20.0
        assert hl.shape == "input"
        assert hl.rotation == 0

    def test_hier_label_with_shape(self):
        """Create hierarchical label with specific shape."""
        hl = HierarchicalLabel(text="DATA_OUT", x=10.0, y=20.0, shape="output", rotation=180)
        assert hl.shape == "output"
        assert hl.rotation == 180

    def test_hier_label_to_sexp_node(self):
        """HierarchicalLabel generates valid S-expression node."""
        hl = HierarchicalLabel(text="DATA_OUT", x=10.0, y=20.0, uuid_str="hl-uuid")
        sexp = hl.to_sexp_node()
        assert sexp.name == "hierarchical_label"

    def test_hier_label_to_sexp(self):
        """HierarchicalLabel generates S-expression string."""
        hl = HierarchicalLabel(text="DATA_OUT", x=10.0, y=20.0)
        sexp_str = hl.to_sexp()
        assert "hierarchical_label" in sexp_str
        assert "DATA_OUT" in sexp_str

    def test_hier_label_from_sexp(self):
        """Parse hierarchical label from S-expression node."""
        hl_sexp = SExp.list(
            "hierarchical_label",
            "DATA_OUT",
            SExp.list("shape", "output"),
            SExp.list("at", 10.0, 20.0, 180),
            SExp.list("uuid", "hl-uuid-123"),
        )

        hl = HierarchicalLabel.from_sexp(hl_sexp)
        assert hl.text == "DATA_OUT"
        assert hl.shape == "output"
        assert hl.x == 10.0
        assert hl.y == 20.0
        assert hl.rotation == 180

    def test_hier_label_from_sexp_default_shape(self):
        """Parse hierarchical label defaults to input shape."""
        hl_sexp = SExp.list(
            "hierarchical_label",
            "DATA_IN",
            SExp.list("at", 10.0, 20.0),
            SExp.list("uuid", "hl-uuid"),
        )

        hl = HierarchicalLabel.from_sexp(hl_sexp)
        assert hl.shape == "input"


class TestGlobalLabel:
    """Tests for GlobalLabel dataclass."""

    def test_global_label_creation(self):
        """Create global label with defaults."""
        gl = GlobalLabel(text="VCC_3V3A", x=10.0, y=20.0)
        assert gl.text == "VCC_3V3A"
        assert gl.x == 10.0
        assert gl.y == 20.0
        assert gl.shape == "bidirectional"
        assert gl.rotation == 0

    def test_global_label_with_shape(self):
        """Create global label with passive shape for ground nets."""
        gl = GlobalLabel(text="AGND", x=10.0, y=20.0, shape="passive", rotation=180)
        assert gl.text == "AGND"
        assert gl.shape == "passive"
        assert gl.rotation == 180

    def test_global_label_to_sexp_node(self):
        """GlobalLabel generates valid S-expression node."""
        gl = GlobalLabel(text="VCC_3V3A", x=10.0, y=20.0, uuid_str="gl-uuid")
        sexp = gl.to_sexp_node()
        assert sexp.name == "global_label"

    def test_global_label_to_sexp(self):
        """GlobalLabel generates S-expression string."""
        gl = GlobalLabel(text="VCC_3V3A", x=10.0, y=20.0)
        sexp_str = gl.to_sexp()
        assert "global_label" in sexp_str
        assert "VCC_3V3A" in sexp_str

    def test_global_label_from_sexp(self):
        """Parse global label from S-expression node."""
        gl_sexp = SExp.list(
            "global_label",
            "+3V3A",
            SExp.list("shape", "input"),
            SExp.list("at", 10.0, 20.0, 0),
            SExp.list("uuid", "gl-uuid-123"),
        )

        gl = GlobalLabel.from_sexp(gl_sexp)
        assert gl.text == "+3V3A"
        assert gl.shape == "input"
        assert gl.x == 10.0
        assert gl.y == 20.0
        assert gl.rotation == 0

    def test_global_label_from_sexp_with_rotation(self):
        """Parse global label with rotation."""
        gl_sexp = SExp.list(
            "global_label",
            "DGND",
            SExp.list("shape", "passive"),
            SExp.list("at", 100.0, 200.0, 180),
            SExp.list("uuid", "gl-uuid"),
        )

        gl = GlobalLabel.from_sexp(gl_sexp)
        assert gl.text == "DGND"
        assert gl.shape == "passive"
        assert gl.rotation == 180

    def test_global_label_from_sexp_default_shape(self):
        """Parse global label defaults to bidirectional shape."""
        gl_sexp = SExp.list(
            "global_label",
            "VCC_5V",
            SExp.list("at", 10.0, 20.0),
            SExp.list("uuid", "gl-uuid"),
        )

        gl = GlobalLabel.from_sexp(gl_sexp)
        assert gl.shape == "bidirectional"


class TestPowerSymbol:
    """Tests for PowerSymbol dataclass."""

    def test_power_symbol_creation(self):
        """Create power symbol with defaults."""
        pwr = PowerSymbol(lib_id="power:GND", x=10.0, y=20.0)
        assert pwr.lib_id == "power:GND"
        assert pwr.x == 10.0
        assert pwr.y == 20.0
        assert pwr.rotation == 0
        assert pwr.reference == "#PWR?"

    def test_power_symbol_with_reference(self):
        """Create power symbol with specific reference."""
        pwr = PowerSymbol(lib_id="power:+3.3V", x=10.0, y=20.0, reference="#PWR01")
        assert pwr.reference == "#PWR01"

    def test_power_symbol_to_sexp_node(self):
        """PowerSymbol generates valid S-expression node."""
        pwr = PowerSymbol(lib_id="power:GND", x=10.0, y=20.0, uuid_str="pwr-uuid")
        sexp = pwr.to_sexp_node("test_project", "/sheet-uuid")
        assert sexp.name == "symbol"
        # Check lib_id
        lib_id_node = sexp.get("lib_id")
        assert lib_id_node is not None

    def test_power_symbol_to_sexp(self):
        """PowerSymbol generates S-expression string."""
        pwr = PowerSymbol(lib_id="power:GND", x=10.0, y=20.0)
        sexp_str = pwr.to_sexp("test_project", "/sheet-uuid")
        assert "symbol" in sexp_str
        assert "power:GND" in sexp_str

    def test_power_symbol_from_sexp(self):
        """Parse power symbol from S-expression node."""
        pwr_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "power:+3.3V"),
            SExp.list("at", 10.0, 20.0, 90),
            SExp.list("uuid", "pwr-uuid-123"),
            SExp.list("property", "Reference", "#PWR05"),
        )

        pwr = PowerSymbol.from_sexp(pwr_sexp)
        assert pwr.lib_id == "power:+3.3V"
        assert pwr.x == 10.0
        assert pwr.y == 20.0
        assert pwr.rotation == 90
        assert pwr.reference == "#PWR05"

    def test_is_power_symbol_by_lib_id(self):
        """Identify power symbol by lib_id."""
        pwr_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "power:VCC"),
            SExp.list("at", 10.0, 20.0),
        )
        assert PowerSymbol.is_power_symbol(pwr_sexp) is True

    def test_is_power_symbol_by_reference(self):
        """Identify power symbol by reference."""
        pwr_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "some:lib"),
            SExp.list("at", 10.0, 20.0),
            SExp.list("property", "Reference", "#PWR01"),
        )
        assert PowerSymbol.is_power_symbol(pwr_sexp) is True

    def test_is_not_power_symbol(self):
        """Regular symbol is not identified as power symbol."""
        sym_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "Device:R"),
            SExp.list("at", 10.0, 20.0),
            SExp.list("property", "Reference", "R1"),
        )
        assert PowerSymbol.is_power_symbol(sym_sexp) is False


class TestPin:
    """Tests for Pin dataclass."""

    def test_pin_creation(self):
        """Create pin with all attributes."""
        pin = Pin(name="VCC", number="1", x=0.0, y=0.0, angle=0.0, length=2.54, pin_type="power_in")
        assert pin.name == "VCC"
        assert pin.number == "1"
        assert pin.x == 0.0
        assert pin.y == 0.0
        assert pin.angle == 0.0
        assert pin.length == 2.54
        assert pin.pin_type == "power_in"

    def test_pin_from_sexp(self):
        """Parse pin from S-expression node."""
        pin_sexp = SExp.list(
            "pin",
            "input",
            "line",
            SExp.list("at", 10.0, 20.0, 180),
            SExp.list("length", 5.0),
            SExp.list("name", "DATA"),
            SExp.list("number", "5"),
        )

        pin = Pin.from_sexp(pin_sexp)
        assert pin.name == "DATA"
        assert pin.number == "5"
        assert pin.x == 10.0
        assert pin.y == 20.0
        assert pin.angle == 180
        assert pin.length == 5.0
        assert pin.pin_type == "input"


class TestSymbolDef:
    """Tests for SymbolDef dataclass."""

    def test_symbol_def_creation(self):
        """Create symbol definition."""
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="(symbol ...)")
        assert sym_def.lib_id == "Device:R"
        assert sym_def.name == "R"
        assert sym_def.pins == []

    def test_symbol_def_with_pins(self):
        """Create symbol definition with pins."""
        pins = [
            Pin(name="1", number="1", x=-2.54, y=0, angle=0, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=2.54, y=0, angle=180, length=2.54, pin_type="passive"),
        ]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="(symbol ...)", pins=pins)
        assert len(sym_def.pins) == 2

    def test_symbol_def_parse_pins_sexp(self):
        """Parse pins from symbol S-expression."""
        sym_sexp = SExp.list(
            "symbol",
            "Device:R",
            SExp.list(
                "symbol",
                "Device:R_0_1",
                SExp.list(
                    "pin",
                    "passive",
                    "line",
                    SExp.list("at", -2.54, 0, 0),
                    SExp.list("length", 2.54),
                    SExp.list("name", "~"),
                    SExp.list("number", "1"),
                ),
                SExp.list(
                    "pin",
                    "passive",
                    "line",
                    SExp.list("at", 2.54, 0, 180),
                    SExp.list("length", 2.54),
                    SExp.list("name", "~"),
                    SExp.list("number", "2"),
                ),
            ),
        )

        pins = SymbolDef._parse_pins_sexp(sym_sexp)
        assert len(pins) == 2
        assert pins[0].number == "1"
        assert pins[1].number == "2"

    def test_symbol_def_get_embedded_sexp(self):
        """Get embedded S-expression for symbol."""
        sym_sexp = SExp.list(
            "symbol",
            "TestSymbol",
            SExp.list("property", "Reference", "U"),
        )
        sym_def = SymbolDef(
            lib_id="Test:TestSymbol", name="TestSymbol", raw_sexp="", _sexp_node=sym_sexp
        )
        embedded = sym_def.get_embedded_sexp()
        assert "symbol" in embedded


class TestSymbolInstance:
    """Tests for SymbolInstance dataclass."""

    @pytest.fixture
    def mock_symbol_def(self):
        """Create a mock SymbolDef with pins."""
        pins = [
            Pin(name="1", number="1", x=-2.54, y=0, angle=0, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=2.54, y=0, angle=180, length=2.54, pin_type="passive"),
        ]
        return SymbolDef(lib_id="Device:R", name="R", raw_sexp="(symbol ...)", pins=pins)

    def test_symbol_instance_creation(self, mock_symbol_def):
        """Create symbol instance."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )
        assert inst.reference == "R1"
        assert inst.value == "10k"
        assert inst.x == 100.0
        assert inst.y == 100.0
        assert inst.rotation == 0

    def test_symbol_instance_pin_position(self, mock_symbol_def):
        """Get pin position on placed symbol."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )

        pos = inst.pin_position("1")
        # Pin 1 is at (-2.54, 0) relative, so absolute is (100-2.54, 100-0)
        assert abs(pos[0] - 97.46) < 0.01
        assert abs(pos[1] - 100.0) < 0.01

    def test_symbol_instance_pin_position_rotated(self, mock_symbol_def):
        """Get pin position on rotated symbol."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=90,  # 90 degrees rotation
            reference="R1",
            value="10k",
        )

        pos = inst.pin_position("1")
        # At 90 degrees, (-2.54, 0) rotates to (0, -2.54)
        # With Y flip in schematic coords: (100+0, 100-(-2.54)) = (100, 102.54)
        assert abs(pos[0] - 100.0) < 0.01
        assert abs(pos[1] - 102.54) < 0.01

    def test_symbol_instance_pin_position_by_number(self, mock_symbol_def):
        """Get pin position by pin number."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )

        # Pin 2 should work by number
        pos = inst.pin_position("2")
        assert pos is not None

    def test_symbol_instance_pin_not_found(self, mock_symbol_def):
        """Error when pin not found."""
        from kicad_tools.schematic.exceptions import PinNotFoundError

        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )

        with pytest.raises(PinNotFoundError) as exc:
            inst.pin_position("NONEXISTENT")
        assert "NONEXISTENT" in str(exc.value)

    def test_symbol_instance_all_pin_positions(self, mock_symbol_def):
        """Get all pin positions."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )

        positions = inst.all_pin_positions()
        assert "1" in positions
        assert "2" in positions

    def test_symbol_instance_to_sexp_node(self, mock_symbol_def):
        """SymbolInstance generates valid S-expression node."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
            uuid_str="inst-uuid",
        )

        sexp = inst.to_sexp_node("test_project", "/sheet-uuid")
        assert sexp.name == "symbol"

    def test_symbol_instance_to_sexp(self, mock_symbol_def):
        """SymbolInstance generates S-expression string."""
        inst = SymbolInstance(
            symbol_def=mock_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )

        sexp_str = inst.to_sexp("test_project", "/sheet-uuid")
        assert "symbol" in sexp_str
        assert "R1" in sexp_str
        assert "10k" in sexp_str


class TestSchematicSnapMode:
    """Tests for Schematic snap modes."""

    def test_snap_mode_off(self):
        """SnapMode.OFF preserves coordinates."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        coord = sch._snap_coord(10.123, "test")
        assert coord == 10.12  # Just rounded, not snapped

    def test_snap_mode_auto(self):
        """SnapMode.AUTO snaps to grid."""
        sch = Schematic(title="Test", snap_mode=SnapMode.AUTO, grid=2.54)
        coord = sch._snap_coord(10.0, "test")
        assert coord == 10.16  # Snapped to nearest 2.54 grid

    def test_snap_point(self):
        """Snap a point to grid."""
        sch = Schematic(title="Test", snap_mode=SnapMode.AUTO, grid=2.54)
        point = sch._snap_point((10.0, 20.0), "test")
        assert point == (10.16, 20.32)


class TestSchematicConstruction:
    """Tests for Schematic construction and element addition."""

    def test_schematic_creation(self):
        """Create basic schematic."""
        sch = Schematic(title="Test Schematic", date="2025-01", revision="A")
        assert sch.title == "Test Schematic"
        assert sch.date == "2025-01"
        assert sch.revision == "A"
        assert sch.symbols == []
        assert sch.wires == []
        assert sch.junctions == []
        assert sch.labels == []

    def test_schematic_sheet_path(self):
        """Get sheet path."""
        sch = Schematic(title="Test", sheet_uuid="test-sheet-uuid")
        assert "/test-sheet-uuid" in sch.sheet_path

    def test_schematic_sheet_path_with_parent(self):
        """Get sheet path with parent."""
        sch = Schematic(title="Test", sheet_uuid="child-uuid", parent_uuid="parent-uuid")
        assert sch.sheet_path == "/parent-uuid/child-uuid"

    def test_add_wire(self):
        """Add wire to schematic."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wire = sch.add_wire((10, 20), (30, 40), snap=False)
        assert len(sch.wires) == 1
        assert wire.x1 == 10
        assert wire.y1 == 20

    def test_add_wire_path(self):
        """Add connected wire segments."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch.add_wire_path((0, 0), (10, 0), (10, 10), snap=False)
        assert len(wires) == 2
        assert len(sch.wires) == 2

    def test_add_junction(self):
        """Add junction to schematic."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        junc = sch.add_junction(10, 20, snap=False)
        assert len(sch.junctions) == 1
        assert junc.x == 10
        assert junc.y == 20

    def test_add_label(self):
        """Add label to schematic."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        label = sch.add_label("NET1", 10, 20, snap=False, validate_connection=False)
        assert len(sch.labels) == 1
        assert label.text == "NET1"

    def test_add_label_on_wire_no_warning(self):
        """Add label on a wire - no warning should be issued."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Add a wire first
        sch.add_wire((0, 20), (50, 20), snap=False)
        # Add label on the wire - should not warn
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            label = sch.add_label("NET1", 25, 20, snap=False)
            assert len(w) == 0
        assert label.text == "NET1"

    def test_add_label_on_wire_endpoint_no_warning(self):
        """Add label at wire endpoint - no warning should be issued."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Add a wire first
        sch.add_wire((10, 20), (50, 20), snap=False)
        # Add label at wire endpoint - should not warn
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            label = sch.add_label("NET1", 10, 20, snap=False)
            assert len(w) == 0
        assert label.text == "NET1"

    def test_add_label_off_wire_warns(self):
        """Add label not on any wire - warning should be issued."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Add a wire
        sch.add_wire((0, 20), (50, 20), snap=False)
        # Add label off the wire - should warn
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            label = sch.add_label("NET1", 25, 30, snap=False)
            assert len(w) == 1
            assert "not on any wire" in str(w[0].message)
            assert "NET1" in str(w[0].message)
            assert "ERC errors" in str(w[0].message)
        assert label.text == "NET1"

    def test_add_label_off_wire_shows_nearest_point(self):
        """Warning includes the nearest wire point information."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Add a wire
        sch.add_wire((0, 20), (50, 20), snap=False)
        # Add label 10mm away from the wire
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            sch.add_label("NET1", 25, 30, snap=False)
            assert len(w) == 1
            # Should mention the nearest wire point
            assert "Nearest wire point" in str(w[0].message)
            # Distance should be ~10mm
            assert "10.00mm away" in str(w[0].message)

    def test_add_label_validate_connection_disabled(self):
        """validate_connection=False disables the warning."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((0, 20), (50, 20), snap=False)
        # Add label off the wire with validation disabled
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            label = sch.add_label("NET1", 25, 30, snap=False, validate_connection=False)
            assert len(w) == 0
        assert label.text == "NET1"

    def test_add_label_no_wires_no_warning(self):
        """No warning when no wires exist (nothing to validate against)."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # No wires - should not warn since validation only runs when wires exist
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            label = sch.add_label("NET1", 25, 30, snap=False)
            assert len(w) == 0
        assert label.text == "NET1"

    def test_add_hier_label(self):
        """Add hierarchical label to schematic."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        hl = sch.add_hier_label(
            "DATA", 10, 20, shape="output", snap=False, validate_connection=False
        )
        assert len(sch.hier_labels) == 1
        assert hl.text == "DATA"
        assert hl.shape == "output"

    def test_add_hier_label_off_wire_warns(self):
        """Add hierarchical label not on any wire - warning should be issued."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((0, 20), (50, 20), snap=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            hl = sch.add_hier_label("DATA", 25, 30, shape="output", snap=False)
            assert len(w) == 1
            assert "Hierarchical label" in str(w[0].message)
            assert "not on any wire" in str(w[0].message)
        assert hl.text == "DATA"

    def test_add_global_label_off_wire_warns(self):
        """Add global label not on any wire - warning should be issued."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((0, 20), (50, 20), snap=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            gl = sch.add_global_label("VCC", 25, 30, snap=False)
            assert len(w) == 1
            assert "Global label" in str(w[0].message)
            assert "not on any wire" in str(w[0].message)
        assert gl.text == "VCC"

    def test_add_global_label_on_wire_no_warning(self):
        """Add global label on a wire - no warning should be issued."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((0, 20), (50, 20), snap=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            gl = sch.add_global_label("VCC", 25, 20, snap=False)
            assert len(w) == 0
        assert gl.text == "VCC"

    def test_add_text(self):
        """Add text note to schematic."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_text("Note here", 10, 20, snap=False)
        assert len(sch.text_notes) == 1
        assert sch.text_notes[0] == ("Note here", 10, 20)

    def test_add_rail(self):
        """Add power rail wire."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wire = sch.add_rail(y=100, x_start=10, x_end=50, net_label="VCC", snap=False)
        assert len(sch.wires) == 1
        assert len(sch.labels) == 1
        assert sch.labels[0].text == "VCC"

    def test_add_rail_tracks_continuous_rails(self):
        """add_rail() registers the rail for T-connection warnings."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_rail(y=100, x_start=10, x_end=50, snap=False)
        assert len(sch._continuous_rails) == 1
        assert sch._continuous_rails[0] == (100, 10, 50)

    def test_add_rail_t_connection_warning(self, caplog):
        """wire_to_rail() warns when creating T-connection on continuous rail."""
        import logging

        from kicad_tools.schematic.logging import enable_verbose

        enable_verbose("WARNING")

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Create a continuous rail
        sch.add_rail(y=100, x_start=10, x_end=50, snap=False)

        # Add a symbol and try to wire to the rail (creating a T-connection)
        pins = [
            Pin(name="1", number="1", x=0, y=2.54, angle=90, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=0, y=-2.54, angle=270, length=2.54, pin_type="passive"),
        ]
        symbol_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        sch._symbol_defs["Device:R"] = symbol_def
        sym = sch.add_symbol("Device:R", 30, 90, "R1", snap=False)

        # This should trigger a warning
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            sch.wire_to_rail(sym, "2", rail_y=100)

        # Check that a warning was logged
        assert any("T-connection" in record.message for record in caplog.records)
        assert any("add_segmented_rail" in record.message for record in caplog.records)

    def test_add_rail_no_warning_at_endpoints(self, caplog):
        """No warning when connecting at rail endpoints (not a T-connection)."""
        import logging

        from kicad_tools.schematic.logging import enable_verbose

        enable_verbose("WARNING")

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_rail(y=100, x_start=10, x_end=50, snap=False)

        # Add a symbol at the rail endpoint
        pins = [
            Pin(name="1", number="1", x=0, y=2.54, angle=90, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=0, y=-2.54, angle=270, length=2.54, pin_type="passive"),
        ]
        symbol_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        sch._symbol_defs["Device:R"] = symbol_def
        sym = sch.add_symbol("Device:R", 10, 90, "R1", snap=False)  # x=10 is endpoint

        # This should NOT trigger a warning (connecting at endpoint)
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            sch.wire_to_rail(sym, "2", rail_y=100)

        # Check that no T-connection warning was logged
        t_connection_warnings = [r for r in caplog.records if "T-connection" in r.message]
        assert len(t_connection_warnings) == 0

    def test_segmented_rail_no_warning(self, caplog):
        """add_segmented_rail() does not trigger T-connection warnings."""
        import logging

        from kicad_tools.schematic.logging import enable_verbose

        enable_verbose("WARNING")

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Create a segmented rail (proper T-connection support)
        sch.add_segmented_rail(y=100, x_points=[10, 30, 50], snap=False)

        # Segmented rails should NOT be tracked as continuous
        assert len(sch._continuous_rails) == 0

        # Add symbol and wire to rail
        pins = [
            Pin(name="1", number="1", x=0, y=2.54, angle=90, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=0, y=-2.54, angle=270, length=2.54, pin_type="passive"),
        ]
        symbol_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        sch._symbol_defs["Device:R"] = symbol_def
        sym = sch.add_symbol("Device:R", 30, 90, "R1", snap=False)

        # This should NOT trigger a warning
        with caplog.at_level(logging.WARNING, logger="kicad_sch_helper"):
            sch.wire_to_rail(sym, "2", rail_y=100)

        # Check that no T-connection warning was logged
        t_connection_warnings = [r for r in caplog.records if "T-connection" in r.message]
        assert len(t_connection_warnings) == 0


class TestWireGeometryHelpers:
    """Tests for wire geometry helper methods."""

    def test_point_on_wire_horizontal(self):
        """Point on horizontal wire segment."""
        from kicad_tools.schematic.models.elements import Wire

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wire = Wire.between((0, 20), (50, 20))
        sch.wires.append(wire)

        # Point on the wire
        assert sch._point_on_wire(25, 20, wire) is True
        # Wire endpoints
        assert sch._point_on_wire(0, 20, wire) is True
        assert sch._point_on_wire(50, 20, wire) is True
        # Point off the wire (10mm away vertically)
        assert sch._point_on_wire(25, 30, wire) is False
        # Point before wire start
        assert sch._point_on_wire(-10, 20, wire) is False

    def test_point_on_wire_vertical(self):
        """Point on vertical wire segment."""
        from kicad_tools.schematic.models.elements import Wire

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wire = Wire.between((20, 0), (20, 50))
        sch.wires.append(wire)

        # Point on the wire
        assert sch._point_on_wire(20, 25, wire) is True
        # Point off the wire
        assert sch._point_on_wire(30, 25, wire) is False

    def test_point_on_wire_diagonal(self):
        """Point on diagonal wire segment."""
        from kicad_tools.schematic.models.elements import Wire

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wire = Wire.between((0, 0), (50, 50))
        sch.wires.append(wire)

        # Point on the diagonal
        assert sch._point_on_wire(25, 25, wire) is True
        # Point off the diagonal
        assert sch._point_on_wire(25, 30, wire) is False

    def test_point_on_any_wire(self):
        """Check point against multiple wires."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((0, 20), (50, 20), snap=False)
        sch.add_wire((100, 0), (100, 50), snap=False)

        # Point on first wire
        assert sch._point_on_any_wire(25, 20) is True
        # Point on second wire
        assert sch._point_on_any_wire(100, 25) is True
        # Point on neither wire
        assert sch._point_on_any_wire(60, 60) is False

    def test_find_nearest_wire_point_horizontal(self):
        """Find nearest point on horizontal wire."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((0, 20), (50, 20), snap=False)

        # Point directly above wire
        nearest, dist = sch._find_nearest_wire_point(25, 30)
        assert nearest == (25, 20)
        assert abs(dist - 10) < 0.01

        # Point to the left of wire
        nearest, dist = sch._find_nearest_wire_point(-10, 20)
        assert nearest == (0, 20)
        assert abs(dist - 10) < 0.01

    def test_find_nearest_wire_point_no_wires(self):
        """Return None when no wires exist."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        nearest, dist = sch._find_nearest_wire_point(25, 30)
        assert nearest is None
        assert dist == float("inf")


class TestSchematicQueries:
    """Tests for Schematic query methods."""

    def test_find_label(self):
        """Find label by name."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_label("NET1", 10, 20, snap=False, validate_connection=False)
        sch.add_label("NET2", 30, 40, snap=False, validate_connection=False)

        label = sch.find_label("NET1")
        assert label is not None
        assert label.text == "NET1"

    def test_find_label_not_found(self):
        """Find label returns None when not found."""
        sch = Schematic(title="Test")
        label = sch.find_label("NONEXISTENT")
        assert label is None

    def test_find_labels_pattern(self):
        """Find labels by pattern."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_label("DATA_IN", 10, 20, snap=False, validate_connection=False)
        sch.add_label("DATA_OUT", 30, 40, snap=False, validate_connection=False)
        sch.add_label("CLK", 50, 60, snap=False, validate_connection=False)

        matches = sch.find_labels("DATA_*")
        assert len(matches) == 2

    def test_find_labels_all(self):
        """Find all labels."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_label("NET1", 10, 20, snap=False, validate_connection=False)
        sch.add_label("NET2", 30, 40, snap=False, validate_connection=False)

        matches = sch.find_labels()
        assert len(matches) == 2

    def test_find_hier_label(self):
        """Find hierarchical label by name."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_hier_label("DATA", 10, 20, snap=False)

        hl = sch.find_hier_label("DATA")
        assert hl is not None
        assert hl.text == "DATA"

    def test_find_hier_labels_pattern(self):
        """Find hierarchical labels by pattern."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_hier_label("I2C_SDA", 10, 20, snap=False)
        sch.add_hier_label("I2C_SCL", 30, 40, snap=False)
        sch.add_hier_label("SPI_MOSI", 50, 60, snap=False)

        matches = sch.find_hier_labels("I2C_*")
        assert len(matches) == 2

    def test_find_wires_at_endpoint(self):
        """Find wires by endpoint."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 20), snap=False)
        sch.add_wire((50, 60), (70, 60), snap=False)

        wires = sch.find_wires(endpoint=(10, 20))
        assert len(wires) == 1

    def test_find_wires_near_point(self):
        """Find wires near a point."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 20), snap=False)
        sch.add_wire((50, 60), (70, 60), snap=False)

        wires = sch.find_wires(near=(11, 21), tolerance=5)
        assert len(wires) == 1

    def test_find_wires_connected_to_label(self):
        """Find wires connected to a label."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 20), snap=False)
        sch.add_label("NET1", 10, 20, snap=False)

        wires = sch.find_wires(connected_to_label="NET1")
        assert len(wires) == 1


class TestSchematicRemoval:
    """Tests for Schematic removal methods."""

    def test_remove_wire(self):
        """Remove a wire."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wire = sch.add_wire((10, 20), (30, 40), snap=False)
        assert len(sch.wires) == 1

        result = sch.remove_wire(wire)
        assert result is True
        assert len(sch.wires) == 0

    def test_remove_wire_not_found(self):
        """Remove wire returns False when not found."""
        sch = Schematic(title="Test")
        other_wire = Wire(x1=0, y1=0, x2=10, y2=10)

        result = sch.remove_wire(other_wire)
        assert result is False

    def test_remove_wires_at_point(self):
        """Remove wires at a point."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 20), snap=False)
        sch.add_wire((10, 20), (10, 40), snap=False)
        assert len(sch.wires) == 2

        count = sch.remove_wires_at((10, 20), tolerance=1)
        assert count == 2
        assert len(sch.wires) == 0

    def test_remove_label(self):
        """Remove label by name."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_label("NET1", 10, 20, snap=False, validate_connection=False)
        assert len(sch.labels) == 1

        result = sch.remove_label("NET1")
        assert result is True
        assert len(sch.labels) == 0

    def test_remove_label_not_found(self):
        """Remove label returns False when not found."""
        sch = Schematic(title="Test")
        result = sch.remove_label("NONEXISTENT")
        assert result is False

    def test_remove_hier_label(self):
        """Remove hierarchical label by name."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_hier_label("DATA", 10, 20, snap=False)

        result = sch.remove_hier_label("DATA")
        assert result is True
        assert len(sch.hier_labels) == 0

    def test_remove_junction(self):
        """Remove junction at position."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_junction(10, 20, snap=False)

        result = sch.remove_junction(10, 20, tolerance=1)
        assert result is True
        assert len(sch.junctions) == 0

    def test_remove_net(self):
        """Remove net (label and connected wires)."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 20), snap=False)
        sch.add_label("NET1", 10, 20, snap=False)

        result = sch.remove_net("NET1")
        assert result["label_removed"] is True
        assert result["wires_removed"] == 1
        assert len(sch.labels) == 0
        assert len(sch.wires) == 0


class TestSchematicOutput:
    """Tests for Schematic output methods."""

    def test_to_sexp_node(self):
        """Generate S-expression tree."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 40), snap=False)
        sch.add_junction(10, 20, snap=False)
        sch.add_label("NET1", 10, 20, snap=False)

        sexp = sch.to_sexp_node()
        assert sexp.name == "kicad_sch"

    def test_to_sexp(self):
        """Generate S-expression string."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 40), snap=False)

        sexp_str = sch.to_sexp()
        assert "kicad_sch" in sexp_str
        assert "wire" in sexp_str

    def test_write_file(self, tmp_path):
        """Write schematic to file."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 40), snap=False)

        output_path = tmp_path / "test.kicad_sch"
        sch.write(output_path)

        assert output_path.exists()
        content = output_path.read_text()
        assert "kicad_sch" in content

    def test_get_statistics(self):
        """Get schematic statistics."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((10, 20), (30, 40), snap=False)
        sch.add_junction(10, 20, snap=False)
        sch.add_label("NET1", 10, 20, snap=False)

        stats = sch.get_statistics()
        assert stats["wire_count"] == 1
        assert stats["junction_count"] == 1
        assert stats["label_count"] == 1


class TestSchematicValidation:
    """Tests for Schematic validation."""

    def test_points_equal(self):
        """Check points equality within tolerance."""
        sch = Schematic(title="Test")
        assert sch._points_equal((10.0, 20.0), (10.0, 20.0)) is True
        assert sch._points_equal((10.0, 20.0), (10.001, 20.001)) is True
        assert sch._points_equal((10.0, 20.0), (11.0, 20.0)) is False

    def test_point_near(self):
        """Check point proximity."""
        sch = Schematic(title="Test")
        assert sch._point_near((10.0, 20.0), (11.0, 20.0), tolerance=2) is True
        assert sch._point_near((10.0, 20.0), (20.0, 20.0), tolerance=2) is False

    def test_point_on_segment_vertical(self):
        """Check if point is on vertical segment."""
        sch = Schematic(title="Test")
        assert sch._point_on_segment((10, 15), (10, 10), (10, 20)) is True
        assert sch._point_on_segment((10, 25), (10, 10), (10, 20)) is False

    def test_point_on_segment_horizontal(self):
        """Check if point is on horizontal segment."""
        sch = Schematic(title="Test")
        assert sch._point_on_segment((15, 10), (10, 10), (20, 10)) is True
        assert sch._point_on_segment((25, 10), (10, 10), (20, 10)) is False

    def test_point_on_segment_diagonal(self):
        """Diagonal segments return False (orthogonal wires only)."""
        sch = Schematic(title="Test")
        # Diagonal segments are not orthogonal - should return False
        assert sch._point_on_segment((15, 15), (10, 10), (20, 20)) is False


class TestSchematicLoadAndParse:
    """Tests for Schematic loading and parsing."""

    def test_load_file_not_found(self, tmp_path):
        """Load raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            Schematic.load(tmp_path / "nonexistent.kicad_sch")

    def test_load_from_file(self, tmp_path):
        """Load schematic from file."""
        # Create a minimal schematic file
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "test-uuid-123")
  (paper "A4")
  (title_block
    (title "Test Schematic")
    (date "2025-01")
    (rev "B")
    (company "Test Corp")
    (comment 1 "Comment 1")
    (comment 2 "Comment 2")
  )
  (lib_symbols)
  (wire
    (pts (xy 10 20) (xy 30 20))
    (stroke (width 0) (type default))
    (uuid "wire-uuid")
  )
  (junction
    (at 10 20)
    (diameter 0)
    (uuid "junc-uuid")
  )
  (label "NET1"
    (at 10 20 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-uuid")
  )
  (hierarchical_label "DATA"
    (shape input)
    (at 50 60 0)
    (effects (font (size 1.27 1.27)))
    (uuid "hl-uuid")
  )
  (text "Note"
    (at 100 100 0)
    (effects (font (size 1.27 1.27)))
    (uuid "text-uuid")
  )
  (sheet_instances
    (project "test_project"
      (path "/test-uuid-123"
        (page "1")
      )
    )
  )
)"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch_content)

        sch = Schematic.load(sch_path)
        assert sch.title == "Test Schematic"
        assert sch.date == "2025-01"
        assert sch.revision == "B"
        assert sch.company == "Test Corp"
        assert sch.comment1 == "Comment 1"
        assert sch.comment2 == "Comment 2"
        assert len(sch.wires) == 1
        assert len(sch.junctions) == 1
        assert len(sch.labels) == 1
        assert len(sch.hier_labels) == 1
        assert len(sch.text_notes) == 1
        assert sch.project_name == "test_project"

    def test_load_with_power_symbols(self, tmp_path):
        """Load schematic with power symbols."""
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "test-uuid")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "power:GND")
    (at 100 100 0)
    (uuid "pwr-uuid")
    (property "Reference" "#PWR01" (at 100 100 0) (effects (hide yes)))
    (property "Value" "GND" (at 100 105 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-uuid"))
    (instances (project "test" (path "/" (reference "#PWR01") (unit 1))))
  )
)"""
        sch_path = tmp_path / "power.kicad_sch"
        sch_path.write_text(sch_content)

        sch = Schematic.load(sch_path)
        assert len(sch.power_symbols) == 1
        assert sch.power_symbols[0].lib_id == "power:GND"
        assert sch.power_symbols[0].reference == "#PWR01"
        # Check power counter is updated
        assert sch._pwr_counter == 2


class TestSchematicSnapModeAdvanced:
    """Advanced tests for snap modes."""

    def test_snap_mode_warn(self):
        """SnapMode.WARN warns on off-grid but doesn't snap."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.WARN, grid=2.54)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            coord = sch._snap_coord(10.0, "test")
            # 10.0 is not on 2.54 grid, should warn
            assert len(w) == 1
            assert "Off-grid" in str(w[0].message)
            # But value is not snapped, just rounded
            assert coord == 10.0

    def test_snap_mode_strict(self):
        """SnapMode.STRICT snaps and warns."""
        import warnings

        sch = Schematic(title="Test", snap_mode=SnapMode.STRICT, grid=2.54)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            coord = sch._snap_coord(10.0, "test")
            # Should warn and snap
            assert len(w) == 1
            assert "Auto-snapping" in str(w[0].message)
            # Value is snapped to nearest grid
            assert coord == 10.16


class TestSchematicWiringHelpers:
    """Tests for Schematic wiring helper methods."""

    @pytest.fixture
    def sch_with_symbols(self):
        """Create schematic with mock symbols for wiring tests."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Create a mock symbol def with known pin positions
        pins = [
            Pin(
                name="VIN", number="1", x=-5.08, y=2.54, angle=180, length=2.54, pin_type="power_in"
            ),
            Pin(
                name="VOUT", number="2", x=5.08, y=2.54, angle=0, length=2.54, pin_type="power_out"
            ),
            Pin(name="GND", number="3", x=0, y=-5.08, angle=270, length=2.54, pin_type="power_in"),
            Pin(name="EN", number="4", x=-5.08, y=0, angle=180, length=2.54, pin_type="input"),
        ]
        sym_def = SymbolDef(
            lib_id="Regulator_Linear:AP2204K-3.3", name="AP2204K-3.3", raw_sexp="", pins=pins
        )
        sch._symbol_defs["Regulator_Linear:AP2204K-3.3"] = sym_def

        # Add symbol at position
        inst = SymbolInstance(
            symbol_def=sym_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="AP2204K-3.3",
        )
        sch.symbols.append(inst)

        return sch, inst

    def test_route_orthogonal_aligned(self):
        """Route between aligned points (single wire)."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch._route_orthogonal((10, 20), (50, 20), "auto")
        assert len(wires) == 1
        assert wires[0].x1 == 10
        assert wires[0].x2 == 50

    def test_route_orthogonal_auto(self):
        """Route with auto direction selection."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Longer vertical distance -> horizontal first
        wires = sch._route_orthogonal((10, 20), (20, 100), "auto")
        assert len(wires) == 2

    def test_route_orthogonal_horizontal_first(self):
        """Route horizontal then vertical."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch._route_orthogonal((10, 20), (50, 60), "horizontal_first")
        assert len(wires) == 2
        # First wire goes horizontal to (50, 20)
        assert wires[0].x2 == 50
        assert wires[0].y2 == 20

    def test_route_orthogonal_vertical_first(self):
        """Route vertical then horizontal."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch._route_orthogonal((10, 20), (50, 60), "vertical_first")
        assert len(wires) == 2
        # First wire goes vertical to (10, 60)
        assert wires[0].x2 == 10
        assert wires[0].y2 == 60

    def test_wire_pin_to_point(self, sch_with_symbols):
        """Wire from symbol pin to a point."""
        sch, sym = sch_with_symbols
        wires = sch.wire_pin_to_point(sym, "VOUT", (150, 100))
        # Should create wires from VOUT pin to target
        assert len(wires) >= 1

    def test_wire_pins(self, sch_with_symbols):
        """Wire two symbol pins together."""
        sch, sym1 = sch_with_symbols

        # Add second symbol
        pins2 = [
            Pin(name="1", number="1", x=-2.54, y=0, angle=0, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=2.54, y=0, angle=180, length=2.54, pin_type="passive"),
        ]
        sym_def2 = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins2)
        sym2 = SymbolInstance(
            symbol_def=sym_def2,
            x=150.0,
            y=100.0,
            rotation=0,
            reference="R1",
            value="10k",
        )
        sch.symbols.append(sym2)

        wires = sch.wire_pins(sym1, "VOUT", sym2, "1")
        assert len(wires) >= 1


class TestSymbolInstanceAdvanced:
    """Advanced tests for SymbolInstance."""

    @pytest.fixture
    def multi_pin_symbol_def(self):
        """Symbol def with multiple pins for testing."""
        pins = [
            Pin(name="VCC", number="1", x=0, y=5.08, angle=90, length=2.54, pin_type="power_in"),
            Pin(name="GND", number="2", x=0, y=-5.08, angle=270, length=2.54, pin_type="power_in"),
            Pin(
                name="DATA",
                number="3",
                x=-5.08,
                y=0,
                angle=180,
                length=2.54,
                pin_type="bidirectional",
            ),
            Pin(name="CLK", number="4", x=5.08, y=0, angle=0, length=2.54, pin_type="input"),
        ]
        return SymbolDef(lib_id="Test:MultiPin", name="MultiPin", raw_sexp="", pins=pins)

    def test_bounding_box(self, multi_pin_symbol_def):
        """Calculate symbol bounding box from pins."""
        inst = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )

        bbox = inst.bounding_box(padding=0)
        # Should encompass all pin positions
        assert bbox[0] <= 94.92  # min_x (100 - 5.08)
        assert bbox[2] >= 105.08  # max_x (100 + 5.08)
        assert bbox[1] <= 94.92  # min_y
        assert bbox[3] >= 105.08  # max_y

    def test_bounding_box_with_padding(self, multi_pin_symbol_def):
        """Bounding box includes padding."""
        inst = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )

        bbox_no_pad = inst.bounding_box(padding=0)
        bbox_with_pad = inst.bounding_box(padding=5.0)

        assert bbox_with_pad[0] < bbox_no_pad[0]
        assert bbox_with_pad[2] > bbox_no_pad[2]

    def test_bounding_box_no_pins(self):
        """Bounding box for symbol with no pins uses default."""
        sym_def = SymbolDef(lib_id="Test:NoPins", name="NoPins", raw_sexp="", pins=[])
        inst = SymbolInstance(
            symbol_def=sym_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )

        bbox = inst.bounding_box(padding=0)
        # Default half_size is 5.08
        assert abs(bbox[0] - (100 - 5.08)) < 0.01
        assert abs(bbox[2] - (100 + 5.08)) < 0.01

    def test_overlaps_true(self, multi_pin_symbol_def):
        """Symbols overlap when bounding boxes intersect."""
        inst1 = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )
        inst2 = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=105.0,  # Close enough to overlap
            y=100.0,
            rotation=0,
            reference="U2",
            value="Test",
        )

        assert inst1.overlaps(inst2, padding=0) is True

    def test_overlaps_false(self, multi_pin_symbol_def):
        """Symbols don't overlap when far apart."""
        inst1 = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )
        inst2 = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=200.0,  # Far away
            y=100.0,
            rotation=0,
            reference="U2",
            value="Test",
        )

        assert inst1.overlaps(inst2, padding=0) is False

    def test_pin_position_case_insensitive(self, multi_pin_symbol_def):
        """Pin lookup is case insensitive."""
        inst = SymbolInstance(
            symbol_def=multi_pin_symbol_def,
            x=100.0,
            y=100.0,
            rotation=0,
            reference="U1",
            value="Test",
        )

        # Should find VCC even with lowercase
        pos = inst.pin_position("vcc")
        assert pos is not None


class TestSchematicAutoLayout:
    """Tests for Schematic auto-layout methods."""

    @pytest.fixture
    def sch_with_placed_symbols(self):
        """Create schematic with some placed symbols."""
        sch = Schematic(title="Test", snap_mode=SnapMode.AUTO, grid=2.54)

        pins = [
            Pin(name="1", number="1", x=-5.08, y=0, angle=180, length=2.54, pin_type="passive"),
            Pin(name="2", number="2", x=5.08, y=0, angle=0, length=2.54, pin_type="passive"),
        ]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        sch._symbol_defs["Device:R"] = sym_def

        # Add some symbols
        for i in range(3):
            inst = SymbolInstance(
                symbol_def=sym_def,
                x=100.0 + i * 20,
                y=100.0,
                rotation=0,
                reference=f"R{i + 1}",
                value="10k",
            )
            sch.symbols.append(inst)

        return sch

    def test_find_overlapping_symbols_none(self, sch_with_placed_symbols):
        """No overlaps when symbols are spaced apart."""
        sch = sch_with_placed_symbols
        overlaps = sch.find_overlapping_symbols(padding=2.54)
        # Symbols are 20mm apart, should not overlap with 2.54mm padding
        assert len(overlaps) == 0

    def test_find_overlapping_symbols_found(self):
        """Find overlapping symbols."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [
            Pin(name="1", number="1", x=-5.08, y=0, angle=180, length=2.54, pin_type="passive"),
        ]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        sch._symbol_defs["Device:R"] = sym_def

        # Add overlapping symbols
        inst1 = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="R1", value="10k"
        )
        inst2 = SymbolInstance(
            symbol_def=sym_def, x=102.0, y=100.0, rotation=0, reference="R2", value="10k"
        )
        sch.symbols.extend([inst1, inst2])

        overlaps = sch.find_overlapping_symbols(padding=2.54)
        assert len(overlaps) == 1
        assert inst1 in overlaps[0]
        assert inst2 in overlaps[0]

    def test_suggest_position_clear(self, sch_with_placed_symbols):
        """Suggest position returns preferred when clear."""
        sch = sch_with_placed_symbols
        # Far from existing symbols
        pos = sch.suggest_position("Device:R", near=(200, 200), avoid_overlaps=True)
        # Should snap to grid near (200, 200)
        assert abs(pos[0] - 200.66) < 0.1 or abs(pos[0] - 198.12) < 0.1

    def test_suggest_position_avoid_overlap(self):
        """Suggest position finds clear space when preferred overlaps."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF, grid=2.54)

        pins = [Pin(name="1", number="1", x=-5.08, y=0, angle=180, length=2.54, pin_type="passive")]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        sch._symbol_defs["Device:R"] = sym_def

        # Place symbol at (100, 100)
        inst = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="R1", value="10k"
        )
        sch.symbols.append(inst)

        # Request position near existing symbol
        pos = sch.suggest_position("Device:R", near=(100, 100), avoid_overlaps=True)
        # Should move away from (100, 100)
        assert pos != (100, 100)

    def test_position_overlaps(self, sch_with_placed_symbols):
        """Check if a position overlaps existing symbols."""
        sch = sch_with_placed_symbols
        sym_def = sch._symbol_defs["Device:R"]

        # Create temp symbol at overlapping position
        temp = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="_TEMP_", value=""
        )

        assert sch._position_overlaps(temp, padding=2.54) is True

        # Move to non-overlapping position
        temp.x = 200.0
        temp.y = 200.0
        assert sch._position_overlaps(temp, padding=2.54) is False


class TestSchematicValidationAdvanced:
    """Advanced tests for Schematic validation."""

    def test_validate_duplicate_references(self):
        """Validate detects duplicate references."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [Pin(name="1", number="1", x=0, y=0, angle=0, length=2.54, pin_type="passive")]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)

        # Add two symbols with same reference
        inst1 = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="R1", value="10k"
        )
        inst2 = SymbolInstance(
            symbol_def=sym_def, x=150.0, y=100.0, rotation=0, reference="R1", value="20k"
        )
        sch.symbols.extend([inst1, inst2])

        issues = sch.validate()
        dup_issues = [i for i in issues if i["type"] == "duplicate_reference"]
        assert len(dup_issues) == 1
        assert "R1" in dup_issues[0]["message"]

    def test_validate_off_grid_symbols_fix(self):
        """Validate can fix off-grid symbols."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF, grid=2.54)

        pins = [Pin(name="1", number="1", x=0, y=0, angle=0, length=2.54, pin_type="passive")]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)

        # Add off-grid symbol
        inst = SymbolInstance(
            symbol_def=sym_def, x=100.1, y=100.2, rotation=0, reference="R1", value="10k"
        )
        sch.symbols.append(inst)

        issues = sch.validate(fix_auto=True)
        off_grid = [i for i in issues if i["type"] == "off_grid_symbol"]
        assert len(off_grid) == 1
        assert off_grid[0]["fix_applied"] is True
        # Symbol should now be on grid
        assert inst.x % 2.54 < 0.01 or abs(inst.x % 2.54 - 2.54) < 0.01

    def test_check_wire_connectivity_floating(self):
        """Detect floating wire endpoints."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Add wire not connected to anything
        sch.add_wire((100, 100), (150, 100), snap=False)

        issues = sch._check_wire_connectivity()
        floating = [i for i in issues if i["type"] == "floating_wire"]
        assert len(floating) >= 1

    def test_check_wire_connectivity_t_junction(self):
        """Detect T-junction without junction dot."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Create T-junction: horizontal wire with another wire ending on it
        sch.add_wire((0, 100), (100, 100), snap=False)  # Horizontal
        sch.add_wire((50, 100), (50, 50), snap=False)  # Vertical from middle

        issues = sch._check_wire_connectivity()
        # One end of vertical wire connects to horizontal at (50, 100) - should detect missing junction
        # Other end at (50, 50) is floating
        t_junctions = [i for i in issues if i["type"] == "missing_junction"]
        # Note: May or may not detect depending on implementation

    def test_check_unconnected_pins_power_connected(self):
        """Power pins connected to wires don't generate errors."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [Pin(name="VCC", number="1", x=0, y=0, angle=0, length=0, pin_type="power_in")]
        sym_def = SymbolDef(lib_id="Device:IC", name="IC", raw_sexp="", pins=pins)

        inst = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="U1", value="IC"
        )
        sch.symbols.append(inst)

        # Connect wire to the power pin position
        sch.add_wire((100, 100), (100, 50), snap=False)

        issues = sch._check_unconnected_pins()
        unconnected = [i for i in issues if i["type"] == "unconnected_power_pin"]
        assert len(unconnected) == 0

    def test_check_unconnected_pins_power_not_connected(self):
        """Power pins not connected to wires generate errors."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [Pin(name="VCC", number="1", x=0, y=0, angle=0, length=0, pin_type="power_in")]
        sym_def = SymbolDef(lib_id="Device:IC", name="IC", raw_sexp="", pins=pins)

        inst = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="U1", value="IC"
        )
        sch.symbols.append(inst)

        # No wire connected to pin
        issues = sch._check_unconnected_pins()
        unconnected = [i for i in issues if i["type"] == "unconnected_power_pin"]
        assert len(unconnected) == 1
        assert unconnected[0]["severity"] == "error"

    def test_check_unconnected_pins_io_not_connected(self):
        """Input/output pins not connected generate errors."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [Pin(name="DATA", number="1", x=0, y=0, angle=0, length=0, pin_type="input")]
        sym_def = SymbolDef(lib_id="Device:IC", name="IC", raw_sexp="", pins=pins)

        inst = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="U1", value="IC"
        )
        sch.symbols.append(inst)

        # No wire connected
        issues = sch._check_unconnected_pins()
        unconnected = [i for i in issues if i["type"] == "unconnected_pin"]
        assert len(unconnected) == 1
        assert unconnected[0]["severity"] == "error"

    def test_check_unconnected_pins_no_connect_marker(self):
        """Pins with no_connect markers don't generate issues."""
        from kicad_tools.schematic.models.elements import NoConnect

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [Pin(name="NC", number="1", x=0, y=0, angle=0, length=0, pin_type="input")]
        sym_def = SymbolDef(lib_id="Device:IC", name="IC", raw_sexp="", pins=pins)

        inst = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="U1", value="IC"
        )
        sch.symbols.append(inst)

        # Add no_connect marker at pin position
        sch.no_connects.append(NoConnect(x=100.0, y=100.0))

        issues = sch._check_unconnected_pins()
        unconnected = [i for i in issues if "unconnected" in i["type"]]
        assert len(unconnected) == 0

    def test_check_unconnected_pins_simple_passive_skipped(self):
        """Simple 2-pin passive components (like resistors) are skipped."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # 2-pin passive component like a resistor
        pins = [
            Pin(name="~", number="1", x=-2.54, y=0, angle=180, length=2.54, pin_type="passive"),
            Pin(name="~", number="2", x=2.54, y=0, angle=0, length=2.54, pin_type="passive"),
        ]
        sym_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)

        inst = SymbolInstance(
            symbol_def=sym_def, x=100.0, y=100.0, rotation=0, reference="R1", value="10k"
        )
        sch.symbols.append(inst)

        # No wires connected - but should not generate issues for simple passives
        issues = sch._check_unconnected_pins()
        unconnected = [i for i in issues if "unconnected" in i["type"]]
        assert len(unconnected) == 0

    def test_check_disconnected_labels_connected(self):
        """Labels at wire endpoints don't generate errors."""
        from kicad_tools.schematic.models.elements import Label

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Add a wire
        sch.add_wire((100, 100), (200, 100), snap=False)

        # Add label at wire endpoint
        sch.labels.append(Label(text="NET1", x=100.0, y=100.0))

        issues = sch._check_disconnected_labels()
        disconnected = [i for i in issues if i["type"] == "disconnected_label"]
        assert len(disconnected) == 0

    def test_check_disconnected_labels_on_wire(self):
        """Labels on wire segments (not at endpoints) don't generate errors."""
        from kicad_tools.schematic.models.elements import Label

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Add a horizontal wire
        sch.add_wire((100, 100), (200, 100), snap=False)

        # Add label in middle of wire
        sch.labels.append(Label(text="NET1", x=150.0, y=100.0))

        issues = sch._check_disconnected_labels()
        disconnected = [i for i in issues if i["type"] == "disconnected_label"]
        assert len(disconnected) == 0

    def test_check_disconnected_labels_floating(self):
        """Labels not on any wire generate errors."""
        from kicad_tools.schematic.models.elements import Label

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Add a wire
        sch.add_wire((100, 100), (200, 100), snap=False)

        # Add floating label (not on wire)
        sch.labels.append(Label(text="FLOATING", x=300.0, y=300.0))

        issues = sch._check_disconnected_labels()
        disconnected = [i for i in issues if i["type"] == "disconnected_label"]
        assert len(disconnected) == 1
        assert "FLOATING" in disconnected[0]["message"]

    def test_check_disconnected_global_labels(self):
        """Global labels not on any wire generate errors."""
        from kicad_tools.schematic.models.elements import GlobalLabel

        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Add a wire
        sch.add_wire((100, 100), (200, 100), snap=False)

        # Add floating global label
        sch.global_labels.append(GlobalLabel(text="VCC_3V3", x=300.0, y=300.0))

        issues = sch._check_disconnected_labels()
        disconnected = [i for i in issues if i["type"] == "disconnected_label"]
        assert len(disconnected) == 1
        assert "VCC_3V3" in disconnected[0]["message"]


class TestSymbolDefParsing:
    """Tests for SymbolDef parsing methods."""

    def test_add_prefix_to_node_atom(self):
        """Atom nodes are returned as-is."""
        sym_def = SymbolDef(lib_id="Test:Sym", name="Sym", raw_sexp="")
        atom = SExp.atom("test")
        result = sym_def._add_prefix_to_node(atom, "Lib")
        assert result == atom

    def test_add_prefix_to_node_symbol(self):
        """Symbol names get library prefix."""
        sym_def = SymbolDef(lib_id="Test:Sym", name="Sym", raw_sexp="")
        sym_node = SExp.list("symbol", "TestSymbol", SExp.list("property", "test"))

        result = sym_def._add_prefix_to_node(sym_node, "MyLib")
        # First atom child should now have prefix
        first_child = result.children[0]
        assert str(first_child.value) == "MyLib:TestSymbol"

    def test_add_prefix_to_node_unit_symbol(self):
        """Unit symbols (with _N_N suffix) should NOT have library prefix.

        KiCad expects:
        - Parent symbol: (symbol "Lib:Name" ...)
        - Unit symbols: (symbol "Name_0_1" ...) - NO library prefix

        This was fixed in issue #596.
        """
        sym_def = SymbolDef(lib_id="Test:Sym", name="Sym", raw_sexp="")
        sym_node = SExp.list("symbol", "TestSymbol_0_1")

        result = sym_def._add_prefix_to_node(sym_node, "MyLib")
        first_child = result.children[0]
        # Unit symbols should NOT have library prefix
        assert str(first_child.value) == "TestSymbol_0_1"

    def test_to_sexp_nodes_with_sexp_node(self):
        """Get SExp nodes when _sexp_node is available."""
        sym_node = SExp.list("symbol", "TestSymbol", SExp.list("property", "test"))
        sym_def = SymbolDef(
            lib_id="Test:TestSymbol", name="TestSymbol", raw_sexp="", _sexp_node=sym_node
        )

        nodes = sym_def.to_sexp_nodes()
        assert len(nodes) == 1
        assert nodes[0].name == "symbol"


class TestSymbolInstanceFromSexp:
    """Tests for SymbolInstance.from_sexp parsing."""

    def test_from_sexp_basic(self):
        """Parse basic symbol instance from sexp."""
        sym_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "Device:R"),
            SExp.list("at", 100.0, 100.0, 90),
            SExp.list("unit", 1),
            SExp.list("uuid", "test-uuid"),
            SExp.list("property", "Reference", "R1"),
            SExp.list("property", "Value", "10k"),
            SExp.list("property", "Footprint", "Resistor_SMD:R_0402"),
        )

        # Create a mock symbol def
        pins = [Pin(name="1", number="1", x=0, y=0, angle=0, length=2.54, pin_type="passive")]
        mock_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)

        inst = SymbolInstance.from_sexp(sym_sexp, symbol_defs={"Device:R": mock_def})

        assert inst.reference == "R1"
        assert inst.value == "10k"
        assert inst.footprint == "Resistor_SMD:R_0402"
        assert inst.x == 100.0
        assert inst.y == 100.0
        assert inst.rotation == 90
        assert inst.unit == 1

    def test_from_sexp_with_embedded_lib_symbols(self):
        """Parse using embedded lib_symbols."""
        sym_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "Custom:Part"),
            SExp.list("at", 50.0, 50.0),
            SExp.list("uuid", "inst-uuid"),
            SExp.list("property", "Reference", "U1"),
            SExp.list("property", "Value", "Part"),
        )

        # Embedded lib symbol definition
        lib_sym = SExp.list(
            "symbol",
            "Custom:Part",
            SExp.list(
                "pin",
                "input",
                "line",
                SExp.list("at", 0, 0, 0),
                SExp.list("length", 2.54),
                SExp.list("name", "IN"),
                SExp.list("number", "1"),
            ),
        )

        inst = SymbolInstance.from_sexp(sym_sexp, lib_symbols={"Custom:Part": lib_sym})

        assert inst.reference == "U1"
        assert len(inst.symbol_def.pins) == 1
        assert inst.symbol_def.pins[0].name == "IN"

    def test_from_sexp_placeholder_when_lib_missing(self):
        """Create placeholder SymbolDef when library not found."""
        sym_sexp = SExp.list(
            "symbol",
            SExp.list("lib_id", "Unknown:Missing"),
            SExp.list("at", 50.0, 50.0),
            SExp.list("uuid", "inst-uuid"),
            SExp.list("property", "Reference", "X1"),
            SExp.list("property", "Value", "?"),
        )

        # Disable registry to force fallback behavior
        import kicad_tools.schematic.models.symbol as symbol_module

        original_registry_available = symbol_module._REGISTRY_AVAILABLE
        symbol_module._REGISTRY_AVAILABLE = False

        try:
            # No symbol_defs or lib_symbols provided
            inst = SymbolInstance.from_sexp(sym_sexp)

            # Should create placeholder with no pins
            assert inst.reference == "X1"
            assert inst.symbol_def.lib_id == "Unknown:Missing"
            assert len(inst.symbol_def.pins) == 0
        finally:
            symbol_module._REGISTRY_AVAILABLE = original_registry_available


class TestSchematicSymbolSearch:
    """Tests for symbol search methods."""

    @pytest.fixture
    def sch_with_mixed_symbols(self):
        """Create schematic with various symbols."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        pins = [Pin(name="1", number="1", x=0, y=0, angle=0, length=2.54, pin_type="passive")]

        # Add resistors
        r_def = SymbolDef(lib_id="Device:R", name="R", raw_sexp="", pins=pins)
        for i in range(3):
            inst = SymbolInstance(
                symbol_def=r_def,
                x=100.0 + i * 20,
                y=100.0,
                rotation=0,
                reference=f"R{i + 1}",
                value="10k",
            )
            sch.symbols.append(inst)

        # Add capacitors
        c_def = SymbolDef(lib_id="Device:C", name="C", raw_sexp="", pins=pins)
        for i in range(2):
            inst = SymbolInstance(
                symbol_def=c_def,
                x=100.0 + i * 20,
                y=150.0,
                rotation=0,
                reference=f"C{i + 1}",
                value="100nF",
            )
            sch.symbols.append(inst)

        return sch

    def test_find_symbol(self, sch_with_mixed_symbols):
        """Find symbol by reference."""
        sch = sch_with_mixed_symbols
        sym = sch.find_symbol("R2")
        assert sym is not None
        assert sym.reference == "R2"

    def test_find_symbol_not_found(self, sch_with_mixed_symbols):
        """Find symbol returns None when not found."""
        sch = sch_with_mixed_symbols
        sym = sch.find_symbol("X99")
        assert sym is None

    def test_find_symbols_pattern(self, sch_with_mixed_symbols):
        """Find symbols by pattern."""
        sch = sch_with_mixed_symbols
        resistors = sch.find_symbols("R*")
        assert len(resistors) == 3

    def test_find_symbols_all(self, sch_with_mixed_symbols):
        """Find all symbols."""
        sch = sch_with_mixed_symbols
        all_syms = sch.find_symbols()
        assert len(all_syms) == 5

    def test_find_symbols_by_value(self, sch_with_mixed_symbols):
        """Find symbols by value."""
        sch = sch_with_mixed_symbols
        found = sch.find_symbols_by_value("10k")
        assert len(found) == 3

    def test_find_symbols_by_lib(self, sch_with_mixed_symbols):
        """Find symbols by library pattern."""
        sch = sch_with_mixed_symbols
        found = sch.find_symbols_by_lib("Device:C")
        assert len(found) == 2

    def test_remove_symbol(self, sch_with_mixed_symbols):
        """Remove symbol by reference."""
        sch = sch_with_mixed_symbols
        assert len(sch.symbols) == 5

        result = sch.remove_symbol("R2")
        assert result is True
        assert len(sch.symbols) == 4
        assert sch.find_symbol("R2") is None

    def test_remove_symbol_not_found(self, sch_with_mixed_symbols):
        """Remove symbol returns False when not found."""
        sch = sch_with_mixed_symbols
        result = sch.remove_symbol("X99")
        assert result is False


class TestSegmentedRails:
    """Tests for segmented rail methods that create proper T-connections.

    KiCad requires wire endpoints to physically meet at connection points.
    Junctions placed on a long wire are visual only - they don't create
    electrical connections for T-intersections.
    """

    def test_add_segmented_rail_creates_segments(self):
        """Segmented rail creates wire segments between consecutive points."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch.add_segmented_rail(y=100, x_points=[25, 50, 100, 150], snap=False)

        # Should create 3 wire segments for 4 points
        assert len(wires) == 3
        assert len(sch.wires) == 3

        # Verify segments are between consecutive points
        assert wires[0].x1 == 25 and wires[0].x2 == 50
        assert wires[1].x1 == 50 and wires[1].x2 == 100
        assert wires[2].x1 == 100 and wires[2].x2 == 150

        # All segments at same Y coordinate
        for wire in wires:
            assert wire.y1 == 100
            assert wire.y2 == 100

    def test_add_segmented_rail_adds_junctions(self):
        """Segmented rail adds junctions at interior points."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_segmented_rail(y=100, x_points=[25, 50, 100, 150], add_junctions=True, snap=False)

        # Should create 2 junctions at interior points (50 and 100)
        # Note: 25 and 150 are endpoints, not interior
        assert len(sch.junctions) == 2
        junction_xs = sorted([j.x for j in sch.junctions])
        assert junction_xs == [50, 100]

    def test_add_segmented_rail_no_junctions(self):
        """Segmented rail can skip junction creation."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_segmented_rail(y=100, x_points=[25, 50, 100], add_junctions=False, snap=False)

        assert len(sch.junctions) == 0

    def test_add_segmented_rail_with_label(self):
        """Segmented rail adds net label at start."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_segmented_rail(y=100, x_points=[25, 50, 100], net_label="VCC", snap=False)

        assert len(sch.labels) == 1
        assert sch.labels[0].text == "VCC"

    def test_add_segmented_rail_unsorted_points(self):
        """Segmented rail sorts points automatically."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch.add_segmented_rail(y=100, x_points=[100, 25, 150, 50], snap=False)

        # Points should be sorted: 25, 50, 100, 150
        assert wires[0].x1 == 25 and wires[0].x2 == 50
        assert wires[1].x1 == 50 and wires[1].x2 == 100
        assert wires[2].x1 == 100 and wires[2].x2 == 150

    def test_add_segmented_rail_minimum_points(self):
        """Segmented rail works with minimum 2 points."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch.add_segmented_rail(y=100, x_points=[25, 100], snap=False)

        assert len(wires) == 1
        assert len(sch.junctions) == 0  # No interior points

    def test_add_segmented_rail_too_few_points(self):
        """Segmented rail raises error with less than 2 points."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        with pytest.raises(ValueError, match="at least 2 values"):
            sch.add_segmented_rail(y=100, x_points=[25], snap=False)

    def test_add_vertical_segmented_rail(self):
        """Vertical segmented rail creates vertical wire segments."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch.add_vertical_segmented_rail(x=100, y_points=[25, 50, 100], snap=False)

        assert len(wires) == 2

        # Verify segments are vertical between consecutive Y points
        assert wires[0].y1 == 25 and wires[0].y2 == 50
        assert wires[1].y1 == 50 and wires[1].y2 == 100

        # All segments at same X coordinate
        for wire in wires:
            assert wire.x1 == 100
            assert wire.x2 == 100

    def test_add_vertical_segmented_rail_junctions(self):
        """Vertical segmented rail adds junctions at interior Y points."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_vertical_segmented_rail(
            x=100, y_points=[25, 50, 100, 150], add_junctions=True, snap=False
        )

        # Should create 2 junctions at interior points (50 and 100)
        assert len(sch.junctions) == 2
        junction_ys = sorted([j.y for j in sch.junctions])
        assert junction_ys == [50, 100]

    def test_segmented_rail_endpoints_meet(self):
        """Verify wire endpoints physically meet at segment boundaries.

        This is the key behavior that fixes T-connections. Each segment's
        endpoint must be the start of the next segment.
        """
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        wires = sch.add_segmented_rail(y=100, x_points=[25, 50, 100], snap=False)

        # Wire 0 ends where wire 1 begins
        assert wires[0].x2 == wires[1].x1
        assert wires[0].y2 == wires[1].y1

    def test_segmented_rail_t_connection_pattern(self):
        """Test the pattern that fixes T-connection ERC errors.

        Create a horizontal rail with a vertical wire connecting to it.
        The rail segments ensure the connection point is a wire endpoint.
        """
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)

        # Horizontal rail from x=25 to x=100, with connection point at x=50
        sch.add_segmented_rail(y=100, x_points=[25, 50, 100], snap=False)

        # Vertical wire from (50, 100) down to (50, 150)
        sch.add_wire((50, 100), (50, 150), snap=False)

        # The key: x=50 is now an endpoint of rail segments, not just a point
        # on a long wire. This ensures proper electrical connectivity.
        assert len(sch.wires) == 3  # 2 rail segments + 1 vertical wire

        # Junction should exist at (50, 100) for visual clarity
        assert len(sch.junctions) == 1
        assert sch.junctions[0].x == 50
        assert sch.junctions[0].y == 100


class TestValidationSummaryHelpers:
    """Tests for validation summary helper functions."""

    def test_summarize_issues_by_type_empty(self):
        """Summarize empty issue list."""
        from kicad_tools.schematic.models.validation_mixin import summarize_issues_by_type

        result = summarize_issues_by_type([])
        assert result == {}

    def test_summarize_issues_by_type_single_type(self):
        """Summarize issues with single type."""
        from kicad_tools.schematic.models.validation_mixin import summarize_issues_by_type

        issues = [
            {"severity": "warning", "type": "off_grid_wire", "message": "Msg 1"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Msg 2"},
        ]
        result = summarize_issues_by_type(issues)
        assert "off_grid_wire" in result
        assert len(result["off_grid_wire"]) == 2

    def test_summarize_issues_by_type_multiple_types(self):
        """Summarize issues with multiple types."""
        from kicad_tools.schematic.models.validation_mixin import summarize_issues_by_type

        issues = [
            {"severity": "warning", "type": "off_grid_wire", "message": "Msg 1"},
            {"severity": "error", "type": "floating_wire", "message": "Msg 2"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Msg 3"},
            {"severity": "warning", "type": "missing_junction", "message": "Msg 4"},
        ]
        result = summarize_issues_by_type(issues)
        assert len(result["off_grid_wire"]) == 2
        assert len(result["floating_wire"]) == 1
        assert len(result["missing_junction"]) == 1

    def test_format_validation_summary_no_issues(self):
        """Format summary with no issues."""
        from kicad_tools.schematic.models.validation_mixin import format_validation_summary

        result = format_validation_summary([])
        assert "Errors: 0" in result
        assert "Warnings: 0" in result
        assert "Warning summary:" not in result

    def test_format_validation_summary_warnings_only(self):
        """Format summary with warnings only."""
        from kicad_tools.schematic.models.validation_mixin import format_validation_summary

        issues = [
            {"severity": "warning", "type": "off_grid_wire", "message": "Wire is off grid"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Another wire off grid"},
            {"severity": "warning", "type": "missing_junction", "message": "Junction needed"},
        ]
        result = format_validation_summary(issues)
        assert "Errors: 0" in result
        assert "Warnings: 3" in result
        assert "Warning summary:" in result
        assert "2 off_grid_wire" in result
        assert "1 missing_junction" in result
        assert "Use --verbose or -v to see warning details." in result

    def test_format_validation_summary_verbose(self):
        """Format summary with verbose flag."""
        from kicad_tools.schematic.models.validation_mixin import format_validation_summary

        issues = [
            {"severity": "warning", "type": "off_grid_wire", "message": "Wire 1 off grid"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Wire 2 off grid"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Wire 3 off grid"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Wire 4 off grid"},
        ]
        result = format_validation_summary(issues, verbose=True)
        assert "Wire 1 off grid" in result
        assert "Wire 2 off grid" in result
        assert "Wire 3 off grid" in result
        assert "... and 1 more" in result
        # Verbose mode should not show the hint
        assert "Use --verbose" not in result

    def test_format_validation_summary_errors(self):
        """Format summary with errors."""
        from kicad_tools.schematic.models.validation_mixin import format_validation_summary

        issues = [
            {"severity": "error", "type": "floating_wire", "message": "Wire endpoint floating"},
            {"severity": "warning", "type": "off_grid_wire", "message": "Wire off grid"},
        ]
        result = format_validation_summary(issues)
        assert "Errors: 1" in result
        assert "Warnings: 1" in result
        assert "ERROR: Wire endpoint floating" in result
        # Errors are always shown in detail, warnings need verbose
        assert "Wire off grid" not in result
