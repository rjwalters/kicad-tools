#!/usr/bin/env python3
"""
Validate PCB design against manufacturer DRC rules.

Runs KiCad's DRC and checks results against manufacturing capabilities
for the specified manufacturer.

Usage:
    python3 scripts/kicad/validate-drc.py design.kicad_pcb
    python3 scripts/kicad/validate-drc.py design.kicad_pcb --manufacturer jlcpcb
    python3 scripts/kicad/validate-drc.py --rules --manufacturer seeed
    python3 scripts/kicad/validate-drc.py --strict  # Treat warnings as errors
    python3 scripts/kicad/validate-drc.py --compare  # Compare all manufacturers
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.manufacturers import (
    ManufacturerProfile,
    compare_design_rules,
    get_manufacturer_ids,
    get_profile,
)

# Project-specific design rules (optional overlay)
PROJECT_RULES = {
    "power_trace_width_mm": 0.5,  # Minimum for power nets
    "clock_trace_width_mm": 0.2,  # Clock distribution
    "clock_max_length_mm": 50,  # Keep clock traces short
}


@dataclass
class DRCViolation:
    """Represents a DRC violation."""

    type: str
    severity: str  # "error" or "warning"
    message: str
    pos_x: float = 0
    pos_y: float = 0
    item1: str = ""
    item2: str = ""


def find_kicad_cli() -> Path | None:
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


def run_kicad_drc(pcb_path: Path, output_path: Path, kicad_cli: Path) -> bool:
    """Run KiCad DRC and save report."""
    print(f"Running KiCad DRC on: {pcb_path.name}")

    try:
        result = subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "drc",
                "--output",
                str(output_path),
                "--format",
                "json",
                "--schematic-parity",
                "--units",
                "mm",
                str(pcb_path),
            ],
            capture_output=True,
            text=True,
        )

        # DRC returns non-zero if there are violations
        if result.returncode != 0 and not output_path.exists():
            print(f"DRC command failed: {result.stderr}")
            return False

        return True

    except subprocess.CalledProcessError as e:
        print(f"Error running DRC: {e}")
        return False


def parse_drc_report(report_path: Path) -> list[DRCViolation]:
    """Parse KiCad DRC JSON report."""
    violations = []

    if not report_path.exists():
        return violations

    with open(report_path) as f:
        data = json.load(f)

    # Parse violations from KiCad format
    for item in data.get("violations", []):
        violation = DRCViolation(
            type=item.get("type", "unknown"),
            severity=item.get("severity", "error"),
            message=item.get("description", ""),
            pos_x=item.get("pos", {}).get("x", 0),
            pos_y=item.get("pos", {}).get("y", 0),
            item1=item.get("items", [{}])[0].get("description", "") if item.get("items") else "",
            item2=item.get("items", [{}])[1].get("description", "")
            if len(item.get("items", [])) > 1
            else "",
        )
        violations.append(violation)

    return violations


def check_manufacturer_rules(
    violations: list[DRCViolation],
    profile: ManufacturerProfile,
    layers: int = 4,
) -> list[str]:
    """Check for violations of manufacturer-specific rules."""
    rules = profile.get_design_rules(layers)
    mfr_warnings = []

    for v in violations:
        msg = v.message.lower()

        # Check for undersized traces
        if "trace width" in msg or "track width" in msg:
            mfr_warnings.append(
                f"Trace width violation - {profile.name} requires minimum "
                f"{rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
            )

        # Check for undersized vias
        if "via" in msg and ("drill" in msg or "diameter" in msg):
            mfr_warnings.append(
                f"Via size violation - {profile.name} requires "
                f"{rules.min_via_drill_mm} mm drill / "
                f"{rules.min_via_diameter_mm} mm diameter minimum"
            )

        # Check for edge clearance
        if "edge" in msg or "outline" in msg:
            mfr_warnings.append(
                f"Edge clearance violation - {profile.name} requires "
                f"{rules.min_copper_to_edge_mm} mm minimum"
            )

        # Check for clearance violations
        if "clearance" in msg:
            mfr_warnings.append(
                f"Clearance violation - {profile.name} requires "
                f"{rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil) minimum"
            )

    return mfr_warnings


def print_summary(
    violations: list[DRCViolation],
    mfr_warnings: list[str],
    profile: ManufacturerProfile,
    strict: bool,
):
    """Print DRC summary."""
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    print(f"\n{'=' * 60}")
    print(f"DRC VALIDATION SUMMARY - {profile.name}")
    print(f"{'=' * 60}")

    print("\nKiCad DRC Results:")
    print(f"  Errors:   {len(errors)}")
    print(f"  Warnings: {len(warnings)}")

    if mfr_warnings:
        print(f"\n{profile.name} Compatibility:")
        for w in set(mfr_warnings):
            print(f"  ⚠ {w}")

    if errors:
        print(f"\n{'─' * 60}")
        print("ERRORS (must fix before manufacturing):")
        for v in errors:
            print(f"\n  ✗ {v.type}")
            print(f"    {v.message}")
            if v.item1:
                print(f"    Item: {v.item1}")
            if v.pos_x or v.pos_y:
                print(f"    Location: ({v.pos_x:.2f}, {v.pos_y:.2f}) mm")

    if warnings:
        print(f"\n{'─' * 60}")
        print("WARNINGS (review recommended):")
        for v in warnings[:10]:  # Limit output
            print(f"\n  ⚠ {v.type}")
            print(f"    {v.message}")
        if len(warnings) > 10:
            print(f"\n  ... and {len(warnings) - 10} more warnings")

    print(f"\n{'=' * 60}")

    if not errors and not warnings:
        print("✓ Design passes all DRC checks!")
        print(f"✓ Ready for {profile.name} manufacturing")
    elif errors:
        print("✗ Design has errors - fix before manufacturing")
    elif warnings and strict:
        print("⚠ Design has warnings (strict mode)")
    else:
        print("⚠ Design has warnings - review before manufacturing")


def print_manufacturer_rules(profile: ManufacturerProfile, layers: int = 4):
    """Print manufacturing rules for a manufacturer."""
    rules = profile.get_design_rules(layers)

    print(f"\n{'=' * 60}")
    print(f"{profile.name.upper()} {layers}-LAYER PCB CAPABILITIES")
    print(f"{'=' * 60}")

    print("\nMinimum Values:")
    print(
        f"  Trace width:      {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
    )
    print(
        f"  Trace spacing:    {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)"
    )
    print(f"  Via drill:        {rules.min_via_drill_mm} mm")
    print(f"  Via diameter:     {rules.min_via_diameter_mm} mm")
    print(f"  Annular ring:     {rules.min_annular_ring_mm} mm")
    print(f"  Copper-to-edge:   {rules.min_copper_to_edge_mm} mm")

    print("\nSilkscreen:")
    print(f"  Min line width:   {rules.min_silkscreen_width_mm} mm")
    print(f"  Min text height:  {rules.min_silkscreen_height_mm} mm")

    print("\nBoard Specifications:")
    print(f"  Thickness:        {rules.board_thickness_mm} mm")
    print(f"  Outer copper:     {rules.outer_copper_oz} oz")
    if rules.inner_copper_oz > 0:
        print(f"  Inner copper:     {rules.inner_copper_oz} oz")

    print(f"\nWebsite: {profile.website}")

    if profile.supports_assembly():
        print("\nAssembly: Supported")
        if profile.parts_library:
            print(f"Parts Library: {profile.parts_library.name}")
            print(f"Catalog: {profile.parts_library.catalog_url or 'N/A'}")
    else:
        print("\nAssembly: Not available (PCB only)")

    print(f"\n{'=' * 60}")


def print_comparison(layers: int = 4):
    """Print comparison of design rules across manufacturers."""
    rules_by_mfr = compare_design_rules(layers=layers)

    print(f"\n{'=' * 70}")
    print(f"MANUFACTURER COMPARISON - {layers}-LAYER PCB")
    print(f"{'=' * 70}")

    # Header
    mfrs = list(rules_by_mfr.keys())
    header = f"{'Constraint':<25}"
    for mfr in mfrs:
        header += f"{mfr.upper():>12}"
    print(header)
    print("-" * 70)

    # Trace width
    row = f"{'Trace width (mil)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_trace_width_mil:>12.1f}"
    print(row)

    # Clearance
    row = f"{'Clearance (mil)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_clearance_mil:>12.1f}"
    print(row)

    # Via drill
    row = f"{'Via drill (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_via_drill_mm:>12.2f}"
    print(row)

    # Via diameter
    row = f"{'Via diameter (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_via_diameter_mm:>12.2f}"
    print(row)

    # Copper to edge
    row = f"{'Copper-to-edge (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_copper_to_edge_mm:>12.2f}"
    print(row)

    print("-" * 70)

    # Assembly support
    row = f"{'Assembly':<25}"
    for mfr in mfrs:
        profile = get_profile(mfr)
        row += f"{'Yes':>12}" if profile.supports_assembly() else f"{'No':>12}"
    print(row)

    print(f"\n{'=' * 70}")


def main():
    parser = argparse.ArgumentParser(description="Validate PCB against manufacturer DRC rules")
    parser.add_argument("pcb", nargs="?", type=Path, help="Path to KiCad PCB file")
    parser.add_argument(
        "-m",
        "--manufacturer",
        default="seeed",
        choices=get_manufacturer_ids(),
        help="Target manufacturer (default: seeed)",
    )
    parser.add_argument(
        "-l", "--layers", type=int, default=4, help="Layer count for rules lookup (default: 4)"
    )
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument("--rules", action="store_true", help="Print manufacturer design rules")
    parser.add_argument(
        "--compare", action="store_true", help="Compare rules across all manufacturers"
    )
    parser.add_argument("--output", "-o", type=Path, help="DRC report output path")

    args = parser.parse_args()

    # Get manufacturer profile
    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Mode: Compare manufacturers
    if args.compare:
        print_comparison(args.layers)
        return

    # Mode: Print rules
    if args.rules:
        print_manufacturer_rules(profile, args.layers)
        return

    # Mode: Validate PCB
    if not args.pcb:
        parser.print_help()
        print("\nError: No PCB file specified")
        print("\nTo see design rules, run with --rules")
        print("To compare manufacturers, run with --compare")
        sys.exit(1)

    if not args.pcb.exists():
        print(f"Error: PCB file not found: {args.pcb}")
        print("Create the PCB layout in KiCad first.")
        print(f"\nTo see {profile.name} manufacturing rules, run with --rules")
        sys.exit(1)

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found")
        print("Install KiCad 8 from: https://www.kicad.org/download/")
        print("\nmacOS: brew install --cask kicad")
        print(f"\nTo see {profile.name} manufacturing rules, run with --rules")
        sys.exit(1)

    # Run DRC
    output_path = args.output or (args.pcb.parent / "drc_report.json")
    if not run_kicad_drc(args.pcb, output_path, kicad_cli):
        sys.exit(1)

    # Parse results
    violations = parse_drc_report(output_path)
    mfr_warnings = check_manufacturer_rules(violations, profile, args.layers)

    # Print summary
    print_summary(violations, mfr_warnings, profile, args.strict)

    # Exit code
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if errors:
        sys.exit(1)
    elif warnings and args.strict:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
