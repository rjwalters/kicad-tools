"""Manufacturer design rule checking for DRC reports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

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
    manufacturer_limit: float | None = None  # mm
    actual_value: float | None = None  # mm

    @property
    def is_compatible(self) -> bool:
        """Check if this violation is within manufacturer limits."""
        return self.result in (CheckResult.PASS, CheckResult.WARNING)

    @property
    def is_not_checked(self) -> bool:
        """True when kct could not judge this violation against fab limits.

        Issue #7170: such a violation is neither compatible nor incompatible;
        it keeps kicad-cli's own severity and is counted separately.
        """
        return self.result == CheckResult.UNKNOWN

    def __str__(self) -> str:
        if self.manufacturer_limit is not None and self.actual_value is not None:
            return (
                f"{self.result.value.upper()}: {self.message} "
                f"(limit: {self.manufacturer_limit:.4f}mm, actual: {self.actual_value:.4f}mm)"
            )
        return f"{self.result.value.upper()}: {self.message}"


#: ViolationTypes that :func:`_check_violation` judges against a fab limit
#: (Issue #7170).  Every other type comes back as a ``not_checked`` UNKNOWN
#: result.  ``tests/test_drc_mfr_not_checked_7170.py`` asserts that no
#: kicad-cli type string meant for one of these branches parses as UNKNOWN,
#: which is how the ``annular_width`` branch went dead.
MANUFACTURER_CHECKED_TYPES: frozenset[ViolationType] = frozenset(
    {
        ViolationType.CLEARANCE,
        ViolationType.CLEARANCE_SEGMENT_VIA,
        ViolationType.TRACK_WIDTH,
        ViolationType.COPPER_EDGE_CLEARANCE,
        ViolationType.VIA_ANNULAR_WIDTH,
        ViolationType.DRILL_HOLE_TOO_SMALL,
        ViolationType.HOLE_TO_HOLE_CLEARANCE,
        ViolationType.DIMENSION_VIA_DIAMETER,
        ViolationType.SOLDER_MASK_BRIDGE,
        ViolationType.UNCONNECTED_ITEMS,
        ViolationType.SHORTING_ITEMS,
    }
)

#: ``rule_name`` of the fallback result for a violation kct cannot judge.
NOT_CHECKED_RULE = "not_checked"


def check_manufacturer_rules(
    report: DRCReport,
    manufacturer_id: str,
    layers: int = 2,
    copper_oz: float = 1.0,
    progress_callback: ProgressCallback | None = None,
) -> list[ManufacturerCheck]:
    """Check DRC violations against manufacturer design rules.

    Args:
        report: Parsed DRC report
        manufacturer_id: Manufacturer ID (e.g., "jlcpcb", "oshpark")
        layers: Layer count for rules lookup
        copper_oz: Copper weight in oz
        progress_callback: Optional callback for progress reporting.
            Signature: (progress: float, message: str, cancelable: bool) -> bool
            Returns False to cancel, True to continue.

    Returns:
        Exactly one check result per input violation (Issue #7170).  A
        violation kct has no fab-limit check for is returned as
        ``CheckResult.UNKNOWN`` with ``rule_name == "not_checked"`` rather
        than dropped, so callers can count what was never judged.
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
    violations = list(report.violations)
    total = len(violations)

    for i, violation in enumerate(violations):
        # Report progress
        if progress_callback is not None:
            progress = i / total if total > 0 else 0.0
            if not progress_callback(progress, f"Checking violation {i + 1}/{total}", True):
                break  # Cancelled

        check = _check_violation(violation, profile.id, profile.name, rules)
        if check is None:
            # Issue #7170: never drop a violation.  An unjudged finding is
            # reported as not checked, keeping kicad-cli's type and severity.
            check = ManufacturerCheck(
                violation=violation,
                result=CheckResult.UNKNOWN,
                message=(
                    f"{violation.type_str or violation.type.value} "
                    f"({violation.severity.value}): not checked against "
                    f"{profile.name} limits"
                ),
                manufacturer_id=profile.id,
                rule_name=NOT_CHECKED_RULE,
                actual_value=violation.actual_value_mm,
            )
        results.append(check)

    # Final progress report
    if progress_callback is not None:
        progress_callback(1.0, f"Checked {len(results)} violations", False)

    return results


def _check_violation(
    violation: DRCViolation,
    manufacturer_id: str,
    manufacturer_name: str,
    rules,  # DesignRules from mfr module
) -> ManufacturerCheck | None:
    """Check a single violation against manufacturer rules."""

    # Clearance violations.  The report parser refines kicad-cli's
    # ``clearance`` between a track and a via to CLEARANCE_SEGMENT_VIA
    # (``_infer_segment_via_type``); it is still copper-to-copper, so judge it
    # here rather than let it fall through to not checked (Issue #7170).
    if violation.type in (ViolationType.CLEARANCE, ViolationType.CLEARANCE_SEGMENT_VIA):
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

    # Hole size out of range (kicad-cli ``drill_out_of_range``).  KiCad
    # raises it for both a too-small and a too-large hole, so judge both
    # bounds (Issue #7170): below the fab's minimum drill or above its
    # maximum hole diameter is a FAIL.
    if violation.type == ViolationType.DRILL_HOLE_TOO_SMALL:
        max_hole = getattr(rules, "max_hole_diameter_mm", None)
        if violation.actual_value_mm is not None:
            actual = violation.actual_value_mm
            too_large = max_hole is not None and actual > max_hole
            too_small = actual < rules.min_via_drill_mm
            result = CheckResult.FAIL if (too_small or too_large) else CheckResult.PASS
            if too_large:
                message = f"Drill {actual:.3f}mm vs {manufacturer_name} max {max_hole:.2f}mm"
                rule_name = "max_hole_diameter"
                limit = max_hole
            else:
                message = f"Drill {actual:.3f}mm vs {manufacturer_name} min {rules.min_via_drill_mm:.2f}mm"
                rule_name = "min_via_drill"
                limit = rules.min_via_drill_mm
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=message,
                manufacturer_id=manufacturer_id,
                rule_name=rule_name,
                manufacturer_limit=limit,
                actual_value=actual,
            )
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=f"Drill size violation - {manufacturer_name} requires {rules.min_via_drill_mm:.2f}mm minimum",
            manufacturer_id=manufacturer_id,
            rule_name="min_via_drill",
            manufacturer_limit=rules.min_via_drill_mm,
        )

    # Hole-to-hole web (kicad-cli ``hole_to_hole``, kct
    # ``hole_to_hole_clearance``).  Issue #7170.
    if violation.type == ViolationType.HOLE_TO_HOLE_CLEARANCE:
        return _min_floor_check(
            violation,
            manufacturer_id,
            label="Hole-to-hole",
            manufacturer_name=manufacturer_name,
            limit=rules.min_hole_to_hole_mm,
            rule_name="min_hole_to_hole",
        )

    # Via pad diameter (kicad-cli ``via_diameter``).  Issue #7170.
    if violation.type == ViolationType.DIMENSION_VIA_DIAMETER:
        return _min_floor_check(
            violation,
            manufacturer_id,
            label="Via diameter",
            manufacturer_name=manufacturer_name,
            limit=rules.min_via_diameter_mm,
            rule_name="min_via_diameter",
        )

    # Pad-to-pad clearance: same-component violations are inherent to the
    # footprint geometry (e.g., adjacent TSSOP pins) and not actionable.
    if violation.type == ViolationType.CLEARANCE_PAD_PAD:
        if violation.is_same_component_pad_clearance():
            return ManufacturerCheck(
                violation=violation,
                result=CheckResult.PASS,
                message=(
                    "Same-component pad-pad clearance - inherent to IC footprint "
                    "geometry (adjacent pins within a single package)"
                ),
                manufacturer_id=manufacturer_id,
                rule_name="pad_pad_clearance",
            )

    # Solder mask bridge violations
    if violation.type == ViolationType.SOLDER_MASK_BRIDGE:
        # Check if this is a fine-pitch IC pad-to-pad bridge
        if violation.is_fine_pitch_inherent(rules.min_solder_mask_dam_mm):
            return ManufacturerCheck(
                violation=violation,
                result=CheckResult.PASS,
                message=(
                    f"Fine-pitch IC solder mask bridge - inherent to IC footprint "
                    f"(dam {violation.actual_value_mm:.4f}mm < "
                    f"{manufacturer_name} min {rules.min_solder_mask_dam_mm:.3f}mm, "
                    f"acceptable for fine-pitch ICs)"
                ),
                manufacturer_id=manufacturer_id,
                rule_name="solder_mask_dam",
                manufacturer_limit=rules.min_solder_mask_dam_mm,
                actual_value=violation.actual_value_mm,
            )
        # Non-fine-pitch: check actual value against manufacturer minimum
        if violation.actual_value_mm is not None:
            result = (
                CheckResult.PASS
                if violation.actual_value_mm >= rules.min_solder_mask_dam_mm
                else CheckResult.FAIL
            )
            return ManufacturerCheck(
                violation=violation,
                result=result,
                message=(
                    f"Solder mask dam {violation.actual_value_mm:.4f}mm vs "
                    f"{manufacturer_name} min {rules.min_solder_mask_dam_mm:.3f}mm"
                ),
                manufacturer_id=manufacturer_id,
                rule_name="solder_mask_dam",
                manufacturer_limit=rules.min_solder_mask_dam_mm,
                actual_value=violation.actual_value_mm,
            )
        return ManufacturerCheck(
            violation=violation,
            result=CheckResult.WARNING,
            message=(
                f"Solder mask bridge - {manufacturer_name} requires "
                f"{rules.min_solder_mask_dam_mm:.3f}mm minimum dam"
            ),
            manufacturer_id=manufacturer_id,
            rule_name="solder_mask_dam",
            manufacturer_limit=rules.min_solder_mask_dam_mm,
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


def _min_floor_check(
    violation: DRCViolation,
    manufacturer_id: str,
    *,
    label: str,
    manufacturer_name: str,
    limit: float,
    rule_name: str,
) -> ManufacturerCheck:
    """Judge ``violation.actual_value_mm`` against a fab minimum ``limit``."""
    if violation.actual_value_mm is not None:
        result = CheckResult.PASS if violation.actual_value_mm >= limit else CheckResult.FAIL
        return ManufacturerCheck(
            violation=violation,
            result=result,
            message=(
                f"{label} {violation.actual_value_mm:.4f}mm vs {manufacturer_name} min {limit:.3f}mm"
            ),
            manufacturer_id=manufacturer_id,
            rule_name=rule_name,
            manufacturer_limit=limit,
            actual_value=violation.actual_value_mm,
        )
    return ManufacturerCheck(
        violation=violation,
        result=CheckResult.WARNING,
        message=f"{label} violation - {manufacturer_name} requires {limit:.3f}mm minimum",
        manufacturer_id=manufacturer_id,
        rule_name=rule_name,
        manufacturer_limit=limit,
    )


def summarize_checks(checks: list[ManufacturerCheck]) -> dict:
    """Summarize manufacturer check results.

    ``unknown`` and its alias ``not_checked`` count results kct could not
    judge against the fab's limits (Issue #7170).
    """
    results = {
        "total": len(checks),
        "pass": sum(1 for c in checks if c.result == CheckResult.PASS),
        "fail": sum(1 for c in checks if c.result == CheckResult.FAIL),
        "warning": sum(1 for c in checks if c.result == CheckResult.WARNING),
        "unknown": sum(1 for c in checks if c.result == CheckResult.UNKNOWN),
    }
    results["compatible"] = results["pass"] + results["warning"]
    results["not_checked"] = results["unknown"]
    results["by_rule"] = {}

    for check in checks:
        if check.rule_name not in results["by_rule"]:
            results["by_rule"][check.rule_name] = {r.value: 0 for r in CheckResult}
        results["by_rule"][check.rule_name][check.result.value] += 1

    return results
