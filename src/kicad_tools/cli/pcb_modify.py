#!/usr/bin/env python3
"""
Modify KiCad PCB files.

Provides commands to move components, update footprints, and make other changes.

Usage:
    python3 pcb-modify.py <pcb.kicad_pcb> <command> [options]

Commands:
    move <ref> <x> <y>      Move component to new position
    rotate <ref> <angle>    Rotate component by angle (degrees)
    flip <ref>              Flip component to opposite layer
    update-value <ref> <value>  Update component value
    rename <old> <new>      Rename component reference
    delete-traces <net>     Delete all traces on a net

Options:
    --dry-run               Show changes without applying
    -o, --output <file>     Write to new file instead of modifying in place

Examples:
    python3 pcb-modify.py board.kicad_pcb move U1 100 50
    python3 pcb-modify.py board.kicad_pcb rotate U1 90
    python3 pcb-modify.py board.kicad_pcb flip U1
    python3 pcb-modify.py board.kicad_pcb update-value R1 "10k"
    python3 pcb-modify.py board.kicad_pcb rename U1 U10 --dry-run
"""

import argparse
import sys
from pathlib import Path

from kicad_tools.core.sexp import SExp
from kicad_tools.core.sexp_file import load_pcb, save_pcb


def find_footprint_sexp(sexp: SExp, reference: str) -> SExp:
    """Find footprint S-expression by reference."""
    for child in sexp.iter_children():
        if child.tag == "footprint":
            # Look for fp_text with reference
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    if fp_text.get_string(1) == reference:
                        return child
    return None


def cmd_move(sexp: SExp, args) -> bool:
    """Move component to new position."""
    fp = find_footprint_sexp(sexp, args.reference)
    if not fp:
        print(f"Error: Footprint '{args.reference}' not found", file=sys.stderr)
        return False

    at = fp.find("at")
    if not at:
        print("Error: Footprint has no position", file=sys.stderr)
        return False

    old_x = at.get_float(0) or 0.0
    old_y = at.get_float(1) or 0.0
    _rotation = at.get_float(2) or 0.0  # noqa: F841

    print(f"Moving {args.reference}:")
    print(f"  From: ({old_x:.4f}, {old_y:.4f})")
    print(f"  To:   ({args.x:.4f}, {args.y:.4f})")

    if not args.dry_run:
        at.set_value(0, args.x)
        at.set_value(1, args.y)

    return True


def cmd_rotate(sexp: SExp, args) -> bool:
    """Rotate component by angle."""
    fp = find_footprint_sexp(sexp, args.reference)
    if not fp:
        print(f"Error: Footprint '{args.reference}' not found", file=sys.stderr)
        return False

    at = fp.find("at")
    if not at:
        print("Error: Footprint has no position", file=sys.stderr)
        return False

    _x = at.get_float(0) or 0.0  # noqa: F841
    _y = at.get_float(1) or 0.0  # noqa: F841
    old_rotation = at.get_float(2) or 0.0

    new_rotation = (old_rotation + args.angle) % 360

    print(f"Rotating {args.reference}:")
    print(f"  From: {old_rotation}°")
    print(f"  To:   {new_rotation}°")

    if not args.dry_run:
        # Need to ensure the rotation value exists
        if len(at.values) < 3:
            at.values.append(new_rotation)
        else:
            at.set_value(2, new_rotation)

    return True


def cmd_flip(sexp: SExp, args) -> bool:
    """Flip component to opposite layer."""
    fp = find_footprint_sexp(sexp, args.reference)
    if not fp:
        print(f"Error: Footprint '{args.reference}' not found", file=sys.stderr)
        return False

    layer = fp.find("layer")
    if not layer:
        print("Error: Footprint has no layer", file=sys.stderr)
        return False

    old_layer = layer.get_string(0) or ""

    # Flip between F.Cu and B.Cu
    if old_layer == "F.Cu":
        new_layer = "B.Cu"
    elif old_layer == "B.Cu":
        new_layer = "F.Cu"
    else:
        print(f"Error: Unexpected layer '{old_layer}'", file=sys.stderr)
        return False

    print(f"Flipping {args.reference}:")
    print(f"  From: {old_layer}")
    print(f"  To:   {new_layer}")

    if not args.dry_run:
        layer.set_value(0, new_layer)

        # Also need to flip layer references in pads
        for pad in fp.find_all("pad"):
            layers = pad.find("layers")
            if layers:
                for i, val in enumerate(layers.values):
                    if val == "F.Cu":
                        layers.values[i] = "B.Cu"
                    elif val == "B.Cu":
                        layers.values[i] = "F.Cu"
                    elif val == "F.Paste":
                        layers.values[i] = "B.Paste"
                    elif val == "B.Paste":
                        layers.values[i] = "F.Paste"
                    elif val == "F.Mask":
                        layers.values[i] = "B.Mask"
                    elif val == "B.Mask":
                        layers.values[i] = "F.Mask"

    return True


