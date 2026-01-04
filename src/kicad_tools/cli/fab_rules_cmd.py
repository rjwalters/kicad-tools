#!/usr/bin/env python3
"""
Manufacturer Design Rules CLI Tool (kicad-fab-rules).

Manage manufacturer design rule profiles, compare project rules against
manufacturer capabilities, and configure KiCad projects for specific manufacturers.

Usage:
    kicad-fab-rules list                           # List available profiles
    kicad-fab-rules show jlcpcb                    # Show profile details
    kicad-fab-rules compare jlcpcb project.kicad_pro  # Compare project vs manufacturer
    kicad-fab-rules apply jlcpcb project.kicad_pro    # Apply rules to project
    kicad-fab-rules export jlcpcb --format json    # Export rules as JSON
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_tools.core.project_file import get_design_settings, load_project
from kicad_tools.manufacturers import (
    DesignRules,
    get_profile,
    list_manufacturers,
)


@dataclass
class ProjectRules:
    """Design rules extracted from a KiCad project file."""

    min_clearance_mm: float | None = None
    min_track_width_mm: float | None = None
    min_via_diameter_mm: float | None = None
    min_via_drill_mm: float | None = None
    min_annular_ring_mm: float | None = None
    min_hole_diameter_mm: float | None = None
    min_copper_to_edge_mm: float | None = None

    @classmethod
    def from_project(cls, data: dict[str, Any]) -> "ProjectRules":
        """Extract design rules from project data."""
        settings = get_design_settings(data) if "board" in data else {}
        rules = settings.get("rules", {})
        defaults = settings.get("defaults", {})

        return cls(
            min_clearance_mm=rules.get("min_clearance") or defaults.get("clearance_min"),
            min_track_width_mm=rules.get("min_track_width") or defaults.get("track_min_width"),
            min_via_diameter_mm=rules.get("min_via_diameter") or defaults.get("via_min_diameter"),
            min_via_drill_mm=rules.get("min_via_hole") or defaults.get("via_min_drill"),
            min_annular_ring_mm=rules.get("min_via_annular_width"),
            min_hole_diameter_mm=rules.get("min_through_hole_diameter"),
            min_copper_to_edge_mm=rules.get("min_copper_edge_clearance"),
        )


@dataclass
class RuleComparison:
    """Comparison result for a single rule."""

    name: str
    project_value: float | None
    manufacturer_value: float
    status: str  # "ok", "stricter", "loose", "missing"
    recommendation: str | None = None


def compare_rules(project_rules: ProjectRules, mfr_rules: DesignRules) -> list[RuleComparison]:
    """Compare project rules against manufacturer requirements.

    Args:
        project_rules: Rules from the KiCad project
        mfr_rules: Manufacturer's minimum requirements

    Returns:
        List of RuleComparison objects
    """
    comparisons = []

    # Helper to compare values (lower is stricter for clearances/widths)
    def compare_min(name: str, project_val: float | None, mfr_val: float) -> RuleComparison:
        if project_val is None:
            return RuleComparison(
                name=name,
                project_value=None,
                manufacturer_value=mfr_val,
                status="missing",
                recommendation=f"Set to at least {mfr_val:.4f}mm",
            )

        if project_val < mfr_val:
            return RuleComparison(
                name=name,
                project_value=project_val,
                manufacturer_value=mfr_val,
                status="loose",
                recommendation=f"Increase to at least {mfr_val:.4f}mm - design won't manufacture!",
            )

        # Check if significantly stricter (>50% margin)
        if project_val > mfr_val * 1.5:
            return RuleComparison(
                name=name,
                project_value=project_val,
                manufacturer_value=mfr_val,
                status="stricter",
                recommendation=f"Consider relaxing to {mfr_val:.4f}mm to reduce false DRC positives",
            )

        return RuleComparison(
            name=name,
            project_value=project_val,
            manufacturer_value=mfr_val,
            status="ok",
        )

    # Compare each rule
    comparisons.append(
        compare_min(
            "Min trace width", project_rules.min_track_width_mm, mfr_rules.min_trace_width_mm
        )
    )
    comparisons.append(
        compare_min("Min clearance", project_rules.min_clearance_mm, mfr_rules.min_clearance_mm)
    )
    comparisons.append(
        compare_min("Min via drill", project_rules.min_via_drill_mm, mfr_rules.min_via_drill_mm)
    )
    comparisons.append(
        compare_min(
            "Min via diameter", project_rules.min_via_diameter_mm, mfr_rules.min_via_diameter_mm
        )
    )
    comparisons.append(
        compare_min(
            "Min annular ring", project_rules.min_annular_ring_mm, mfr_rules.min_annular_ring_mm
        )
    )
    comparisons.append(
        compare_min(
            "Min hole diameter",
            project_rules.min_hole_diameter_mm,
            mfr_rules.min_hole_diameter_mm,
        )
    )
    comparisons.append(
        compare_min(
            "Copper to edge", project_rules.min_copper_to_edge_mm, mfr_rules.min_copper_to_edge_mm
        )
    )

    return comparisons


def cmd_list(args):
    """List available manufacturer profiles."""
    if args.format == "json":
        profiles = [
            {
                "id": profile.id,
                "name": profile.name,
                "website": profile.website,
                "assembly": profile.supports_assembly(),
                "parts_library": profile.parts_library.name if profile.parts_library else None,
            }
            for profile in list_manufacturers()
        ]
        print(json.dumps(profiles, indent=2))
        return 0

    # Table format
    print("\nAvailable Manufacturer Profiles")
    print("=" * 60)
    print(f"{'ID':<12} {'Name':<20} {'Assembly':<12} {'Parts Library'}")
    print("-" * 60)

    for profile in list_manufacturers():
        assembly = "Yes" if profile.supports_assembly() else "No"
        parts = profile.parts_library.name if profile.parts_library else "-"
        print(f"{profile.id:<12} {profile.name:<20} {assembly:<12} {parts}")

    print("-" * 60)
    print("\nUse 'kicad-fab-rules show <id>' for profile details")
    return 0


def cmd_show(args):
    """Show manufacturer profile details."""
    try:
        profile = get_profile(args.profile)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    rules = profile.get_design_rules(args.layers, args.copper)

    if args.format == "json":
        data = {
            "profile": profile.to_dict(),
            "design_rules": rules.to_dict(),
        }
        print(json.dumps(data, indent=2))
        return 0

    # Text format
    print(f"\n{'=' * 60}")
    print(f"{profile.name.upper()} Design Rules")
    print(f"{'=' * 60}")

    print(f"\nProfile: {profile.id}")
    print(f"Website: {profile.website}")
    print(f"Configuration: {args.layers}-layer, {args.copper}oz copper")

    print(f"\n{'Trace & Spacing':─^40}")
    print(
        f"  Min trace width:    {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
    )
    print(
        f"  Min clearance:      {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)"
    )

    print(f"\n{'Vias':─^40}")
    print(f"  Min via drill:      {rules.min_via_drill_mm:.3f} mm")
    print(f"  Min via diameter:   {rules.min_via_diameter_mm:.3f} mm")
    print(f"  Min annular ring:   {rules.min_annular_ring_mm:.3f} mm")

    print(f"\n{'Holes':─^40}")
    print(f"  Min hole diameter:  {rules.min_hole_diameter_mm:.3f} mm")
    print(f"  Max hole diameter:  {rules.max_hole_diameter_mm:.3f} mm")

    print(f"\n{'Edge Clearance':─^40}")
    print(f"  Copper to edge:     {rules.min_copper_to_edge_mm:.3f} mm")
    print(f"  Hole to edge:       {rules.min_hole_to_edge_mm:.3f} mm")

    print(f"\n{'Silkscreen':─^40}")
    print(f"  Min line width:     {rules.min_silkscreen_width_mm:.3f} mm")
    print(f"  Min text height:    {rules.min_silkscreen_height_mm:.3f} mm")

    print(f"\n{'Solder Mask':─^40}")
    print(f"  Min dam width:      {rules.min_solder_mask_dam_mm:.3f} mm")

    print(f"\n{'=' * 60}")
    return 0


def cmd_compare(args):
    """Compare project rules against manufacturer requirements."""
    try:
        profile = get_profile(args.profile)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: Project file not found: {project_path}", file=sys.stderr)
        return 1

    if project_path.suffix != ".kicad_pro":
        print(f"Error: Expected .kicad_pro file, got: {project_path.suffix}", file=sys.stderr)
        return 1

    try:
        data = load_project(project_path)
    except Exception as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        return 1

    project_rules = ProjectRules.from_project(data)
    mfr_rules = profile.get_design_rules(args.layers, args.copper)
    comparisons = compare_rules(project_rules, mfr_rules)

    if args.format == "json":
        result = {
            "project": str(project_path),
            "manufacturer": profile.id,
            "layers": args.layers,
            "copper_oz": args.copper,
            "comparisons": [
                {
                    "name": c.name,
                    "project_value": c.project_value,
                    "manufacturer_value": c.manufacturer_value,
                    "status": c.status,
                    "recommendation": c.recommendation,
                }
                for c in comparisons
            ],
            "compatible": all(c.status != "loose" for c in comparisons),
        }
        print(json.dumps(result, indent=2))
        return 0

    # Text format
    print(f"\nDesign Rules Comparison: {project_path.name} vs {profile.name.upper()}")
    print("=" * 70)
    print(f"\n{'Constraint':<20} {'Project':>12} {profile.id.upper():>12} {'Status':>12}")
    print("-" * 70)

    for c in comparisons:
        project_str = f"{c.project_value:.4f}mm" if c.project_value else "Not set"
        mfr_str = f"{c.manufacturer_value:.4f}mm"

        if c.status == "ok":
            status = "✓ OK"
        elif c.status == "stricter":
            status = "⚠ Stricter"
        elif c.status == "loose":
            status = "✗ Too loose"
        else:
            status = "? Missing"

        print(f"{c.name:<20} {project_str:>12} {mfr_str:>12} {status:>12}")

    # Summary
    print("-" * 70)

    has_issues = any(c.status == "loose" for c in comparisons)
    missing = [c for c in comparisons if c.status == "missing"]
    stricter = [c for c in comparisons if c.status == "stricter"]

    if has_issues:
        print("\n✗ INCOMPATIBLE - Project rules are looser than manufacturer minimums!")
        print("\nRecommendations:")
        for c in comparisons:
            if c.status == "loose" and c.recommendation:
                print(f"  • {c.name}: {c.recommendation}")
    elif missing:
        print("\n⚠ NEEDS REVIEW - Some rules are not set in project")
        print("\nRecommendations:")
        for c in missing:
            if c.recommendation:
                print(f"  • {c.name}: {c.recommendation}")
    else:
        print("\n✓ COMPATIBLE - Your rules meet manufacturer requirements")

        if stricter:
            print("\nOptional relaxations (to reduce false DRC positives):")
            for c in stricter:
                if c.recommendation:
                    print(f"  • {c.name}: {c.recommendation}")

    print(f"\n{'=' * 70}")

    return 1 if has_issues else 0


def cmd_apply(args):
    """Apply manufacturer rules to project."""
    from kicad_tools.core.project_file import (
        apply_manufacturer_rules,
        save_project,
        set_manufacturer_metadata,
    )

    try:
        profile = get_profile(args.profile)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: Project file not found: {project_path}", file=sys.stderr)
        return 1

    if project_path.suffix != ".kicad_pro":
        print(f"Error: Expected .kicad_pro file, got: {project_path.suffix}", file=sys.stderr)
        return 1

    try:
        data = load_project(project_path)
    except Exception as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        return 1

    rules = profile.get_design_rules(args.layers, args.copper)

    print(f"\nApplying {profile.name.upper()} Design Rules")
    print("=" * 60)
    print(f"\nFile: {project_path}")
    print(f"Configuration: {args.layers}-layer, {args.copper}oz copper")

    print("\nRules to apply:")
    print(
        f"  Min clearance:     {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)"
    )
    print(
        f"  Min trace width:   {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
    )
    print(f"  Min via diameter:  {rules.min_via_diameter_mm:.3f} mm")
    print(f"  Min via drill:     {rules.min_via_drill_mm:.3f} mm")
    print(f"  Min annular ring:  {rules.min_annular_ring_mm:.3f} mm")
    print(f"  Copper to edge:    {rules.min_copper_to_edge_mm:.3f} mm")

    if args.dry_run:
        print("\n(dry run - no changes made)")
        print(f"\n{'=' * 60}")
        return 0

    # Apply rules
    apply_manufacturer_rules(
        data,
        min_clearance_mm=rules.min_clearance_mm,
        min_track_width_mm=rules.min_trace_width_mm,
        min_via_diameter_mm=rules.min_via_diameter_mm,
        min_via_drill_mm=rules.min_via_drill_mm,
        min_annular_ring_mm=rules.min_annular_ring_mm,
        min_hole_diameter_mm=rules.min_hole_diameter_mm,
        min_copper_to_edge_mm=rules.min_copper_to_edge_mm,
    )

    # Set manufacturer metadata
    set_manufacturer_metadata(
        data,
        manufacturer_id=profile.id,
        layers=args.layers,
        copper_oz=args.copper,
    )

    # Save
    output_path = Path(args.output) if args.output else project_path
    try:
        save_project(data, output_path)
        print(f"\n✓ Project updated: {output_path}")
        print(f"  Manufacturer: {profile.name} ({profile.id})")
    except Exception as e:
        print(f"\nError saving project: {e}", file=sys.stderr)
        return 1

    print(f"\n{'=' * 60}")
    return 0


def cmd_export(args):
    """Export manufacturer rules."""
    try:
        profile = get_profile(args.profile)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    rules = profile.get_design_rules(args.layers, args.copper)

    if args.format == "json":
        data = {
            "manufacturer": profile.id,
            "name": profile.name,
            "layers": args.layers,
            "copper_oz": args.copper,
            "rules": rules.to_dict(),
        }

        if args.output:
            output_path = Path(args.output)
            output_path.write_text(json.dumps(data, indent=2))
            print(f"Rules exported to: {output_path}")
        else:
            print(json.dumps(data, indent=2))

    elif args.format == "kicad_dru":
        dru_content = f"""(version 1)
