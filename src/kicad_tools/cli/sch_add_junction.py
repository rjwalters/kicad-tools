#!/usr/bin/env python3
"""
Add a junction to a KiCad schematic file.

Usage:
    kct sch add-junction board.kicad_sch --at 125 50
    kct sch add-junction board.kicad_sch --at 125 50 --dry-run

Options:
    --at <x> <y>   Position of the junction
    --dry-run      Show planned actions without modifying
    --backup       Create timestamped backup before writing
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


def run_add_junction(args) -> int:
    """Execute the add-junction command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    position = (_snap(args.at[0]), _snap(args.at[1]))

    print(f"Planned: Junction at ({position[0]:.2f}, {position[1]:.2f})")

    if args.dry_run:
        print("DRY RUN - No changes will be made")
        return 0

    if args.backup:
        backup_path = (
            f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    junc = sch.add_junction(position)
    sch.save()

    print(
        f"Junction added at ({junc.position[0]:.2f}, {junc.position[1]:.2f})"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a junction to a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--at",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="Junction coordinates",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    args = parser.parse_args(argv)
    return run_add_junction(args)


if __name__ == "__main__":
    sys.exit(main())
