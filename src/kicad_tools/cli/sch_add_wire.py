#!/usr/bin/env python3
"""
Add a wire segment to a KiCad schematic file.

Usage:
    kct sch add-wire board.kicad_sch --from 100 80 --to 120 80
    kct sch add-wire board.kicad_sch --from 100 80 --to 120 80 --dry-run

Options:
    --from <x> <y>   Start point of the wire
    --to <x> <y>     End point of the wire
    --dry-run        Show planned actions without modifying
    --backup         Create timestamped backup before writing
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


def run_add_wire(args) -> int:
    """Execute the add-wire command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    start = (_snap(args.start[0]), _snap(args.start[1]))
    end = (_snap(args.end[0]), _snap(args.end[1]))

    # Skip zero-length wires
    if abs(start[0] - end[0]) < 0.01 and abs(start[1] - end[1]) < 0.01:
        print("Error: Wire start and end are the same point", file=sys.stderr)
        return 1

    print(
        f"Planned: Wire from ({start[0]:.2f}, {start[1]:.2f})"
        f" to ({end[0]:.2f}, {end[1]:.2f})"
    )

    if args.dry_run:
        print("DRY RUN - No changes will be made")
        return 0

    if args.backup:
        backup_path = (
            f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    wire = sch.add_wire(start, end)
    sch.save()

    print(
        f"Wire added: ({wire.start[0]:.2f}, {wire.start[1]:.2f})"
        f" -> ({wire.end[0]:.2f}, {wire.end[1]:.2f})"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a wire segment to a KiCad schematic",
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
        help="Wire start coordinates",
    )
    parser.add_argument(
        "--to",
        nargs=2,
        type=float,
        required=True,
        dest="end",
        metavar=("X", "Y"),
        help="Wire end coordinates",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    args = parser.parse_args(argv)
    return run_add_wire(args)


if __name__ == "__main__":
    sys.exit(main())
