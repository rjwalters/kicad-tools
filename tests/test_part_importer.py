"""Tests for the parts importer module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.parts.importer import (
    DatasheetDownloadError,
    DatasheetParseError,
    DatasheetSearchError,
    FootprintMatchError,
    ImportOptions,
    ImportResult,
    ImportStage,
    LayoutStyle,
    PartImporter,
    SymbolGenerationError,
)


class TestImportResult:
    """Tests for ImportResult dataclass."""

    def test_successful_result(self):
        result = ImportResult(
            part_number="STM32F103C8T6",
            success=True,
            message="Imported successfully",
            symbol_name="STM32F103C8T6",
            footprint_match="Package_QFP:LQFP-48",
            footprint_confidence=0.95,
            pin_count=48,
        )
        assert result.success is True
        assert result.symbol_name == "STM32F103C8T6"
        assert result.error_stage is None

    def test_failed_result(self):
        result = ImportResult(
            part_number="UnknownPart",
            success=False,
            message="Datasheet not found",
            error_stage=ImportStage.SEARCH,
            error_details="No results from any source",
        )
        assert result.success is False
        assert result.error_stage == ImportStage.SEARCH
        assert "No results" in result.error_details

    def test_result_with_warnings(self):
        result = ImportResult(
            part_number="STM32F103C8T6",
            success=True,
            message="Imported with warnings",
            warnings=["Low footprint match confidence: 30%"],
        )
        assert result.success is True
        assert len(result.warnings) == 1

    def test_result_repr(self):
        result = ImportResult(
            part_number="TestPart",
            success=True,
            message="OK",
        )
        repr_str = repr(result)
        assert "TestPart" in repr_str
        assert "OK" in repr_str


class TestImportOptions:
    """Tests for ImportOptions dataclass."""

    def test_defaults(self):
        options = ImportOptions()
        assert options.package is None
        assert options.layout == LayoutStyle.FUNCTIONAL
        assert options.reference == "U"
        assert options.overwrite is False
        assert options.dry_run is False
        assert options.auto_download is True
        assert options.use_cache is True

    def test_custom_options(self):
        options = ImportOptions(
            package="LQFP48",
            layout=LayoutStyle.PHYSICAL,
            reference="IC",
            description="STM32 MCU",
            overwrite=True,
            dry_run=True,
        )
        assert options.package == "LQFP48"
        assert options.layout == LayoutStyle.PHYSICAL
        assert options.reference == "IC"
        assert options.overwrite is True
        assert options.dry_run is True


class TestLayoutStyle:
    """Tests for LayoutStyle enum."""

    def test_layout_styles(self):
        assert LayoutStyle.FUNCTIONAL.value == "functional"
        assert LayoutStyle.PHYSICAL.value == "physical"
        assert LayoutStyle.SIMPLE.value == "simple"


class TestImportStage:
    """Tests for ImportStage enum."""

    def test_all_stages(self):
        stages = [
            ImportStage.SEARCH,
            ImportStage.DOWNLOAD,
            ImportStage.PARSE,
            ImportStage.MATCH_FOOTPRINT,
            ImportStage.GENERATE_SYMBOL,
            ImportStage.SAVE,
        ]
        assert len(stages) == 6
        assert all(isinstance(s.value, str) for s in stages)


class TestPartImporter:
    """Tests for PartImporter class."""

    @pytest.fixture
    def importer(self, tmp_path):
        """Create a PartImporter with a temporary output directory."""
        symbol_lib = tmp_path / "test.kicad_sym"
        return PartImporter(symbol_library=symbol_lib)

    def test_initialization(self, tmp_path):
        """Test importer initialization."""
        symbol_lib = tmp_path / "test.kicad_sym"
        footprint_lib = tmp_path / "test.pretty"

        importer = PartImporter(
            symbol_library=symbol_lib,
            footprint_library=footprint_lib,
            default_layout=LayoutStyle.PHYSICAL,
            preferred_sources=["lcsc"],
        )

        assert importer.symbol_library == symbol_lib
        assert importer.footprint_library == footprint_lib
        assert importer.default_layout == LayoutStyle.PHYSICAL
        assert importer.preferred_sources == ["lcsc"]

    def test_context_manager(self, importer):
        """Test importer as context manager."""
        with importer as imp:
            assert imp is importer

    @patch("kicad_tools.datasheet.DatasheetManager")
    def test_lazy_datasheet_manager(self, mock_manager_class, importer):
        """Test that datasheet manager is lazily initialized."""
        mock_manager = MagicMock()
        mock_manager_class.return_value = mock_manager

        # Access the property
        manager = importer.datasheet_manager

        # Should have been initialized
        mock_manager_class.assert_called_once()
        assert manager is mock_manager

        # Second access should not re-initialize
        manager2 = importer.datasheet_manager
        assert mock_manager_class.call_count == 1
        assert manager2 is manager


class TestPartImporterImport:
    """Tests for PartImporter.import_part method."""

    @pytest.fixture
    def mock_datasheet_result(self):
        """Create a mock DatasheetResult object (before download)."""
        from kicad_tools.datasheet.models import DatasheetResult

        return DatasheetResult(
            part_number="STM32F103C8T6",
            manufacturer="STMicroelectronics",
            description="STM32 MCU",
            datasheet_url="https://example.com/datasheet.pdf",
            source="lcsc",
            confidence=0.95,
        )

    @pytest.fixture
    def mock_datasheet(self):
        """Create a mock Datasheet object (after download)."""
        from datetime import datetime

        from kicad_tools.datasheet.models import Datasheet

        return Datasheet(
            part_number="STM32F103C8T6",
            manufacturer="STMicroelectronics",
            local_path=Path("/tmp/test.pdf"),
            source_url="https://example.com/datasheet.pdf",
            source="lcsc",
            downloaded_at=datetime.now(),
            file_size=1024,
        )

    @pytest.fixture
    def mock_pin_table(self):
        """Create a mock PinTable object."""
        from kicad_tools.datasheet.pins import ExtractedPin, PinTable

        pins = [
            ExtractedPin(number="1", name="VBAT", type="power_in"),
            ExtractedPin(number="2", name="PC13", type="bidirectional"),
            ExtractedPin(number="3", name="PA0", type="bidirectional"),
        ]
        return PinTable(pins=pins, package="LQFP48")

    @patch.object(PartImporter, "_search_datasheet")
    @patch.object(PartImporter, "_download_datasheet")
    @patch.object(PartImporter, "_parse_datasheet")
    @patch.object(PartImporter, "_match_footprint")
    @patch.object(PartImporter, "_generate_symbol")
    @patch.object(PartImporter, "_save_symbol")
    def test_successful_import(
        self,
        mock_save,
        mock_generate,
        mock_match,
        mock_parse,
        mock_download,
        mock_search,
        mock_datasheet_result,
        mock_datasheet,
        mock_pin_table,
        tmp_path,
    ):
        """Test successful part import."""
        # Setup mocks
        mock_search.return_value = mock_datasheet_result
        mock_download.return_value = mock_datasheet
        mock_parse.return_value = mock_pin_table
        mock_match.return_value = ("Package_QFP:LQFP-48", 0.95)
        mock_generate.return_value = "STM32F103C8T6"

        # Create importer and import part
        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        result = importer.import_part("STM32F103C8T6")

        # Verify result
        assert result.success is True
        assert result.symbol_name == "STM32F103C8T6"
        assert result.footprint_match == "Package_QFP:LQFP-48"
        assert result.footprint_confidence == 0.95
        assert result.pin_count == 3

    @patch.object(PartImporter, "_search_datasheet")
    def test_search_failure(self, mock_search, tmp_path):
        """Test handling of search failure."""
        mock_search.side_effect = DatasheetSearchError("No datasheet found")

        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        result = importer.import_part("UnknownPart")

        assert result.success is False
        assert result.error_stage == ImportStage.SEARCH
        assert "No datasheet found" in result.message

    @patch.object(PartImporter, "_search_datasheet")
    @patch.object(PartImporter, "_download_datasheet")
    def test_download_failure(self, mock_download, mock_search, mock_datasheet_result, tmp_path):
        """Test handling of download failure."""
        mock_search.return_value = mock_datasheet_result
        mock_download.side_effect = DatasheetDownloadError("Download failed")

        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        result = importer.import_part("TestPart")

        assert result.success is False
        assert result.error_stage == ImportStage.DOWNLOAD

    @patch.object(PartImporter, "_search_datasheet")
    @patch.object(PartImporter, "_download_datasheet")
    @patch.object(PartImporter, "_parse_datasheet")
    def test_parse_failure(
        self,
        mock_parse,
        mock_download,
        mock_search,
        mock_datasheet_result,
        mock_datasheet,
        tmp_path,
    ):
        """Test handling of parse failure."""
        mock_search.return_value = mock_datasheet_result
        mock_download.return_value = mock_datasheet
        mock_parse.side_effect = DatasheetParseError("Parse failed")

        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        result = importer.import_part("TestPart")

        assert result.success is False
        assert result.error_stage == ImportStage.PARSE

    @patch.object(PartImporter, "_search_datasheet")
    @patch.object(PartImporter, "_download_datasheet")
    @patch.object(PartImporter, "_parse_datasheet")
    @patch.object(PartImporter, "_match_footprint")
    @patch.object(PartImporter, "_generate_symbol")
    @patch.object(PartImporter, "_save_symbol")
    def test_dry_run(
        self,
        mock_save,
        mock_generate,
        mock_match,
        mock_parse,
        mock_download,
        mock_search,
        mock_datasheet_result,
        mock_datasheet,
        mock_pin_table,
        tmp_path,
    ):
        """Test dry run mode doesn't save."""
        mock_search.return_value = mock_datasheet_result
        mock_download.return_value = mock_datasheet
        mock_parse.return_value = mock_pin_table
        mock_match.return_value = ("Package_QFP:LQFP-48", 0.95)
        mock_generate.return_value = "TestPart"

        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        options = ImportOptions(dry_run=True)
        result = importer.import_part("TestPart", options)

        assert result.success is True
        assert "Dry run" in result.message
        mock_save.assert_not_called()


