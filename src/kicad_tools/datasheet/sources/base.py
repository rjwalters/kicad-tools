"""
Base interface for datasheet sources.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import DatasheetResult


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

    def __str__(self) -> str:
        return self.name
