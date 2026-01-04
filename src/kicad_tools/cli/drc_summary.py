"""
Structured DRC summary with severity levels.

Categorizes DRC violations by severity and compares against manufacturer capabilities.
Provides actionable summaries with false positive identification.

Usage:
    kicad-drc-summary board.kicad_pcb              # Quick summary (runs DRC)
    kicad-drc-summary design-drc.json              # Parse existing report
    kicad-drc-summary board.kicad_pcb --fab jlcpcb # Compare against manufacturer rules
    kicad-drc-summary board.kicad_pcb --format json # JSON output for CI
    kicad-drc-summary board.kicad_pcb --blocking-only # Only show blocking issues

Exit Codes:
    0 - No blocking issues
    1 - Blocking issues found or command failure
    2 - Warnings only (with --strict)
"""

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..drc import DRCReport, DRCViolation, ViolationType
from ..manufacturers import DesignRules, get_manufacturer_ids, get_profile
from .runner import find_kicad_cli, run_drc


class IssueSeverity(Enum):
    """Severity classification for DRC issues."""

    BLOCKING = "blocking"  # Must fix before manufacturing
    WARNING = "warning"  # Should review
    COSMETIC = "cosmetic"  # Can ignore


@dataclass
class SeverityRule:
    """Rule for classifying violation severity."""

    violation_types: set[ViolationType]
    severity: IssueSeverity
    description: str


# Default severity classification rules
SEVERITY_RULES: list[SeverityRule] = [
    # BLOCKING: Must fix before manufacturing
    SeverityRule(
        violation_types={ViolationType.SHORTING_ITEMS},
        severity=IssueSeverity.BLOCKING,
        description="shorts",
    ),
    SeverityRule(
        violation_types={ViolationType.CLEARANCE},
        severity=IssueSeverity.BLOCKING,
        description="clearance violations",
    ),
    SeverityRule(
        violation_types={ViolationType.COPPER_EDGE_CLEARANCE},
        severity=IssueSeverity.BLOCKING,
        description="copper to edge violations",
    ),
    SeverityRule(
        violation_types={ViolationType.DRILL_HOLE_TOO_SMALL, ViolationType.NPTH_HOLE_TOO_SMALL},
        severity=IssueSeverity.BLOCKING,
        description="hole size violations",
    ),
    SeverityRule(
        violation_types={ViolationType.TRACK_WIDTH},
        severity=IssueSeverity.BLOCKING,
        description="track width violations",
    ),
    SeverityRule(
        violation_types={ViolationType.VIA_HOLE_LARGER_THAN_PAD},
        severity=IssueSeverity.BLOCKING,
        description="via hole larger than pad",
    ),
    # WARNING: Should review
    SeverityRule(
        violation_types={ViolationType.UNCONNECTED_ITEMS},
        severity=IssueSeverity.WARNING,
        description="unconnected items",
    ),
    SeverityRule(
        violation_types={ViolationType.VIA_ANNULAR_WIDTH},
        severity=IssueSeverity.WARNING,
        description="annular width",
    ),
    SeverityRule(
        violation_types={ViolationType.COURTYARD_OVERLAP},
        severity=IssueSeverity.WARNING,
        description="courtyard overlap",
    ),
    SeverityRule(
        violation_types={
            ViolationType.MICRO_VIA_HOLE_TOO_SMALL,
            ViolationType.HOLE_NEAR_HOLE,
            ViolationType.TRACK_ANGLE,
        },
        severity=IssueSeverity.WARNING,
        description="via/track issues",
    ),
    SeverityRule(
        violation_types={
            ViolationType.MISSING_FOOTPRINT,
            ViolationType.EXTRA_FOOTPRINT,
            ViolationType.DUPLICATE_FOOTPRINT,
        },
        severity=IssueSeverity.WARNING,
        description="footprint issues",
    ),
    SeverityRule(
        violation_types={ViolationType.MALFORMED_OUTLINE},
        severity=IssueSeverity.WARNING,
        description="outline issues",
    ),
    # COSMETIC: Can ignore
    SeverityRule(
        violation_types={ViolationType.SILK_OVER_COPPER},
        severity=IssueSeverity.COSMETIC,
        description="silk over copper",
    ),
    SeverityRule(
        violation_types={ViolationType.SILK_OVERLAP},
        severity=IssueSeverity.COSMETIC,
        description="silk overlap",
    ),
    SeverityRule(
        violation_types={ViolationType.SOLDER_MASK_BRIDGE},
        severity=IssueSeverity.COSMETIC,
        description="solder mask bridge",
    ),
]


