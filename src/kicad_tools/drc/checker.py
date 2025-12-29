"""Manufacturer design rule checking for DRC reports."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .report import DRCReport
from .violation import DRCViolation, ViolationType


class CheckResult(Enum):
    """Result of a manufacturer rule check."""

    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    UNKNOWN = "unknown"  # Could not determine


@dataclass
class ManufacturerCheck:
    """Result of checking a violation against manufacturer rules."""

    violation: DRCViolation
    result: CheckResult
    message: str
    manufacturer_id: str
    rule_name: str
    manufacturer_limit: Optional[float] = None  # mm
    actual_value: Optional[float] = None  # mm

    @property
    def is_compatible(self) -> bool:
        """Check if this violation is within manufacturer limits."""
        return self.result in (CheckResult.PASS, CheckResult.WARNING)

    def __str__(self) -> str:
        if self.manufacturer_limit is not None and self.actual_value is not None:
            return (
                f"{self.result.value.upper()}: {self.message} "
                f"(limit: {self.manufacturer_limit:.4f}mm, actual: {self.actual_value:.4f}mm)"
            )
        return f"{self.result.value.upper()}: {self.message}"


def check_manufacturer_rules(
    report: DRCReport,
    manufacturer_id: str,
    layers: int = 2,
    copper_oz: float = 1.0,
) -> list[ManufacturerCheck]:
    """Check DRC violations against manufacturer design rules.

    Args:
        report: Parsed DRC report
        manufacturer_id: Manufacturer ID (e.g., "jlcpcb", "oshpark")
        layers: Layer count for rules lookup
        copper_oz: Copper weight in oz

    Returns:
        List of check results for each relevant violation
    """
    # Import here to avoid circular dependency
    from kicad_tools.manufacturers import get_profile

    try:
        profile = get_profile(manufacturer_id)
    except ValueError:
        # Unknown manufacturer - return unknown results
        return [
            ManufacturerCheck(
                violation=v,
                result=CheckResult.UNKNOWN,
                message=f"Unknown manufacturer: {manufacturer_id}",
                manufacturer_id=manufacturer_id,
                rule_name="unknown",
            )
            for v in report.violations
        ]

    rules = profile.get_design_rules(layers=layers, copper_oz=copper_oz)
    results: list[ManufacturerCheck] = []

    for violation in report.violations:
        check = _check_violation(violation, profile.id, profile.name, rules)
        if check:
            results.append(check)

    return results


def _check_violation(
    violation: DRCViolation,
    manufacturer_id: str,
    manufacturer_name: str,
    rules,  # DesignRules from mfr module
) -> Optional[ManufacturerCheck]:
    """Check a single violation against manufacturer rules."""

    # Clearance violations
    if violation.type == ViolationType.CLEARANCE:
        if violation.actual_value_mm is not None:
            result = (
                CheckResult.PASS
                if violation.actual_value_mm >= rules.min_clearance_mm
                else CheckResult.FAIL
            )
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=f"Clearance {violation.actual_value_mm:.4f}mm vs {manufacturer_name} min {rules.min_clearance_mm:.4f}mm",
                manufacturer_id=manufacturer_id,
                rule_name="min_clearance",
                manufacturer_limit=rules.min_clearance_mm,
                actual_value=violation.actual_value_mm,
            )
        # No actual value available - check if within general limits
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=f"Clearance violation - {manufacturer_name} requires {rules.min_clearance_mm:.4f}mm minimum",
            manufacturer_id=manufacturer_id,
            rule_name="min_clearance",
            manufacturer_limit=rules.min_clearance_mm,
        )

    # Track width violations
    if violation.type == ViolationType.TRACK_WIDTH:
        if violation.actual_value_mm is not None:
            result = (
                CheckResult.PASS
                if violation.actual_value_mm >= rules.min_trace_width_mm
                else CheckResult.FAIL
            )
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=f"Track width {violation.actual_value_mm:.4f}mm vs {manufacturer_name} min {rules.min_trace_width_mm:.4f}mm",
                manufacturer_id=manufacturer_id,
                rule_name="min_trace_width",
                manufacturer_limit=rules.min_trace_width_mm,
                actual_value=violation.actual_value_mm,
            )
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=f"Track width violation - {manufacturer_name} requires {rules.min_trace_width_mm:.4f}mm minimum",
            manufacturer_id=manufacturer_id,
            rule_name="min_trace_width",
            manufacturer_limit=rules.min_trace_width_mm,
        )

    # Copper to edge clearance
    if violation.type == ViolationType.COPPER_EDGE_CLEARANCE:
        if violation.actual_value_mm is not None:
            result = (
                CheckResult.PASS
                if violation.actual_value_mm >= rules.min_copper_to_edge_mm
                else CheckResult.FAIL
            )
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=f"Edge clearance {violation.actual_value_mm:.4f}mm vs {manufacturer_name} min {rules.min_copper_to_edge_mm:.3f}mm",
                manufacturer_id=manufacturer_id,
                rule_name="min_copper_to_edge",
                manufacturer_limit=rules.min_copper_to_edge_mm,
                actual_value=violation.actual_value_mm,
            )
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=f"Edge clearance violation - {manufacturer_name} requires {rules.min_copper_to_edge_mm:.3f}mm minimum",
            manufacturer_id=manufacturer_id,
            rule_name="min_copper_to_edge",
            manufacturer_limit=rules.min_copper_to_edge_mm,
        )

    # Via annular ring
    if violation.type == ViolationType.VIA_ANNULAR_WIDTH:
        if violation.actual_value_mm is not None:
            result = (
                CheckResult.PASS
                if violation.actual_value_mm >= rules.min_annular_ring_mm
                else CheckResult.FAIL
            )
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=f"Annular ring {violation.actual_value_mm:.4f}mm vs {manufacturer_name} min {rules.min_annular_ring_mm:.3f}mm",
                manufacturer_id=manufacturer_id,
                rule_name="min_annular_ring",
                manufacturer_limit=rules.min_annular_ring_mm,
                actual_value=violation.actual_value_mm,
            )
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=f"Annular ring violation - {manufacturer_name} requires {rules.min_annular_ring_mm:.3f}mm minimum",
            manufacturer_id=manufacturer_id,
            rule_name="min_annular_ring",
            manufacturer_limit=rules.min_annular_ring_mm,
        )

    # Drill hole too small
    if violation.type == ViolationType.DRILL_HOLE_TOO_SMALL:
        if violation.actual_value_mm is not None:
            result = (
                CheckResult.PASS
                if violation.actual_value_mm >= rules.min_via_drill_mm
                else CheckResult.FAIL
            )
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=f"Drill {violation.actual_value_mm:.3f}mm vs {manufacturer_name} min {rules.min_via_drill_mm:.2f}mm",
                manufacturer_id=manufacturer_id,
                rule_name="min_via_drill",
                manufacturer_limit=rules.min_via_drill_mm,
                actual_value=violation.actual_value_mm,
            )
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=f"Drill size violation - {manufacturer_name} requires {rules.min_via_drill_mm:.2f}mm minimum",
            manufacturer_id=manufacturer_id,
            rule_name="min_via_drill",
            manufacturer_limit=rules.min_via_drill_mm,
        )

    # Connection issues - always critical, manufacturer-independent
    if violation.type in (ViolationType.UNCONNECTED_ITEMS, ViolationType.SHORTING_ITEMS):
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.FAIL,
            message="Critical connection issue - must fix before manufacturing",
            manufacturer_id=manufacturer_id,
            rule_name="connection",
        )

    # Default: unknown how to check against manufacturer rules
    return None


def summarize_checks(checks: list[ManufacturerCheck]) -> dict:
    """Summarize manufacturer check results."""
    results = {
        "total": len(checks),
        "pass": sum(1 for c in checks if c.result == CheckResult.PASS),
        "fail": sum(1 for c in checks if c.result == CheckResult.FAIL),
        "warning": sum(1 for c in checks if c.result == CheckResult.WARNING),
        "unknown": sum(1 for c in checks if c.result == CheckResult.UNKNOWN),
    }
    results["compatible"] = results["pass"] + results["warning"]
    results["by_rule"] = {}

    for check in checks:
        if check.rule_name not in results["by_rule"]:
            results["by_rule"][check.rule_name] = {"pass": 0, "fail": 0, "warning": 0}
        results["by_rule"][check.rule_name][check.result.value] += 1

    return results
