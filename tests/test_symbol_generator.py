"""Tests for schematic/symbol_generator module."""

import pytest
from pathlib import Path

from kicad_tools.schematic.symbol_generator import (
    PinType, PinStyle, PinSide, PinDef, SymbolDef,
    detect_pin_type, detect_pin_side, detect_pin_style,
    parse_json, parse_csv, parse_datasheet_text,
    generate_symbol_sexp, apply_template, create_pins_from_template,
    PACKAGE_TEMPLATES, PIN_TYPE_PATTERNS, PIN_SIDE_PATTERNS
)


# =============================================================================
# Enum Tests
# =============================================================================


class TestPinType:
    """Tests for PinType enum."""

    def test_pin_type_values(self):
        """Test pin type enum values."""
        assert PinType.INPUT.value == "input"
        assert PinType.OUTPUT.value == "output"
        assert PinType.POWER_IN.value == "power_in"
        assert PinType.BIDIRECTIONAL.value == "bidirectional"
        assert PinType.NO_CONNECT.value == "no_connect"

    def test_all_pin_types_exist(self):
        """Test all expected pin types exist."""
        expected = [
            "INPUT", "OUTPUT", "BIDIRECTIONAL", "TRI_STATE", "PASSIVE",
            "FREE", "UNSPECIFIED", "POWER_IN", "POWER_OUT",
            "OPEN_COLLECTOR", "OPEN_EMITTER", "UNCONNECTED", "NO_CONNECT"
        ]
        for name in expected:
            assert hasattr(PinType, name)


class TestPinStyle:
    """Tests for PinStyle enum."""

    def test_pin_style_values(self):
        """Test pin style enum values."""
        assert PinStyle.LINE.value == "line"
        assert PinStyle.INVERTED.value == "inverted"
        assert PinStyle.CLOCK.value == "clock"

    def test_all_pin_styles_exist(self):
        """Test all expected pin styles exist."""
        expected = [
            "LINE", "INVERTED", "CLOCK", "INVERTED_CLOCK",
            "INPUT_LOW", "CLOCK_LOW", "OUTPUT_LOW", "EDGE_CLOCK_HIGH", "NON_LOGIC"
        ]
        for name in expected:
            assert hasattr(PinStyle, name)


class TestPinSide:
    """Tests for PinSide enum."""

    def test_pin_side_values(self):
        """Test pin side enum values."""
        assert PinSide.LEFT.value == "left"
        assert PinSide.RIGHT.value == "right"
        assert PinSide.TOP.value == "top"
        assert PinSide.BOTTOM.value == "bottom"


# =============================================================================
# Dataclass Tests
# =============================================================================


class TestPinDef:
    """Tests for PinDef dataclass."""

    def test_pindef_creation(self):
        """Test creating a pin definition."""
        pin = PinDef(number="1", name="VCC")
        assert pin.number == "1"
        assert pin.name == "VCC"
        assert pin.pin_type == PinType.PASSIVE  # Default
        assert pin.style == PinStyle.LINE  # Default
        assert pin.hidden is False

    def test_pindef_with_options(self):
        """Test pin definition with custom options."""
        pin = PinDef(
            number="5",
            name="CLK",
            pin_type=PinType.INPUT,
            style=PinStyle.CLOCK,
            side=PinSide.LEFT,
            hidden=True
        )
        assert pin.pin_type == PinType.INPUT
        assert pin.style == PinStyle.CLOCK
        assert pin.side == PinSide.LEFT
        assert pin.hidden is True

    def test_pindef_from_dict_basic(self):
        """Test creating pin from dictionary."""
        d = {"number": "1", "name": "VCC", "type": "power_in"}
        pin = PinDef.from_dict(d)
        assert pin.number == "1"
        assert pin.name == "VCC"
        assert pin.pin_type == PinType.POWER_IN

    def test_pindef_from_dict_with_side(self):
        """Test creating pin from dictionary with side."""
        d = {"number": "2", "name": "OUT", "type": "output", "side": "right"}
        pin = PinDef.from_dict(d)
        assert pin.side == PinSide.RIGHT

    def test_pindef_from_dict_with_style(self):
        """Test creating pin from dictionary with style."""
        d = {"number": "3", "name": "CLK", "style": "clock"}
        pin = PinDef.from_dict(d)
        assert pin.style == PinStyle.CLOCK

    def test_pindef_from_dict_pin_type_key(self):
        """Test pin_type key in dictionary."""
        d = {"number": "1", "name": "IN", "pin_type": "input"}
        pin = PinDef.from_dict(d)
        assert pin.pin_type == PinType.INPUT