class TestPartImporterBatch:
    """Tests for batch import functionality."""

    @patch.object(PartImporter, "import_part")
    def test_batch_import(self, mock_import, tmp_path):
        """Test batch import of multiple parts."""
        # Setup mock to return different results
        mock_import.side_effect = [
            ImportResult(part_number="Part1", success=True, message="OK", symbol_name="Part1"),
            ImportResult(
                part_number="Part2",
                success=False,
                message="Failed",
                error_stage=ImportStage.SEARCH,
            ),
            ImportResult(part_number="Part3", success=True, message="OK", symbol_name="Part3"),
        ]

        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        results = importer.import_parts(["Part1", "Part2", "Part3"])

        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert results[2].success is True

    @patch.object(PartImporter, "import_part")
    def test_batch_import_with_progress(self, mock_import, tmp_path):
        """Test batch import with progress callback."""
        mock_import.return_value = ImportResult(part_number="Test", success=True, message="OK")

        progress_calls = []

        def progress_callback(current, total, part_number):
            progress_calls.append((current, total, part_number))

        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")
        importer.import_parts(["Part1", "Part2"], progress_callback=progress_callback)

        assert len(progress_calls) == 2
        assert progress_calls[0] == (1, 2, "Part1")
        assert progress_calls[1] == (2, 2, "Part2")


class TestPartImporterFootprintMatching:
    """Tests for footprint matching functionality."""

    def test_infer_package_by_pin_count(self, tmp_path):
        """Test package inference from pin count."""
        importer = PartImporter(symbol_library=tmp_path / "test.kicad_sym")

        assert importer._infer_package(8) == "SOIC-8"
        assert importer._infer_package(16) == "SOIC-16"
        assert importer._infer_package(48) == "LQFP-48"
        assert importer._infer_package(100) == "LQFP-100"
        assert importer._infer_package(5) is None  # Unknown


class TestExceptions:
    """Tests for custom exception classes."""

    def test_datasheet_search_error(self):
        error = DatasheetSearchError("Not found")
        assert str(error) == "Not found"

    def test_datasheet_download_error(self):
        error = DatasheetDownloadError("Download failed")
        assert str(error) == "Download failed"

    def test_datasheet_parse_error(self):
        error = DatasheetParseError("Parse failed")
        assert str(error) == "Parse failed"

    def test_footprint_match_error(self):
        error = FootprintMatchError("No match")
        assert str(error) == "No match"

    def test_symbol_generation_error(self):
        error = SymbolGenerationError("Generation failed")
        assert str(error) == "Generation failed"
