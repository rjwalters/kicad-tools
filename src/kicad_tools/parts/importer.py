"""
Part importer - unified workflow for importing parts from datasheets to KiCad libraries.

This module orchestrates the complete workflow:
1. Search for datasheet by part number
2. Download datasheet PDF
3. Parse datasheet and extract pin information
4. Match footprint from standard libraries
5. Generate KiCad symbol
6. Add to symbol library

Example::

    from kicad_tools.parts import PartImporter

    importer = PartImporter(
        symbol_library="myproject.kicad_sym",
        footprint_library="MyProject.pretty",
    )

    # Import single part
    result = importer.import_part("STM32F103C8T6")
    print(f"Symbol: {result.symbol_name}")
    print(f"Footprint: {result.footprint_match}")

    # Batch import
    results = importer.import_parts(["STM32F103C8T6", "ATmega328P"])
    for r in results:
        print(f"{r.part_number}: {'✓' if r.success else '✗'} {r.message}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kicad_tools.utils import ensure_parent_dir

if TYPE_CHECKING:
    from ..datasheet import Datasheet, PinTable
    from ..datasheet.models import DatasheetResult


logger = logging.getLogger(__name__)


class ImportStage(Enum):
    """Stages of the import process."""

    SEARCH = "search"
    DOWNLOAD = "download"
    PARSE = "parse"
    MATCH_FOOTPRINT = "match_footprint"
    GENERATE_SYMBOL = "generate_symbol"
    SAVE = "save"


class LayoutStyle(Enum):
    """Pin layout style for generated symbols."""

    FUNCTIONAL = "functional"  # Group by function (power, GPIO, comms)
    PHYSICAL = "physical"  # Match IC package physical layout
    SIMPLE = "simple"  # Power top/bottom, signals left/right


@dataclass
class ImportResult:
    """Result of a part import operation."""

    part_number: str
    success: bool
    message: str

    # On success
    symbol_name: str | None = None
    footprint_match: str | None = None
    footprint_confidence: float = 0.0
    datasheet_path: Path | None = None
    pin_count: int = 0

    # On failure
    error_stage: ImportStage | None = None
    error_details: str | None = None

    # Warnings (partial success)
    warnings: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        status = "✓" if self.success else "✗"
        return f"ImportResult({status} {self.part_number}: {self.message})"


@dataclass
class ImportOptions:
    """Options for part import."""

    # Package selection
    package: str | None = None  # Specific package variant (e.g., "LQFP48")

    # Layout options
    layout: LayoutStyle = LayoutStyle.FUNCTIONAL

    # Symbol options
    reference: str = "U"  # Reference designator
    description: str = ""  # Symbol description
    keywords: str = ""  # Search keywords

    # Behavior
    overwrite: bool = False  # Overwrite existing symbols
    dry_run: bool = False  # Don't actually save anything
    auto_download: bool = True  # Automatically download datasheets
    use_cache: bool = True  # Use cached datasheets

    # Type overrides for pins
    pin_type_overrides: dict[str, str] = field(default_factory=dict)


class PartImporter:
    """
    Unified part importer that orchestrates the complete workflow.

    Example::

        importer = PartImporter(
            symbol_library="myproject.kicad_sym",
            footprint_library="MyProject.pretty",
        )

        result = importer.import_part("STM32F103C8T6")
        if result.success:
            print(f"Imported {result.symbol_name}")
        else:
            print(f"Failed at {result.error_stage}: {result.error_details}")
    """

    def __init__(
        self,
        symbol_library: str | Path,
        footprint_library: str | Path | None = None,
        datasheet_cache_dir: Path | None = None,
        default_layout: LayoutStyle = LayoutStyle.FUNCTIONAL,
        preferred_sources: list[str] | None = None,
        footprint_search_paths: list[Path] | None = None,
        octopart_api_key: str | None = None,
    ):
        """
        Initialize the PartImporter.

        Args:
            symbol_library: Path to the output symbol library file (.kicad_sym)
            footprint_library: Path to project footprint library (.pretty directory)
            datasheet_cache_dir: Directory for cached datasheets
            default_layout: Default pin layout style
            preferred_sources: Preferred datasheet sources (e.g., ["lcsc", "octopart"])
            footprint_search_paths: Additional paths to search for footprints
            octopart_api_key: API key for Octopart
        """
        self.symbol_library = Path(symbol_library)
        self.footprint_library = Path(footprint_library) if footprint_library else None
        self.default_layout = default_layout
        self.preferred_sources = preferred_sources or ["lcsc", "octopart"]
        self.footprint_search_paths = footprint_search_paths or []
        self._octopart_api_key = octopart_api_key

        # Initialize managers lazily
        self._datasheet_manager = None
        self._datasheet_cache_dir = datasheet_cache_dir

    @property
    def datasheet_manager(self):
        """Lazily initialize the datasheet manager."""
        if self._datasheet_manager is None:
            from ..datasheet import DatasheetManager

            self._datasheet_manager = DatasheetManager(
                cache_dir=self._datasheet_cache_dir,
                octopart_api_key=self._octopart_api_key,
            )
        return self._datasheet_manager

    def import_part(
        self,
        part_number: str,
        options: ImportOptions | None = None,
        progress_callback: Callable[[ImportStage, str], None] | None = None,
    ) -> ImportResult:
        """
        Import a single part.

        Executes the complete workflow:
        1. Search for datasheet
        2. Download datasheet
        3. Parse and extract pins
        4. Match footprint
        5. Generate symbol
        6. Save to library

        Args:
            part_number: Part number to import (e.g., "STM32F103C8T6")
            options: Import options (uses defaults if not provided)
            progress_callback: Optional callback for progress updates

        Returns:
            ImportResult with success/failure status and details
        """
        options = options or ImportOptions()
        result = ImportResult(part_number=part_number, success=False, message="")

        def _progress(stage: ImportStage, msg: str):
            if progress_callback:
                progress_callback(stage, msg)
            logger.debug(f"[{stage.value}] {msg}")

        try:
            # Stage 1: Search for datasheet
            _progress(ImportStage.SEARCH, f"Searching for datasheet: {part_number}")
            datasheet_or_result = self._search_datasheet(part_number, options)

            # Stage 2: Download datasheet
            from ..datasheet.models import DatasheetResult

            if isinstance(datasheet_or_result, DatasheetResult):
                url = datasheet_or_result.datasheet_url
            else:
                url = datasheet_or_result.source_url
            _progress(ImportStage.DOWNLOAD, f"Downloading: {url}")
            datasheet = self._download_datasheet(datasheet_or_result, options)
            result.datasheet_path = datasheet.local_path

            # Stage 3: Parse datasheet
            _progress(ImportStage.PARSE, "Extracting pin information")
            pin_table = self._parse_datasheet(datasheet, options)
            result.pin_count = len(pin_table.pins)

            if not pin_table.pins:
                result.warnings.append("No pins extracted from datasheet")

            # Stage 4: Match footprint
            _progress(ImportStage.MATCH_FOOTPRINT, "Matching footprint")
            footprint_match, footprint_confidence = self._match_footprint(
                part_number, pin_table, options
            )
            result.footprint_match = footprint_match
            result.footprint_confidence = footprint_confidence

            if footprint_confidence < 0.5:
                result.warnings.append(
                    f"Low footprint match confidence: {footprint_confidence:.0%}"
                )

            # Stage 5: Generate symbol
            _progress(ImportStage.GENERATE_SYMBOL, "Generating symbol")
            symbol_name = self._generate_symbol(
                part_number, pin_table, footprint_match, datasheet, options
            )
            result.symbol_name = symbol_name

            # Stage 6: Save
            if not options.dry_run:
                _progress(ImportStage.SAVE, f"Saving to {self.symbol_library}")
                self._save_symbol(symbol_name, options)
                result.message = f"Imported {symbol_name} to {self.symbol_library}"
            else:
                result.message = f"Dry run: would import {symbol_name}"

            result.success = True

        except DatasheetSearchError as e:
            result.error_stage = ImportStage.SEARCH
            result.error_details = str(e)
            result.message = f"Datasheet not found: {e}"
            logger.warning(f"Search failed for {part_number}: {e}")

        except DatasheetDownloadError as e:
            result.error_stage = ImportStage.DOWNLOAD
            result.error_details = str(e)
            result.message = f"Download failed: {e}"
            logger.warning(f"Download failed for {part_number}: {e}")

        except DatasheetParseError as e:
            result.error_stage = ImportStage.PARSE
            result.error_details = str(e)
            result.message = f"Parse failed: {e}"
            logger.warning(f"Parse failed for {part_number}: {e}")

        except FootprintMatchError as e:
            result.error_stage = ImportStage.MATCH_FOOTPRINT
            result.error_details = str(e)
            result.message = f"Footprint match failed: {e}"
            logger.warning(f"Footprint match failed for {part_number}: {e}")

        except SymbolGenerationError as e:
            result.error_stage = ImportStage.GENERATE_SYMBOL
            result.error_details = str(e)
            result.message = f"Symbol generation failed: {e}"
            logger.warning(f"Symbol generation failed for {part_number}: {e}")

        except Exception as e:
            result.error_stage = ImportStage.SAVE
            result.error_details = str(e)
            result.message = f"Unexpected error: {e}"
            logger.exception(f"Unexpected error importing {part_number}")

        return result

    def import_parts(
        self,
        part_numbers: list[str],
        options: ImportOptions | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[ImportResult]:
        """
        Import multiple parts.

        Args:
            part_numbers: List of part numbers to import
            options: Import options (uses defaults if not provided)
            progress_callback: Optional callback(current, total, part_number)

        Returns:
            List of ImportResult for each part
        """
        results = []
        total = len(part_numbers)

        for i, part_number in enumerate(part_numbers):
            if progress_callback:
                progress_callback(i + 1, total, part_number)

            result = self.import_part(part_number, options)
            results.append(result)

        return results

    def _search_datasheet(
        self, part_number: str, options: ImportOptions
    ) -> Datasheet | DatasheetResult:
        """Search for a datasheet by part number.

        Returns:
            A Datasheet if cached, or a DatasheetResult if not yet downloaded.
        """

        # Check cache first
        if options.use_cache and self.datasheet_manager.is_cached(part_number):
            cached = self.datasheet_manager.get_cached(part_number)
            if cached:
                logger.info(f"Using cached datasheet for {part_number}")
                return cached

        # Search for the datasheet
        search_result = self.datasheet_manager.search(part_number)

        if not search_result.has_results:
            raise DatasheetSearchError(f"No datasheet found for '{part_number}'")

        # Return the best match (highest confidence)
        best = search_result.results[0]
        logger.info(f"Found datasheet: {best.datasheet_url} ({best.confidence:.0%})")

        return best

    def _download_datasheet(
        self, datasheet_or_result: Datasheet | DatasheetResult, options: ImportOptions
    ) -> Datasheet:
        """Download a datasheet if not already downloaded.

        Args:
            datasheet_or_result: Either a cached Datasheet or a search result
            options: Import options

        Returns:
            A downloaded Datasheet object
        """
        from ..datasheet.models import Datasheet, DatasheetResult

        # If already downloaded, return it
        if isinstance(datasheet_or_result, Datasheet):
            if datasheet_or_result.local_path and datasheet_or_result.local_path.exists():
                return datasheet_or_result

        if not options.auto_download:
            raise DatasheetDownloadError("Auto-download disabled and no local path")

        # Convert to DatasheetResult if needed
        if isinstance(datasheet_or_result, DatasheetResult):
            result = datasheet_or_result
        else:
            # This is a Datasheet without a local path - create a result to download
            result = DatasheetResult(
                part_number=datasheet_or_result.part_number,
                manufacturer=datasheet_or_result.manufacturer or "",
                description="",
                datasheet_url=datasheet_or_result.source_url,
                source=datasheet_or_result.source or "unknown",
                confidence=1.0,
            )

        return self.datasheet_manager.download(result)

    def _parse_datasheet(self, datasheet: Datasheet, options: ImportOptions) -> PinTable:
        """Parse the datasheet and extract pin information."""
        from ..datasheet import DatasheetParser

        if not datasheet.local_path or not datasheet.local_path.exists():
            raise DatasheetParseError("No local datasheet file")

        parser = DatasheetParser(datasheet.local_path)

        # Extract pins
        pin_table = parser.extract_pins(
            package=options.package,
            type_overrides=options.pin_type_overrides,
        )

        logger.info(f"Extracted {len(pin_table.pins)} pins from datasheet")

        return pin_table

    def _match_footprint(
        self,
        part_number: str,
        pin_table: PinTable,
        options: ImportOptions,
    ) -> tuple[str | None, float]:
        """
        Match a footprint from standard libraries.

        Returns:
            Tuple of (footprint_name, confidence) or (None, 0.0) if no match
        """
        from ..footprints.library_path import (
            STANDARD_LIBRARY_MAPPINGS,
            detect_kicad_library_path,
            guess_standard_library,
        )

        # Determine package type from pin count and explicit package option
        package = options.package or pin_table.package
        pin_count = len(pin_table.pins)

        if not package:
            # Try to infer package from pin count
            package = self._infer_package(pin_count)

        if not package:
            logger.warning(f"Could not determine package for {part_number}")
            return None, 0.0

        # Try to find matching footprint in standard libraries
        library_paths = detect_kicad_library_path()

        if not library_paths.found:
            logger.warning("KiCad standard library not found")
            return None, 0.0

        # Guess the library based on package name
        library_name = guess_standard_library(package)
        if library_name:
            fp_path = library_paths.get_footprint_file(library_name, package)
            if fp_path:
                footprint_id = f"{library_name}:{package}"
                return footprint_id, 0.95

        # Try common package patterns
        for prefix, lib in STANDARD_LIBRARY_MAPPINGS.items():
            if package.upper().startswith(prefix.upper()):
                lib_name = lib.removesuffix(".pretty")
                fp_path = library_paths.get_footprint_file(lib_name, package)
                if fp_path:
                    footprint_id = f"{lib_name}:{package}"
                    return footprint_id, 0.85

        # No exact match, return the package name as a suggestion
        return package, 0.3

    def _infer_package(self, pin_count: int) -> str | None:
        """Infer package type from pin count."""
        # Common package mappings by pin count
        package_hints = {
            8: "SOIC-8",
            14: "SOIC-14",
            16: "SOIC-16",
            20: "TSSOP-20",
            24: "TSSOP-24",
            28: "TSSOP-28",
            32: "LQFP-32",
            44: "LQFP-44",
            48: "LQFP-48",
            64: "LQFP-64",
            100: "LQFP-100",
            144: "LQFP-144",
        }
        return package_hints.get(pin_count)

    def _generate_symbol(
        self,
        part_number: str,
        pin_table: PinTable,
        footprint: str | None,
        datasheet: Datasheet,
        options: ImportOptions,
    ) -> str:
        """Generate a KiCad symbol from the extracted pins."""
        from ..schematic.symbol_generator import (
            PinDef,
            PinType,
            SymbolDef,
            detect_pin_side,
            detect_pin_style,
            generate_symbol_sexp,
        )

        # Convert extracted pins to symbol pin definitions
        pins = []
        for pin in pin_table.pins:
            try:
                pin_type = PinType(pin.type)
            except ValueError:
                pin_type = PinType.PASSIVE

            pin_def = PinDef(
                number=pin.number,
                name=pin.name,
                pin_type=pin_type,
                style=detect_pin_style(pin.name, pin_type),
                side=detect_pin_side(pin.name, pin_type),
            )
            pins.append(pin_def)

        # Create symbol definition
        symbol = SymbolDef(
            name=part_number,
            pins=pins,
            reference=options.reference,
            footprint=footprint or "",
            datasheet=datasheet.source_url or "",
            description=options.description or f"{part_number} IC",
            keywords=options.keywords or part_number,
        )

        # Generate S-expression (but don't save yet)
        sexp = generate_symbol_sexp(symbol)

        # Store for later save
        self._pending_symbol = sexp

        return part_number

    def _save_symbol(self, symbol_name: str, options: ImportOptions) -> None:
        """Save the generated symbol to the library."""
        if not hasattr(self, "_pending_symbol"):
            raise SymbolGenerationError("No symbol to save")

        sexp = self._pending_symbol

        # Create library file if it doesn't exist
        ensure_parent_dir(self.symbol_library)

        if self.symbol_library.exists() and not options.overwrite:
            # TODO: Append to existing library instead of overwriting
            # For now, just write new library
            pass

        self.symbol_library.write_text(sexp)
        logger.info(f"Saved symbol to {self.symbol_library}")

        del self._pending_symbol

    def close(self) -> None:
        """Close all connections."""
        if self._datasheet_manager:
            self._datasheet_manager.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


# Custom exceptions for each stage


class DatasheetSearchError(Exception):
    """Error during datasheet search."""

    pass


class DatasheetDownloadError(Exception):
    """Error during datasheet download."""

    pass


class DatasheetParseError(Exception):
    """Error during datasheet parsing."""

    pass


class FootprintMatchError(Exception):
    """Error during footprint matching."""

    pass


class SymbolGenerationError(Exception):
    """Error during symbol generation."""

    pass
