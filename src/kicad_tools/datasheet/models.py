"""
Data models for datasheet search and download.

Defines dataclasses for datasheet search results and cached datasheets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class DatasheetResult:
    """
    A datasheet search result from any source.

    Represents a datasheet found via search, before downloading.
    """

    part_number: str
    manufacturer: str
    description: str
    datasheet_url: str
    source: str  # octopart, lcsc, digikey, etc.
    confidence: float = 1.0  # 0-1, how confident this is the right part

    def __str__(self) -> str:
        return f"{self.part_number} ({self.manufacturer}) - {self.source}"


@dataclass
class Datasheet:
    """
    A downloaded and cached datasheet.

    Represents a datasheet that has been downloaded to local storage.
    """

    part_number: str
    manufacturer: str
    local_path: Path
    source_url: str
    downloaded_at: datetime
    file_size: int
    page_count: int | None = None
    source: str = ""  # Which source it came from

    @property
    def exists(self) -> bool:
        """Check if the local file exists."""
        return self.local_path.exists()

    @property
    def file_size_mb(self) -> float:
        """Get file size in megabytes."""
        return self.file_size / (1024 * 1024)

    def __str__(self) -> str:
        return f"{self.part_number}: {self.local_path}"


@dataclass
class DatasheetSearchResult:
    """Result from a datasheet search across all sources."""

    query: str
    results: list[DatasheetResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # source -> error message
    # Subset of ``errors`` whose failures were caused by a missing optional
    # dependency (an ``ImportError`` raised by a source's ``@requires_requests``
    # guard). Tracked separately so callers can distinguish "no results because
    # nothing matched" from "no results because every source is missing a
    # dependency" without string-sniffing ``errors``.
    import_errors: dict[str, str] = field(default_factory=dict)  # source -> message

    @property
    def has_results(self) -> bool:
        """Check if any results were found."""
        return len(self.results) > 0

    @property
    def all_failures_are_import_errors(self) -> bool:
        """True when at least one source failed and every failure was an ImportError.

        Used to detect the "no configured datasheet source could even run
        because an optional dependency is missing" condition, so the CLI can
        surface install guidance instead of a misleading "no datasheet found".
        """
        return bool(self.errors) and set(self.import_errors) == set(self.errors)

    @property
    def sources_searched(self) -> list[str]:
        """Get list of sources that returned results."""
        return list({r.source for r in self.results})

    @property
    def sources_failed(self) -> list[str]:
        """Get list of sources that failed."""
        return list(self.errors.keys())

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)
