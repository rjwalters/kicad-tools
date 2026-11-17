"""Tests for FootprintLibrary.load() functionality."""

from pathlib import Path

import pytest

from kicad_tools.pcb import Footprint, FootprintLibrary, PadInfo

# Path to test fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_PRETTY_DIR = FIXTURES_DIR / "Test_Library.pretty"


class TestFootprintFromFile:
    """Tests for Footprint.from_file() parsing."""

    def test_load_simple_footprint(self):
        """Test loading a simple 2-pad SMD footprint."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        assert fp.name == "C_0402_1005Metric"
        assert len(fp.pads) == 2

    def test_footprint_description(self):
        """Test that footprint description is parsed correctly."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        assert "Capacitor SMD 0402" in fp.description
        assert "IPC-7351" in fp.description

    def test_footprint_tags(self):
        """Test that footprint tags are parsed correctly."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        assert "capacitor" in fp.tags
        assert "smd" in fp.tags
        assert "0402" in fp.tags

    def test_pad_positions(self):
        """Test that pad positions are parsed correctly."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        pad1 = fp.get_pad("1")
        pad2 = fp.get_pad("2")

        assert pad1 is not None
        assert pad2 is not None
        assert pad1.x == pytest.approx(-0.48)
        assert pad1.y == pytest.approx(0.0)
        assert pad2.x == pytest.approx(0.48)
        assert pad2.y == pytest.approx(0.0)

    def test_pad_size(self):
        """Test that pad size is parsed correctly."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        pad1 = fp.get_pad("1")
        assert pad1 is not None
        assert pad1.width == pytest.approx(0.56)
        assert pad1.height == pytest.approx(0.62)

    def test_pad_shape(self):
        """Test that pad shape is parsed correctly."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        pad1 = fp.get_pad("1")
        assert pad1 is not None
        assert pad1.shape == "roundrect"

    def test_pad_layers(self):
        """Test that pad layers are parsed correctly."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        pad1 = fp.get_pad("1")
        assert pad1 is not None
        assert "F.Cu" in pad1.layers
        assert "F.Paste" in pad1.layers
        assert "F.Mask" in pad1.layers

    def test_multipad_footprint(self):
        """Test loading a footprint with more than 2 pads."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "SOT-23-5.kicad_mod")

        assert fp.name == "SOT-23-5"
        assert len(fp.pads) == 5

        # Check pin positions make sense for a 5-pin SOT-23
        pad1 = fp.get_pad("1")
        pad5 = fp.get_pad("5")
        assert pad1 is not None
        assert pad5 is not None
        # Pins 1 and 5 should be on opposite sides
        assert pad1.x < 0  # Left side
        assert pad5.x > 0  # Right side

    def test_get_pad_positions_dict(self):
        """Test get_pad_positions() returns correct dict format."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        positions = fp.get_pad_positions()

        assert "1" in positions
        assert "2" in positions
        assert positions["1"] == pytest.approx((-0.48, 0.0), abs=0.01)
        assert positions["2"] == pytest.approx((0.48, 0.0), abs=0.01)

    def test_layers_property(self):
        """Test layers property returns all layers used by pads."""
        fp = Footprint.from_file(TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod")

        layers = fp.layers

        assert "F.Cu" in layers
        assert "F.Paste" in layers
        assert "F.Mask" in layers

    def test_invalid_file_raises(self):
        """Test that loading a non-footprint file raises ValueError."""
        # Create a temp file that's not a valid footprint
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".kicad_mod", delete=False) as f:
            f.write("(kicad_sch (version 1))")  # Wrong type
            temp_path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="Not a footprint file"):
                Footprint.from_file(temp_path)
        finally:
            temp_path.unlink()


class TestFootprintLibraryLoad:
    """Tests for FootprintLibrary.load()."""

    def test_load_pretty_directory(self):
        """Test loading all footprints from a .pretty directory."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        assert len(lib) == 3

    def test_load_returns_footprint_library(self):
        """Test that load() returns a FootprintLibrary instance."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        assert isinstance(lib, FootprintLibrary)

    def test_path_property(self):
        """Test that path property returns the loaded path."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        assert lib.path == TEST_PRETTY_DIR

    def test_footprints_property(self):
        """Test that footprints property returns list of footprints."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        footprints = lib.footprints
        assert isinstance(footprints, list)
        assert len(footprints) == 3
        assert all(isinstance(fp, Footprint) for fp in footprints)

    def test_get_footprint_by_name(self):
        """Test getting a specific footprint by name."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        fp = lib.get_footprint("C_0402_1005Metric")
        assert fp is not None
        assert fp.name == "C_0402_1005Metric"

    def test_get_footprint_nonexistent(self):
        """Test that getting a nonexistent footprint returns None."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        fp = lib.get_footprint("NonExistent")
        assert fp is None

    def test_contains(self):
        """Test 'in' operator for checking footprint existence."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        assert "C_0402_1005Metric" in lib
        assert "R_0603_1608Metric" in lib
        assert "NonExistent" not in lib

    def test_iterate_footprints(self):
        """Test iterating over footprints in the library."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        footprints = list(lib)
        assert len(footprints) == 3
        names = {fp.name for fp in footprints}
        assert "C_0402_1005Metric" in names
        assert "R_0603_1608Metric" in names
        assert "SOT-23-5" in names

    def test_load_nonexistent_path_raises(self):
        """Test that loading a nonexistent path raises ValueError."""
        with pytest.raises(ValueError, match="does not exist"):
            FootprintLibrary.load("/nonexistent/path.pretty")

    def test_load_file_instead_of_dir_raises(self):
        """Test that loading a file instead of directory raises ValueError."""
        file_path = TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod"
        with pytest.raises(ValueError, match="Not a directory"):
            FootprintLibrary.load(file_path)

    def test_load_non_pretty_dir_raises(self):
        """Test that loading a non-.pretty directory raises ValueError."""
        with pytest.raises(ValueError, match="Not a .pretty directory"):
            FootprintLibrary.load(FIXTURES_DIR)


class TestPadInfo:
    """Tests for PadInfo dataclass."""

    def test_position_property(self):
        """Test that position property returns (x, y) tuple."""
        pad = PadInfo(name="1", x=1.5, y=-2.3)
        assert pad.position == (1.5, -2.3)

    def test_size_property(self):
        """Test that size property returns (width, height) tuple."""
        pad = PadInfo(name="1", x=0, y=0, width=0.5, height=0.8)
        assert pad.size == (0.5, 0.8)

    def test_default_values(self):
        """Test default values for optional PadInfo fields."""
        pad = PadInfo(name="1", x=0, y=0)
        assert pad.width == 0
        assert pad.height == 0
        assert pad.shape == "roundrect"
        assert pad.pad_type == "smd"
        assert pad.layers == ("F.Cu", "F.Mask", "F.Paste")


class TestFootprintLibraryCompatibility:
    """Tests for backward compatibility with existing FootprintLibrary usage."""

    def test_default_constructor(self):
        """Test that default constructor still works."""
        lib = FootprintLibrary()
        assert lib.library_paths is not None
        assert len(lib.library_paths) > 0

    def test_get_pads_with_builtin_data(self):
        """Test that get_pads still works for built-in footprints."""
        lib = FootprintLibrary()
        pads = lib.get_pads("Capacitor_SMD:C_0603_1608Metric")

        assert "1" in pads
        assert "2" in pads
        assert pads["1"][0] == pytest.approx(-0.775)
        assert pads["2"][0] == pytest.approx(0.775)

    def test_list_known_footprints(self):
        """Test that list_known_footprints still works."""
        lib = FootprintLibrary()
        known = lib.list_known_footprints()

        assert "Capacitor_SMD:C_0603_1608Metric" in known
        assert "Package_TO_SOT_SMD:SOT-23-5" in known

    def test_loaded_library_has_empty_builtin(self):
        """Test that loaded library doesn't mix with builtin data."""
        lib = FootprintLibrary.load(TEST_PRETTY_DIR)

        # The loaded library should only contain footprints from the directory
        assert len(lib) == 3
        # Standard footprints shouldn't be in the loaded library
        assert "Capacitor_SMD:C_0603_1608Metric" not in lib
