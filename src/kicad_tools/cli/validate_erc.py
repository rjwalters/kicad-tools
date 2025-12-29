#!/usr/bin/env python3
"""
Validate schematic design against Electrical Rules Check (ERC).

Runs KiCad's ERC and reports violations categorized by type and severity.

Usage:
    python3 scripts/kicad/validate-erc.py design.kicad_sch
    python3 scripts/kicad/validate-erc.py design.kicad_sch --strict
    python3 scripts/kicad/validate-erc.py design.kicad_sch --json
    python3 scripts/kicad/validate-erc.py design.kicad_sch --filter unconnected
    python3 scripts/kicad/validate-erc.py --list-types

Exit Codes:
    0 - No errors (warnings may be present)
    1 - Errors found or command failure
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ERC violation types (from KiCad documentation)
ERC_TYPES = {
    # Connection errors
    "pin_not_connected": "Unconnected pin",
    "pin_not_driven": "Input pin not driven",
    "power_pin_not_driven": "Power input not driven",
    "no_connect_connected": "No-connect pin is connected",
    "no_connect_dangling": "No-connect flag not connected to pin",
    # Pin conflicts
    "conflicting_netclass": "Conflicting netclass assignments",
    "different_unit_footprint": "Different footprint across symbol units",
    "different_unit_net": "Different nets on same pin across units",
    "duplicate_pin_error": "Duplicate pin in symbol",
    "duplicate_reference": "Duplicate reference designator",
    # Symbol/sheet errors
    "endpoint_off_grid": "Wire endpoint off grid",
    "extra_units": "Extra units in multi-unit symbol",
    "global_label_dangling": "Global label not connected",
    "hier_label_mismatch": "Hierarchical label mismatch",
    "label_dangling": "Label not connected",
    "lib_symbol_issues": "Library symbol issues",
    "missing_bidi_pin": "Missing bidirectional pin",
    "missing_input_pin": "Missing input pin",
    "missing_power_pin": "Missing power pin",
    "missing_unit": "Missing unit in multi-unit symbol",
    "multiple_net_names": "Wire has multiple net names",
    # Schematic structure
    "bus_entry_needed": "Bus entry needed",
    "bus_to_bus_conflict": "Bus to bus conflict",
    "bus_to_net_conflict": "Bus to net conflict",
    "four_way_junction": "Four-way wire junction",
    "net_not_bus_member": "Net label on bus wire",
    "similar_labels": "Similar labels (possible typo)",
    "simulation_model": "Simulation model issue",
    "unresolved_variable": "Unresolved text variable",
    "unannotated": "Symbol not annotated",
    "unspecified": "Unspecified error",
    "wire_dangling": "Wire not connected at both ends",
}


@dataclass
class ERCViolation:
    """Represents an ERC violation."""

    type: str
    severity: str  # "error", "warning", or "exclusion"
    description: str
    sheet: str = ""
    pos_x: float = 0
    pos_y: float = 0
    items: list = field(default_factory=list)
    excluded: bool = False

    @property
    def type_description(self) -> str:
        """Get human-readable type description."""
        return ERC_TYPES.get(self.type, self.type.replace("_", " ").title())

    @property
    def location_str(self) -> str:
        """Format location for display."""
        if self.sheet:
            return f"{self.sheet} at ({self.pos_x:.1f}, {self.pos_y:.1f})"
        elif self.pos_x or self.pos_y:
            return f"({self.pos_x:.1f}, {self.pos_y:.1f})"
        return ""


@dataclass
class ERCReport:
    """Parsed ERC report."""

    violations: list[ERCViolation] = field(default_factory=list)
    source_file: str = ""
    kicad_version: str = ""
    coordinate_units: str = "mm"

    @property
    def errors(self) -> list[ERCViolation]:
        return [v for v in self.violations if v.severity == "error" and not v.excluded]

    @property
    def warnings(self) -> list[ERCViolation]:
        return [v for v in self.violations if v.severity == "warning" and not v.excluded]

    @property
    def exclusions(self) -> list[ERCViolation]:
        return [v for v in self.violations if v.excluded]

    def by_type(self) -> dict[str, list[ERCViolation]]:
        """Group violations by type."""
        grouped = defaultdict(list)
        for v in self.violations:
            if not v.excluded:
                grouped[v.type].append(v)
        return dict(grouped)

    def by_sheet(self) -> dict[str, list[ERCViolation]]:
        """Group violations by sheet."""
        grouped = defaultdict(list)
        for v in self.violations:
            if not v.excluded:
                sheet = v.sheet or "root"
                grouped[sheet].append(v)
        return dict(grouped)


def find_kicad_cli() -> Optional[Path]:
    """Find kicad-cli executable."""
    locations = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/homebrew/bin/kicad-cli",
    ]

    for loc in locations:
        if Path(loc).exists():
            return Path(loc)

    try:
        result = subprocess.run(["which", "kicad-cli"], capture_output=True, text=True)
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass

    return None


def run_kicad_erc(
    sch_path: Path,
    output_path: Path,
    kicad_cli: Path,
    severity_all: bool = True,
) -> tuple[bool, str]:
    """
    Run KiCad ERC and save report.

    Returns:
        Tuple of (success, error_message)
    """
    print(f"Running KiCad ERC on: {sch_path.name}")

    cmd = [
        str(kicad_cli),
        "sch",
        "erc",
        "--output",
        str(output_path),
        "--format",
        "json",
        "--units",
        "mm",
    ]

    if severity_all:
        cmd.append("--severity-all")

    cmd.append(str(sch_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        # ERC may return non-zero if there are violations
        if not output_path.exists():
            return False, result.stderr or "ERC produced no output"

        return True, ""

    except subprocess.CalledProcessError as e:
        return False, str(e)
    except FileNotFoundError as e:
        return False, f"kicad-cli not found: {e}"


def parse_erc_report(report_path: Path) -> ERCReport:
    """Parse KiCad ERC JSON report."""
    report = ERCReport()

    if not report_path.exists():
        return report

    with open(report_path) as f:
        data = json.load(f)

    report.source_file = data.get("source", "")
    report.kicad_version = data.get("kicad_version", "")
    report.coordinate_units = data.get("coordinate_units", "mm")

    # Parse violations
    for sheet_data in data.get("sheets", []):
        sheet_path = sheet_data.get("path", "")
        _sheet_uuid = sheet_data.get("uuid_path", "")  # noqa: F841 - preserved for future use

        for item in sheet_data.get("violations", []):
            violation = ERCViolation(
                type=item.get("type", "unknown"),
                severity=item.get("severity", "error"),
                description=item.get("description", ""),
                sheet=sheet_path,
                pos_x=item.get("pos", {}).get("x", 0),
                pos_y=item.get("pos", {}).get("y", 0),
                items=[i.get("description", "") for i in item.get("items", [])],
                excluded=item.get("excluded", False),
            )
            report.violations.append(violation)

    return report


def _matches_filter(violation: ERCViolation, filter_type: str) -> bool:
    """Check if violation matches filter (searches type, description, and type_description)."""
    filter_lower = filter_type.lower()
    return (
        filter_lower in violation.type.lower()
        or filter_lower in violation.description.lower()
        or filter_lower in violation.type_description.lower()
    )


def print_summary(
    report: ERCReport,
    verbose: bool = False,
    filter_type: Optional[str] = None,
    group_by_sheet: bool = False,
):
    """Print ERC summary."""
    errors = report.errors
    warnings = report.warnings
    exclusions = report.exclusions

    # Apply filter
    if filter_type:
        errors = [e for e in errors if _matches_filter(e, filter_type)]
        warnings = [w for w in warnings if _matches_filter(w, filter_type)]

    print(f"\n{'=' * 60}")
    print("ERC VALIDATION SUMMARY")
    print(f"{'=' * 60}")

    if report.source_file:
        print(f"File: {Path(report.source_file).name}")
    if report.kicad_version:
        print(f"KiCad: {report.kicad_version}")

    print("\nResults:")
    print(f"  Errors:     {len(errors)}")
    print(f"  Warnings:   {len(warnings)}")
    if exclusions:
        print(f"  Excluded:   {len(exclusions)} (not counted)")

    if filter_type:
        print(f"\n  (filtered by: {filter_type})")

    # Group by type summary (filtered)
    if filter_type:
        filtered_violations = [
            v for v in report.violations if not v.excluded and _matches_filter(v, filter_type)
        ]
        by_type = defaultdict(list)
        for v in filtered_violations:
            by_type[v.type].append(v)
        by_type = dict(by_type)
    else:
        by_type = report.by_type()

    if by_type:
        print(f"\n{'─' * 60}")
        print("BY TYPE:")
        for vtype, violations in sorted(by_type.items(), key=lambda x: -len(x[1])):
            type_errors = sum(1 for v in violations if v.severity == "error")
            type_warns = sum(1 for v in violations if v.severity == "warning")
            desc = ERC_TYPES.get(vtype, vtype)
            counts = []
            if type_errors:
                counts.append(f"{type_errors} error{'s' if type_errors != 1 else ''}")
            if type_warns:
                counts.append(f"{type_warns} warning{'s' if type_warns != 1 else ''}")
            print(f"  {desc}: {', '.join(counts)}")

    # Detailed output
    if errors:
        print(f"\n{'─' * 60}")
        print("ERRORS (must fix):")
        _print_violations(errors, verbose, group_by_sheet)

    if warnings:
        print(f"\n{'─' * 60}")
        print("WARNINGS (review recommended):")
        _print_violations(warnings[:20] if not verbose else warnings, verbose, group_by_sheet)
        if len(warnings) > 20 and not verbose:
            print(f"\n  ... and {len(warnings) - 20} more warnings (use --verbose)")

    print(f"\n{'=' * 60}")

    if not errors and not warnings:
        print("ERC PASSED - No violations found")
    elif errors:
        print("ERC FAILED - Fix errors before proceeding")
    else:
        print("ERC WARNING - Review warnings")


def _print_violations(violations: list[ERCViolation], verbose: bool, group_by_sheet: bool):
    """Print list of violations."""
    if group_by_sheet:
        by_sheet = defaultdict(list)
        for v in violations:
            by_sheet[v.sheet or "root"].append(v)

        for sheet, sheet_violations in sorted(by_sheet.items()):
            print(f"\n  [{sheet}]")
            for v in sheet_violations:
                _print_single_violation(v, verbose, indent="    ")
    else:
        for v in violations:
            _print_single_violation(v, verbose, indent="  ")


def _print_single_violation(v: ERCViolation, verbose: bool, indent: str = "  "):
    """Print a single violation."""
    symbol = "✗" if v.severity == "error" else "⚠"
    print(f"\n{indent}{symbol} {v.type_description}")
    print(f"{indent}  {v.description}")

    if verbose:
        if v.items:
            for item in v.items:
                print(f"{indent}  → {item}")
        if v.location_str:
            print(f"{indent}  Location: {v.location_str}")


def print_json_output(report: ERCReport, filter_type: Optional[str] = None):
    """Print machine-readable JSON output."""
    violations = [v for v in report.violations if not v.excluded]

    if filter_type:
        violations = [v for v in violations if _matches_filter(v, filter_type)]

    output = {
        "source": report.source_file,
        "kicad_version": report.kicad_version,
        "summary": {
            "errors": len([v for v in violations if v.severity == "error"]),
            "warnings": len([v for v in violations if v.severity == "warning"]),
        },
        "violations": [
            {
                "type": v.type,
                "type_description": v.type_description,
                "severity": v.severity,
                "description": v.description,
                "sheet": v.sheet,
                "position": {"x": v.pos_x, "y": v.pos_y},
                "items": v.items,
            }
            for v in violations
        ],
    }

    print(json.dumps(output, indent=2))


def print_types():
    """Print all known ERC violation types."""
    print("\nKnown ERC Violation Types:")
    print("=" * 60)

    # Group by category
    categories = {
        "Connection": [
            "pin_not_connected",
            "pin_not_driven",
            "power_pin_not_driven",
            "no_connect_connected",
            "no_connect_dangling",
        ],
        "Pin Conflicts": [
            "conflicting_netclass",
            "different_unit_footprint",
            "different_unit_net",
            "duplicate_pin_error",
            "duplicate_reference",
        ],
        "Labels": [
            "global_label_dangling",
            "hier_label_mismatch",
            "label_dangling",
            "multiple_net_names",
            "similar_labels",
        ],
        "Structure": [
            "bus_entry_needed",
            "bus_to_bus_conflict",
            "bus_to_net_conflict",
            "endpoint_off_grid",
            "four_way_junction",
            "net_not_bus_member",
            "wire_dangling",
        ],
        "Symbols": [
            "extra_units",
            "lib_symbol_issues",
            "missing_bidi_pin",
            "missing_input_pin",
            "missing_power_pin",
            "missing_unit",
            "simulation_model",
            "unannotated",
        ],
        "Other": ["unresolved_variable", "unspecified"],
    }

    for category, types in categories.items():
        print(f"\n{category}:")
        for t in types:
            desc = ERC_TYPES.get(t, t)
            print(f"  {t:30} {desc}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate schematic against ERC rules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "schematic", nargs="?", type=Path, help="Path to KiCad schematic file (.kicad_sch)"
    )
    parser.add_argument("--strict", action="store_true", help="Exit with error code on warnings")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed violation information"
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--filter", "-f", type=str, metavar="TYPE", help="Filter violations by type (partial match)"
    )
    parser.add_argument("--by-sheet", action="store_true", help="Group violations by sheet")
    parser.add_argument("--output", "-o", type=Path, help="ERC report output path")
    parser.add_argument(
        "--list-types", action="store_true", help="List all known ERC violation types"
    )
    parser.add_argument(
        "--keep-report", action="store_true", help="Keep the JSON report file after completion"
    )

    args = parser.parse_args()

    # Mode: List types
    if args.list_types:
        print_types()
        return 0

    # Validate schematic path
    if not args.schematic:
        # Try default paths
        defaults = [
            REPO_ROOT / "hardware/chorus-revA/kicad/chorus-revA.kicad_sch",
            REPO_ROOT / "hardware/chorus-test-revA/kicad/chorus-test-revA.kicad_sch",
        ]
        for default in defaults:
            if default.exists():
                args.schematic = default
                break

        if not args.schematic:
            parser.print_help()
            print("\nError: No schematic file specified")
            return 1

    if not args.schematic.exists():
        print(f"Error: Schematic not found: {args.schematic}")
        return 1

    if not args.schematic.suffix == ".kicad_sch":
        print(f"Error: Not a schematic file: {args.schematic}")
        print("Expected .kicad_sch extension")
        return 1

    # Find kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found")
        print("Install KiCad 8 from: https://www.kicad.org/download/")
        print("\nmacOS: brew install --cask kicad")
        return 1

    # Run ERC
    output_path = args.output or (args.schematic.parent / f"{args.schematic.stem}-erc.json")
    success, error = run_kicad_erc(args.schematic, output_path, kicad_cli)

    if not success:
        print(f"Error running ERC: {error}")
        return 1

    # Parse results
    report = parse_erc_report(output_path)

    # Output
    if args.json:
        print_json_output(report, args.filter)
    else:
        print_summary(report, args.verbose, args.filter, args.by_sheet)

    # Cleanup
    if not args.keep_report and output_path.exists():
        output_path.unlink()

    # Exit code
    if report.errors:
        return 1
    elif report.warnings and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
