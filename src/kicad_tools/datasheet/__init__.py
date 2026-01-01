"""
Datasheet search and download infrastructure.

Provides tools for searching, downloading, and caching component datasheets
from various sources (LCSC, Octopart, etc.).

Example::

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
"""

from .cache import DatasheetCache, get_default_cache_path
from .exceptions import (
    DatasheetCacheError,
    DatasheetDownloadError,
    DatasheetError,
    DatasheetSearchError,
)
from .manager import DatasheetManager
from .models import Datasheet, DatasheetResult, DatasheetSearchResult
from .sources import DatasheetSource, LCSCDatasheetSource, OctopartDatasheetSource

__all__ = [
    # Main interface
    "DatasheetManager",
    # Models
    "Datasheet",
    "DatasheetResult",
    "DatasheetSearchResult",
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
]