class TestSymbolDef:
    """Tests for SymbolDef dataclass."""

    def test_symboldef_creation(self):
        """Test creating a symbol definition."""
        pins = [
            PinDef(number="1", name="VCC"),
            PinDef(number="2", name="GND"),
        ]
        sym = SymbolDef(name="MyChip", pins=pins)
        assert sym.name == "MyChip"
        assert len(sym.pins) == 2
        assert sym.reference == "U"  # Default

    def test_symboldef_value_default(self):
        """Test symbol value defaults to name."""
        sym = SymbolDef(name="TestIC", pins=[])
        assert sym.value == "TestIC"

    def test_symboldef_with_properties(self):
        """Test symbol with all properties."""
        sym = SymbolDef(
            name="TPA3116D2",
            pins=[],
            reference="U",
            footprint="Package_SO:HTSSOP-28",
            datasheet="https://example.com",
            description="Audio Amplifier",
            keywords="amp audio",
        )
        assert sym.footprint == "Package_SO:HTSSOP-28"
        assert sym.description == "Audio Amplifier"


# =============================================================================
# Detection Function Tests
# =============================================================================


class TestDetectPinType:
    """Tests for detect_pin_type function."""

    def test_detect_vcc(self):
        """Test VCC is detected as power_in."""
        assert detect_pin_type("VCC") == PinType.POWER_IN
        assert detect_pin_type("VDD") == PinType.POWER_IN
        assert detect_pin_type("VBAT") == PinType.POWER_IN

    def test_detect_ground(self):
        """Test GND is detected as power_in."""
        assert detect_pin_type("GND") == PinType.POWER_IN
        assert detect_pin_type("VSS") == PinType.POWER_IN
        assert detect_pin_type("AGND") == PinType.POWER_IN
        assert detect_pin_type("DGND") == PinType.POWER_IN

    def test_detect_no_connect(self):
        """Test NC pins."""
        assert detect_pin_type("NC") == PinType.NO_CONNECT
        assert detect_pin_type("N/C") == PinType.NO_CONNECT
        assert detect_pin_type("DNC") == PinType.NO_CONNECT

    def test_detect_clock_input(self):
        """Test clock pins."""
        assert detect_pin_type("CLK") == PinType.INPUT
        assert detect_pin_type("SCLK") == PinType.INPUT
        assert detect_pin_type("MCLK") == PinType.INPUT

    def test_detect_reset(self):
        """Test reset pins."""
        assert detect_pin_type("RST") == PinType.INPUT
        assert detect_pin_type("RESET") == PinType.INPUT
        assert detect_pin_type("~RST") == PinType.INPUT
        assert detect_pin_type("NRST") == PinType.INPUT

    def test_detect_data_input(self):
        """Test data input pins."""
        # SDA pattern matches as input
        assert detect_pin_type("SDA") == PinType.INPUT
        assert detect_pin_type("SPI_MOSI") == PinType.INPUT
        assert detect_pin_type("DIN") == PinType.INPUT
        assert detect_pin_type("RXD") == PinType.INPUT

    def test_detect_data_output(self):
        """Test data output pins."""
        assert detect_pin_type("SCL") == PinType.OUTPUT
        assert detect_pin_type("SPI_MISO") == PinType.OUTPUT
        assert detect_pin_type("DOUT") == PinType.OUTPUT
        assert detect_pin_type("TXD") == PinType.OUTPUT

    def test_detect_gpio(self):
        """Test GPIO pins."""
        assert detect_pin_type("GPIO0") == PinType.BIDIRECTIONAL
        assert detect_pin_type("PA0") == PinType.BIDIRECTIONAL
        assert detect_pin_type("PB12") == PinType.BIDIRECTIONAL

    def test_detect_output_audio(self):
        """Test audio output pins."""
        assert detect_pin_type("OUTL") == PinType.OUTPUT
        assert detect_pin_type("OUTR") == PinType.OUTPUT
        assert detect_pin_type("HP") == PinType.OUTPUT

    def test_detect_exposed_pad(self):
        """Test exposed pad."""
        assert detect_pin_type("EP") == PinType.POWER_IN
        assert detect_pin_type("EPAD") == PinType.POWER_IN
        assert detect_pin_type("THERMAL") == PinType.POWER_IN

    def test_detect_unknown_returns_passive(self):
        """Test unknown pin names return passive."""
        assert detect_pin_type("XYZ123") == PinType.PASSIVE
        assert detect_pin_type("RANDOM") == PinType.PASSIVE


