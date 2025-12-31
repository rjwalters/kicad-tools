#!/usr/bin/env python3
"""
Example: DRC Checking

Demonstrates how to parse and analyze KiCad DRC (Design Rule Check) reports
using the kicad-tools Python API.

Usage:
    python check_drc.py [drc_report_file]

If no file is specified, uses the included sample_drc.rpt.
"""

import sys
from pathlib import Path

from kicad_tools.drc import DRCReport, check_manufacturer_rules


def check_drc(report_path: Path) -> None:
    """Parse and analyze a DRC report."""
    print(f"Loading DRC report: {report_path}")
    print("=" * 70)

    report = DRCReport.load(report_path)

    # Report metadata
    print(f"\nPCB: {report.pcb_name}")
    if report.created_at:
        print(f"Created: {report.created_at}")

    # Summary
    print("\n=== Summary ===")
    print(f"Total violations: {report.violation_count}")
    print(f"  Errors: {report.error_count}")
    print(f"  Warnings: {report.warning_count}")
    if report.footprint_errors > 0:
        print(f"Footprint errors: {report.footprint_errors}")

    # Violations by type
    by_type = report.violations_by_type()
    if by_type:
        print("\n=== Violations by Type ===")
        for vtype, violations in sorted(by_type.items(), key=lambda x: -len(x[1])):
            print(f"  {vtype.value}: {len(violations)}")

    # Detailed error listing
    if report.errors:
        print("\n=== Errors (must fix) ===")
        for i, v in enumerate(report.errors[:10], 1):  # Show first 10
            print(f"\n{i}. [{v.type.value}] {v.message}")
            for loc in v.locations:
                print(f"   @ ({loc.x_mm:.2f}mm, {loc.y_mm:.2f}mm)")
            if v.items:
                for item in v.items[:2]:  # Show first 2 items
                    print(f"   - {item}")
        if len(report.errors) > 10:
            print(f"\n   ... and {len(report.errors) - 10} more errors")

    # Manufacturer comparison
    print("\n=== Manufacturer Compatibility ===")
    manufacturers = ["jlcpcb", "oshpark", "pcbway"]

    for mfr in manufacturers:
        checks = check_manufacturer_rules(report, mfr, layers=2)
        if checks:
            pass_count = sum(1 for c in checks if c.is_compatible)
            fail_count = sum(1 for c in checks if not c.is_compatible)
            total = len(checks)

            status = "COMPATIBLE" if fail_count == 0 else "ISSUES"
            print(f"\n{mfr.upper()}: {status}")
            print(f"  Checked: {total} rules")
            print(f"  Pass: {pass_count}, Fail: {fail_count}")

            if fail_count > 0:
                print("  Failures:")
                for check in checks:
                    if not check.is_compatible:
                        print(f"    - {check.message}")


def main() -> int:
    """Main entry point."""
    # Default to the included sample report
    if len(sys.argv) > 1:
        report_path = Path(sys.argv[1])
    else:
        report_path = Path(__file__).parent / "sample_drc.rpt"

    if not report_path.exists():
        print(f"Error: File not found: {report_path}", file=sys.stderr)
        return 1

    try:
        check_drc(report_path)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
