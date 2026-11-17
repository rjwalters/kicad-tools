#!/usr/bin/env python3
"""
Add wire segments to a KiCad schematic file.

Supports placing single or multi-segment wires between arbitrary coordinates,
with optional grid snapping and automatic junction insertion.

Usage:
    kct sch add-wire board.kicad_sch --from 100 50 --to 120 50
    kct sch add-wire board.kicad_sch --from 100 50 --to 120 50 --to 120 80
    kct sch add-wire board.kicad_sch --from 100 50 --to 120 50 --junction
    kct sch add-wire board.kicad_sch --from 100 50 --to 120 50 --dry-run

Options:
    --from X Y             Start coordinate (two floats)
    --to X Y               End coordinate (two floats, repeatable for multi-segment)
    --junction             Auto-insert junctions where endpoints land on existing wires
    --dry-run              Show planned actions without modifying
    --backup               Create timestamped backup before writing
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.cli.sch_json import add_format_flag, record, run_with_json_summary
from kicad_tools.cli.sch_wire_common import (
    _point_on_wire_midpoint,
)
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic
from kicad_tools.schematic.grid import snap_to_grid


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "wire", "junction"
    description: str


def run_add_wire(args) -> int:
    """Execute the add-wire command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Snap all coordinates to grid
    from_point = (snap_to_grid(args.start[0]), snap_to_grid(args.start[1]))

    to_points: list[tuple[float, float]] = []
    for tp in args.to:
        to_points.append((snap_to_grid(tp[0]), snap_to_grid(tp[1])))

    if not to_points:
        print("Error: At least one --to coordinate is required", file=sys.stderr)
        return 1

    # Build the full path of points: from -> to1 -> to2 -> ...
    path_points = [from_point] + to_points

    # Check for zero-length segments
    for i in range(len(path_points) - 1):
        p1 = path_points[i]
        p2 = path_points[i + 1]
        if abs(p1[0] - p2[0]) < 0.01 and abs(p1[1] - p2[1]) < 0.01:
            print(
                f"Warning: Zero-length wire segment from"
                f" ({p1[0]:.2f}, {p1[1]:.2f}) to ({p2[0]:.2f}, {p2[1]:.2f})"
                f" will be skipped",
                file=sys.stderr,
            )

    # Collect planned actions
    planned: list[PlannedAction] = []

    for i in range(len(path_points) - 1):
        p1 = path_points[i]
        p2 = path_points[i + 1]
        if abs(p1[0] - p2[0]) < 0.01 and abs(p1[1] - p2[1]) < 0.01:
            continue  # Skip zero-length segments
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from ({p1[0]:.2f}, {p1[1]:.2f}) to ({p2[0]:.2f}, {p2[1]:.2f})",
            )
        )

    # Plan junction insertion if requested
    junction_points: list[tuple[float, float]] = []
    if getattr(args, "junction", False):
        # Check all endpoints (including intermediate) against existing wires
        for pt in path_points:
            for wire in sch.wires:
                if _point_on_wire_midpoint(pt, wire.start, wire.end):
                    junction_points.append(pt)
                    planned.append(
                        PlannedAction(
                            "junction",
                            f"Junction at ({pt[0]:.2f}, {pt[1]:.2f}) (wire intersection)",
                        )
                    )
                    break  # One junction per point is enough

    record(
        planned=[{"kind": a.kind, "description": a.description} for a in planned],
        junction_points=[[p[0], p[1]] for p in junction_points],
        wires_added=0,
        junctions_added=0,
    )

    if not planned:
        print("No wire segments to add (all zero-length after snapping)")
        return 0

    # Report planned actions
    if args.dry_run:
        print("DRY RUN - No changes will be made")
        print("=" * 60)

    print(f"Planned actions ({len(planned)}):")
    for action in planned:
        print(f"  [{action.kind}] {action.description}")

    if args.dry_run:
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        record(backup=backup_path)
        print(f"Backup created: {backup_path}")

    # Apply changes: add wire segments
    wires_added = 0
    for i in range(len(path_points) - 1):
        p1 = path_points[i]
        p2 = path_points[i + 1]
        if abs(p1[0] - p2[0]) < 0.01 and abs(p1[1] - p2[1]) < 0.01:
            continue  # Skip zero-length segments
        sch.add_wire(p1, p2)
        wires_added += 1

    # Add junctions
    for pt in junction_points:
        sch.add_junction(pt)

    # Save
    sch.save()

    record(wires_added=wires_added, junctions_added=len(junction_points))

    print(f"\nWires added: {wires_added}")
    if junction_points:
        print(f"Junctions added: {len(junction_points)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add wire segments to a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--from",
        nargs=2,
        type=float,
        required=True,
        dest="start",
        metavar=("X", "Y"),
        help="Start coordinate",
    )
    parser.add_argument(
        "--to",
        nargs=2,
        type=float,
        action="append",
        required=True,
        metavar=("X", "Y"),
        help="End coordinate (repeatable for multi-segment wires)",
    )
    parser.add_argument(
        "--junction",
        action="store_true",
        help="Auto-insert junctions where endpoints land on existing wire midpoints",
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    add_format_flag(parser)

    args = parser.parse_args(argv)
    return run_with_json_summary(
        "add-wire",
        args.schematic,
        lambda: run_add_wire(args),
        output_format=args.format,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
