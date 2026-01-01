"""
Exceptions for the datasheet module.
"""

from kicad_tools.exceptions import KiCadToolsError


class DatasheetError(KiCadToolsError):
    """Base exception for datasheet-related errors."""

    pass


class DatasheetDownloadError(DatasheetError):
    """Raised when a datasheet download fails."""

    pass


class DatasheetSearchError(DatasheetError):
    """Raised when a datasheet search fails."""

    pass


class DatasheetCacheError(DatasheetError):
    """Raised when cache operations fail."""

    pass
