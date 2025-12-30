#!/usr/bin/env python3
"""
PCB Manufacturer CLI Tool.

Manage manufacturer profiles, compare design rules, and configure
KiCad projects for specific manufacturers.

Usage:
    python3 scripts/kicad/mfr.py list                    # List manufacturers
    python3 scripts/kicad/mfr.py info jlcpcb             # Show manufacturer details
    python3 scripts/kicad/mfr.py rules jlcpcb            # Show design rules
    python3 scripts/kicad/mfr.py rules jlcpcb --layers 2 # 2-layer rules
    python3 scripts/kicad/mfr.py compare                 # Compare all manufacturers
    python3 scripts/kicad/mfr.py compare --layers 6      # Compare 6-layer rules
    python3 scripts/kicad/mfr.py export-dru jlcpcb       # Export KiCad DRC rules
    python3 scripts/kicad/mfr.py apply-rules proj.kicad_pro jlcpcb  # Apply rules
    python3 scripts/kicad/mfr.py validate board.kicad_pcb jlcpcb    # Validate design
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple

from kicad_tools.manufacturers import (
    compare_design_rules,
    find_compatible_manufacturers,
    get_profile,
    list_manufacturers,
)


def cmd_list(args):
    """List available manufacturers."""
    print(f"\n{'=' * 60}")
    print("AVAILABLE MANUFACTURERS")
    print(f"{'=' * 60}\n")

    for profile in list_manufacturers():
        assembly = "PCBA" if profile.supports_assembly() else "PCB only"
        parts = profile.parts_library.name if profile.parts_library else "N/A"
        print(f"  {profile.id:<12} {profile.name:<20} [{assembly}]")
        if profile.parts_library:
            print(f"  {'':<12} Parts: {parts}")
        print()

    print(f"{'=' * 60}")
    print("Use 'mfr.py info <id>' for detailed information")
    print("Use 'mfr.py rules <id>' for design rules")


def cmd_info(args):
    """Show detailed manufacturer information."""
    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"{profile.name.upper()}")
    print(f"{'=' * 60}")

    print("\nBasic Info:")
    print(f"  ID:       {profile.id}")
    print(f"  Website:  {profile.website}")
    print(f"  Layers:   {', '.join(str(layer) for layer in profile.supported_layers)}")
    print(f"  Pricing:  {profile.pricing_model}")

    print("\nLead Times:")
    for key, days in profile.lead_times.items():
        print(f"  {key.replace('_', ' ').title()}: {days} days")

    if profile.supports_assembly():
        print("\nAssembly: Supported")
        if profile.assembly:
            print(f"  Min component pitch: {profile.assembly.min_component_pitch_mm} mm")
            print(f"  Min BGA pitch: {profile.assembly.min_bga_pitch_mm} mm")
            print(f"  Double-sided: {'Yes' if profile.assembly.supports_double_sided else 'No'}")
    else:
        print("\nAssembly: Not available")

    if profile.parts_library:
        lib = profile.parts_library
        print(f"\nParts Library: {lib.name}")
        if lib.catalog_url:
            print(f"  Catalog: {lib.catalog_url}")
        print("  Tiers:")
        for tier, info in lib.tiers.items():
            desc = info.get("description", "")
            lead = info.get("lead_time_days", "?")
            fee = info.get("setup_fee_usd", 0)
            print(f"    {tier}: {desc}")
            print(f"          Lead time: {lead} days, Setup: ${fee}")

    print(f"\n{'=' * 60}")


def cmd_rules(args):
    """Show design rules for a manufacturer."""
    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    rules = profile.get_design_rules(args.layers, args.copper)

    print(f"\n{'=' * 60}")
    print(f"{profile.name.upper()} - {args.layers}-LAYER {args.copper}oz RULES")
    print(f"{'=' * 60}")

    print("\nTrace & Spacing:")
    print(
        f"  Min trace width:    {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
    )
    print(
        f"  Min clearance:      {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)"
    )

    print("\nVias:")
    print(f"  Min via drill:      {rules.min_via_drill_mm} mm")
    print(f"  Min via diameter:   {rules.min_via_diameter_mm} mm")
    print(f"  Min annular ring:   {rules.min_annular_ring_mm} mm")

    print("\nHoles:")
    print(f"  Min hole diameter:  {rules.min_hole_diameter_mm} mm")
    print(f"  Max hole diameter:  {rules.max_hole_diameter_mm} mm")

    print("\nEdge Clearance:")
    print(f"  Copper to edge:     {rules.min_copper_to_edge_mm} mm")
    print(f"  Hole to edge:       {rules.min_hole_to_edge_mm} mm")

    print("\nSilkscreen:")
    print(f"  Min line width:     {rules.min_silkscreen_width_mm} mm")
    print(f"  Min text height:    {rules.min_silkscreen_height_mm} mm")

    print("\nSolder Mask:")
    print(f"  Min dam width:      {rules.min_solder_mask_dam_mm} mm")

    print("\nBoard:")
    print(f"  Thickness:          {rules.board_thickness_mm} mm")
    print(f"  Outer copper:       {rules.outer_copper_oz} oz")
    if rules.inner_copper_oz > 0:
        print(f"  Inner copper:       {rules.inner_copper_oz} oz")

    if args.json:
        print(f"\n{'─' * 60}")
        print("JSON:")
        print(json.dumps(rules.to_dict(), indent=2))

    print(f"\n{'=' * 60}")


def cmd_compare(args):
    """Compare design rules across manufacturers."""
    rules_by_mfr = compare_design_rules(
        layers=args.layers,
        copper_oz=args.copper,
    )

    print(f"\n{'=' * 70}")
    print(f"MANUFACTURER COMPARISON - {args.layers}-LAYER {args.copper}oz")
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

    # Annular ring
    row = f"{'Annular ring (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_annular_ring_mm:>12.2f}"
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

    # Parts library
    row = f"{'Parts Library':<25}"
    for mfr in mfrs:
        profile = get_profile(mfr)
        lib = profile.parts_library.name if profile.parts_library else "None"
        row += f"{lib[:12]:>12}"
    print(row)

    print(f"\n{'=' * 70}")


def cmd_find(args):
    """Find compatible manufacturers for design constraints."""
    compatible = find_compatible_manufacturers(
        trace_width_mm=args.trace * 0.0254,  # mil to mm
        clearance_mm=args.clearance * 0.0254,
        via_drill_mm=args.via,
        layers=args.layers,
        needs_assembly=args.assembly,
    )

    print(f"\n{'=' * 60}")
    print("COMPATIBLE MANUFACTURERS")
    print(f"{'=' * 60}")

    print("\nDesign Constraints:")
    print(f"  Trace width: {args.trace} mil ({args.trace * 0.0254:.3f} mm)")
    print(f"  Clearance: {args.clearance} mil ({args.clearance * 0.0254:.3f} mm)")
    print(f"  Via drill: {args.via} mm")
    print(f"  Layers: {args.layers}")
    print(f"  Assembly: {'Required' if args.assembly else 'Not required'}")

    if compatible:
        print(f"\n✓ Compatible manufacturers ({len(compatible)}):")
        for profile in compatible:
            print(f"  - {profile.name} ({profile.website})")
    else:
        print("\n✗ No compatible manufacturers found")
        print("  Consider relaxing constraints or using different design rules")

    print(f"\n{'=' * 60}")


def cmd_export_dru(args):
    """Export KiCad DRC rules file."""
    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    rules = profile.get_design_rules(args.layers, args.copper)

    # Generate .kicad_dru content
    dru_content = f"""(version 1)
