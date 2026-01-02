"""
Datasheet sources for fetching datasheets from various suppliers.
"""

from .base import DatasheetSource, HTTPDatasheetSource, requires_requests
from .lcsc import LCSCDatasheetSource
from .octopart import OctopartDatasheetSource

__all__ = [
    "DatasheetSource",
    "HTTPDatasheetSource",
    "LCSCDatasheetSource",
    "OctopartDatasheetSource",
    "requires_requests",
]
