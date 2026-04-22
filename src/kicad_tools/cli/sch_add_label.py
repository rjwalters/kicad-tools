#!/usr/bin/env python3
"""
Add a label (local, global, or hierarchical) to a KiCad schematic file.

Supports placing all three label types with optional wire connections
from the label position to target coordinates.

Usage:
    kct sch add-label board.kicad_sch --type global --name I2S_BCLK \\
        --at 100 80 --shape output
    kct sch add-label board.kicad_sch --type local --name SDA --at 100 80
    kct sch add-label board.kicad_sch --type hierarchical --name CLK \\
        --at 100 80 --shape input --connect 120,80

Options:
    --type {global,local,hierarchical}  Label type (required)
    --name <name>                       Label text / net name (required)
    --at <x> <y>                        Placement position (required)
    --shape <shape>                     Label shape (global/hierarchical only)
    --connect <x,y>                     Draw wire from label to target (repeatable)
    --dry-run                           Show planned actions without modifying
    --backup                            Create timestamped backup before writing
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "label", "global_label", "hierarchical_label", "wire", "junction"
    description: str


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


def _point_on_wire_midpoint(
    point: tuple[float, float],
    wire_start: tuple[float, float],
    wire_end: tuple[float, float],
    tolerance: float = 0.1,
) -> bool:
    """Check if a point lies on a wire segment but not at its endpoints."""
    x, y = point

    # Check if point is at either endpoint
    for ep in (wire_start, wire_end):
        if abs(x - ep[0]) < tolerance and abs(y - ep[1]) < tolerance:
            return False

    # Check if point is on the segment
    x1, y1 = wire_start
    x2, y2 = wire_end

    # Bounding box check
    if not (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance):
        return False
    if not (min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance):
        return False

    # Perpendicular distance check
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length < tolerance:
        return False

    dist = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / length
    return dist < tolerance


def parse_connect_target(spec: str) -> tuple[float, float]:
    """Parse a --connect argument like '120,80' into (x, y) coordinates."""
    if "," not in spec:
        raise ValueError(
            f"Invalid --connect format: '{spec}'. Expected 'x,y' (e.g., '120,80')"
        )

    parts = spec.split(",")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid --connect format: '{spec}'. Expected exactly 'x,y'"
        )

    try:
        x = float(parts[0].strip())
        y = float(parts[1].strip())
    except ValueError:
        raise ValueError(
            f"Invalid coordinate values in --connect: '{spec}'. Expected numeric x,y"
        )

    return (x, y)


VALID_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}


def run_add_label(args) -> int:
    """Execute the add-label command."""
    schematic_path = Path(args.schematic)

    # Validate --shape with --type local
    label_type = args.type
    shape = getattr(args, "shape", None)

    if label_type == "local" and shape is not None:
        print(
            "Error: --shape is not valid for local labels (local labels have no shape attribute)",
            file=sys.stderr,
        )
        return 1

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Snap placement coordinates to grid
    at_x = _snap(args.at[0])
    at_y = _snap(args.at[1])
    position = (at_x, at_y)

    rotation = getattr(args, "rotation", 0) or 0
    name = args.name

    # Default shape for global/hierarchical if not provided
    if label_type != "local" and shape is None:
        shape = "input"

    # Collect planned actions
    planned: list[PlannedAction] = []

    # Map label type to s-expression tag name for reporting
    type_to_tag = {
        "local": "label",
        "global": "global_label",
        "hierarchical": "hierarchical_label",
    }
    tag = type_to_tag[label_type]

    if label_type == "local":
        planned.append(
            PlannedAction(
                tag,
                f"Place local label '{name}' at ({at_x:.2f}, {at_y:.2f})"
                f" rotation={rotation}",
            )
        )
    else:
        planned.append(
            PlannedAction(
                tag,
                f"Place {label_type} label '{name}' (shape={shape})"
                f" at ({at_x:.2f}, {at_y:.2f}) rotation={rotation}",
            )
        )

    # Plan wire connections
    connect_targets: list[tuple[float, float]] = []
    if args.connects:
        for spec_str in args.connects:
            try:
                target = parse_connect_target(spec_str)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            # Snap connect target to grid
            target = (_snap(target[0]), _snap(target[1]))
            connect_targets.append(target)

    for target in connect_targets:
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from ({at_x:.2f}, {at_y:.2f})"
                f" to ({target[0]:.2f}, {target[1]:.2f})",
            )
        )

        # Check if target intersects existing wires at midpoints
        for wire in sch.wires:
            if _point_on_wire_midpoint(target, wire.start, wire.end):
                planned.append(
                    PlannedAction(
                        "junction",
                        f"Junction at ({target[0]:.2f}, {target[1]:.2f})"
                        f" (wire intersection)",
                    )
                )
                break

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
        backup_path = (
            f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    # Apply changes

    # 1. Place the label
    if label_type == "local":
        label_instance = sch.add_label(name, position, rotation)
    elif label_type == "global":
        label_instance = sch.add_global_label(name, position, rotation, shape)
    else:  # hierarchical
        label_instance = sch.add_hierarchical_label(name, position, rotation, shape)

    # 2. Add wire connections
    for target in connect_targets:
        sch.add_wire(position, target)

        # Add junction if target intersects an existing wire midpoint
        for wire in sch.wires:
            # Skip the wire we just added (last wire in the list)
            if wire is sch.wires[-1]:
                continue
            if _point_on_wire_midpoint(target, wire.start, wire.end):
                sch.add_junction(target)
                break

    # 3. Save
    sch.save()

    print(f"\nLabel placed successfully: {label_instance.text}")
    print(f"  Type: {label_type}")
    if label_type != "local":
        print(f"  Shape: {label_instance.shape}")
    print(f"  Position: ({position[0]:.2f}, {position[1]:.2f})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a label to a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--type",
        required=True,
        choices=["global", "local", "hierarchical"],
        help="Label type",
    )
    parser.add_argument(
        "--name", required=True, help="Label text / net name"
    )
    parser.add_argument(
        "--at",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="Placement coordinates",
    )
    parser.add_argument(
        "--shape",
        choices=["input", "output", "bidirectional", "tri_state", "passive"],
        default=None,
        help="Label shape (global and hierarchical only)",
    )
    parser.add_argument(
        "--rotation", type=float, default=0, help="Rotation in degrees (default: 0)"
    )
    parser.add_argument(
        "--connect",
        action="append",
        dest="connects",
        metavar="X,Y",
        help="Draw wire from label to target coordinates (e.g., 120,80). Repeatable.",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    args = parser.parse_args(argv)
    return run_add_label(args)


if __name__ == "__main__":
    sys.exit(main())
