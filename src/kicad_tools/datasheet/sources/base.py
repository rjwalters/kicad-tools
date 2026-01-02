"""
Base interface for datasheet sources.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.utils import ensure_parent_dir

if TYPE_CHECKING:
    from ..models import DatasheetResult

logger = logging.getLogger(__name__)


def requires_requests(func):
    """
    Decorator to check if requests library is available.

    Raises ImportError with installation instructions if requests is not installed.
    Used by datasheet sources that need HTTP functionality.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'requests' library is required for datasheet operations. "
                "Install with: pip install kicad-tools[parts]"
            )
        return func(*args, **kwargs)

    return wrapper


class DatasheetSource(ABC):
    """
    Abstract base class for datasheet sources.

    Each source implementation provides search and download functionality
    for a specific datasheet provider (LCSC, Octopart, DigiKey, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The name of this datasheet source."""
        ...

    @abstractmethod
    def search(self, part_number: str) -> list[DatasheetResult]:
        """
        Search for datasheets matching the part number.

        Args:
            part_number: Part number or search query

        Returns:
            List of matching DatasheetResult objects
        """
        ...

    @abstractmethod
    def download(self, result: DatasheetResult, output_path: Path) -> Path:
        """
        Download a datasheet to the specified path.

        Args:
            result: The DatasheetResult to download
            output_path: Where to save the file

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        ...

    def _download_file(
        self,
        session: Any,
        url: str,
        output_path: Path,
        timeout: float,
    ) -> Path:
        """
        Download a file from a URL using the provided session.

        This is a shared helper method that handles the common HTTP download
        logic used by multiple datasheet sources.

        Args:
            session: A requests.Session object to use for the download
            url: The URL to download from
            output_path: Where to save the file
            timeout: Request timeout in seconds

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        import requests

        from ..exceptions import DatasheetDownloadError

        try:
            response = session.get(
                url,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

            ensure_parent_dir(output_path)

            # Write to file in chunks
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Downloaded datasheet to {output_path}")
            return output_path

        except requests.RequestException as e:
            raise DatasheetDownloadError(f"Failed to download datasheet from {url}: {e}") from e

    def __str__(self) -> str:
        return self.name
