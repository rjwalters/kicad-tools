#!/usr/bin/env python3
"""
Fix vias to meet manufacturer specifications.

Usage:
    kicad-tools fix-vias board.kicad_pcb [options]

Examples:
    # Resize vias to meet JLCPCB minimums (default)
    kicad-tools fix-vias board.kicad_pcb --mfr jlcpcb

    # Specify sizes directly
    kicad-tools fix-vias board.kicad_pcb --drill 0.3 --diameter 0.6

    # Preview changes without applying
    kicad-tools fix-vias board.kicad_pcb --mfr jlcpcb --dry-run

    # Output to a different file
    kicad-tools fix-vias board.kicad_pcb --mfr jlcpcb -o fixed_board.kicad_pcb
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.manufacturers.base import load_design_rules_from_yaml
from kicad_tools.sexp.parser import SExp, parse_file


@dataclass
class ViaFix:
    """Record of a via that was fixed."""

    x: float
    y: float
    net: int
    old_drill: float
    new_drill: float
    old_diameter: float
    new_diameter: float
    uuid: str


@dataclass
class ViaClearanceWarning:
    """Warning for a via that may cause clearance issues after resize."""

    x: float
    y: float
    new_diameter: float
    nearby_item: str
    clearance_mm: float


def get_design_rules(
    mfr: str | None, layers: int, copper: float, drill: float | None, diameter: float | None
) -> tuple[float, float]:
    """Get the target drill and diameter from manufacturer or explicit values.

    Returns:
        Tuple of (min_drill_mm, min_diameter_mm)
    """
    if drill is not None and diameter is not None:
        return drill, diameter

    if mfr:
        try:
            rules_dict = load_design_rules_from_yaml(mfr)
            # Build key like "2layer_1oz"
            key = f"{layers}layer_{int(copper)}oz"
            if key in rules_dict:
                rules = rules_dict[key]
            else:
                # Try without copper weight
                key = f"{layers}layer_1oz"
                if key in rules_dict:
                    rules = rules_dict[key]
                else:
                    # Fall back to first available
                    rules = list(rules_dict.values())[0]

            target_drill = drill if drill is not None else rules.min_via_drill_mm
            target_diameter = diameter if diameter is not None else rules.min_via_diameter_mm
            return target_drill, target_diameter
        except FileNotFoundError:
            print(f"Warning: No configuration found for manufacturer '{mfr}'", file=sys.stderr)

    # Default values if nothing specified
    return drill or 0.3, diameter or 0.6


def find_all_vias(doc: SExp) -> list[tuple[SExp, float, float, float, float, int, str]]:
    """Find all vias in the PCB document.

    Returns:
        List of (node, x, y, drill, diameter, net, uuid) tuples
    """
    vias = []

    for via_node in doc.find_all("via"):
        at_node = via_node.find("at")
        if not at_node:
            continue

        at_atoms = at_node.get_atoms()
        x = float(at_atoms[0]) if at_atoms else 0
        y = float(at_atoms[1]) if len(at_atoms) > 1 else 0

        size_node = via_node.find("size")
        drill_node = via_node.find("drill")
        net_node = via_node.find("net")
        uuid_node = via_node.find("uuid")

        diameter = float(size_node.get_first_atom()) if size_node else 0
        drill = float(drill_node.get_first_atom()) if drill_node else 0
        net = int(net_node.get_first_atom()) if net_node else 0
        uuid = uuid_node.get_first_atom() if uuid_node else ""

        vias.append((via_node, x, y, drill, diameter, net, uuid))

    return vias


def find_nearby_items(
    doc: SExp, x: float, y: float, radius: float
) -> list[tuple[str, float, float]]:
    """Find PCB items near a point.

    Returns:
        List of (item_type, ix, iy) for items within radius
    """
    items = []

    # Check pads (in footprints)
    for fp_node in doc.find_all("footprint"):
        for pad_node in fp_node.find_all("pad"):
            at_node = pad_node.find("at")
            if at_node:
                at_atoms = at_node.get_atoms()
                px = float(at_atoms[0]) if at_atoms else 0
                py = float(at_atoms[1]) if len(at_atoms) > 1 else 0
                dist = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
                if dist < radius:
                    items.append(("pad", px, py))

    # Check other vias
    for via_node in doc.find_all("via"):
        at_node = via_node.find("at")
        if at_node:
            at_atoms = at_node.get_atoms()
            vx = float(at_atoms[0]) if at_atoms else 0
            vy = float(at_atoms[1]) if len(at_atoms) > 1 else 0
            # Skip if same position (it's the via we're checking)
            if abs(vx - x) < 0.001 and abs(vy - y) < 0.001:
                continue
            dist = ((vx - x) ** 2 + (vy - y) ** 2) ** 0.5
            if dist < radius:
                items.append(("via", vx, vy))

    # Check track segments (but not endpoints at the via position - those are connected)
    for seg_node in doc.find_all("segment"):
        start_node = seg_node.find("start")
        end_node = seg_node.find("end")
        if start_node and end_node:
            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()
            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

            # Skip if endpoint is at the via position (it's connected)
            if abs(sx - x) < 0.001 and abs(sy - y) < 0.001:
                continue
            if abs(ex - x) < 0.001 and abs(ey - y) < 0.001:
                continue

            # Check distance from via to line segment midpoint
            mx, my = (sx + ex) / 2, (sy + ey) / 2
            dist = ((mx - x) ** 2 + (my - y) ** 2) ** 0.5
            if dist < radius:
                items.append(("track", mx, my))

    return items


def fix_vias(
    doc: SExp,
    target_drill: float,
    target_diameter: float,
    min_clearance: float = 0.2,
    dry_run: bool = False,
) -> tuple[list[ViaFix], list[ViaClearanceWarning]]:
    """Fix undersized vias in the PCB.

    Args:
        doc: Parsed PCB document
        target_drill: Minimum drill size in mm
        target_diameter: Minimum via diameter in mm
        min_clearance: Minimum clearance for warnings in mm
        dry_run: If True, don't modify the document

    Returns:
        Tuple of (fixes, warnings)
    """
    fixes = []
    warnings = []

    vias = find_all_vias(doc)

    for via_node, x, y, current_drill, current_diameter, net, uuid in vias:
        need_drill_fix = current_drill < target_drill
        need_diameter_fix = current_diameter < target_diameter

        if not need_drill_fix and not need_diameter_fix:
            continue

        new_drill = max(current_drill, target_drill)
        new_diameter = max(current_diameter, target_diameter)

        # Record the fix
        fixes.append(
            ViaFix(
                x=x,
                y=y,
                net=net,
                old_drill=current_drill,
                new_drill=new_drill,
                old_diameter=current_diameter,
                new_diameter=new_diameter,
                uuid=uuid,
            )
        )

        # Check for potential clearance issues
        size_increase = new_diameter - current_diameter
        if size_increase > 0:
            check_radius = new_diameter / 2 + min_clearance * 2
            nearby = find_nearby_items(doc, x, y, check_radius)
            for item_type, ix, iy in nearby:
                dist = ((ix - x) ** 2 + (iy - y) ** 2) ** 0.5
                clearance = dist - new_diameter / 2
                if clearance < min_clearance:
                    warnings.append(
                        ViaClearanceWarning(
                            x=x,
                            y=y,
                            new_diameter=new_diameter,
                            nearby_item=f"{item_type} at ({ix:.2f}, {iy:.2f})",
                            clearance_mm=clearance,
                        )
                    )

        # Apply the fix if not dry run
        if not dry_run:
            drill_node = via_node.find("drill")
            size_node = via_node.find("size")

            if drill_node and need_drill_fix:
                drill_node.set_value(0, new_drill)

            if size_node and need_diameter_fix:
                size_node.set_value(0, new_diameter)

    return fixes, warnings


def print_fix_results(
    fixes: list[ViaFix],
    warnings: list[ViaClearanceWarning],
    output_format: str = "text",
    dry_run: bool = False,
    target_drill: float = 0,
    target_diameter: float = 0,
    mfr: str | None = None,
) -> None:
    """Print the results of via fixes.

    Args:
        fixes: List of via fixes
        warnings: List of clearance warnings
        output_format: Output format ("text", "json", "summary")
        dry_run: Whether this was a dry run
        target_drill: Target drill size used
        target_diameter: Target diameter used
        mfr: Manufacturer name (for display)
    """
    if output_format == "json":
        data = {
            "target_drill_mm": target_drill,
            "target_diameter_mm": target_diameter,
            "manufacturer": mfr,
            "dry_run": dry_run,
            "fixes": [
                {
                    "x": f.x,
                    "y": f.y,
                    "net": f.net,
                    "old_drill_mm": f.old_drill,
                    "new_drill_mm": f.new_drill,
                    "old_diameter_mm": f.old_diameter,
                    "new_diameter_mm": f.new_diameter,
                    "uuid": f.uuid,
                }
                for f in fixes
            ],
            "warnings": [
                {
                    "x": w.x,
                    "y": w.y,
                    "new_diameter_mm": w.new_diameter,
                    "nearby_item": w.nearby_item,
                    "clearance_mm": w.clearance_mm,
                }
                for w in warnings
            ],
        }
        print(json.dumps(data, indent=2))
        return

    if output_format == "summary":
        action = "Would resize" if dry_run else "Resized"
        source = f" to {mfr.upper()} minimums" if mfr else ""
        print(f"{action} vias{source} (drill: {target_drill}mm, diameter: {target_diameter}mm):")
        print(f"  {len(fixes)} vias {'would be ' if dry_run else ''}updated")
        if warnings:
            print(f"  {len(warnings)} potential clearance violations")
        return

    # Text output
    if not fixes:
        print("No vias needed resizing.")
        return

    action = "Would resize" if dry_run else "Resizing"
    source = f" to {mfr.upper()} minimums" if mfr else ""
    print(f"{action} vias{source} (drill: {target_drill}mm, diameter: {target_diameter}mm):")

    # Group by layer for display
    print(f"  Updated {len(fixes)} via(s)")

    # Show some examples
    if len(fixes) <= 5:
        for f in fixes:
            print(
                f"    Via at ({f.x:.2f}, {f.y:.2f}): "
                f"drill {f.old_drill:.3f}→{f.new_drill:.3f}mm, "
                f"diameter {f.old_diameter:.3f}→{f.new_diameter:.3f}mm"
            )
    else:
        for f in fixes[:3]:
            print(
                f"    Via at ({f.x:.2f}, {f.y:.2f}): "
                f"drill {f.old_drill:.3f}→{f.new_drill:.3f}mm, "
                f"diameter {f.old_diameter:.3f}→{f.new_diameter:.3f}mm"
            )
        print(f"    ... and {len(fixes) - 3} more")

    if warnings:
        print(f"\nWarning: {len(warnings)} via(s) may cause DRC violations after resize:")
        for w in warnings[:5]:
            print(
                f"  - Via at ({w.x:.2f}, {w.y:.2f}) - {w.clearance_mm:.2f}mm clearance to {w.nearby_item}"
            )
        if len(warnings) > 5:
            print(f"  ... and {len(warnings) - 5} more")


def main(argv: list[str] | None = None) -> int:
    """Main entry point for fix-vias command."""
    parser = argparse.ArgumentParser(
        description="Fix vias to meet manufacturer specifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Resize vias to meet JLCPCB minimums
    kct fix-vias board.kicad_pcb --mfr jlcpcb

    # Specify sizes directly
    kct fix-vias board.kicad_pcb --drill 0.3 --diameter 0.6

    # Preview changes without applying
    kct fix-vias board.kicad_pcb --mfr jlcpcb --dry-run
        """,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--mfr",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Manufacturer to use for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of PCB layers (default: 2)",
    )
    parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Outer copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--drill",
        type=float,
        help="Target drill diameter in mm (overrides manufacturer rules)",
    )
    parser.add_argument(
        "--diameter",
        type=float,
        help="Target via diameter in mm (overrides manufacturer rules)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )

    args = parser.parse_args(argv)

    # Validate input file
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix.lower() != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Get target dimensions
    target_drill, target_diameter = get_design_rules(
        args.mfr, args.layers, args.copper, args.drill, args.diameter
    )

    # Parse PCB
    try:
        doc = parse_file(pcb_path)
    except Exception as e:
        print(f"Error parsing PCB file: {e}", file=sys.stderr)
        return 1

    # Fix vias
    fixes, warnings = fix_vias(doc, target_drill, target_diameter, dry_run=args.dry_run)

    # Print results
    if not args.quiet:
        print_fix_results(
            fixes,
            warnings,
            output_format=args.format,
            dry_run=args.dry_run,
            target_drill=target_drill,
            target_diameter=target_diameter,
            mfr=args.mfr,
        )

    # Save if not dry run and there were fixes
    if fixes and not args.dry_run:
        output_path = Path(args.output) if args.output else pcb_path
        try:
            save_pcb(doc, output_path)
            if not args.quiet and args.format == "text":
                print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB file: {e}", file=sys.stderr)
            return 1

    # Return non-zero if there were warnings (like DRC might fail)
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
