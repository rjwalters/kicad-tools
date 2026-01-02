"""
LCSC/JLCPCB datasheet source.

Uses the existing LCSCClient to fetch datasheet URLs from JLCPCB's API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models import DatasheetResult
from ..utils import calculate_part_confidence
from .base import HTTPDatasheetSource, requires_requests

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class LCSCDatasheetSource(HTTPDatasheetSource):
    """
    Datasheet source using LCSC/JLCPCB API.

    Wraps the existing LCSCClient to search for parts and extract datasheet URLs.

    Example::

        source = LCSCDatasheetSource()
        results = source.search("STM32F103C8T6")
        for result in results:
            print(f"{result.part_number}: {result.datasheet_url}")
    """

    def __init__(self, timeout: float = 30.0):
        """
        Initialize the LCSC datasheet source.

        Args:
            timeout: Request timeout in seconds
        """
        super().__init__(timeout=timeout)

    @property
    def name(self) -> str:
        return "lcsc"

    def _get_default_headers(self) -> dict[str, str]:
        """Get default HTTP headers for LCSC requests."""
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://jlcpcb.com",
            "Referer": "https://jlcpcb.com/parts",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    @requires_requests
    def search(self, part_number: str) -> list[DatasheetResult]:
        """
        Search LCSC for datasheets matching the part number.

        Args:
            part_number: Part number to search for

        Returns:
            List of DatasheetResult objects with datasheet URLs
        """
        from kicad_tools.parts.lcsc import LCSCClient

        results = []
        client = LCSCClient(use_cache=True, timeout=self.timeout)

        try:
            # Try exact LCSC part lookup first if it looks like an LCSC part
            if part_number.upper().startswith("C") and part_number[1:].isdigit():
                part = client.lookup(part_number)
                if part and part.datasheet_url:
                    results.append(
                        DatasheetResult(
                            part_number=part.mfr_part or part.lcsc_part,
                            manufacturer=part.manufacturer,
                            description=part.description,
                            datasheet_url=part.datasheet_url,
                            source=self.name,
                            confidence=1.0,
                        )
                    )
                    return results

            # Search for the part
            search_result = client.search(part_number, page_size=10)
            for part in search_result.parts:
                if part.datasheet_url:
                    confidence = calculate_part_confidence(part_number, part.mfr_part or "")

                    results.append(
                        DatasheetResult(
                            part_number=part.mfr_part or part.lcsc_part,
                            manufacturer=part.manufacturer,
                            description=part.description,
                            datasheet_url=part.datasheet_url,
                            source=self.name,
                            confidence=confidence,
                        )
                    )

        except Exception as e:
            logger.warning(f"LCSC search failed for '{part_number}': {e}")

        return results
