"""
DatasheetManager - orchestrates datasheet search and download across multiple sources.

Provides a unified interface for searching, downloading, and caching datasheets
from various suppliers.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .cache import DatasheetCache
from .exceptions import DatasheetDownloadError, DatasheetSearchError
from .models import Datasheet, DatasheetResult, DatasheetSearchResult
from .sources import DatasheetSource, LCSCDatasheetSource, OctopartDatasheetSource

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class DatasheetManager:
    """
    Unified manager for datasheet search and download.

    Searches multiple sources (LCSC, Octopart, etc.) and manages a local cache
    of downloaded datasheets.

    Example::

        manager = DatasheetManager()

        # Search for datasheets
        results = manager.search("STM32F103C8T6")
        for result in results:
            print(f"{result.part_number}: {result.manufacturer}")
            print(f"  URL: {result.datasheet_url}")
            print(f"  Source: {result.source}")

        # Download a datasheet
        datasheet = manager.download(results[0])
        print(f"Downloaded to: {datasheet.local_path}")

        # Download by part number directly
        datasheet = manager.download_by_part("STM32F103C8T6")

        # List cached datasheets
        for ds in manager.list_cached():
            print(f"{ds.part_number}: {ds.local_path}")

        # Check if cached
        if manager.is_cached("STM32F103C8T6"):
            datasheet = manager.get_cached("STM32F103C8T6")
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        sources: list[DatasheetSource] | None = None,
        octopart_api_key: str | None = None,
        timeout: float = 30.0,
    ):
        """
        Initialize the DatasheetManager.

        Args:
            cache_dir: Directory for cached datasheets (default: ~/.cache/kicad-tools/datasheets)
            sources: List of DatasheetSource instances to use (default: LCSC + Octopart)
            octopart_api_key: API key for Octopart (optional, can be set via OCTOPART_API_KEY env)
            timeout: Request timeout in seconds
        """
        self.cache = DatasheetCache(cache_dir)
        self.timeout = timeout

        # Get Octopart API key from env if not provided
        if octopart_api_key is None:
            octopart_api_key = os.environ.get("OCTOPART_API_KEY")

        # Initialize default sources if not provided
        if sources is None:
            self.sources: list[DatasheetSource] = [
                LCSCDatasheetSource(timeout=timeout),
                OctopartDatasheetSource(api_key=octopart_api_key, timeout=timeout),
            ]
        else:
            self.sources = sources

    def search(self, part_number: str) -> DatasheetSearchResult:
        """
        Search all sources for datasheets matching the part number.

        Args:
            part_number: Part number or search query

        Returns:
            DatasheetSearchResult with results from all sources
        """
        all_results = []
        errors = {}

        for source in self.sources:
            try:
                results = source.search(part_number)
                all_results.extend(results)
                logger.debug(f"Source {source.name}: found {len(results)} results")
            except Exception as e:
                logger.warning(f"Source {source.name} failed: {e}")
                errors[source.name] = str(e)

        # Sort by confidence (highest first)
        all_results.sort(key=lambda r: r.confidence, reverse=True)

        # Deduplicate by URL (keep highest confidence)
        seen_urls = set()
        unique_results = []
        for result in all_results:
            if result.datasheet_url not in seen_urls:
                seen_urls.add(result.datasheet_url)
                unique_results.append(result)

        return DatasheetSearchResult(
            query=part_number,
            results=unique_results,
            errors=errors,
        )

    def download(
        self,
        result: DatasheetResult,
        output_dir: Path | None = None,
        force: bool = False,
    ) -> Datasheet:
        """
        Download a datasheet.

        Args:
            result: The DatasheetResult to download
            output_dir: Directory to save to (default: cache directory)
            force: Force download even if cached

        Returns:
            Datasheet object with local path

        Raises:
            DatasheetDownloadError: If download fails
        """
        # Check cache first
        if not force and self.is_cached(result.part_number):
            cached = self.get_cached(result.part_number)
            if cached:
                logger.info(f"Using cached datasheet for {result.part_number}")
                return cached

        # Determine output path
        if output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{result.part_number}.pdf"
        else:
            output_path = self.cache.get_datasheet_path(result.part_number)

        # Find the right source for downloading
        source = self._get_source(result.source)
        if source is None:
            # Use a generic download
            source = self.sources[0] if self.sources else LCSCDatasheetSource()

        try:
            downloaded_path = source.download(result, output_path)

            # Get file size
            file_size = downloaded_path.stat().st_size

            # Create Datasheet object
            datasheet = Datasheet(
                part_number=result.part_number,
                manufacturer=result.manufacturer,
                local_path=downloaded_path,
                source_url=result.datasheet_url,
                source=result.source,
                downloaded_at=datetime.now(),
                file_size=file_size,
            )

            # Cache if downloaded to cache directory
            if output_dir is None:
                self.cache.put(datasheet)

            return datasheet

        except Exception as e:
            raise DatasheetDownloadError(
                f"Failed to download datasheet for {result.part_number}: {e}"
            ) from e

    def download_by_part(
        self,
        part_number: str,
        output_dir: Path | None = None,
        force: bool = False,
    ) -> Datasheet:
        """
        Search for and download a datasheet by part number.

        Searches all sources and downloads the best match.

        Args:
            part_number: Part number to search for
            output_dir: Directory to save to (default: cache directory)
            force: Force download even if cached

        Returns:
            Datasheet object with local path

        Raises:
            DatasheetSearchError: If no datasheet found
            DatasheetDownloadError: If download fails
        """
        # Check cache first
        if not force and self.is_cached(part_number):
            cached = self.get_cached(part_number)
            if cached:
                logger.info(f"Using cached datasheet for {part_number}")
                return cached

        # Search for the datasheet
        search_result = self.search(part_number)

        if not search_result.has_results:
            raise DatasheetSearchError(f"No datasheet found for '{part_number}'")

        # Download the best match
        return self.download(search_result.results[0], output_dir=output_dir)

    def is_cached(self, part_number: str) -> bool:
        """
        Check if a datasheet is cached.

        Args:
            part_number: Part number to check

        Returns:
            True if cached and not expired
        """
        return self.cache.is_cached(part_number)

    def get_cached(self, part_number: str) -> Datasheet | None:
        """
        Get a cached datasheet.

        Args:
            part_number: Part number to look up

        Returns:
            Datasheet if cached, None otherwise
        """
        return self.cache.get(part_number)

    def list_cached(self) -> list[Datasheet]:
        """
        List all cached datasheets.

        Returns:
            List of cached Datasheet objects
        """
        return self.cache.list()

    def clear_cache(self, older_than_days: int | None = None) -> int:
        """
        Clear cached datasheets.

        Args:
            older_than_days: If provided, only clear entries older than this

        Returns:
            Number of entries removed
        """
        if older_than_days is not None:
            return self.cache.clear_older_than(older_than_days)
        return self.cache.clear()

    def cache_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dict with cache stats
        """
        return self.cache.stats()

    def _get_source(self, source_name: str) -> DatasheetSource | None:
        """Get a source by name."""
        for source in self.sources:
            if source.name == source_name:
                return source
        return None

    def close(self) -> None:
        """Close all source connections."""
        for source in self.sources:
            if hasattr(source, "close"):
                source.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
