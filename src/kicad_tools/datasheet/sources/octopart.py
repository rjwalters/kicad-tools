"""
Octopart datasheet source.

Uses Octopart's free API tier to search for datasheets across multiple suppliers.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..models import DatasheetResult
from ..utils import calculate_part_confidence
from .base import HTTPDatasheetSource, requires_requests

logger = logging.getLogger(__name__)


# Octopart API endpoint
OCTOPART_API_URL = "https://octopart.com/api/v4/rest/search"


class OctopartDatasheetSource(HTTPDatasheetSource):
    """
    Datasheet source using Octopart API.

    Octopart aggregates data from multiple suppliers (DigiKey, Mouser, etc.)
    and provides datasheet links. The free tier allows 3 requests/second.

    Note: Octopart requires an API key for access. Without a key, searches
    will fail gracefully.

    Example::

        source = OctopartDatasheetSource(api_key="your-api-key")
        results = source.search("STM32F103C8T6")
        for result in results:
            print(f"{result.part_number}: {result.datasheet_url}")
    """

    # Rate limit: 3 requests per second for free tier
    MIN_REQUEST_INTERVAL = 0.34  # seconds between requests

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        """
        Initialize the Octopart datasheet source.

        Args:
            api_key: Octopart API key (optional, but required for API access)
            timeout: Request timeout in seconds
        """
        super().__init__(timeout=timeout)
        self.api_key = api_key
        self._last_request_time = 0.0

    @property
    def name(self) -> str:
        return "octopart"

    def _get_default_headers(self) -> dict[str, str]:
        """Get default HTTP headers for Octopart requests."""
        return {
            "Accept": "application/json",
            "User-Agent": "kicad-tools/1.0 (https://github.com/rjwalters/kicad-tools)",
        }

    def _rate_limit(self) -> None:
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.MIN_REQUEST_INTERVAL:
            sleep_time = self.MIN_REQUEST_INTERVAL - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    @requires_requests
    def search(self, part_number: str) -> list[DatasheetResult]:
        """
        Search Octopart for datasheets matching the part number.

        Args:
            part_number: Part number to search for

        Returns:
            List of DatasheetResult objects with datasheet URLs
        """
        if not self.api_key:
            logger.debug("Octopart API key not configured, skipping search")
            return []

        import requests

        session = self._get_session()
        results = []

        try:
            self._rate_limit()

            params = {
                "apikey": self.api_key,
                "q": part_number,
                "limit": 10,
                "include[]": ["datasheets", "descriptions"],
            }

            response = session.get(
                OCTOPART_API_URL,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            for hit in data.get("results", []):
                part_data = hit.get("part", {})
                mpn = part_data.get("mpn", "")
                manufacturer = part_data.get("manufacturer", {}).get("name", "")
                description = ""

                # Get description
                descriptions = part_data.get("descriptions", [])
                if descriptions:
                    description = descriptions[0].get("text", "")

                # Get datasheets
                datasheets = part_data.get("datasheets", [])
                for ds in datasheets:
                    url = ds.get("url", "")
                    if url:
                        confidence = calculate_part_confidence(part_number, mpn)

                        results.append(
                            DatasheetResult(
                                part_number=mpn,
                                manufacturer=manufacturer,
                                description=description,
                                datasheet_url=url,
                                source=self.name,
                                confidence=confidence,
                            )
                        )

        except requests.RequestException as e:
            logger.warning(f"Octopart search failed for '{part_number}': {e}")

        return results

    @requires_requests
    def download(self, result: DatasheetResult, output_path: Path) -> Path:
        """
        Download a datasheet found via Octopart.

        Includes rate limiting to comply with Octopart's API limits.

        Args:
            result: The DatasheetResult to download
            output_path: Where to save the file

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        self._rate_limit()
        return super().download(result, output_path)
