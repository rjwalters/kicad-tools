#!/usr/bin/env python3
"""
Clean up stale wires in a KiCad schematic.

Detects and removes zero-length wires and dangling wire endpoints
that are not connected to any pin, label, junction, or other wire.

Usage:
    kct sch cleanup-wires board.kicad_sch
    kct sch cleanup-wires board.kicad_sch --dry-run
    kct sch cleanup-wires board.kicad_sch --backup

Options:
    --dry-run              Show what would change without modifying
    --backup               Create backup before modifying
    --format {text,json}   Output format (default: text)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.schema import Schematic
from kicad_tools.schema.library import LibrarySymbol
from kicad_tools.sexp import SExp


@dataclass
class WireIssue:
    """Describes a wire that should be cleaned up."""

    reason: str  # "zero_length", "dangling", or "duplicate"
    wire_sexp: SExp
    start: tuple[float, float]
    end: tuple[float, float]


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


def _build_connection_map(
    schematic: Schematic,
    wire_sexps: list[SExp],
) -> set[tuple[int, int]]:
    """Build a set of all electrically connected points (labels, junctions, pins).

    This excludes wire endpoints themselves -- we build those separately
    so we can check whether a given wire endpoint touches another wire.
    """
    points: set[tuple[int, int]] = set()

    # Junctions
    for junc in schematic.junctions:
        points.add((int(junc.position[0] * 10), int(junc.position[1] * 10)))

    # Labels
    for lbl in schematic.labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    for lbl in schematic.global_labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    for lbl in schematic.hierarchical_labels:
        points.add((int(lbl.position[0] * 10), int(lbl.position[1] * 10)))

    # No-connect markers count as connections for this purpose
    for nc_node in schematic.sexp.find_all("no_connect"):
        if at := nc_node.find("at"):
            x = at.get_float(0) or 0
            y = at.get_float(1) or 0
            points.add((int(x * 10), int(y * 10)))

    # Symbol pin positions -- use library data for accurate pin locations
    for sym in schematic.symbols:
        lib_sexp = schematic.get_lib_symbol(sym.lib_id)
        if lib_sexp is not None:
            lib_sym = LibrarySymbol.from_sexp(lib_sexp)
            pin_positions = lib_sym.get_all_pin_positions(
                sym.position, sym.rotation, sym.mirror
            )
            for pos in pin_positions.values():
                points.add((int(pos[0] * 10), int(pos[1] * 10)))
        else:
            # Fallback to symbol center when library data is unavailable
            points.add((int(sym.position[0] * 10), int(sym.position[1] * 10)))
        # Power symbols always connect via their position
        if sym.lib_id.startswith("power:"):
            points.add((int(sym.position[0] * 10), int(sym.position[1] * 10)))

    return points


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


def find_cleanup_candidates(schematic: Schematic) -> list[WireIssue]:
    """Identify wires that should be removed."""
    wire_sexps = list(schematic.sexp.find_all("wire"))
    issues: list[WireIssue] = []

    # Phase 1: zero-length wires
    non_zero_wires = []
    for ws in wire_sexps:
        start, end = _wire_start_end(ws)
        if abs(start[0] - end[0]) < 0.01 and abs(start[1] - end[1]) < 0.01:
            issues.append(
                WireIssue(
                    reason="zero_length",
                    wire_sexp=ws,
                    start=start,
                    end=end,
                )
            )
        else:
            non_zero_wires.append(ws)

    # Phase 2: duplicate wires (same endpoints, order-insensitive)
    seen: dict[tuple[tuple[float, float], tuple[float, float]], SExp] = {}
    unique_wires = []
    for ws in non_zero_wires:
        start, end = _wire_start_end(ws)
        # Normalize endpoint order so (A->B) and (B->A) produce the same key
        key = (min(start, end), max(start, end))
        if key in seen:
            issues.append(
                WireIssue(
                    reason="duplicate",
                    wire_sexp=ws,
                    start=start,
                    end=end,
                )
            )
        else:
            seen[key] = ws
            unique_wires.append(ws)

    # Phase 3: dangling wires (endpoints not connected to anything else)
    connection_points = _build_connection_map(schematic, unique_wires)
    endpoint_counts = _wire_endpoint_counts(unique_wires)

    for ws in unique_wires:
        start, end = _wire_start_end(ws)

        dangling_ends = 0
        for pt in [start, end]:
            key = (int(pt[0] * 10), int(pt[1] * 10))
            # A point is "connected" if it touches a label/junction/pin
            # or if multiple wires share this endpoint
            has_connection = key in connection_points
            has_other_wire = endpoint_counts.get(key, 0) > 1

            if not has_connection and not has_other_wire:
                dangling_ends += 1

        # Only flag wires where BOTH ends are dangling (fully isolated)
        if dangling_ends == 2:
            issues.append(
                WireIssue(
                    reason="dangling",
                    wire_sexp=ws,
                    start=start,
                    end=end,
                )
            )

    return issues


def remove_wires(schematic: Schematic, issues: list[WireIssue]) -> int:
    """Remove flagged wires from the schematic's S-expression tree.

    Returns the number of wires removed.
    """
    removed = 0
    for issue in issues:
        if schematic.sexp.remove(issue.wire_sexp):
            removed += 1
    if removed:
        schematic.invalidate_cache()
    return removed


def run_cleanup_wires(args) -> int:
    """Execute the cleanup-wires command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except FileNotFoundError:
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    issues = find_cleanup_candidates(sch)

    if not issues:
        if args.format == "json":
            print(json.dumps({"removed": 0, "issues": []}, indent=2))
        else:
            print("No stale wires found.")
        return 0

    # Build output data
    zero_count = sum(1 for i in issues if i.reason == "zero_length")
    dangling_count = sum(1 for i in issues if i.reason == "dangling")
    duplicate_count = sum(1 for i in issues if i.reason == "duplicate")

    if args.format == "json":
        data = {
            "dry_run": args.dry_run,
            "removed": len(issues) if not args.dry_run else 0,
            "zero_length": zero_count,
            "dangling": dangling_count,
            "duplicate": duplicate_count,
            "issues": [
                {
                    "reason": i.reason,
                    "start": list(i.start),
                    "end": list(i.end),
                }
                for i in issues
            ],
        }
        if not args.dry_run:
            # Create backup if requested
            if args.backup:
                backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(schematic_path, backup_path)

            remove_wires(sch, issues)
            sch.save()

        print(json.dumps(data, indent=2))
        return 0

    # Text output
    if args.dry_run:
        print("DRY RUN - No changes will be made")
        print("=" * 60)

    print(f"Wires to clean up: {len(issues)}")
    if zero_count:
        print(f"  Zero-length: {zero_count}")
    if duplicate_count:
        print(f"  Duplicate: {duplicate_count}")
    if dangling_count:
        print(f"  Dangling: {dangling_count}")
    print()

    reason_labels = {"zero_length": "zero-length", "dangling": "dangling", "duplicate": "duplicate"}
    for issue in issues:
        label = reason_labels.get(issue.reason, issue.reason)
        print(
            f"  [{label}] ({issue.start[0]:.2f}, {issue.start[1]:.2f}) -> "
            f"({issue.end[0]:.2f}, {issue.end[1]:.2f})"
        )

    if args.dry_run:
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        print(f"\nBackup created: {backup_path}")

    removed = remove_wires(sch, issues)
    sch.save()
    print(f"\nRemoved {removed} wire(s)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Clean up stale wires in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")

    args = parser.parse_args(argv)
    return run_cleanup_wires(args)


if __name__ == "__main__":
    sys.exit(main())
