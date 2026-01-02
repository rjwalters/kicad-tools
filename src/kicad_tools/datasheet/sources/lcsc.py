"""
LCSC/JLCPCB datasheet source.

Uses the existing LCSCClient to fetch datasheet URLs from JLCPCB's API.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import DatasheetResult
from ..utils import calculate_part_confidence
from .base import DatasheetSource

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _requires_requests(func):
    """Decorator to check if requests is available."""

    def wrapper(*args, **kwargs):
        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'requests' library is required for datasheet downloads. "
                "Install with: pip install kicad-tools[parts]"
            )
        return func(*args, **kwargs)

    return wrapper


class LCSCDatasheetSource(DatasheetSource):
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
        self.timeout = timeout
        self._session = None

    @property
    def name(self) -> str:
        return "lcsc"

    def _get_session(self):
        """Get or create requests session."""
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update(
                {
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
            )
        return self._session

    @_requires_requests
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
                    confidence = calculate_part_confidence(
                        part_number, part.mfr_part or ""
                    )

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

    @_requires_requests
    def download(self, result: DatasheetResult, output_path: Path) -> Path:
        """
        Download a datasheet from LCSC.

        Args:
            result: The DatasheetResult to download
            output_path: Where to save the file

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        import requests

        from ..exceptions import DatasheetDownloadError

        session = self._get_session()

        try:
            response = session.get(
                result.datasheet_url,
                timeout=self.timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

            # Ensure parent directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Write to file
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Downloaded datasheet to {output_path}")
            return output_path

        except requests.RequestException as e:
            raise DatasheetDownloadError(
                f"Failed to download datasheet from {result.datasheet_url}: {e}"
            ) from e

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