(rule "Trace Width - {profile.name}"
  (constraint track_width (min {rules.min_trace_width_mm}mm)))
(rule "Clearance - {profile.name}"
  (constraint clearance (min {rules.min_clearance_mm}mm)))
(rule "Via Drill - {profile.name}"
  (constraint hole_size (min {rules.min_via_drill_mm}mm)))
(rule "Via Diameter - {profile.name}"
  (constraint via_diameter (min {rules.min_via_diameter_mm}mm)))
(rule "Annular Ring - {profile.name}"
  (constraint annular_width (min {rules.min_annular_ring_mm}mm)))
(rule "Copper to Edge - {profile.name}"
  (constraint edge_clearance (min {rules.min_copper_to_edge_mm}mm)))
"""

        if args.output:
            output_path = Path(args.output)
            output_path.write_text(dru_content)
            print(f"DRC rules exported to: {output_path}")
        else:
            print(dru_content)

    return 0


def main(argv=None):
    """Main entry point for kicad-fab-rules CLI."""
    parser = argparse.ArgumentParser(
        prog="kicad-fab-rules",
        description="Manufacturer design rule profiles for KiCad projects",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  kicad-fab-rules list                              List available profiles
  kicad-fab-rules show jlcpcb                       Show JLCPCB design rules
  kicad-fab-rules show jlcpcb --layers 4            Show 4-layer rules
  kicad-fab-rules compare jlcpcb project.kicad_pro  Compare project vs JLCPCB
  kicad-fab-rules apply jlcpcb project.kicad_pro    Apply JLCPCB rules to project
  kicad-fab-rules export jlcpcb --format json       Export rules as JSON
  kicad-fab-rules export jlcpcb --format kicad_dru  Export as KiCad DRC rules
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # list
    p_list = subparsers.add_parser("list", help="List available manufacturer profiles")
    p_list.add_argument("--format", choices=["table", "json"], default="table")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subparsers.add_parser("show", help="Show manufacturer profile details")
    p_show.add_argument("profile", help="Profile ID (jlcpcb, oshpark, pcbway, seeed)")
    p_show.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    p_show.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_show.add_argument("--format", choices=["text", "json"], default="text")
    p_show.set_defaults(func=cmd_show)

    # compare
    p_compare = subparsers.add_parser(
        "compare",
        help="Compare project rules against manufacturer requirements",
        description="Compare a KiCad project's design rules against manufacturer capabilities. "
        "Shows which rules are compatible, too strict, or too loose.",
    )
    p_compare.add_argument("profile", help="Manufacturer profile ID")
    p_compare.add_argument("project", help="Path to .kicad_pro file")
    p_compare.add_argument("-l", "--layers", type=int, default=2, help="Layer count")
    p_compare.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_compare.add_argument("--format", choices=["text", "json"], default="text")
    p_compare.set_defaults(func=cmd_compare)

    # apply
    p_apply = subparsers.add_parser(
        "apply",
        help="Apply manufacturer rules to project",
        description="Apply manufacturer design rules to a KiCad project file. "
        "Updates the project's design settings to match manufacturer requirements.",
    )
    p_apply.add_argument("profile", help="Manufacturer profile ID")
    p_apply.add_argument("project", help="Path to .kicad_pro file")
    p_apply.add_argument("-l", "--layers", type=int, default=2, help="Layer count")
    p_apply.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_apply.add_argument("-o", "--output", help="Output file (default: modify in place)")
    p_apply.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    p_apply.set_defaults(func=cmd_apply)

    # export
    p_export = subparsers.add_parser(
        "export",
        help="Export manufacturer rules",
        description="Export manufacturer design rules in various formats.",
    )
    p_export.add_argument("profile", help="Manufacturer profile ID")
    p_export.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    p_export.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_export.add_argument(
        "--format",
        choices=["json", "kicad_dru"],
        default="json",
        help="Output format",
    )
    p_export.add_argument("-o", "--output", help="Output file (default: stdout)")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