def get_severity(violation: DRCViolation) -> IssueSeverity:
    """Determine severity level for a violation."""
    for rule in SEVERITY_RULES:
        if violation.type in rule.violation_types:
            return rule.severity
    # Default to WARNING for unknown types
    return IssueSeverity.WARNING


@dataclass
class ManufacturerComparison:
    """Result of comparing a violation against manufacturer limits."""

    violation: DRCViolation
    is_false_positive: bool
    manufacturer_limit: float | None = None
    actual_value: float | None = None
    message: str = ""


@dataclass
class DRCSummary:
    """Structured DRC summary with severity categorization."""

    pcb_name: str
    source_file: str
    total_violations: int

    # By severity
    blocking: list[DRCViolation] = field(default_factory=list)
    warnings: list[DRCViolation] = field(default_factory=list)
    cosmetic: list[DRCViolation] = field(default_factory=list)

    # Counts by type within each severity
    blocking_by_type: Counter = field(default_factory=Counter)
    warning_by_type: Counter = field(default_factory=Counter)
    cosmetic_by_type: Counter = field(default_factory=Counter)

    # Manufacturer comparison results
    manufacturer: str | None = None
    false_positives: list[ManufacturerComparison] = field(default_factory=list)
    true_violations: list[ManufacturerComparison] = field(default_factory=list)

    # Net breakdown for unconnected items
    unconnected_by_net: Counter = field(default_factory=Counter)

    @property
    def blocking_count(self) -> int:
        return len(self.blocking)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def cosmetic_count(self) -> int:
        return len(self.cosmetic)

    @property
    def has_blocking(self) -> bool:
        return self.blocking_count > 0

    @property
    def verdict(self) -> str:
        """Generate verdict string."""
        if self.has_blocking:
            return "BLOCKING - Fix issues before manufacturing"
        elif self.warning_count > 0:
            return "WARNINGS - Review before fab"
        elif self.cosmetic_count > 0:
            return "COSMETIC ONLY - Safe to manufacture"
        else:
            return "PASSED - No issues found"

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON output."""
        result = {
            "pcb_name": self.pcb_name,
            "source_file": self.source_file,
            "total_violations": self.total_violations,
            "verdict": self.verdict,
            "counts": {
                "blocking": self.blocking_count,
                "warning": self.warning_count,
                "cosmetic": self.cosmetic_count,
            },
            "blocking": {
                "count": self.blocking_count,
                "by_type": dict(self.blocking_by_type),
                "violations": [v.to_dict() for v in self.blocking],
            },
            "warnings": {
                "count": self.warning_count,
                "by_type": dict(self.warning_by_type),
            },
            "cosmetic": {
                "count": self.cosmetic_count,
                "by_type": dict(self.cosmetic_by_type),
            },
        }

        if self.unconnected_by_net:
            result["unconnected_by_net"] = dict(self.unconnected_by_net)

        if self.manufacturer:
            result["manufacturer"] = {
                "id": self.manufacturer,
                "false_positives": len(self.false_positives),
                "true_violations": len(self.true_violations),
                "details": [
                    {
                        "type": fp.violation.type_str,
                        "is_false_positive": fp.is_false_positive,
                        "message": fp.message,
                        "manufacturer_limit": fp.manufacturer_limit,
                        "actual_value": fp.actual_value,
                    }
                    for fp in self.false_positives + self.true_violations
                ],
            }

        return result


def compare_with_manufacturer(
    violation: DRCViolation,
    rules: DesignRules,
    manufacturer_id: str,
) -> ManufacturerComparison | None:
    """Compare a violation against manufacturer limits.

    Returns ManufacturerComparison if the violation is relevant to manufacturer
    comparison (has actual/required values), otherwise None.
    """
    # Only compare violations that have measurement values
    if violation.actual_value_mm is None:
        return None

    actual = violation.actual_value_mm
    mfr_name = manufacturer_id.upper()

    # Clearance violations
    if violation.type == ViolationType.CLEARANCE:
        mfr_limit = rules.min_clearance_mm
        is_false_positive = actual >= mfr_limit
        msg = f"{mfr_name} accepts {mfr_limit:.3f}mm" if is_false_positive else ""
        return ManufacturerComparison(
            violation=violation,
            is_false_positive=is_false_positive,
            manufacturer_limit=mfr_limit,
            actual_value=actual,
            message=msg,
        )

    # Track width violations
    if violation.type == ViolationType.TRACK_WIDTH:
        mfr_limit = rules.min_trace_width_mm
        is_false_positive = actual >= mfr_limit
        msg = f"{mfr_name} accepts {mfr_limit:.3f}mm" if is_false_positive else ""
        return ManufacturerComparison(
            violation=violation,
            is_false_positive=is_false_positive,
            manufacturer_limit=mfr_limit,
            actual_value=actual,
            message=msg,
        )

    # Via annular width
    if violation.type == ViolationType.VIA_ANNULAR_WIDTH:
        mfr_limit = rules.min_annular_ring_mm
        is_false_positive = actual >= mfr_limit
        msg = f"{mfr_name} accepts {mfr_limit:.3f}mm" if is_false_positive else ""
        return ManufacturerComparison(
            violation=violation,
            is_false_positive=is_false_positive,
            manufacturer_limit=mfr_limit,
            actual_value=actual,
            message=msg,
        )

    # Copper edge clearance
    if violation.type == ViolationType.COPPER_EDGE_CLEARANCE:
        mfr_limit = rules.min_copper_to_edge_mm
        is_false_positive = actual >= mfr_limit
        msg = f"{mfr_name} accepts {mfr_limit:.3f}mm" if is_false_positive else ""
        return ManufacturerComparison(
            violation=violation,
            is_false_positive=is_false_positive,
            manufacturer_limit=mfr_limit,
            actual_value=actual,
            message=msg,
        )

    return None


def create_summary(
    report: DRCReport,
    manufacturer_id: str | None = None,
    layers: int = 2,
) -> DRCSummary:
    """Create a structured DRC summary from a report."""
    summary = DRCSummary(
        pcb_name=report.pcb_name,
        source_file=report.source_file,
        total_violations=report.violation_count,
        manufacturer=manufacturer_id,
    )

    # Get manufacturer rules if specified
    rules = None
    if manufacturer_id:
        profile = get_profile(manufacturer_id)
        rules = profile.get_design_rules(layers)

    for violation in report.violations:
        severity = get_severity(violation)

        # Categorize by severity
        if severity == IssueSeverity.BLOCKING:
            summary.blocking.append(violation)
            summary.blocking_by_type[violation.type_str] += 1
        elif severity == IssueSeverity.WARNING:
            summary.warnings.append(violation)
            summary.warning_by_type[violation.type_str] += 1
        else:
            summary.cosmetic.append(violation)
            summary.cosmetic_by_type[violation.type_str] += 1

        # Track unconnected items by net
        if violation.type == ViolationType.UNCONNECTED_ITEMS:
            for net in violation.nets:
                summary.unconnected_by_net[net] += 1

        # Compare with manufacturer limits
        if rules:
            comparison = compare_with_manufacturer(violation, rules, manufacturer_id)
            if comparison:
                if comparison.is_false_positive:
                    summary.false_positives.append(comparison)
                else:
                    summary.true_violations.append(comparison)

    return summary


def output_table(summary: DRCSummary, blocking_only: bool = False) -> None:
    """Output summary as formatted table."""
    print(f"\nDRC Summary: {summary.source_file or summary.pcb_name}")
    print("=" * 60)

    # BLOCKING section
    print("\nBLOCKING (must fix before manufacturing):")
    if summary.blocking_count == 0:
        print("  (none)")
    else:
        for type_str, count in summary.blocking_by_type.most_common():
            print(f"  X {count} {type_str}")

    if blocking_only:
        print(f"\n{'=' * 60}")
        print(f"VERDICT: {summary.verdict}")
        return

    # WARNINGS section
    print("\nWARNINGS (should review):")
    if summary.warning_count == 0:
        print("  (none)")
    else:
        for type_str, count in summary.warning_by_type.most_common():
            print(f"  ! {count} {type_str}")

        # Show unconnected items breakdown
        if summary.unconnected_by_net:
            top_nets = summary.unconnected_by_net.most_common(5)
            net_summary = ", ".join(f"{net}: {count}" for net, count in top_nets)
            print(f"      {net_summary}")

    # COSMETIC section
    print("\nCOSMETIC (can ignore):")
    if summary.cosmetic_count == 0:
        print("  (none)")
    else:
        for type_str, count in summary.cosmetic_by_type.most_common():
            print(f"  o {count} {type_str}")

    # Manufacturer comparison section
    if summary.manufacturer:
        print(f"\n{'-' * 60}")
        print(f"MANUFACTURER: {summary.manufacturer.upper()}")

        if summary.false_positives:
            print(f"\n  False positives ({len(summary.false_positives)}):")
            for fp in summary.false_positives[:5]:
                print(f"    - {fp.violation.type_str}: {fp.message}")
            if len(summary.false_positives) > 5:
                print(f"    ... and {len(summary.false_positives) - 5} more")

        if summary.true_violations:
            print(f"\n  Actual violations ({len(summary.true_violations)}):")
            for tv in summary.true_violations[:5]:
                print(
                    f"    X {tv.violation.type_str}: actual {tv.actual_value:.3f}mm "
                    f"< required {tv.manufacturer_limit:.3f}mm"
                )
            if len(summary.true_violations) > 5:
                print(f"    ... and {len(summary.true_violations) - 5} more")

    print(f"\n{'=' * 60}")
    print(f"VERDICT: {summary.verdict}")


def output_json(summary: DRCSummary) -> None:
    """Output summary as JSON."""
    print(json.dumps(summary.to_dict(), indent=2))


def run_drc_on_pcb(
    pcb_path: Path,
    keep_report: bool = False,
) -> DRCReport | None:
    """Run DRC on a PCB and return parsed report."""
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return None

    # Check for kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found", file=sys.stderr)
        print("Install KiCad 8 from: https://www.kicad.org/download/", file=sys.stderr)
        print("\nmacOS: brew install --cask kicad", file=sys.stderr)
        return None

    print(f"Running DRC on: {pcb_path.name}")

    result = run_drc(pcb_path, None)

    if not result.success:
        print(f"Error running DRC: {result.stderr}", file=sys.stderr)
        return None

    # Parse the report
    try:
        report = DRCReport.load(result.output_path)
    except Exception as e:
        print(f"Error parsing DRC report: {e}", file=sys.stderr)
        return None

    # Cleanup temporary file unless keeping
    if not keep_report and result.output_path:
        result.output_path.unlink(missing_ok=True)

    return report


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-drc-summary command."""
    parser = argparse.ArgumentParser(
        prog="kicad-drc-summary",
        description="Structured DRC summary with severity levels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        help="PCB (.kicad_pcb) to check or DRC report (.json/.rpt) to parse",
    )
    parser.add_argument(
        "--fab",
        "-f",
        dest="manufacturer",
        choices=get_manufacturer_ids(),
        help="Compare against manufacturer rules (jlcpcb, oshpark, pcbway, seeed)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help="Number of copper layers (default: 2)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--blocking-only",
        action="store_true",
        help="Only show blocking issues",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 2 on warnings (when no blocking issues)",
    )
    parser.add_argument(
        "--keep-report",
        action="store_true",
        help="Keep the DRC report file after running (for PCB input)",
    )

    args = parser.parse_args(argv)
    input_path = Path(args.input)

    # Load or run DRC
    if input_path.suffix == ".kicad_pcb":
        report = run_drc_on_pcb(input_path, args.keep_report)
        if report is None:
            return 1
    elif input_path.suffix in (".json", ".rpt"):
        try:
            report = DRCReport.load(input_path)
        except FileNotFoundError:
            print(f"Error: File not found: {input_path}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error loading report: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Error: Unsupported file type: {input_path.suffix}", file=sys.stderr)
        print("Expected .kicad_pcb (PCB) or .json/.rpt (report)", file=sys.stderr)
        return 1

    # Create structured summary
    summary = create_summary(
        report,
        manufacturer_id=args.manufacturer,
        layers=args.layers,
    )

    # Output
    if args.format == "json":
        output_json(summary)
    else:
        output_table(summary, blocking_only=args.blocking_only)

    # Exit code
    if summary.has_blocking:
        return 1
    elif summary.warning_count > 0 and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
