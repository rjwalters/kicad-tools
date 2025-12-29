"""DRC (Design Rule Check) report parsing and validation.

This module provides tools for parsing KiCad DRC reports and validating
PCB designs against manufacturer design rules.

Example:
    >>> from kicad_tools.drc import DRCReport, DRCViolation
    >>> report = DRCReport.load("design-drc.rpt")
    >>> print(f"Found {report.violation_count} violations")
    >>> for v in report.errors:
    ...     print(f"  {v.type}: {v.message}")
"""

from .checker import (
    CheckResult,
    ManufacturerCheck,
    check_manufacturer_rules,
)
from .report import (
    DRCReport,
    parse_json_report,
    parse_text_report,
)
from .violation import (
    DRCViolation,
    Location,
    Severity,
    ViolationType,
)

__all__ = [
    # Violation types
    "DRCViolation",
    "ViolationType",
    "Severity",
    "Location",
    # Report parsing
    "DRCReport",
    "parse_text_report",
    "parse_json_report",
    # Rule checking
    "check_manufacturer_rules",
    "ManufacturerCheck",
    "CheckResult",
]