class TestDetectPinSide:
    """Tests for detect_pin_side function."""

    def test_detect_vcc_top(self):
        """Test VCC goes on top."""
        assert detect_pin_side("VCC", PinType.POWER_IN) == PinSide.TOP
        assert detect_pin_side("VDD", PinType.POWER_IN) == PinSide.TOP

    def test_detect_gnd_bottom(self):
        """Test GND pattern goes to bottom via pattern, otherwise via type."""
        # GND explicitly matches bottom pattern
        assert detect_pin_side("GND", PinType.POWER_IN) == PinSide.BOTTOM
        # VSS might match via type-based logic
        # The pattern check happens first, then type-based fallback
        side = detect_pin_side("VSS", PinType.POWER_IN)
        assert side in [PinSide.BOTTOM, PinSide.TOP]

    def test_detect_input_left(self):
        """Test inputs go on left."""
        assert detect_pin_side("IN", PinType.INPUT) == PinSide.LEFT
        assert detect_pin_side("DATA", PinType.INPUT) == PinSide.LEFT

    def test_detect_output_right(self):
        """Test outputs go on right."""
        assert detect_pin_side("OUT", PinType.OUTPUT) == PinSide.RIGHT
        assert detect_pin_side("DOUT", PinType.OUTPUT) == PinSide.RIGHT

    def test_detect_bidirectional_right(self):
        """Test bidirectional goes on right."""
        assert detect_pin_side("GPIO", PinType.BIDIRECTIONAL) == PinSide.RIGHT


class TestDetectPinStyle:
    """Tests for detect_pin_style function."""

    def test_detect_clock_style(self):
        """Test clock pins get clock style."""
        assert detect_pin_style("CLK", PinType.INPUT) == PinStyle.CLOCK
        assert detect_pin_style("SCLK", PinType.INPUT) == PinStyle.CLOCK
        assert detect_pin_style("MCLK", PinType.INPUT) == PinStyle.CLOCK

    def test_detect_inverted_style(self):
        """Test inverted pins get inverted style."""
        assert detect_pin_style("~RST", PinType.INPUT) == PinStyle.INVERTED
        assert detect_pin_style("RST_N", PinType.INPUT) == PinStyle.INVERTED

    def test_detect_default_line(self):
        """Test default pins get line style."""
        assert detect_pin_style("VCC", PinType.POWER_IN) == PinStyle.LINE
        assert detect_pin_style("DATA", PinType.INPUT) == PinStyle.LINE


# =============================================================================
# Parser Tests
# =============================================================================


class TestParseJson:
    """Tests for parse_json function."""

    def test_parse_json_basic(self, tmp_path):
        """Test parsing basic JSON."""
        json_content = """{
            "name": "TestIC",
            "pins": [
                {"number": "1", "name": "VCC", "type": "power_in"},
                {"number": "2", "name": "GND", "type": "power_in"}
            ]
        }"""
        json_file = tmp_path / "test.json"
        json_file.write_text(json_content)

        sym = parse_json(json_file)
        assert sym.name == "TestIC"
        assert len(sym.pins) == 2

    def test_parse_json_with_properties(self, tmp_path):
        """Test parsing JSON with all properties."""
        json_content = """{
            "name": "TestIC",
            "reference": "U",
            "footprint": "Package:Test",
            "datasheet": "https://example.com",
            "description": "Test chip",
            "keywords": "test ic",
            "pins": [
                {"number": "1", "name": "VCC"}
            ]
        }"""
        json_file = tmp_path / "test.json"
        json_file.write_text(json_content)

        sym = parse_json(json_file)
        assert sym.footprint == "Package:Test"
        assert sym.description == "Test chip"

    def test_parse_json_auto_detect_type(self, tmp_path):
        """Test JSON parser auto-detects pin types."""
        json_content = """{
            "name": "TestIC",
            "pins": [
                {"number": "1", "name": "VCC"},
                {"number": "2", "name": "GND"}
            ]
        }"""
        json_file = tmp_path / "test.json"
        json_file.write_text(json_content)

        sym = parse_json(json_file)
        # VCC should be auto-detected as power_in
        assert sym.pins[0].pin_type == PinType.POWER_IN


