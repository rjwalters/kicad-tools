"""Tests for schematic helper modules."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from kicad_tools.schematic.grid import (
    GridSize,
    DEFAULT_GRID,
    snap_to_grid,
    snap_point,
    is_on_grid,
    check_grid_alignment,
)
from kicad_tools.schematic.exceptions import (
    PinNotFoundError,
    SymbolNotFoundError,
    LibraryNotFoundError,
)
from kicad_tools.schematic.helpers import (
    _string_similarity,
    _find_similar,
    _expand_pin_aliases,
    _group_pins_by_type,
    _format_pin_list,
    PIN_ALIASES,
)
from kicad_tools.schematic.models import Pin
from kicad_tools.schematic.logging import enable_verbose, disable_verbose


class TestGridSize:
    """Tests for GridSize enum."""

    def test_schematic_grid_sizes(self):
        """Verify schematic grid sizes."""
        assert GridSize.SCH_COARSE.value == 2.54
        assert GridSize.SCH_STANDARD.value == 1.27
        assert GridSize.SCH_FINE.value == 0.635
        assert GridSize.SCH_ULTRA_FINE.value == 0.254

    def test_pcb_grid_sizes(self):
        """Verify PCB grid sizes."""
        assert GridSize.PCB_COARSE.value == 1.0
        assert GridSize.PCB_STANDARD.value == 0.5
        assert GridSize.PCB_FINE.value == 0.25
        assert GridSize.PCB_ULTRA_FINE.value == 0.1

    def test_default_grid(self):
        """Default grid is schematic standard."""
        assert DEFAULT_GRID == 1.27


class TestSnapToGrid:
    """Tests for snap_to_grid() function."""

    def test_snap_exact_value(self):
        """Exact grid values stay unchanged."""
        assert snap_to_grid(2.54) == 2.54
        assert snap_to_grid(1.27) == 1.27
        assert snap_to_grid(0.0) == 0.0

    def test_snap_rounds_up(self):
        """Values round to nearest grid point."""
        assert snap_to_grid(1.5) == 1.27  # Closer to 1.27 than 2.54
        assert snap_to_grid(2.0) == 2.54  # Closer to 2.54 than 1.27

    def test_snap_rounds_down(self):
        """Values below midpoint round down."""
        assert snap_to_grid(1.0) == 1.27  # Closer to 1.27 than 0.0
        assert snap_to_grid(0.5) == 0.0   # Closer to 0.0 than 1.27

    def test_snap_negative_values(self):
        """Negative values snap correctly."""
        assert snap_to_grid(-1.27) == -1.27
        assert snap_to_grid(-1.0) == -1.27

    def test_snap_custom_grid(self):
        """Custom grid size works."""
        assert snap_to_grid(1.0, grid=0.5) == 1.0
        assert snap_to_grid(1.3, grid=0.5) == 1.5
        assert snap_to_grid(1.2, grid=0.5) == 1.0

    def test_snap_returns_rounded(self):
        """Result is rounded to 2 decimal places."""
        result = snap_to_grid(1.5)
        assert result == round(result, 2)


class TestSnapPoint:
    """Tests for snap_point() function."""

    def test_snap_point_basic(self):
        """Snap a point to grid."""
        result = snap_point((1.5, 2.0))
        assert result == (1.27, 2.54)

    def test_snap_point_exact(self):
        """Exact grid point stays unchanged."""
        result = snap_point((1.27, 2.54))
        assert result == (1.27, 2.54)

    def test_snap_point_custom_grid(self):
        """Custom grid size."""
        result = snap_point((0.3, 0.7), grid=0.5)
        assert result == (0.5, 0.5)


class TestIsOnGrid:
    """Tests for is_on_grid() function."""

    def test_exact_grid_value(self):
        """Exact grid values are on grid."""
        assert is_on_grid(1.27) is True
        assert is_on_grid(2.54) is True
        assert is_on_grid(0.0) is True

    def test_off_grid_value(self):
        """Off-grid values return False."""
        assert is_on_grid(1.0) is False
        assert is_on_grid(1.5) is False
        assert is_on_grid(0.5) is False

    def test_within_tolerance(self):
        """Values within tolerance are on grid."""
        assert is_on_grid(1.2705, tolerance=0.001) is True
        assert is_on_grid(1.269, tolerance=0.001) is False

    def test_custom_grid(self):
        """Custom grid size."""
        assert is_on_grid(0.5, grid=0.5) is True
        assert is_on_grid(0.5, grid=0.25) is True
        assert is_on_grid(0.3, grid=0.5) is False


class TestCheckGridAlignment:
    """Tests for check_grid_alignment() function."""

    def test_on_grid_no_warning(self):
        """On-grid points return True without warning."""
        result = check_grid_alignment((1.27, 2.54), warn=False)
        assert result is True

    def test_off_grid_returns_false(self):
        """Off-grid points return False."""
        result = check_grid_alignment((1.0, 1.5), warn=False)
        assert result is False

    def test_off_grid_with_warning(self):
        """Off-grid points emit warning when warn=True."""
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            check_grid_alignment((1.0, 1.5), warn=True)
            assert len(w) == 1
            assert "Off-grid" in str(w[0].message)

    def test_context_in_warning(self):
        """Context string appears in warning."""
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            check_grid_alignment((1.0, 1.5), context="wire endpoint", warn=True)
            assert "wire endpoint" in str(w[0].message)


class TestStringSimilarity:
    """Tests for _string_similarity() function."""

    def test_identical_strings(self):
        """Identical strings have similarity 1.0."""
        assert _string_similarity("hello", "hello") == 1.0

    def test_case_insensitive(self):
        """Comparison is case insensitive."""
        assert _string_similarity("Hello", "hello") == 1.0
        assert _string_similarity("VCC", "vcc") == 1.0

    def test_completely_different(self):
        """Completely different strings have low similarity."""
        assert _string_similarity("abc", "xyz") < 0.3

    def test_partial_match(self):
        """Partially matching strings have medium similarity."""
        sim = _string_similarity("hello", "helo")
        assert 0.5 < sim < 1.0


class TestFindSimilar:
    """Tests for _find_similar() function."""

    def test_exact_prefix_match(self):
        """Exact prefix gets high score."""
        candidates = ["VCC", "VDD", "GND", "VSS"]
        result = _find_similar("VC", candidates)
        assert "VCC" in result
        assert result[0] == "VCC"  # Should be first

    def test_fuzzy_match(self):
        """Fuzzy matching works."""
        candidates = ["ENABLE", "RESET", "CLOCK"]
        result = _find_similar("ENABL", candidates)
        assert "ENABLE" in result

    def test_max_results(self):
        """Result count limited by max_results."""
        candidates = ["A", "AB", "ABC", "ABCD", "ABCDE", "ABCDEF"]
        result = _find_similar("A", candidates, max_results=3)
        assert len(result) <= 3

    def test_threshold(self):
        """Low similarity excluded by threshold."""
        candidates = ["ABC", "XYZ"]
        result = _find_similar("ABC", candidates, threshold=0.8)
        assert "ABC" in result
        assert "XYZ" not in result


class TestExpandPinAliases:
    """Tests for _expand_pin_aliases() function."""

    def test_vcc_aliases(self):
        """VCC has power aliases."""
        aliases = _expand_pin_aliases("VCC")
        assert "VCC" in aliases
        assert any(a in aliases for a in ["vdd", "v+"])

    def test_gnd_aliases(self):
        """GND has ground aliases."""
        aliases = _expand_pin_aliases("GND")
        assert "GND" in aliases
        assert any(a in aliases for a in ["vss", "ground"])

    def test_spi_aliases(self):
        """SPI pins have aliases."""
        aliases = _expand_pin_aliases("MOSI")
        assert "MOSI" in aliases
        assert any(a in aliases for a in ["sdi", "din"])

    def test_unknown_pin(self):
        """Unknown pin returns just itself."""
        aliases = _expand_pin_aliases("CUSTOM_PIN")
        assert aliases == ["CUSTOM_PIN"]

    def test_special_chars_stripped(self):
        """Special characters stripped for lookup."""
        # ~EN should find EN aliases
        aliases = _expand_pin_aliases("~EN")
        assert "~EN" in aliases


class TestGroupPinsByType:
    """Tests for _group_pins_by_type() function."""

    def test_group_power_pins(self):
        """Power pins grouped correctly."""
        mock_pin = Mock()
        mock_pin.pin_type = "power_in"
        groups = _group_pins_by_type([mock_pin])
        assert "power" in groups
        assert mock_pin in groups["power"]

    def test_group_input_pins(self):
        """Input pins grouped correctly."""
        mock_pin = Mock()
        mock_pin.pin_type = "input"
        groups = _group_pins_by_type([mock_pin])
        assert "input" in groups

    def test_group_output_pins(self):
        """Output and tri-state grouped as output."""
        mock_output = Mock()
        mock_output.pin_type = "output"
        mock_tristate = Mock()
        mock_tristate.pin_type = "tri_state"
        groups = _group_pins_by_type([mock_output, mock_tristate])
        assert len(groups.get("output", [])) == 2

    def test_empty_groups_removed(self):
        """Empty groups not in result."""
        mock_pin = Mock()
        mock_pin.pin_type = "input"
        groups = _group_pins_by_type([mock_pin])
        assert "output" not in groups
        assert "power" not in groups


class TestFormatPinList:
    """Tests for _format_pin_list() function."""

    def test_empty_list(self):
        """Empty list shows (none)."""
        result = _format_pin_list([])
        assert "(none)" in result

    def test_pin_with_name_and_number(self):
        """Pin with name and number shows both."""
        mock_pin = Mock()
        mock_pin.name = "VCC"
        mock_pin.number = "1"
        result = _format_pin_list([mock_pin])
        assert "VCC" in result
        assert "1" in result

    def test_pin_number_only(self):
        """Pin with number only shows just number."""
        mock_pin = Mock()
        mock_pin.name = "1"  # Same as number
        mock_pin.number = "1"
        result = _format_pin_list([mock_pin])
        assert "pin 1" in result

    def test_custom_indent(self):
        """Custom indent used."""
        mock_pin = Mock()
        mock_pin.name = "VCC"
        mock_pin.number = "1"
        result = _format_pin_list([mock_pin], indent="    ")
        assert result.startswith("    ")


class TestPinNotFoundError:
    """Tests for PinNotFoundError exception."""

    def test_basic_message(self):
        """Basic error message."""
        err = PinNotFoundError("VCC", "Device:LED", [])
        assert "VCC" in str(err)
        assert "Device:LED" in str(err)

    def test_with_suggestions(self):
        """Error includes suggestions."""
        err = PinNotFoundError("VCC", "Device:LED", [], suggestions=["A", "K"])
        assert "Did you mean" in str(err)
        assert "A" in str(err)
        assert "K" in str(err)

    def test_with_available_pins(self):
        """Error shows available pins grouped."""
        mock_pin = Mock()
        mock_pin.name = "Anode"
        mock_pin.number = "1"
        mock_pin.pin_type = "passive"
        err = PinNotFoundError("VCC", "Device:LED", [mock_pin])
        assert "Available pins" in str(err)


class TestSymbolNotFoundError:
    """Tests for SymbolNotFoundError exception."""

    def test_basic_message(self):
        """Basic error message."""
        err = SymbolNotFoundError("BadSymbol", "Device.kicad_sym")
        assert "BadSymbol" in str(err)
        assert "Device.kicad_sym" in str(err)

    def test_with_suggestions(self):
        """Error includes suggestions."""
        err = SymbolNotFoundError("LED_0603", "Device.kicad_sym",
                                  suggestions=["LED", "LED_Small"])
        assert "Did you mean" in str(err)

    def test_with_available_symbols(self):
        """Error shows available symbols."""
        err = SymbolNotFoundError("BadSymbol", "Device.kicad_sym",
                                  available_symbols=["LED", "R", "C"])
        assert "Available symbols" in str(err)

    def test_limits_shown_symbols(self):
        """Only shows first 10 symbols."""
        symbols = [f"Symbol{i}" for i in range(20)]
        err = SymbolNotFoundError("BadSymbol", "Device.kicad_sym",
                                  available_symbols=symbols)
        assert "10 more" in str(err)


class TestLibraryNotFoundError:
    """Tests for LibraryNotFoundError exception."""

    def test_basic_message(self):
        """Basic error message."""
        err = LibraryNotFoundError("BadLibrary", [Path("/path1"), Path("/path2")])
        assert "BadLibrary" in str(err)

    def test_shows_searched_paths(self):
        """Error shows searched paths."""
        err = LibraryNotFoundError("BadLibrary", [Path("/path1"), Path("/path2")])
        assert "Searched paths" in str(err)
        assert "/path1" in str(err)
        assert "/path2" in str(err)

    def test_shows_fix_suggestions(self):
        """Error shows fix suggestions."""
        err = LibraryNotFoundError("BadLibrary", [])
        assert "To fix:" in str(err)


class TestPinClass:
    """Tests for Pin dataclass."""

    def test_pin_creation(self):
        """Create pin with all fields."""
        pin = Pin(
            name="VCC",
            number="1",
            x=0.0,
            y=2.54,
            angle=90,
            length=2.54,
            pin_type="power_in"
        )
        assert pin.name == "VCC"
        assert pin.number == "1"
        assert pin.x == 0.0
        assert pin.y == 2.54
        assert pin.angle == 90
        assert pin.length == 2.54
        assert pin.pin_type == "power_in"

    def test_connection_point(self):
        """Get pin connection point."""
        pin = Pin(name="VCC", number="1", x=10.0, y=20.0, angle=0, length=2.54)
        assert pin.connection_point() == (10.0, 20.0)

    def test_default_pin_type(self):
        """Default pin type is passive."""
        pin = Pin(name="1", number="1", x=0, y=0, angle=0, length=2.54)
        assert pin.pin_type == "passive"


class TestLogging:
    """Tests for logging functions."""

    def test_enable_verbose(self):
        """Enable verbose logging."""
        enable_verbose("DEBUG")
        # Should not raise
        disable_verbose()

    def test_disable_verbose(self):
        """Disable verbose logging."""
        enable_verbose("INFO")
        disable_verbose()
        # Should not raise

    def test_enable_with_format(self):
        """Enable with custom format."""
        enable_verbose("INFO", format="%(message)s")
        disable_verbose()


class TestPinAliasesDict:
    """Tests for PIN_ALIASES constant."""

    def test_power_aliases_exist(self):
        """Power pin aliases defined."""
        assert "vcc" in PIN_ALIASES
        assert "gnd" in PIN_ALIASES
        assert "vdd" in PIN_ALIASES
        assert "vss" in PIN_ALIASES

    def test_spi_aliases_exist(self):
        """SPI pin aliases defined."""
        assert "sck" in PIN_ALIASES
        assert "mosi" in PIN_ALIASES
        assert "miso" in PIN_ALIASES
        assert "cs" in PIN_ALIASES

    def test_i2c_aliases_exist(self):
        """I2C pin aliases defined."""
        assert "sda" in PIN_ALIASES
        assert "scl" in PIN_ALIASES

    def test_reset_aliases_exist(self):
        """Reset pin aliases defined."""
        assert "rst" in PIN_ALIASES
        assert "reset" in PIN_ALIASES
