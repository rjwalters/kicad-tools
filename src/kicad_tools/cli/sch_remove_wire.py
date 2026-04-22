#!/usr/bin/env python3
"""
Remove a specific wire segment from a KiCad schematic.

Supports two matching modes:
  --from X1,Y1 --to X2,Y2   Match a wire by its exact endpoints
  --near X,Y                 Match the nearest wire to a point

After removal, orphaned junctions (at points with fewer than 3 remaining
wire endpoints) are automatically cleaned up.

Usage:
    kct sch remove-wire board.kicad_sch --from 100,50 --to 150,50
    kct sch remove-wire board.kicad_sch --near 125,50
    kct sch remove-wire board.kicad_sch --from 100,50 --to 150,50 --dry-run
    kct sch remove-wire board.kicad_sch --near 125,50 --backup

Options:
    --from X,Y               Start endpoint of the wire to remove
    --to X,Y                 End endpoint of the wire to remove
    --near X,Y               Find and remove the wire nearest to this point
    --tolerance <mm>         Coordinate matching tolerance (default: 1.27 mm)
    --dry-run                Show what would change without modifying
    --backup                 Create backup before modifying
    --format {text,json}     Output format (default: text)
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import datetime
from pathlib import Path

from kicad_tools.schema import Schematic
from kicad_tools.sexp import SExp

POINT_TOLERANCE = 1.27  # mm - standard KiCad grid


def _wire_start_end(wire_sexp: SExp) -> tuple[tuple[float, float], tuple[float, float]]:
    """Extract start and end points from a wire S-expression node."""
    pts_node = wire_sexp.find("pts")
    if not pts_node:
        return (0.0, 0.0), (0.0, 0.0)

    xy_nodes = pts_node.find_all("xy")
    if len(xy_nodes) < 2:
        return (0.0, 0.0), (0.0, 0.0)

    x1 = xy_nodes[0].get_float(0) or 0.0
    y1 = xy_nodes[0].get_float(1) or 0.0
    x2 = xy_nodes[1].get_float(0) or 0.0
    y2 = xy_nodes[1].get_float(1) or 0.0

    return (x1, y1), (x2, y2)


def _wire_endpoint_counts(
    wire_sexps: list[SExp],
) -> dict[tuple[int, int], int]:
    """Count how many wires touch each endpoint."""
    counts: dict[tuple[int, int], int] = {}
    for ws in wire_sexps:
        start, end = _wire_start_end(ws)
        for pt in [start, end]:
            key = (int(pt[0] * 10), int(pt[1] * 10))
            counts[key] = counts.get(key, 0) + 1
    return counts


def find_wire_by_endpoints(
    schematic: Schematic,
    from_pt: tuple[float, float],
    to_pt: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> SExp | None:
    """Find a wire matching the given start and end points (order-insensitive).

    Returns the matching wire S-expression node, or None if not found.
    """
    from_key = (int(from_pt[0] * 10), int(from_pt[1] * 10))
    to_key = (int(to_pt[0] * 10), int(to_pt[1] * 10))
    tol_units = int(tolerance * 10)

    for wire_sexp in schematic.sexp.find_all("wire"):
        start, end = _wire_start_end(wire_sexp)
        start_key = (int(start[0] * 10), int(start[1] * 10))
        end_key = (int(end[0] * 10), int(end[1] * 10))

        # Order-insensitive: try both orderings
        match_forward = (
            abs(start_key[0] - from_key[0]) <= tol_units
            and abs(start_key[1] - from_key[1]) <= tol_units
            and abs(end_key[0] - to_key[0]) <= tol_units
            and abs(end_key[1] - to_key[1]) <= tol_units
        )
        match_reverse = (
            abs(start_key[0] - to_key[0]) <= tol_units
            and abs(start_key[1] - to_key[1]) <= tol_units
            and abs(end_key[0] - from_key[0]) <= tol_units
            and abs(end_key[1] - from_key[1]) <= tol_units
        )

        if match_forward or match_reverse:
            return wire_sexp

    return None


def find_nearest_wire(
    schematic: Schematic,
    point: tuple[float, float],
) -> SExp | None:
    """Find the wire whose endpoint is nearest to the given point.

    Returns the nearest wire S-expression node, or None if no wires exist.
    """
    best_wire: SExp | None = None
    best_dist = float("inf")

    for wire_sexp in schematic.sexp.find_all("wire"):
        start, end = _wire_start_end(wire_sexp)

        for pt in [start, end]:
            dist = math.hypot(pt[0] - point[0], pt[1] - point[1])
            if dist < best_dist:
                best_dist = dist
                best_wire = wire_sexp

    return best_wire


def remove_wire_and_orphan_junctions(
    schematic: Schematic,
    wire_sexp: SExp,
) -> tuple[bool, int]:
    """Remove a wire and clean up any orphaned junctions at its endpoints.

    A junction is considered orphaned if fewer than 3 wire endpoints remain
    at that location after the wire is removed.

    Returns (wire_removed, junctions_removed).
    """
    start, end = _wire_start_end(wire_sexp)

    # Remove the wire
    if not schematic.sexp.remove(wire_sexp):
        return False, 0

    # Check for orphaned junctions at the wire's former endpoints
    remaining_wires = list(schematic.sexp.find_all("wire"))
    endpoint_counts = _wire_endpoint_counts(remaining_wires)

    junctions_removed = 0
    for pt in [start, end]:
        pt_key = (int(pt[0] * 10), int(pt[1] * 10))
        wire_count = endpoint_counts.get(pt_key, 0)

        # Junction is only meaningful at 3+ way intersections
        if wire_count >= 3:
            continue

        # Find and remove junctions at this point
        for junc_sexp in list(schematic.sexp.find_all("junction")):
            at_node = junc_sexp.find("at")
            if not at_node:
                continue
            jx = at_node.get_float(0) or 0.0
            jy = at_node.get_float(1) or 0.0
            junc_key = (int(jx * 10), int(jy * 10))

            if junc_key == pt_key:
                if schematic.sexp.remove(junc_sexp):
                    junctions_removed += 1

    # Wire was removed, always invalidate
    schematic.invalidate_cache()

    return True, junctions_removed


def _parse_coordinate(value: str) -> tuple[float, float]:
    """Parse a comma-separated coordinate pair like '100,50' or '100.5,50.3'."""
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Expected X,Y coordinate pair, got: {value!r}")
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid coordinate values: {value!r}")


def run_remove_wire(args) -> int:
    """Execute the remove-wire command."""
    schematic_path = Path(args.schematic)

    # Validate mutually exclusive options
    has_endpoints = args.from_pt is not None and args.to_pt is not None
    has_near = args.near is not None

    if has_endpoints and has_near:
        print(
            "Error: --from/--to and --near are mutually exclusive",
            file=sys.stderr,
        )
        return 1

    if not has_endpoints and not has_near:
        print(
            "Error: Either --from and --to, or --near must be specified",
            file=sys.stderr,
        )
        return 1

    if (args.from_pt is not None) != (args.to_pt is not None):
        print(
            "Error: --from and --to must be used together",
            file=sys.stderr,
        )
        return 1

    try:
        sch = Schematic.load(schematic_path)
    except FileNotFoundError:
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Find the target wire
    if has_endpoints:
        wire = find_wire_by_endpoints(sch, args.from_pt, args.to_pt, tolerance=args.tolerance)
        if wire is None:
            msg = (
                f"No wire found matching endpoints "
                f"({args.from_pt[0]:.2f}, {args.from_pt[1]:.2f}) -> "
                f"({args.to_pt[0]:.2f}, {args.to_pt[1]:.2f}) "
                f"within tolerance {args.tolerance} mm"
            )
            if args.format == "json":
                print(json.dumps({"error": msg, "removed": False}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1
    else:
        wire = find_nearest_wire(sch, args.near)
        if wire is None:
            msg = "No wires found in schematic"
            if args.format == "json":
                print(json.dumps({"error": msg, "removed": False}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

    start, end = _wire_start_end(wire)

    # Dry run
    if args.dry_run:
        if args.format == "json":
            data = {
                "dry_run": True,
                "removed": False,
                "wire": {
                    "start": list(start),
                    "end": list(end),
                },
                "junctions_removed": 0,
            }
            if has_near:
                data["matched_by"] = "nearest"
                data["near"] = list(args.near)
            else:
                data["matched_by"] = "endpoints"
            print(json.dumps(data, indent=2))
        else:
            print("DRY RUN - No changes will be made")
            print("=" * 60)
            if has_near:
                print(f"Nearest wire to ({args.near[0]:.2f}, {args.near[1]:.2f}):")
            print(f"  Wire: ({start[0]:.2f}, {start[1]:.2f}) -> ({end[0]:.2f}, {end[1]:.2f})")
            print("  Would be removed")
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)

    # Execute removal
    wire_removed, junctions_removed = remove_wire_and_orphan_junctions(sch, wire)

    if not wire_removed:
        if args.format == "json":
            print(json.dumps({"error": "Failed to remove wire", "removed": False}, indent=2))
        else:
            print("Error: Failed to remove wire", file=sys.stderr)
        return 1

    sch.save()

    if args.format == "json":
        data = {
            "dry_run": False,
            "removed": True,
            "wire": {
                "start": list(start),
                "end": list(end),
            },
            "junctions_removed": junctions_removed,
        }
        if has_near:
            data["matched_by"] = "nearest"
            data["near"] = list(args.near)
        else:
            data["matched_by"] = "endpoints"
        print(json.dumps(data, indent=2))
    else:
        if has_near:
            print(f"Selected nearest wire to ({args.near[0]:.2f}, {args.near[1]:.2f}):")
        print(f"Removed wire: ({start[0]:.2f}, {start[1]:.2f}) -> ({end[0]:.2f}, {end[1]:.2f})")
        if junctions_removed:
            print(f"Removed {junctions_removed} orphaned junction(s)")
        if args.backup:
            print(f"Backup created: {backup_path}")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove a specific wire segment from a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--from",
        dest="from_pt",
        type=_parse_coordinate,
        help="Start endpoint (X,Y) of wire to remove",
    )
    parser.add_argument(
        "--to",
        dest="to_pt",
        type=_parse_coordinate,
        help="End endpoint (X,Y) of wire to remove",
    )
    parser.add_argument(
        "--near",
        type=_parse_coordinate,
        help="Find and remove wire nearest to this point (X,Y)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=POINT_TOLERANCE,
        help=f"Coordinate matching tolerance in mm (default: {POINT_TOLERANCE})",
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")

    args = parser.parse_args(argv)
    return run_remove_wire(args)


if __name__ == "__main__":
    sys.exit(main())