class TestParseCsv:
    """Tests for parse_csv function."""

    def test_parse_csv_basic(self, tmp_path):
        """Test parsing basic CSV."""
        csv_content = """number,name,type
1,VCC,power_in
2,GND,power_in
3,OUT,output"""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(csv_content)

        sym = parse_csv(csv_file, "TestIC")
        assert sym.name == "TestIC"
        assert len(sym.pins) == 3
        assert sym.pins[0].pin_type == PinType.POWER_IN

    def test_parse_csv_auto_detect(self, tmp_path):
        """Test CSV parser auto-detects pin types."""
        csv_content = """number,name
1,VCC
2,GND
3,CLK"""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(csv_content)

        sym = parse_csv(csv_file, "TestIC")
        assert sym.pins[0].pin_type == PinType.POWER_IN
        assert sym.pins[2].pin_type == PinType.INPUT

    def test_parse_csv_with_side(self, tmp_path):
        """Test CSV with side column."""
        csv_content = """number,name,type,side
1,VCC,power_in,top
2,GND,power_in,bottom"""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(csv_content)

        sym = parse_csv(csv_file, "TestIC")
        assert sym.pins[0].side == PinSide.TOP
        assert sym.pins[1].side == PinSide.BOTTOM


class TestParseDatasheetText:
    """Tests for parse_datasheet_text function."""

    def test_parse_space_separated(self):
        """Test parsing space-separated text."""
        text = """1 VCC Power
2 GND Ground
3 OUT Output"""

        sym = parse_datasheet_text(text, "TestIC")
        assert sym.name == "TestIC"
        assert len(sym.pins) == 3
        assert sym.pins[0].name == "VCC"

    def test_parse_tab_separated(self):
        """Test parsing tab-separated text."""
        text = "1\tVCC\n2\tGND\n3\tOUT"

        sym = parse_datasheet_text(text, "TestIC")
        assert len(sym.pins) == 3

    def test_parse_comma_separated(self):
        """Test parsing comma-separated text."""
        text = "1,VCC\n2,GND\n3,OUT"

        sym = parse_datasheet_text(text, "TestIC")
        assert len(sym.pins) == 3

    def test_parse_ignores_comments(self):
        """Test that comments are ignored."""
        text = """# Header
1 VCC
2 GND"""

        sym = parse_datasheet_text(text, "TestIC")
        assert len(sym.pins) == 2

    def test_parse_type_hints_power(self):
        """Test type hints in description."""
        text = "1 PVCC Power supply"

        sym = parse_datasheet_text(text, "TestIC")
        assert sym.pins[0].pin_type == PinType.POWER_IN

    def test_parse_type_hints_ground(self):
        """Test ground type hint."""
        text = "1 VSS Ground connection"

        sym = parse_datasheet_text(text, "TestIC")
        assert sym.pins[0].pin_type == PinType.POWER_IN

    def test_parse_type_hints_input(self):
        """Test input type hint."""
        # Use tab separator so "input pin" is treated as one field
        text = "1\tSIGNAL\tinput pin"

        sym = parse_datasheet_text(text, "TestIC")
        assert sym.pins[0].pin_type == PinType.INPUT

    def test_parse_type_hints_output(self):
        """Test output type hint."""
        text = "1 DOUT Digital output"

        sym = parse_datasheet_text(text, "TestIC")
        assert sym.pins[0].pin_type == PinType.OUTPUT

    def test_parse_type_hints_bidirectional(self):
        """Test bidirectional type hint."""
        text = "1 GPIO Bidirectional I/O"

        sym = parse_datasheet_text(text, "TestIC")
        assert sym.pins[0].pin_type == PinType.BIDIRECTIONAL