def cmd_update_value(sexp: SExp, args) -> bool:
    """Update component value."""
    fp = find_footprint_sexp(sexp, args.reference)
    if not fp:
        print(f"Error: Footprint '{args.reference}' not found", file=sys.stderr)
        return False

    # Find value fp_text
    for fp_text in fp.find_all("fp_text"):
        if fp_text.get_string(0) == "value":
            old_value = fp_text.get_string(1) or ""

            print(f"Updating {args.reference} value:")
            print(f"  From: {old_value}")
            print(f"  To:   {args.value}")

            if not args.dry_run:
                fp_text.set_value(1, args.value)

            return True

    print("Error: Footprint has no value text", file=sys.stderr)
    return False


def cmd_rename(sexp: SExp, args) -> bool:
    """Rename component reference."""
    # Check if new reference already exists
    if find_footprint_sexp(sexp, args.new_reference):
        print(f"Error: Reference '{args.new_reference}' already exists", file=sys.stderr)
        return False

    fp = find_footprint_sexp(sexp, args.old_reference)
    if not fp:
        print(f"Error: Footprint '{args.old_reference}' not found", file=sys.stderr)
        return False

    print("Renaming:")
    print(f"  From: {args.old_reference}")
    print(f"  To:   {args.new_reference}")

    if not args.dry_run:
        # Update fp_text reference
        for fp_text in fp.find_all("fp_text"):
            if fp_text.get_string(0) == "reference":
                fp_text.set_value(1, args.new_reference)
                break

    return True


def cmd_delete_traces(sexp: SExp, args) -> bool:
    """Delete all traces on a net."""
    # Find net number by name
    net_number = None
    for child in sexp.iter_children():
        if child.tag == "net":
            if child.get_string(1) == args.net:
                net_number = child.get_int(0)
                break

    if net_number is None:
        print(f"Error: Net '{args.net}' not found", file=sys.stderr)
        return False

    # Count segments and vias to delete
    segments_to_delete = []
    vias_to_delete = []

    for i, child in enumerate(sexp.values):
        if isinstance(child, SExp):
            if child.tag == "segment":
                if net := child.find("net"):
                    if net.get_int(0) == net_number:
                        segments_to_delete.append(i)
            elif child.tag == "via":
                if net := child.find("net"):
                    if net.get_int(0) == net_number:
                        vias_to_delete.append(i)

    print(f"Deleting traces on net '{args.net}' (#{net_number}):")
    print(f"  Segments: {len(segments_to_delete)}")
    print(f"  Vias:     {len(vias_to_delete)}")

    if not args.dry_run:
        # Delete in reverse order to preserve indices
        for i in sorted(segments_to_delete + vias_to_delete, reverse=True):
            del sexp.values[i]

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Modify KiCad PCB files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "command",
        choices=["move", "rotate", "flip", "update-value", "rename", "delete-traces"],
        help="Command to run",
    )
    parser.add_argument("args", nargs="*", help="Command arguments")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("-o", "--output", help="Output file (default: modify in place)")

    args = parser.parse_args()

    if not Path(args.pcb).exists():
        print(f"Error: File not found: {args.pcb}", file=sys.stderr)
        sys.exit(1)

    try:
        sexp = load_pcb(args.pcb)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse command-specific arguments
    success = False

    if args.command == "move":
        if len(args.args) != 3:
            print("Usage: move <reference> <x> <y>", file=sys.stderr)
            sys.exit(1)
        args.reference = args.args[0]
        args.x = float(args.args[1])
        args.y = float(args.args[2])
        success = cmd_move(sexp, args)

    elif args.command == "rotate":
        if len(args.args) != 2:
            print("Usage: rotate <reference> <angle>", file=sys.stderr)
            sys.exit(1)
        args.reference = args.args[0]
        args.angle = float(args.args[1])
        success = cmd_rotate(sexp, args)

    elif args.command == "flip":
        if len(args.args) != 1:
            print("Usage: flip <reference>", file=sys.stderr)
            sys.exit(1)
        args.reference = args.args[0]
        success = cmd_flip(sexp, args)

    elif args.command == "update-value":
        if len(args.args) != 2:
            print("Usage: update-value <reference> <value>", file=sys.stderr)
            sys.exit(1)
        args.reference = args.args[0]
        args.value = args.args[1]
        success = cmd_update_value(sexp, args)

    elif args.command == "rename":
        if len(args.args) != 2:
            print("Usage: rename <old_reference> <new_reference>", file=sys.stderr)
            sys.exit(1)
        args.old_reference = args.args[0]
        args.new_reference = args.args[1]
        success = cmd_rename(sexp, args)

    elif args.command == "delete-traces":
        if len(args.args) != 1:
            print("Usage: delete-traces <net_name>", file=sys.stderr)
            sys.exit(1)
        args.net = args.args[0]
        success = cmd_delete_traces(sexp, args)

    if not success:
        sys.exit(1)

    # Save changes
    if not args.dry_run:
        output_path = args.output or args.pcb
        try:
            save_pcb(sexp, output_path)
            print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("\n(dry run - no changes made)")


if __name__ == "__main__":
    main()
