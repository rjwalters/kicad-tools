"""Compatibility adapter between validate.violations and drc.violation types.

The pure-Python DRC checker (``validate.checker.DRCChecker``) produces
``validate.violations.DRCResults`` containing ``validate.violations.DRCViolation``
objects with flat fields.  The DRC repair tools (``drc.repair_clearance``,
``drc.fixer``) consume ``drc.report.DRCReport`` containing
``drc.violation.DRCViolation`` objects with richer structure.

This module provides a conversion function so that any code path that has
``DRCResults`` from the pure-Python checker can produce a ``DRCReport``
suitable for the repair tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.types import Severity
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation as ReportViolation
from kicad_tools.drc.violation import Location, ViolationType

if TYPE_CHECKING:
    from kicad_tools.validate.violations import DRCResults


def drc_results_to_report(
    results: DRCResults,
    pcb_path: str | Path | None = None,
) -> DRCReport:
    """Convert ``validate.violations.DRCResults`` to ``drc.report.DRCReport``.

    This bridges the type mismatch between the pure-Python DRC checker
    output and the repair tools' expected input.

    Args:
        results: DRC results from ``DRCChecker.check_all()`` or similar.
        pcb_path: Optional path to the PCB file for metadata.

    Returns:
        A ``DRCReport`` containing converted violations.
    """
    violations: list[ReportViolation] = []

    for v in results.violations:
        vtype = ViolationType.from_string(v.rule_id)

        loc_list: list[Location] = []
        if v.location:
            loc_list.append(
                Location(x_mm=v.location[0], y_mm=v.location[1], layer=v.layer or "")
            )

        violations.append(
            ReportViolation(
                type=vtype,
                type_str=v.rule_id,
                severity=Severity.from_string(v.severity),
                message=v.message,
                locations=loc_list,
                items=list(v.items),
                required_value_mm=v.required_value,
                actual_value_mm=v.actual_value,
            )
        )

    pcb_name = ""
    source_file = ""
    if pcb_path is not None:
        p = Path(pcb_path)
        pcb_name = p.name
        source_file = str(p)

    return DRCReport(
        source_file=source_file,
        created_at=None,
        pcb_name=pcb_name,
        violations=violations,
    )