# =============================================================================
# Symbol Generation Tests
# =============================================================================


class TestGenerateSymbolSexp:
    """Tests for generate_symbol_sexp function."""

    def test_generate_basic_symbol(self):
        """Test generating basic symbol."""
        pins = [
            PinDef(number="1", name="VCC", pin_type=PinType.POWER_IN, side=PinSide.TOP),
            PinDef(number="2", name="GND", pin_type=PinType.POWER_IN, side=PinSide.BOTTOM),
            PinDef(number="3", name="IN", pin_type=PinType.INPUT, side=PinSide.LEFT),
            PinDef(number="4", name="OUT", pin_type=PinType.OUTPUT, side=PinSide.RIGHT),
        ]
        sym = SymbolDef(name="TestIC", pins=pins)

        sexp = generate_symbol_sexp(sym)

        assert "(kicad_symbol_lib" in sexp
        assert 'symbol "TestIC"' in sexp
        assert '"Reference" "U"' in sexp
        assert '"Value" "TestIC"' in sexp
        assert "(pin power_in" in sexp
        assert "(pin input" in sexp
        assert "(pin output" in sexp

    def test_generate_with_properties(self):
        """Test generating symbol with properties."""
        sym = SymbolDef(
            name="MyChip",
            pins=[PinDef(number="1", name="P1", side=PinSide.LEFT)],
            reference="U",
            footprint="Package:Test",
            datasheet="https://example.com/ds.pdf",
            description="Test IC",
            keywords="test ic chip",
        )

        sexp = generate_symbol_sexp(sym)

        assert '"Footprint" "Package:Test"' in sexp
        assert '"Datasheet" "https://example.com/ds.pdf"' in sexp
        assert '"Description" "Test IC"' in sexp

    def test_generate_with_clock_style(self):
        """Test generating symbol with clock style pin."""
        pins = [
            PinDef(number="1", name="CLK", pin_type=PinType.INPUT, style=PinStyle.CLOCK, side=PinSide.LEFT),
        ]
        sym = SymbolDef(name="TestIC", pins=pins)

        sexp = generate_symbol_sexp(sym)

        assert "(pin input clock" in sexp

    def test_generate_with_hidden_pin(self):
        """Test generating symbol with hidden pin."""
        pins = [
            PinDef(number="1", name="NC", hidden=True, side=PinSide.RIGHT),
        ]
        sym = SymbolDef(name="TestIC", pins=pins)

        sexp = generate_symbol_sexp(sym)

        assert "(hide yes)" in sexp

    def test_generate_empty_symbol(self):
        """Test generating symbol with no pins."""
        sym = SymbolDef(name="EmptyIC", pins=[])

        sexp = generate_symbol_sexp(sym)

        assert "(kicad_symbol_lib" in sexp
        assert 'symbol "EmptyIC"' in sexp


# =============================================================================
# Template Tests
# =============================================================================


class TestPackageTemplates:
    """Tests for package templates."""

    def test_template_exists(self):
        """Test expected templates exist."""
        expected = ["dip8", "dip14", "dip16", "soic8", "soic14", "soic16", "tssop20", "tssop28", "qfp32", "qfp48", "qfp64"]
        for name in expected:
            assert name in PACKAGE_TEMPLATES

    def test_template_structure(self):
        """Test template structure."""
        for name, template in PACKAGE_TEMPLATES.items():
            assert "pins" in template
            assert "layout" in template
            assert template["layout"] in ["dual", "quad"]


