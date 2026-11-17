"""
CLI command for constraint conflict detection.

Usage:
    kct constraints check board.kicad_pcb              # Check all constraints
    kct constraints check board.kicad_pcb --format json   # JSON output
    kct constraints check board.kicad_pcb --keepout config.yaml  # With keepout config

Exit Codes:
    0 - No conflicts found
    1 - Conflicts found or command failure
"""

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.constraints import (
    ConstraintConflict,
    ConstraintConflictDetector,
)
from kicad_tools.constraints.locks import RegionConstraint
from kicad_tools.optim.constraints import GroupingConstraint
from kicad_tools.optim.keepout import (
    KeepoutZone,
    detect_keepout_zones,
    load_keepout_zones_from_yaml,
)
from kicad_tools.schema.pcb import PCB

if TYPE_CHECKING:
    pass


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct constraints command."""
    parser = argparse.ArgumentParser(
        prog="kct constraints check",
        description="Detect conflicts between placement/routing constraints",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file to check",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--keepout",
        metavar="FILE",
        help="YAML file defining keepout zones",
    )
    parser.add_argument(
        "--constraints",
        metavar="FILE",
        help="YAML file with grouping constraints",
    )
    parser.add_argument(
        "--auto-keepout",
        action="store_true",
        help="Auto-detect keepout zones from mounting holes and connectors",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed conflict information",
    )

    args = parser.parse_args(argv)

    # Load PCB
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Collect constraints
    keepout_zones: list[KeepoutZone] = []
    grouping_constraints: list[GroupingConstraint] = []
    region_constraints: list[RegionConstraint] = []

    # Load keepout zones
    if args.keepout:
        keepout_path = Path(args.keepout)
        if not keepout_path.exists():
            print(f"Error: Keepout file not found: {keepout_path}", file=sys.stderr)
            return 1
        try:
            keepout_zones.extend(load_keepout_zones_from_yaml(str(keepout_path)))
        except Exception as e:
            print(f"Error loading keepout zones: {e}", file=sys.stderr)
            return 1

    # Auto-detect keepout zones
    if args.auto_keepout:
        detected = detect_keepout_zones(pcb)
        keepout_zones.extend(detected)

    # Load grouping constraints
    if args.constraints:
        constraints_path = Path(args.constraints)
        if not constraints_path.exists():
            print(f"Error: Constraints file not found: {constraints_path}", file=sys.stderr)
            return 1
        try:
            grouping_constraints.extend(_load_grouping_constraints(constraints_path))
        except Exception as e:
            print(f"Error loading constraints: {e}", file=sys.stderr)
            return 1

    # Detect conflicts
    detector = ConstraintConflictDetector()
    conflicts = detector.detect(
        keepout_zones=keepout_zones,
        grouping_constraints=grouping_constraints,
        region_constraints=region_constraints,
        pcb=pcb,
    )

    # Output results
    if args.format == "json":
        output_json(conflicts, pcb_path)
    elif args.format == "summary":
        output_summary(conflicts, pcb_path)
    else:
        output_table(conflicts, pcb_path, args.verbose)

    return 1 if conflicts else 0


def _load_grouping_constraints(path: Path) -> list[GroupingConstraint]:
    """Load grouping constraints from YAML file."""
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)

    constraints = []
    for group_data in data.get("groups", []):
        from kicad_tools.optim.constraints import SpatialConstraint

        spatial_constraints = []
        for c in group_data.get("constraints", []):
            ctype = c.get("type")
            if ctype == "max_distance":
                spatial_constraints.append(
                    SpatialConstraint.max_distance(
                        anchor=c.get("anchor", ""),
                        radius_mm=c.get("radius_mm", 10.0),
                    )
                )
            elif ctype == "alignment":
                spatial_constraints.append(
                    SpatialConstraint.alignment(
                        axis=c.get("axis", "horizontal"),
                        tolerance_mm=c.get("tolerance_mm", 0.5),
                    )
                )

        constraints.append(
            GroupingConstraint(
                name=group_data.get("name", ""),
                members=group_data.get("members", []),
                constraints=spatial_constraints,
            )
        )

    return constraints


def output_table(
    conflicts: list[ConstraintConflict],
    pcb_path: Path,
    verbose: bool = False,
) -> None:
    """Output conflicts as a formatted table."""
    print(f"\n{'=' * 60}")
    print("CONSTRAINT CONFLICT CHECK")
    print(f"{'=' * 60}")
    print(f"File: {pcb_path.name}")
    print(f"Conflicts: {len(conflicts)}")

    if not conflicts:
        print(f"\n{'=' * 60}")
        print("NO CONFLICTS FOUND")
        return

    # Group by conflict type
    by_type: dict[str, list[ConstraintConflict]] = {}
    for c in conflicts:
        key = c.conflict_type.value
        if key not in by_type:
            by_type[key] = []
        by_type[key].append(c)

    print(f"\n{'-' * 60}")
    print("BY TYPE:")
    for ctype, clist in sorted(by_type.items()):
        print(f"  {ctype}: {len(clist)}")

    print(f"\n{'-' * 60}")
    print("CONFLICTS:")

    for i, conflict in enumerate(conflicts, 1):
        _print_conflict(i, conflict, verbose)

    print(f"\n{'=' * 60}")
    print(f"FOUND {len(conflicts)} CONFLICT(S) - Review and resolve")


def _print_conflict(index: int, conflict: ConstraintConflict, verbose: bool) -> None:
    """Print a single conflict."""
    symbol = "!" if conflict.conflict_type.value == "overlap" else "X"
    print(f"\n  [{symbol}] Conflict #{index}: {conflict.conflict_type.value.upper()}")
    print(
        f"      {conflict.constraint1_type}:{conflict.constraint1_name} vs "
        f"{conflict.constraint2_type}:{conflict.constraint2_name}"
    )
    print(f"      {conflict.description}")

    if conflict.location:
        print(f"      Location: ({conflict.location[0]:.2f}, {conflict.location[1]:.2f}) mm")

    if conflict.priority_winner:
        print(f"      Priority winner: {conflict.priority_winner}")

    if verbose and conflict.resolutions:
        print("      Possible resolutions:")
        for res in conflict.resolutions:
            priority_str = "+" * max(0, res.priority) if res.priority > 0 else ""
            print(f"        {priority_str} {res.action}: {res.description}")
            print(f"          Trade-off: {res.trade_off}")


def output_json(
    conflicts: list[ConstraintConflict],
    pcb_path: Path,
) -> None:
    """Output conflicts as JSON."""
    data = {
        "file": str(pcb_path),
        "summary": {
            "conflicts": len(conflicts),
            "passed": len(conflicts) == 0,
        },
        "conflicts": [c.to_dict() for c in conflicts],
    }
    print(json.dumps(data, indent=2))


def output_summary(
    conflicts: list[ConstraintConflict],
    pcb_path: Path,
) -> None:
    """Output conflict summary."""
    if not conflicts:
        print(f"PASSED: {pcb_path.name} - No constraint conflicts")
        return

    print(f"Constraint Conflicts: {pcb_path.name}")
    print("=" * 50)

    # Group by type
    by_type: dict[str, int] = {}
    for c in conflicts:
        key = c.conflict_type.value
        by_type[key] = by_type.get(key, 0) + 1

    print(f"{'Type':<20} {'Count':<10}")
    print("-" * 30)
    for ctype, count in sorted(by_type.items()):
        print(f"{ctype:<20} {count:<10}")
    print("-" * 30)
    print(f"{'TOTAL':<20} {len(conflicts):<10}")


if __name__ == "__main__":
    sys.exit(main())
