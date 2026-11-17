"""Tests for schematic library module to increase coverage.

Tests for:
- list_libraries
- list_symbols
- search_symbols
- find_pins
- get_pins_by_type
- describe_symbol
"""

import pytest

from kicad_tools.schematic.library import (
    describe_symbol,
    find_pins,
    get_pins_by_type,
    list_libraries,
    list_symbols,
    search_symbols,
)
from kicad_tools.schematic.models.pin import Pin
from kicad_tools.schematic.models.symbol import SymbolDef, SymbolInstance


class TestListLibraries:
    """Tests for list_libraries function."""

    def test_list_libraries_with_custom_paths(self, tmp_path):
        """List libraries from custom paths."""
        # Create mock library files
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        (lib_dir / "Audio.kicad_sym").write_text("(kicad_symbol_lib)")
        (lib_dir / "Device.kicad_sym").write_text("(kicad_symbol_lib)")
        (lib_dir / "Power.kicad_sym").write_text("(kicad_symbol_lib)")

        libs = list_libraries([lib_dir])
        assert "Audio" in libs
        assert "Device" in libs
        assert "Power" in libs

    def test_list_libraries_sorted(self, tmp_path):
        """Libraries are returned sorted."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        (lib_dir / "Zebra.kicad_sym").write_text("(kicad_symbol_lib)")
        (lib_dir / "Alpha.kicad_sym").write_text("(kicad_symbol_lib)")
        (lib_dir / "Middle.kicad_sym").write_text("(kicad_symbol_lib)")

        libs = list_libraries([lib_dir])
        assert libs == ["Alpha", "Middle", "Zebra"]

    def test_list_libraries_empty_path(self, tmp_path):
        """Empty library path returns empty list."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        libs = list_libraries([empty_dir])
        assert libs == []

    def test_list_libraries_nonexistent_path(self, tmp_path):
        """Non-existent path is handled gracefully."""
        nonexistent = tmp_path / "nonexistent"
        libs = list_libraries([nonexistent])
        assert libs == []

    def test_list_libraries_deduplicated(self, tmp_path):
        """Duplicate libraries are deduplicated."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        (dir1 / "Same.kicad_sym").write_text("(kicad_symbol_lib)")
        (dir2 / "Same.kicad_sym").write_text("(kicad_symbol_lib)")

        libs = list_libraries([dir1, dir2])
        assert libs.count("Same") == 1


class TestListSymbols:
    """Tests for list_symbols function."""

    def test_list_symbols_from_library(self, tmp_path):
        """List symbols from a library file."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        lib_content = """(kicad_symbol_lib
          (version "20231120")
          (symbol "Resistor"
            (property "Reference" "R")
          )
          (symbol "Capacitor"
            (property "Reference" "C")
          )
        )"""
        (lib_dir / "Device.kicad_sym").write_text(lib_content)

        symbols = list_symbols("Device", [lib_dir])
        assert "Resistor" in symbols
        assert "Capacitor" in symbols

    def test_list_symbols_excludes_internal_symbols(self, tmp_path):
        """Symbols starting with underscore are excluded."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        # The regex excludes symbols where first char is underscore
        lib_content = """(kicad_symbol_lib
          (symbol "Op_Amp"
            (property "Reference" "U")
          )
          (symbol "_internal_symbol"
            (rectangle...)
          )
        )"""
        (lib_dir / "Amplifier.kicad_sym").write_text(lib_content)

        symbols = list_symbols("Amplifier", [lib_dir])
        assert "Op_Amp" in symbols
        # Symbols starting with _ are excluded
        assert "_internal_symbol" not in symbols

    def test_list_symbols_sorted(self, tmp_path):
        """Symbols are returned sorted."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        lib_content = """(kicad_symbol_lib
          (symbol "Zebra")
          (symbol "Alpha")
          (symbol "Middle")
        )"""
        (lib_dir / "Test.kicad_sym").write_text(lib_content)

        symbols = list_symbols("Test", [lib_dir])
        assert symbols == ["Alpha", "Middle", "Zebra"]

    def test_list_symbols_library_not_found(self, tmp_path):
        """Error when library not found."""
        from kicad_tools.schematic.exceptions import LibraryNotFoundError

        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()

        with pytest.raises(LibraryNotFoundError):
            list_symbols("NonExistent", [lib_dir])


class TestSearchSymbols:
    """Tests for search_symbols function."""

    def test_search_symbols_pattern(self, tmp_path):
        """Search symbols matching pattern."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        lib_content = """(kicad_symbol_lib
          (symbol "LM7805")
          (symbol "LM7812")
          (symbol "TPS63000")
        )"""
        (lib_dir / "Regulator.kicad_sym").write_text(lib_content)

        matches = search_symbols("LM*", [lib_dir])
        assert len(matches) == 2
        assert "Regulator:LM7805" in matches
        assert "Regulator:LM7812" in matches

    def test_search_symbols_case_insensitive(self, tmp_path):
        """Search is case insensitive."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        lib_content = """(kicad_symbol_lib
          (symbol "PCM5122")
          (symbol "pcm5142")
        )"""
        (lib_dir / "Audio.kicad_sym").write_text(lib_content)

        matches = search_symbols("pcm*", [lib_dir])
        assert len(matches) == 2

    def test_search_symbols_no_matches(self, tmp_path):
        """No matches returns empty list."""
        lib_dir = tmp_path / "libs"
        lib_dir.mkdir()
        lib_content = """(kicad_symbol_lib
          (symbol "Something")
        )"""
        (lib_dir / "Test.kicad_sym").write_text(lib_content)

        matches = search_symbols("Nonexistent*", [lib_dir])
        assert matches == []


class TestFindPins:
    """Tests for find_pins function."""

    @pytest.fixture
    def mock_symbol(self):
        """Create a mock symbol instance with pins."""
        pins = [
            Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54, pin_type="power_in"),
            Pin(name="GND", number="2", x=0, y=0, angle=0, length=2.54, pin_type="power_in"),
            Pin(name="SCK", number="3", x=0, y=0, angle=0, length=2.54, pin_type="input"),
            Pin(name="SDA", number="4", x=0, y=0, angle=0, length=2.54, pin_type="bidirectional"),
            Pin(name="SCL", number="5", x=0, y=0, angle=0, length=2.54, pin_type="bidirectional"),
            Pin(name="OUT", number="6", x=0, y=0, angle=0, length=2.54, pin_type="output"),
        ]
        sym_def = SymbolDef(lib_id="Test:IC", name="IC", raw_sexp="", pins=pins)
        return SymbolInstance(
            symbol_def=sym_def,
            x=100,
            y=100,
            rotation=0,
            reference="U1",
            value="IC",
        )

    def test_find_pins_by_name_pattern(self, mock_symbol):
        """Find pins matching name pattern."""
        matches = find_pins(mock_symbol, "SC*")
        assert len(matches) == 2  # SCK and SCL
        names = [p.name for p in matches]
        assert "SCK" in names
        assert "SCL" in names

    def test_find_pins_by_number_pattern(self, mock_symbol):
        """Find pins matching number pattern."""
        matches = find_pins(mock_symbol, "1")
        assert len(matches) == 1
        assert matches[0].number == "1"

    def test_find_pins_case_insensitive(self, mock_symbol):
        """Pin search is case insensitive."""
        matches = find_pins(mock_symbol, "vcc")
        assert len(matches) == 1
        assert matches[0].name == "VCC"

    def test_find_pins_wildcard(self, mock_symbol):
        """Find all pins with wildcard."""
        matches = find_pins(mock_symbol, "*")
        assert len(matches) == 6

    def test_find_pins_no_matches(self, mock_symbol):
        """No matches returns empty list."""
        matches = find_pins(mock_symbol, "NONEXISTENT")
        assert matches == []


class TestGetPinsByType:
    """Tests for get_pins_by_type function."""

    @pytest.fixture
    def mock_symbol(self):
        """Create a mock symbol instance with pins."""
        pins = [
            Pin(name="VCC", number="1", x=0, y=0, angle=0, length=2.54, pin_type="power_in"),
            Pin(name="GND", number="2", x=0, y=0, angle=0, length=2.54, pin_type="power_in"),
            Pin(name="IN", number="3", x=0, y=0, angle=0, length=2.54, pin_type="input"),
            Pin(name="OUT", number="4", x=0, y=0, angle=0, length=2.54, pin_type="output"),
        ]
        sym_def = SymbolDef(lib_id="Test:IC", name="IC", raw_sexp="", pins=pins)
        return SymbolInstance(
            symbol_def=sym_def,
            x=100,
            y=100,
            rotation=0,
            reference="U1",
            value="IC",
        )

    def test_get_power_pins(self, mock_symbol):
        """Get power input pins."""
        pins = get_pins_by_type(mock_symbol, "power_in")
        assert len(pins) == 2
        names = [p.name for p in pins]
        assert "VCC" in names
        assert "GND" in names

    def test_get_input_pins(self, mock_symbol):
        """Get input pins."""
        pins = get_pins_by_type(mock_symbol, "input")
        assert len(pins) == 1
        assert pins[0].name == "IN"

    def test_get_output_pins(self, mock_symbol):
        """Get output pins."""
        pins = get_pins_by_type(mock_symbol, "output")
        assert len(pins) == 1
        assert pins[0].name == "OUT"

    def test_get_nonexistent_type(self, mock_symbol):
        """Non-existent pin type returns empty list."""
        pins = get_pins_by_type(mock_symbol, "passive")
        assert pins == []


class TestDescribeSymbol:
    """Tests for describe_symbol function."""

    @pytest.fixture
    def mock_symbol(self):
        """Create a mock symbol instance with pins."""
        pins = [
            Pin(
                name="VCC", number="1", x=-5.08, y=2.54, angle=180, length=2.54, pin_type="power_in"
            ),
            Pin(
                name="GND",
                number="2",
                x=-5.08,
                y=-2.54,
                angle=180,
                length=2.54,
                pin_type="power_in",
            ),
            Pin(name="IN", number="3", x=-5.08, y=0, angle=180, length=2.54, pin_type="input"),
            Pin(name="OUT", number="4", x=5.08, y=0, angle=0, length=2.54, pin_type="output"),
        ]
        sym_def = SymbolDef(lib_id="Test:OpAmp", name="OpAmp", raw_sexp="", pins=pins)
        return SymbolInstance(
            symbol_def=sym_def,
            x=100,
            y=100,
            rotation=0,
            reference="U1",
            value="TL072",
        )

    def test_describe_symbol_basic(self, mock_symbol):
        """Describe symbol includes basic info."""
        desc = describe_symbol(mock_symbol)
        assert "U1" in desc
        assert "Test:OpAmp" in desc
        assert "TL072" in desc
        assert "100" in desc  # position

    def test_describe_symbol_pins(self, mock_symbol):
        """Description includes pin information."""
        desc = describe_symbol(mock_symbol)
        assert "VCC" in desc
        assert "GND" in desc
        assert "IN" in desc
        assert "OUT" in desc
        assert "4" in desc  # pin count

    def test_describe_symbol_groups_pins(self, mock_symbol):
        """Pins are grouped by type in description."""
        desc = describe_symbol(mock_symbol)
        # Should have group headers
        assert "power" in desc.lower() or "input" in desc.lower() or "output" in desc.lower()
