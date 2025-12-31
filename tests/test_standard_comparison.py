"""Tests for footprint standard library comparison functionality."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from kicad_tools.footprints.library_path import (
    LibraryPaths,
    detect_kicad_library_path,
    guess_standard_library,
    list_available_libraries,
    parse_library_id,
)
from kicad_tools.footprints.standard_comparison import (
    ComparisonSeverity,
    ComparisonType,
    FootprintComparison,
    PadComparison,
    StandardFootprint,
    StandardFootprintComparator,
    StandardPad,
)


class TestLibraryPath:
    """Tests for library path detection."""

    def test_detect_with_override(self, tmp_path):
        """Test detection with explicit override."""
        # Create a fake footprints directory
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        paths = detect_kicad_library_path(config_override=str(footprints_dir))

        assert paths.found
        assert paths.source == "config"
        assert paths.footprints_path == footprints_dir

    def test_detect_with_env_var(self, tmp_path, monkeypatch):
        """Test detection with environment variable."""
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(footprints_dir))

        paths = detect_kicad_library_path()

        assert paths.found
        assert paths.source == "env"
        assert paths.footprints_path == footprints_dir

    def test_detect_not_found(self, monkeypatch):
        """Test when no library is found."""
        # Clear the env var
        monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)

        # Patch platform to return an unsupported system
        with patch("kicad_tools.footprints.library_path.platform.system", return_value="Unknown"):
            paths = detect_kicad_library_path()

        assert not paths.found
        assert paths.source == "auto"
        assert paths.footprints_path is None

    def test_get_library_path(self, tmp_path):
        """Test getting a library path."""
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        # Create a .pretty directory
        cap_lib = footprints_dir / "Capacitor_SMD.pretty"
        cap_lib.mkdir()

        paths = LibraryPaths(footprints_path=footprints_dir, source="test")

        # With .pretty extension
        assert paths.get_library_path("Capacitor_SMD.pretty") == cap_lib
        # Without .pretty extension
        assert paths.get_library_path("Capacitor_SMD") == cap_lib
        # Non-existent library
        assert paths.get_library_path("NonExistent") is None

    def test_get_footprint_file(self, tmp_path):
        """Test getting a footprint file path."""
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        # Create a library with a footprint
        cap_lib = footprints_dir / "Capacitor_SMD.pretty"
        cap_lib.mkdir()
        fp_file = cap_lib / "C_0402_1005Metric.kicad_mod"
        fp_file.write_text("(footprint C_0402_1005Metric)")

        paths = LibraryPaths(footprints_path=footprints_dir, source="test")

        # With .kicad_mod extension
        assert paths.get_footprint_file("Capacitor_SMD", "C_0402_1005Metric.kicad_mod") == fp_file
        # Without .kicad_mod extension
        assert paths.get_footprint_file("Capacitor_SMD", "C_0402_1005Metric") == fp_file
        # Non-existent footprint
        assert paths.get_footprint_file("Capacitor_SMD", "NonExistent") is None


class TestGuessStandardLibrary:
    """Tests for guessing standard library from footprint name."""

    def test_capacitor_prefix(self):
        """Test guessing capacitor library."""
        assert guess_standard_library("C_0402_1005Metric") == "Capacitor_SMD"
        assert guess_standard_library("CP_Elec_5x5.8") == "Capacitor_SMD"

    def test_resistor_prefix(self):
        """Test guessing resistor library."""
        assert guess_standard_library("R_0603_1608Metric") == "Resistor_SMD"

    def test_inductor_prefix(self):
        """Test guessing inductor library."""
        assert guess_standard_library("L_0805_2012Metric") == "Inductor_SMD"

    def test_led_prefix(self):
        """Test guessing LED library."""
        assert guess_standard_library("LED_0603_1608Metric") == "LED_SMD"

    def test_package_prefixes(self):
        """Test guessing package libraries."""
        assert guess_standard_library("SOIC-8_3.9x4.9mm") == "Package_SO"
        assert guess_standard_library("QFN-16_3x3mm") == "Package_DFN_QFN"
        assert guess_standard_library("SOT-23") == "Package_TO_SOT_SMD"

    def test_unknown_prefix(self):
        """Test unknown prefix returns None."""
        assert guess_standard_library("CustomFootprint") is None
        assert guess_standard_library("MyPart_123") is None


class TestParseLibraryId:
    """Tests for parsing library IDs."""

    def test_with_library_name(self):
        """Test parsing ID with library name."""
        lib, fp = parse_library_id("Capacitor_SMD:C_0402_1005Metric")
        assert lib == "Capacitor_SMD"
        assert fp == "C_0402_1005Metric"

    def test_without_library_name(self):
        """Test parsing ID without library name."""
        lib, fp = parse_library_id("C_0402_1005Metric")
        assert lib is None
        assert fp == "C_0402_1005Metric"

    def test_multiple_colons(self):
        """Test parsing ID with multiple colons."""
        lib, fp = parse_library_id("MyLib:Complex:Name")
        assert lib == "MyLib"
        assert fp == "Complex:Name"


class TestListAvailableLibraries:
    """Tests for listing available libraries."""

    def test_list_libraries(self, tmp_path):
        """Test listing all available libraries."""
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        # Create some .pretty directories
        (footprints_dir / "Capacitor_SMD.pretty").mkdir()
        (footprints_dir / "Resistor_SMD.pretty").mkdir()
        (footprints_dir / "LED_SMD.pretty").mkdir()

        paths = LibraryPaths(footprints_path=footprints_dir, source="test")
        libraries = list_available_libraries(paths)

        assert len(libraries) == 3
        assert "Capacitor_SMD" in libraries
        assert "Resistor_SMD" in libraries
        assert "LED_SMD" in libraries

    def test_list_libraries_empty(self, tmp_path):
        """Test listing libraries when none exist."""
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        paths = LibraryPaths(footprints_path=footprints_dir, source="test")
        libraries = list_available_libraries(paths)

        assert libraries == []

    def test_list_libraries_no_path(self):
        """Test listing libraries when path is None."""
        paths = LibraryPaths(footprints_path=None, source="test")
        libraries = list_available_libraries(paths)

        assert libraries == []


class TestStandardPad:
    """Tests for StandardPad dataclass."""

    def test_standard_pad_creation(self):
        """Test creating a StandardPad."""
        pad = StandardPad(
            number="1",
            type="smd",
            shape="roundrect",
            position=(0.5, 0.0),
            size=(0.5, 0.6),
            rotation=0.0,
        )

        assert pad.number == "1"
        assert pad.type == "smd"
        assert pad.shape == "roundrect"
        assert pad.position == (0.5, 0.0)
        assert pad.size == (0.5, 0.6)
        assert pad.rotation == 0.0


class TestStandardFootprint:
    """Tests for StandardFootprint dataclass."""

    def test_get_pad_found(self):
        """Test finding a pad by number."""
        pad1 = StandardPad("1", "smd", "rect", (0, 0), (0.5, 0.5))
        pad2 = StandardPad("2", "smd", "rect", (1, 0), (0.5, 0.5))

        fp = StandardFootprint(
            name="TestFP",
            library="TestLib",
            path=Path("/test/TestFP.kicad_mod"),
            pads=[pad1, pad2],
        )

        assert fp.get_pad("1") == pad1
        assert fp.get_pad("2") == pad2

    def test_get_pad_not_found(self):
        """Test finding a non-existent pad."""
        fp = StandardFootprint(
            name="TestFP",
            library="TestLib",
            path=Path("/test/TestFP.kicad_mod"),
            pads=[],
        )

        assert fp.get_pad("1") is None


class TestPadComparison:
    """Tests for PadComparison dataclass."""

    def test_pad_comparison_creation(self):
        """Test creating a PadComparison."""
        comp = PadComparison(
            pad_number="1",
            comparison_type=ComparisonType.PAD_SIZE_MISMATCH,
            severity=ComparisonSeverity.WARNING,
            message="Size mismatch",
            our_value=(0.5, 0.6),
            standard_value=(0.5, 0.5),
            delta=(0.0, 0.1),
            delta_percent=10.0,
        )

        assert comp.pad_number == "1"
        assert comp.comparison_type == ComparisonType.PAD_SIZE_MISMATCH
        assert comp.severity == ComparisonSeverity.WARNING


class TestFootprintComparison:
    """Tests for FootprintComparison dataclass."""

    def test_has_issues_true(self):
        """Test has_issues when there are issues."""
        comp = FootprintComparison(
            footprint_ref="C1",
            footprint_name="C_0402",
            standard_library="Capacitor_SMD",
            standard_footprint="C_0402_1005Metric",
            found_standard=True,
            pad_comparisons=[
                PadComparison(
                    pad_number="1",
                    comparison_type=ComparisonType.PAD_SIZE_MISMATCH,
                    severity=ComparisonSeverity.WARNING,
                    message="Test",
                )
            ],
        )

        assert comp.has_issues

    def test_has_issues_false(self):
        """Test has_issues when there are no issues."""
        comp = FootprintComparison(
            footprint_ref="C1",
            footprint_name="C_0402",
            standard_library="Capacitor_SMD",
            standard_footprint="C_0402_1005Metric",
            found_standard=True,
            pad_comparisons=[],
        )

        assert not comp.has_issues

    def test_error_count(self):
        """Test counting errors."""
        comp = FootprintComparison(
            footprint_ref="C1",
            footprint_name="C_0402",
            standard_library="Capacitor_SMD",
            standard_footprint="C_0402_1005Metric",
            found_standard=True,
            pad_comparisons=[
                PadComparison(
                    pad_number="1",
                    comparison_type=ComparisonType.PAD_SIZE_MISMATCH,
                    severity=ComparisonSeverity.ERROR,
                    message="Test",
                ),
                PadComparison(
                    pad_number="2",
                    comparison_type=ComparisonType.PAD_POSITION_MISMATCH,
                    severity=ComparisonSeverity.WARNING,
                    message="Test",
                ),
            ],
        )

        assert comp.error_count == 1
        assert comp.warning_count == 1

    def test_matches_standard(self):
        """Test matches_standard property."""
        # Matches standard
        comp1 = FootprintComparison(
            footprint_ref="C1",
            footprint_name="C_0402",
            standard_library="Capacitor_SMD",
            standard_footprint="C_0402_1005Metric",
            found_standard=True,
            pad_comparisons=[],
        )
        assert comp1.matches_standard

        # Has warnings (doesn't match)
        comp2 = FootprintComparison(
            footprint_ref="C1",
            footprint_name="C_0402",
            standard_library="Capacitor_SMD",
            standard_footprint="C_0402_1005Metric",
            found_standard=True,
            pad_comparisons=[
                PadComparison(
                    pad_number="1",
                    comparison_type=ComparisonType.PAD_SIZE_MISMATCH,
                    severity=ComparisonSeverity.WARNING,
                    message="Test",
                )
            ],
        )
        assert not comp2.matches_standard

        # Standard not found (doesn't match)
        comp3 = FootprintComparison(
            footprint_ref="C1",
            footprint_name="C_0402",
            standard_library=None,
            standard_footprint=None,
            found_standard=False,
            pad_comparisons=[],
        )
        assert not comp3.matches_standard


class TestStandardFootprintComparator:
    """Tests for StandardFootprintComparator."""

    def test_comparator_initialization(self, tmp_path):
        """Test initializing comparator with custom path."""
        footprints_dir = tmp_path / "footprints"
        footprints_dir.mkdir()

        comparator = StandardFootprintComparator(
            tolerance_mm=0.1,
            library_path=str(footprints_dir),
        )

        assert comparator.tolerance_mm == 0.1
        assert comparator.library_found
        assert comparator.library_path == footprints_dir

    def test_compare_position_within_tolerance(self):
        """Test position comparison within tolerance."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        pcb_pad = MagicMock()
        pcb_pad.number = "1"
        pcb_pad.position = (0.52, 0.01)  # 0.02mm off

        std_pad = StandardPad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
        )

        result = comparator._compare_position(pcb_pad, std_pad)
        assert result is None  # Within tolerance

    def test_compare_position_outside_tolerance(self):
        """Test position comparison outside tolerance."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        pcb_pad = MagicMock()
        pcb_pad.number = "1"
        pcb_pad.position = (0.6, 0.0)  # 0.1mm off

        std_pad = StandardPad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
        )

        result = comparator._compare_position(pcb_pad, std_pad)
        assert result is not None
        assert result.comparison_type == ComparisonType.PAD_POSITION_MISMATCH

    def test_compare_size_within_tolerance(self):
        """Test size comparison within tolerance."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        pcb_pad = MagicMock()
        pcb_pad.number = "1"
        pcb_pad.size = (0.52, 0.53)  # Close to standard

        std_pad = StandardPad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
        )

        result = comparator._compare_size(pcb_pad, std_pad)
        assert result is None  # Within tolerance

    def test_compare_size_outside_tolerance(self):
        """Test size comparison outside tolerance."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        pcb_pad = MagicMock()
        pcb_pad.number = "1"
        pcb_pad.size = (0.7, 0.7)  # Significantly different

        std_pad = StandardPad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
        )

        result = comparator._compare_size(pcb_pad, std_pad)
        assert result is not None
        assert result.comparison_type == ComparisonType.PAD_SIZE_MISMATCH

    def test_compare_shape_same(self):
        """Test shape comparison when same."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        pcb_pad = MagicMock()
        pcb_pad.number = "1"
        pcb_pad.shape = "rect"

        std_pad = StandardPad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
        )

        result = comparator._compare_shape(pcb_pad, std_pad)
        assert result is None

    def test_compare_shape_different(self):
        """Test shape comparison when different."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        pcb_pad = MagicMock()
        pcb_pad.number = "1"
        pcb_pad.shape = "circle"

        std_pad = StandardPad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
        )

        result = comparator._compare_shape(pcb_pad, std_pad)
        assert result is not None
        assert result.comparison_type == ComparisonType.PAD_SHAPE_MISMATCH

    def test_summarize(self):
        """Test summarizing comparison results."""
        comparator = StandardFootprintComparator(tolerance_mm=0.05)

        comparisons = [
            FootprintComparison(
                footprint_ref="C1",
                footprint_name="C_0402",
                standard_library="Capacitor_SMD",
                standard_footprint="C_0402_1005Metric",
                found_standard=True,
                pad_comparisons=[],
            ),
            FootprintComparison(
                footprint_ref="C2",
                footprint_name="C_0402",
                standard_library="Capacitor_SMD",
                standard_footprint="C_0402_1005Metric",
                found_standard=True,
                pad_comparisons=[
                    PadComparison(
                        pad_number="1",
                        comparison_type=ComparisonType.PAD_SIZE_MISMATCH,
                        severity=ComparisonSeverity.ERROR,
                        message="Test",
                    )
                ],
            ),
            FootprintComparison(
                footprint_ref="U1",
                footprint_name="CustomPart",
                standard_library=None,
                standard_footprint=None,
                found_standard=False,
                pad_comparisons=[],
            ),
        ]

        summary = comparator.summarize(comparisons)

        assert summary["total_checked"] == 3
        assert summary["found_standard"] == 2
        assert summary["not_found"] == 1
        assert summary["matching_standard"] == 1
        assert summary["with_issues"] == 1
        assert summary["total_errors"] == 1
        assert summary["total_warnings"] == 0