(rule "Trace Width"
  (constraint track_width (min {rules.min_trace_width_mm}mm)))
(rule "Clearance"
  (constraint clearance (min {rules.min_clearance_mm}mm)))
(rule "Via Drill"
  (constraint hole_size (min {rules.min_via_drill_mm}mm)))
(rule "Via Diameter"
  (constraint via_diameter (min {rules.min_via_diameter_mm}mm)))
(rule "Annular Ring"
  (constraint annular_width (min {rules.min_annular_ring_mm}mm)))
(rule "Copper to Edge"
  (constraint edge_clearance (min {rules.min_copper_to_edge_mm}mm)))
(rule "Hole to Edge"
  (constraint hole_to_hole (min {rules.min_hole_to_edge_mm}mm)))
(rule "Silkscreen Width"
  (constraint silk_clearance (min {rules.min_silkscreen_width_mm}mm)))
"""

    # Output
    if args.output:
        output_path = args.output
    else:
        rules_dir = Path(__file__).parent / "manufacturers" / "rules"
        rules_dir.mkdir(exist_ok=True)
        output_path = rules_dir / f"{profile.id}-{args.layers}layer-{args.copper:.0f}oz.kicad_dru"

    output_path.write_text(dru_content)
    print(f"DRC rules exported to: {output_path}")


def cmd_apply_rules(args):
    """Apply manufacturer design rules to a KiCad project or PCB file."""
    from kicad_tools.core.project_file import (
        apply_manufacturer_rules,
        load_project,
        save_project,
        set_manufacturer_metadata,
    )
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    rules = profile.get_design_rules(args.layers, args.copper)
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"APPLYING {profile.name.upper()} DESIGN RULES")
    print(f"{'=' * 60}")
    print(f"\nFile: {file_path}")
    print(f"Configuration: {args.layers}-layer, {args.copper}oz copper")

    print("\nDesign Rules to Apply:")
    print(f"  Min clearance:     {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)")
    print(f"  Min trace width:   {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)")
    print(f"  Min via diameter:  {rules.min_via_diameter_mm:.3f} mm")
    print(f"  Min via drill:     {rules.min_via_drill_mm:.3f} mm")
    print(f"  Min annular ring:  {rules.min_annular_ring_mm:.3f} mm")
    print(f"  Copper to edge:    {rules.min_copper_to_edge_mm:.3f} mm")

    if args.dry_run:
        print("\n(dry run - no changes made)")
        print(f"\n{'=' * 60}")
        return

    # Handle project files (.kicad_pro)
    if file_path.suffix == ".kicad_pro":
        try:
            data = load_project(file_path)
        except Exception as e:
            print(f"Error loading project: {e}", file=sys.stderr)
            sys.exit(1)

        # Apply manufacturer rules
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
        output_path = Path(args.output) if args.output else file_path
        try:
            save_project(data, output_path)
            print(f"\nProject file updated: {output_path}")
        except Exception as e:
            print(f"Error saving project: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle PCB files (.kicad_pcb) - update zone clearances
    elif file_path.suffix == ".kicad_pcb":
        try:
            sexp = load_pcb(file_path)
        except Exception as e:
            print(f"Error loading PCB: {e}", file=sys.stderr)
            sys.exit(1)

        zones_updated = _update_zone_clearances(sexp, rules.min_clearance_mm)

        if zones_updated > 0:
            print(f"\nUpdated {zones_updated} zone(s) with clearance: {rules.min_clearance_mm:.4f} mm")
        else:
            print("\nNo zones found to update.")

        # Save
        output_path = Path(args.output) if args.output else file_path
        try:
            save_pcb(sexp, output_path)
            print(f"PCB file updated: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Error: Unsupported file type: {file_path.suffix}", file=sys.stderr)
        print("Supported: .kicad_pro, .kicad_pcb")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"Manufacturer set to: {profile.name} ({profile.id})")
    print(f"{'=' * 60}")


def _update_zone_clearances(sexp, clearance_mm: float) -> int:
    """Update zone clearances in a PCB S-expression.

    Args:
        sexp: PCB S-expression tree
        clearance_mm: New clearance value in mm

    Returns:
        Number of zones updated
    """
    from kicad_tools.core.sexp import SExp

    zones_updated = 0

    for child in sexp.values:
        if isinstance(child, SExp) and child.tag == "zone":
            # Find or create connect_pads element
            connect_pads = child.find("connect_pads")
            if connect_pads:
                # Update existing clearance
                clearance = connect_pads.find("clearance")
                if clearance:
                    clearance.set_value(0, clearance_mm)
                else:
                    # Add clearance element
                    from kicad_tools.core.sexp import SExp as SExpClass
                    new_clearance = SExpClass("clearance", [clearance_mm])
                    connect_pads.values.append(new_clearance)
                zones_updated += 1

    return zones_updated


def cmd_validate(args):
    """Validate a PCB design against manufacturer design rules."""
    from kicad_tools.core.sexp import SExp
    from kicad_tools.core.sexp_file import load_pcb

    try:
        profile = get_profile(args.manufacturer)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    rules = profile.get_design_rules(args.layers, args.copper)
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    if file_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {file_path.suffix}", file=sys.stderr)
        sys.exit(1)

    try:
        sexp = load_pcb(file_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"VALIDATING AGAINST {profile.name.upper()} RULES")
    print(f"{'=' * 60}")
    print(f"\nFile: {file_path}")
    print(f"Configuration: {args.layers}-layer, {args.copper}oz copper")

    print("\nDesign Rules:")
    print(f"  Min clearance:     {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)")
    print(f"  Min trace width:   {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)")
    print(f"  Min via diameter:  {rules.min_via_diameter_mm:.3f} mm")
    print(f"  Min via drill:     {rules.min_via_drill_mm:.3f} mm")

    # Run validation checks
    violations = _validate_pcb_design(sexp, rules)

    print(f"\n{'-' * 60}")

    if violations:
        print(f"\nVIOLATIONS FOUND: {len(violations)}")
        print()

        for v_type, v_details in violations:
            print(f"  [{v_type}] {v_details}")

        print(f"\n{'=' * 60}")
        sys.exit(1)
    else:
        print("\nNo violations found - design meets manufacturer requirements.")
        print(f"\n{'=' * 60}")


def _validate_pcb_design(sexp, rules) -> List[Tuple[str, str]]:
    """Validate PCB design against manufacturer rules.

    Args:
        sexp: PCB S-expression tree
        rules: DesignRules from manufacturer profile

    Returns:
        List of (violation_type, details) tuples
    """
    from kicad_tools.core.sexp import SExp

    violations = []

    # Check trace widths
    for child in sexp.values:
        if isinstance(child, SExp) and child.tag == "segment":
            width = child.find("width")
            if width:
                trace_width = width.get_float(0) or 0.0
                if trace_width < rules.min_trace_width_mm:
                    violations.append((
                        "TRACE_WIDTH",
                        f"Trace width {trace_width:.4f}mm < min {rules.min_trace_width_mm:.4f}mm"
                    ))

    # Check via diameters and drills
    for child in sexp.values:
        if isinstance(child, SExp) and child.tag == "via":
            size = child.find("size")
            drill = child.find("drill")

            if size:
                via_diameter = size.get_float(0) or 0.0
                if via_diameter < rules.min_via_diameter_mm:
                    violations.append((
                        "VIA_DIAMETER",
                        f"Via diameter {via_diameter:.3f}mm < min {rules.min_via_diameter_mm:.3f}mm"
                    ))

            if drill:
                via_drill = drill.get_float(0) or 0.0
                if via_drill < rules.min_via_drill_mm:
                    violations.append((
                        "VIA_DRILL",
                        f"Via drill {via_drill:.3f}mm < min {rules.min_via_drill_mm:.3f}mm"
                    ))

    # Check zone clearances
    for child in sexp.values:
        if isinstance(child, SExp) and child.tag == "zone":
            connect_pads = child.find("connect_pads")
            if connect_pads:
                clearance = connect_pads.find("clearance")
                if clearance:
                    zone_clearance = clearance.get_float(0) or 0.0
                    if zone_clearance < rules.min_clearance_mm:
                        net = child.find("net_name")
                        net_name = net.get_string(0) if net else "unknown"
                        violations.append((
                            "ZONE_CLEARANCE",
                            f"Zone '{net_name}' clearance {zone_clearance:.4f}mm < min {rules.min_clearance_mm:.4f}mm"
                        ))

    # Check pad drill sizes in footprints
    for child in sexp.values:
        if isinstance(child, SExp) and child.tag == "footprint":
            ref = None
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref = fp_text.get_string(1)
                    break

            for pad in child.find_all("pad"):
                drill = pad.find("drill")
                if drill:
                    drill_size = drill.get_float(0) or 0.0
                    if drill_size > 0 and drill_size < rules.min_hole_diameter_mm:
                        violations.append((
                            "HOLE_SIZE",
                            f"{ref or 'Unknown'}: Hole {drill_size:.3f}mm < min {rules.min_hole_diameter_mm:.3f}mm"
                        ))

    return violations


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PCB Manufacturer Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  mfr.py list                       List all manufacturers
  mfr.py info jlcpcb                Show JLCPCB details
  mfr.py rules seeed --layers 4     Seeed 4-layer rules
  mfr.py compare                    Compare all manufacturers
  mfr.py find --trace 5 --via 0.3   Find compatible manufacturers
  mfr.py export-dru jlcpcb          Export KiCad DRC rules
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # list
    p_list = subparsers.add_parser("list", help="List manufacturers")
    p_list.set_defaults(func=cmd_list)

    # info
    p_info = subparsers.add_parser("info", help="Show manufacturer info")
    p_info.add_argument("manufacturer", help="Manufacturer ID")
    p_info.set_defaults(func=cmd_info)

    # rules
    p_rules = subparsers.add_parser("rules", help="Show design rules")
    p_rules.add_argument("manufacturer", help="Manufacturer ID")
    p_rules.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    p_rules.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_rules.add_argument("--json", action="store_true", help="Include JSON output")
    p_rules.set_defaults(func=cmd_rules)

    # compare
    p_compare = subparsers.add_parser("compare", help="Compare manufacturers")
    p_compare.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    p_compare.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_compare.set_defaults(func=cmd_compare)

    # find
    p_find = subparsers.add_parser("find", help="Find compatible manufacturers")
    p_find.add_argument("--trace", type=float, default=5.0, help="Min trace width (mil)")
    p_find.add_argument("--clearance", type=float, default=5.0, help="Min clearance (mil)")
    p_find.add_argument("--via", type=float, default=0.3, help="Min via drill (mm)")
    p_find.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    p_find.add_argument("--assembly", action="store_true", help="Require assembly")
    p_find.set_defaults(func=cmd_find)

    # export-dru
    p_dru = subparsers.add_parser("export-dru", help="Export KiCad DRC rules")
    p_dru.add_argument("manufacturer", help="Manufacturer ID")
    p_dru.add_argument("-l", "--layers", type=int, default=4, help="Layer count")
    p_dru.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_dru.add_argument("-o", "--output", type=Path, help="Output path")
    p_dru.set_defaults(func=cmd_export_dru)

    # apply-rules
    p_apply = subparsers.add_parser(
        "apply-rules",
        help="Apply manufacturer design rules to project/PCB",
        description="Apply manufacturer design rules to a KiCad project (.kicad_pro) "
        "or update zone clearances in a PCB file (.kicad_pcb).",
    )
    p_apply.add_argument("file", help="Path to .kicad_pro or .kicad_pcb file")
    p_apply.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    p_apply.add_argument("-l", "--layers", type=int, default=2, help="Layer count (default: 2)")
    p_apply.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_apply.add_argument("-o", "--output", help="Output file (default: modify in place)")
    p_apply.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    p_apply.set_defaults(func=cmd_apply_rules)

    # validate
    p_validate = subparsers.add_parser(
        "validate",
        help="Validate PCB against manufacturer rules",
        description="Check a PCB design against manufacturer design rules. "
        "Reports violations for trace widths, via sizes, zone clearances, etc.",
    )
    p_validate.add_argument("file", help="Path to .kicad_pcb file")
    p_validate.add_argument("manufacturer", help="Manufacturer ID (jlcpcb, seeed, etc.)")
    p_validate.add_argument("-l", "--layers", type=int, default=2, help="Layer count (default: 2)")
    p_validate.add_argument("-c", "--copper", type=float, default=1.0, help="Copper weight (oz)")
    p_validate.set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
