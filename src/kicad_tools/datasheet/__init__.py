"""
Datasheet module providing search, download, and PDF parsing functionality.

This module provides tools for:
1. Searching for datasheets from LCSC, Octopart, etc.
2. Downloading and caching datasheets locally
3. Parsing PDF datasheets, converting to markdown, extracting images/tables

Search and Download Example::

    from kicad_tools.datasheet import DatasheetManager

    manager = DatasheetManager()

    # Search by part number
    results = manager.search("STM32F103C8T6")
    for result in results:
        print(f"{result.part_number}: {result.manufacturer}")
        print(f"  URL: {result.datasheet_url}")
        print(f"  Source: {result.source}")

    # Download to default cache location
    datasheet = manager.download(results[0])
    print(f"Downloaded to: {datasheet.local_path}")

    # Download to specific directory
    datasheet = manager.download(results[0], output_dir="datasheets/")

    # Download by part number directly
    datasheet = manager.download_by_part("STM32F103C8T6")

    # List cached datasheets
    for ds in manager.list_cached():
        print(f"{ds.part_number}: {ds.local_path}")

    # Check if datasheet is cached
    if manager.is_cached("STM32F103C8T6"):
        datasheet = manager.get_cached("STM32F103C8T6")

    # Clear cache
    manager.clear_cache()
    manager.clear_cache(older_than_days=30)

PDF Parsing Example::

    from kicad_tools.datasheet import DatasheetParser

    parser = DatasheetParser("STM32F103.pdf")

    # Convert to markdown
    markdown = parser.to_markdown()

    # Extract images
    images = parser.extract_images()
    for img in images:
        img.save(f"output/{img.suggested_filename}")

    # Extract tables
    tables = parser.extract_tables()
    for table in tables:
        print(table.to_markdown())

Requires optional dependencies:
    pip install kicad-tools[parts]     # For search/download
    pip install kicad-tools[datasheet]  # For PDF parsing
"""

# Search and download functionality
from .cache import DatasheetCache, get_default_cache_path
from .exceptions import (
    DatasheetCacheError,
    DatasheetDownloadError,
    DatasheetError,
    DatasheetSearchError,
)

# PDF parsing functionality
from .footprint_matcher import FootprintMatch, FootprintMatcher, GeneratorSuggestion
from .images import ExtractedImage, classify_image
from .manager import DatasheetManager
from .models import Datasheet, DatasheetResult, DatasheetSearchResult
from .package import PackageInfo, parse_package_name
from .parser import DatasheetParser, ParsedDatasheet
from .pin_inference import infer_pin_type

# Symbol generation
from .pin_layout import LayoutStyle, PinLayoutEngine, PinPosition, SymbolLayout
from .pins import ExtractedPin, PinTable
from .sources import DatasheetSource, LCSCDatasheetSource, OctopartDatasheetSource
from .symbol_generator import (
    GeneratedPin,
    GeneratedSymbol,
    SymbolGenerator,
    create_symbol_from_datasheet,
)
from .tables import ExtractedTable

__all__ = [
    # Main interfaces
    "DatasheetManager",
    "DatasheetParser",
    # Search/download models
    "Datasheet",
    "DatasheetResult",
    "DatasheetSearchResult",
    # PDF parsing models
    "ParsedDatasheet",
    "ExtractedImage",
    "ExtractedTable",
    "classify_image",
    # Pin extraction
    "ExtractedPin",
    "PinTable",
    "infer_pin_type",
    # Package extraction
    "PackageInfo",
    "parse_package_name",
    # Footprint matching
    "FootprintMatcher",
    "FootprintMatch",
    "GeneratorSuggestion",
    # Cache
    "DatasheetCache",
    "get_default_cache_path",
    # Sources
    "DatasheetSource",
    "LCSCDatasheetSource",
    "OctopartDatasheetSource",
    # Exceptions
    "DatasheetError",
    "DatasheetDownloadError",
    "DatasheetSearchError",
    "DatasheetCacheError",
    # Symbol generation
    "SymbolGenerator",
    "GeneratedSymbol",
    "GeneratedPin",
    "PinLayoutEngine",
    "LayoutStyle",
    "PinPosition",
    "SymbolLayout",
    "create_symbol_from_datasheet",
]