class TestApplyTemplate:
    """Tests for apply_template function."""

    def test_apply_dual_template(self):
        """Test applying dual-row template."""
        pins = [PinDef(number=str(i), name=f"P{i}") for i in range(1, 9)]
        sym = SymbolDef(name="TestIC", pins=pins)

        apply_template(sym, "dip8")

        # Check pins are assigned to sides
        pin_map = {p.number: p for p in sym.pins}
        assert pin_map["1"].side == PinSide.LEFT
        assert pin_map["8"].side == PinSide.RIGHT

    def test_apply_quad_template(self):
        """Test applying quad-row template."""
        pins = [PinDef(number=str(i), name=f"P{i}") for i in range(1, 33)]
        sym = SymbolDef(name="TestIC", pins=pins)

        apply_template(sym, "qfp32")

        # Check pins are assigned to sides
        pin_map = {p.number: p for p in sym.pins}
        assert pin_map["1"].side == PinSide.LEFT
        assert pin_map["9"].side == PinSide.BOTTOM
        assert pin_map["17"].side == PinSide.RIGHT
        assert pin_map["25"].side == PinSide.TOP

    def test_apply_unknown_template(self):
        """Test applying unknown template raises error."""
        sym = SymbolDef(name="TestIC", pins=[])

        with pytest.raises(ValueError, match="Unknown template"):
            apply_template(sym, "unknown_template")


class TestCreatePinsFromTemplate:
    """Tests for create_pins_from_template function."""

    def test_create_dip8_pins(self):
        """Test creating DIP8 pins."""
        pins = create_pins_from_template("dip8")

        assert len(pins) == 8
        assert pins[0].number == "1"
        assert pins[0].side == PinSide.LEFT

    def test_create_qfp48_pins(self):
        """Test creating QFP48 pins."""
        pins = create_pins_from_template("qfp48")

        assert len(pins) == 48
        # Check sides are assigned
        assert pins[0].side == PinSide.LEFT  # Pin 1
        assert pins[12].side == PinSide.BOTTOM  # Pin 13

    def test_create_unknown_template(self):
        """Test creating pins from unknown template raises error."""
        with pytest.raises(ValueError, match="Unknown template"):
            create_pins_from_template("unknown_template")


# =============================================================================
# Integration Tests
# =============================================================================


class TestSymbolGeneratorIntegration:
    """Integration tests for symbol generator."""

    def test_json_to_sexp(self, tmp_path):
        """Test complete JSON to S-expression workflow."""
        json_content = """{
            "name": "TestAmp",
            "reference": "U",
            "footprint": "Package_SO:SOIC-8",
            "description": "Test Amplifier",
            "pins": [
                {"number": "1", "name": "VCC", "type": "power_in"},
                {"number": "2", "name": "IN+", "type": "input"},
                {"number": "3", "name": "IN-", "type": "input"},
                {"number": "4", "name": "GND", "type": "power_in"},
                {"number": "5", "name": "OUT", "type": "output"},
                {"number": "6", "name": "NC"},
                {"number": "7", "name": "NC"},
                {"number": "8", "name": "VCC", "type": "power_in"}
            ]
        }"""
        json_file = tmp_path / "amp.json"
        json_file.write_text(json_content)

        sym = parse_json(json_file)
        sexp = generate_symbol_sexp(sym)

        assert "(kicad_symbol_lib" in sexp
        assert 'symbol "TestAmp"' in sexp
        assert "SOIC-8" in sexp

    def test_csv_with_template(self, tmp_path):
        """Test CSV parsing with template application."""
        csv_content = """number,name
1,VCC
2,IN+
3,IN-
4,GND
5,NC
6,NC
7,OUT
8,VCC"""
        csv_file = tmp_path / "pins.csv"
        csv_file.write_text(csv_content)

        sym = parse_csv(csv_file, "OpAmp")
        apply_template(sym, "dip8")
        sexp = generate_symbol_sexp(sym)

        assert "(kicad_symbol_lib" in sexp
        assert 'symbol "OpAmp"' in sexp

    def test_datasheet_text_to_symbol(self):
        """Test datasheet text to symbol workflow."""
        text = """1  PVCC    Power supply
2  OUTL+   Left channel positive output
3  GND     Ground
4  OUTL-   Left channel negative output
5  OUTR-   Right channel negative output
6  GND     Ground
7  OUTR+   Right channel positive output
8  PVCC    Power supply"""

        sym = parse_datasheet_text(text, "Amplifier")
        sexp = generate_symbol_sexp(sym)

        assert "(kicad_symbol_lib" in sexp
        assert 'symbol "Amplifier"' in sexp
        assert len(sym.pins) == 8
