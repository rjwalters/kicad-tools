"""ERC (Electrical Rules Check) report parsing and validation.

This module provides tools for parsing KiCad ERC reports and analyzing
schematic electrical rule violations.

Example:
    >>> from kicad_tools.erc import ERCReport, ERCViolation
    >>> report = ERCReport.load("design-erc.json")
    >>> print(f"Found {report.error_count} errors")
    >>> for v in report.errors:
    ...     print(f"  {v.type}: {v.description}")
"""

from .report import (
    ERCReport,
    parse_json_report,
    parse_text_report,
)
from .violation import (
    ERC_CATEGORIES,
    ERC_TYPE_DESCRIPTIONS,
    ERCViolation,
    ERCViolationType,
    Severity,
)

__all__ = [
    # Violation types
    "ERCViolation",
    "ERCViolationType",
    "Severity",
    "ERC_TYPE_DESCRIPTIONS",
    "ERC_CATEGORIES",
    # Report parsing
    "ERCReport",
    "parse_json_report",
    "parse_text_report",
]
