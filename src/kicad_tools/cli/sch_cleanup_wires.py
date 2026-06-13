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
import math
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.schema import Schematic
from kicad_tools.schema.library import LibrarySymbol
from kicad_tools.sexp import SExp

# Quantization multiplier for coordinate hashing.  Using 1000 gives
# micron-level (0.001mm) resolution, which prevents false "connected"
# results from bucket collisions that occurred at the old 0.1mm (×10)
# resolution.
_QUANT = 1000

# Any wire shorter than this (mm) is unconditionally flagged as a stub,
# regardless of how many endpoints appear connected.  No intentional
# schematic wire is ever this short.
_MICRO_STUB_FLOOR = 0.05

# Tolerance (mm) used when deciding whether a wire endpoint coincides with
# a "strong anchor" (label, pin, sheet pin, no-connect).  This matches
# `validate/sch_orphan_label._COORD_EPS` so that files which pass the
# orphan-label validator can never be torn apart by ``cleanup-wires``.
# It is intentionally looser than the 1 um quantization bucket used for
# everything else, because schematic edits / repairs frequently re-emit
# coordinates with sub-um float drift.
_ANCHOR_EPS = 0.01


@dataclass
class WireIssue:
    """Describes a wire that should be cleaned up."""

    reason: str  # "zero_length", "dangling", "duplicate", "overlap", or "stub"
    wire_sexp: SExp
    start: tuple[float, float]
    end: tuple[float, float]


def _quantize(coord: float) -> int:
    """Quantize a coordinate to an integer bucket at micron resolution."""
    return int(round(coord * _QUANT))


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


def _point_on_segment(
    point: tuple[float, float],
    seg_start: tuple[float, float],
    seg_end: tuple[float, float],
    tolerance: float = 0.005,
) -> bool:
    """Return True if *point* lies within *tolerance* mm of the line segment.

    Uses perpendicular distance plus a parametric bounds check so that
    the test works for points anywhere along the segment body, not just
    at the endpoints.

    The endpoints themselves are *excluded* (returns False when the point
    is within ``tolerance`` of either endpoint) because endpoint-to-endpoint
    matching is already handled by the caller.
    """
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end

    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq < tolerance * tolerance:
        # Degenerate (zero-length) segment -- skip
        return False

    # Parametric projection of point onto the infinite line through A-B
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq

    # Exclude the very ends (those are covered by endpoint matching)
    if t <= 0.0 or t >= 1.0:
        return False

    # Closest point on the segment to *point*
    cx = ax + t * dx
    cy = ay + t * dy
    dist_sq = (px - cx) ** 2 + (py - cy) ** 2

    return dist_sq <= tolerance * tolerance


def _is_collinear_overlap(
    seg1_start: tuple[float, float],
    seg1_end: tuple[float, float],
    seg2_start: tuple[float, float],
    seg2_end: tuple[float, float],
    tolerance: float = 0.005,
) -> bool:
    """Return True if seg2 is fully enclosed within the collinear seg1.

    Both segments must share the same direction vector (within *tolerance*)
    and seg2's endpoints must both lie on seg1's body (not just touching
    at a single endpoint).
    """
    ax, ay = seg1_start
    bx, by = seg1_end

    dx = bx - ax
    dy = by - ay
    seg1_len_sq = dx * dx + dy * dy

    if seg1_len_sq < tolerance * tolerance:
        return False

    seg1_len = math.sqrt(seg1_len_sq)

    # Check that both seg2 endpoints lie on the line through seg1
    for pt in [seg2_start, seg2_end]:
        px, py = pt
        # Perpendicular distance to infinite line
        cross = abs((px - ax) * dy - (py - ay) * dx)
        if cross / seg1_len > tolerance:
            return False

    # Project seg2 endpoints onto seg1's parameterised line
    t_vals = []
    for pt in [seg2_start, seg2_end]:
        px, py = pt
        t = ((px - ax) * dx + (py - ay) * dy) / seg1_len_sq
        t_vals.append(t)

    t_min = min(t_vals)
    t_max = max(t_vals)

    # seg2 must be *strictly inside* seg1 (not just sharing an endpoint)
    eps = tolerance / seg1_len
    return t_min >= -eps and t_max <= 1.0 + eps and (t_max - t_min) > eps


def _build_connection_map(
    schematic: Schematic,
    wire_sexps: list[SExp],
) -> set[tuple[int, int]]:
    """Build a set of all electrically connected points (labels, junctions, pins).

    This excludes wire endpoints themselves -- we build those separately
    so we can check whether a given wire endpoint touches another wire.

    The returned set covers all anchor kinds enumerated by
    :func:`_collect_strong_anchors`, quantized to the 1 um bucket used for
    fast exact lookup.  For tolerance-aware matching (e.g. against files
    with sub-um float drift) callers should use the strong-anchor list
    directly instead.
    """
    points: set[tuple[int, int]] = set()
    for x, y in _collect_strong_anchors(schematic):
        points.add((_quantize(x), _quantize(y)))
    return points


def _collect_strong_anchors(
    schematic: Schematic,
) -> list[tuple[float, float]]:
    """Enumerate every "strong anchor" position in *schematic*.

    A strong anchor is a non-wire object whose position is the canonical
    end of a net segment in KiCad's electrical model: junctions, labels of
    every kind (local / global / hierarchical / directive / netclass),
    no-connect markers, symbol pins, and hierarchical sheet pins.

    Wire endpoints anchored at any of these positions are by definition
    load-bearing -- ``cleanup-wires`` must never remove them.
    """
    anchors: list[tuple[float, float]] = []

    # Junctions
    for junc in schematic.junctions:
        anchors.append((junc.position[0], junc.position[1]))

    # Local / global / hierarchical labels
    for lbl in schematic.labels:
        anchors.append((lbl.position[0], lbl.position[1]))
    for lbl in schematic.global_labels:
        anchors.append((lbl.position[0], lbl.position[1]))
    for lbl in schematic.hierarchical_labels:
        anchors.append((lbl.position[0], lbl.position[1]))

    # KiCad 8 introduced ``directive_label`` and ``netclass_flag`` nodes
    # that are not exposed through :class:`Schematic` properties.  Read
    # them directly so a wire endpoint anchored on one of them is still
    # considered load-bearing.
    for tag in ("directive_label", "netclass_flag"):
        for node in schematic.sexp.find_all(tag):
            if at := node.find("at"):
                anchors.append((at.get_float(0) or 0.0, at.get_float(1) or 0.0))

    # No-connect markers
    for nc_node in schematic.sexp.find_all("no_connect"):
        if at := nc_node.find("at"):
            anchors.append((at.get_float(0) or 0.0, at.get_float(1) or 0.0))

    # Hierarchical sheet pins: each (sheet ...) block may contain
    # (pin "NAME" SHAPE (at X Y R) ...) child nodes that mark the
    # connection points on the sheet boundary.  These are NOT enumerated
    # by Schematic.hierarchical_labels (those live inside the child sheet,
    # not on the parent's sheet block).
    for sheet_node in schematic.sexp.find_all("sheet"):
        for pin_node in sheet_node.find_all("pin"):
            if at := pin_node.find("at"):
                anchors.append((at.get_float(0) or 0.0, at.get_float(1) or 0.0))

    # Symbol pin positions -- use library data for accurate pin locations
    for sym in schematic.symbols:
        lib_sexp = schematic.get_lib_symbol(sym.lib_id)
        if lib_sexp is not None:
            lib_sym = LibrarySymbol.from_sexp(lib_sexp)
            pin_positions = lib_sym.get_all_pin_positions(sym.position, sym.rotation, sym.mirror)
            for pos in pin_positions.values():
                anchors.append((pos[0], pos[1]))
        else:
            # Fallback to symbol center when library data is unavailable
            anchors.append((sym.position[0], sym.position[1]))
        # Power symbols always connect via their position
        if sym.lib_id.startswith("power:"):
            anchors.append((sym.position[0], sym.position[1]))

    return anchors


def _endpoint_at_strong_anchor(
    point: tuple[float, float],
    strong_anchors: list[tuple[float, float]],
    tolerance: float = _ANCHOR_EPS,
) -> bool:
    """Return ``True`` if *point* is within *tolerance* mm of any anchor.

    This is the tolerance-aware veto used by stub detection (Phase 3b).
    It deliberately does NOT use the 1 um quantization bucket -- files
    that round-trip through repair tools frequently introduce sub-um
    float drift, which would otherwise miss a legitimate label/pin
    anchor and let the stub heuristic remove a load-bearing wire.
    """
    px, py = point
    return any(abs(px - ax) <= tolerance and abs(py - ay) <= tolerance for ax, ay in strong_anchors)


def _wire_endpoint_counts(
    wire_sexps: list[SExp],
) -> dict[tuple[int, int], int]:
    """Count how many wires touch each endpoint."""
    counts: dict[tuple[int, int], int] = {}
    for ws in wire_sexps:
        start, end = _wire_start_end(ws)
        for pt in [start, end]:
            key = (_quantize(pt[0]), _quantize(pt[1]))
            counts[key] = counts.get(key, 0) + 1
    return counts


def _endpoint_touches_other_wire_body(
    point: tuple[float, float],
    wire_sexp: SExp,
    all_wires: list[SExp],
    tolerance: float = 0.005,
) -> bool:
    """Return True if *point* lies on the body of any wire other than *wire_sexp*.

    This catches T-junction connections where a wire endpoint lands on the
    midpoint of another wire segment, which endpoint-to-endpoint matching
    alone would miss.
    """
    wire_id = id(wire_sexp)
    for other_ws in all_wires:
        if id(other_ws) == wire_id:
            continue
        other_start, other_end = _wire_start_end(other_ws)
        if _point_on_segment(point, other_start, other_end, tolerance):
            return True
    return False


def _is_endpoint_connected(
    point: tuple[float, float],
    wire_sexp: SExp,
    connection_points: set[tuple[int, int]],
    endpoint_counts: dict[tuple[int, int], int],
    all_wires: list[SExp],
) -> bool:
    """Return True if *point* is electrically connected.

    A point is connected if:
    - it touches a label, junction, pin, or no-connect marker, OR
    - multiple wires share this exact endpoint, OR
    - it lies on the body (mid-segment) of another wire.

    This is the *geometric* connectivity check used for Phase 3 (fully
    dangling detection).  For stub detection see
    :func:`_is_endpoint_electrically_connected`.
    """
    key = (_quantize(point[0]), _quantize(point[1]))
    if key in connection_points:
        return True
    if endpoint_counts.get(key, 0) > 1:
        return True
    if _endpoint_touches_other_wire_body(point, wire_sexp, all_wires):
        return True
    return False


def _is_endpoint_electrically_connected(
    point: tuple[float, float],
    wire_sexp: SExp,
    connection_points: set[tuple[int, int]],
    endpoint_counts: dict[tuple[int, int], int],
) -> bool:
    """Return True if *point* has a real electrical connection.

    Unlike :func:`_is_endpoint_connected`, this does **not** count a
    wire-body touch (T-junction without a junction marker) as connected.
    In KiCad's electrical model, a wire endpoint that lands on the
    interior of another wire without a junction symbol is *not* connected
    -- the ERC reports it as "Wire endpoint is not connected".

    This stricter check is used for stub detection (Phase 3b) so that
    sub-mm stubs whose only "connection" is a wire-body overlap are
    correctly flagged for removal.
    """
    key = (_quantize(point[0]), _quantize(point[1]))
    if key in connection_points:
        return True
    if endpoint_counts.get(key, 0) > 1:
        return True
    return False


def find_cleanup_candidates(
    schematic: Schematic, *, stub_threshold: float = 1.27
) -> list[WireIssue]:
    """Identify wires that should be removed.

    Args:
        schematic: The schematic to analyze.
        stub_threshold: Maximum wire length (in mm) for a single-end-dangling
            wire to be flagged as a stub. Set to 0 to disable stub detection.
    """
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

    # Phase 2b: collinear overlap detection
    # For each pair of unique wires, check if the shorter one is fully
    # enclosed within the longer one.  Flag the shorter as "overlap".
    overlap_ids: set[int] = set()
    for i, ws_a in enumerate(unique_wires):
        if id(ws_a) in overlap_ids:
            continue
        a_start, a_end = _wire_start_end(ws_a)
        a_len_sq = (a_end[0] - a_start[0]) ** 2 + (a_end[1] - a_start[1]) ** 2
        for j in range(i + 1, len(unique_wires)):
            ws_b = unique_wires[j]
            if id(ws_b) in overlap_ids:
                continue
            b_start, b_end = _wire_start_end(ws_b)
            b_len_sq = (b_end[0] - b_start[0]) ** 2 + (b_end[1] - b_start[1]) ** 2

            # Check if the shorter is enclosed in the longer
            if a_len_sq >= b_len_sq:
                if _is_collinear_overlap(a_start, a_end, b_start, b_end):
                    overlap_ids.add(id(ws_b))
                    issues.append(
                        WireIssue(
                            reason="overlap",
                            wire_sexp=ws_b,
                            start=b_start,
                            end=b_end,
                        )
                    )
            else:
                if _is_collinear_overlap(b_start, b_end, a_start, a_end):
                    overlap_ids.add(id(ws_a))
                    issues.append(
                        WireIssue(
                            reason="overlap",
                            wire_sexp=ws_a,
                            start=a_start,
                            end=a_end,
                        )
                    )
                    break  # ws_a is flagged, move on

    # Remove overlapping wires from the unique set for subsequent phases
    unique_wires = [ws for ws in unique_wires if id(ws) not in overlap_ids]

    # Phase 3: dangling wires (endpoints not connected to anything else)
    connection_points = _build_connection_map(schematic, unique_wires)
    strong_anchors = _collect_strong_anchors(schematic)
    endpoint_counts = _wire_endpoint_counts(unique_wires)

    for ws in unique_wires:
        start, end = _wire_start_end(ws)

        dangling_ends = 0
        for pt in [start, end]:
            if not _is_endpoint_connected(pt, ws, connection_points, endpoint_counts, unique_wires):
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

    # Phase 3b: short single-end-dangling stubs
    # These are sub-mm wire fragments left by repair operations that have one
    # end connected and one end dangling.  Only flag them when their length is
    # below the configurable threshold (default 1.27mm).
    #
    # We use the *strict* electrical connectivity check here: a wire
    # endpoint that merely touches another wire's body (T-junction without
    # a junction marker) is NOT considered connected, because KiCad's ERC
    # treats it the same way.  This ensures that sub-mm stubs whose only
    # "anchor" is a wire-body overlap are correctly detected.
    #
    # Sub-mm stubs where *both* endpoints appear electrically connected are
    # also caught in two ways:
    #   (a) Micro-stubs (length < _MICRO_STUB_FLOOR) are unconditionally
    #       flagged -- no intentional wire is ever that short.
    #   (b) Short wires where both endpoints are connected only through
    #       shared wire endpoints (no pin, label, or junction at either
    #       end) are likely repair artifacts and are flagged as stubs.
    if stub_threshold > 0:
        # Build a set of wires already flagged so we don't double-count
        flagged_ids = {id(issue.wire_sexp) for issue in issues}

        for ws in unique_wires:
            if id(ws) in flagged_ids:
                continue

            start, end = _wire_start_end(ws)
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            if length >= stub_threshold:
                continue

            # (a) Micro-stubs: unconditionally flag wires shorter than the
            # floor -- no real schematic wire is ever this short.  This is
            # intentionally checked BEFORE the strong-anchor veto, because
            # zero-area wire fragments at a label position are still
            # invalid (KiCad treats them as ERC errors).
            if length < _MICRO_STUB_FLOOR:
                issues.append(
                    WireIssue(
                        reason="stub",
                        wire_sexp=ws,
                        start=start,
                        end=end,
                    )
                )
                continue

            # Hard veto: if EITHER endpoint coincides (within
            # _ANCHOR_EPS) with any strong anchor -- a label of any kind,
            # a symbol pin, a no-connect, or a hierarchical sheet pin --
            # the wire is load-bearing and must never be classified as a
            # stub.  This is the fix for the J2.24-style perpendicular
            # L-shape into a label, where the wire endpoint is exactly on
            # the label position but the quantization bucket misses it
            # because of sub-um float drift introduced by repair tools.
            if _endpoint_at_strong_anchor(start, strong_anchors) or _endpoint_at_strong_anchor(
                end, strong_anchors
            ):
                continue

            electrically_dangling = 0
            for pt in [start, end]:
                if not _is_endpoint_electrically_connected(
                    pt, ws, connection_points, endpoint_counts
                ):
                    electrically_dangling += 1

            # At least one electrically-dangling end on a short wire is a stub.
            # With the strict check, a stub anchored only by a wire-body touch
            # will show electrically_dangling == 2 (both ends have no real
            # connection).  A stub with one end on a shared wire endpoint and
            # the free end dangling will show electrically_dangling == 1.
            # Both cases should be flagged.
            if electrically_dangling >= 1:
                issues.append(
                    WireIssue(
                        reason="stub",
                        wire_sexp=ws,
                        start=start,
                        end=end,
                    )
                )
                continue

            # (b) Both endpoints are electrically connected.  Check if
            # neither endpoint touches a "strong" connection (pin, label,
            # junction, no-connect).  A short wire that merely bridges two
            # other wire endpoints without any component-level anchor is
            # very likely a repair artifact (the typical chorus-test-revA
            # pattern: 0.05-0.43mm fragments left between wire endpoints).
            start_key = (_quantize(start[0]), _quantize(start[1]))
            end_key = (_quantize(end[0]), _quantize(end[1]))

            start_at_connection_point = start_key in connection_points
            end_at_connection_point = end_key in connection_points

            if not start_at_connection_point and not end_at_connection_point:
                # Neither end touches a pin, label, junction, or no-connect.
                # Both ends are connected only via shared wire endpoints.
                # Flag as a stub -- it is a redundant bridge fragment.
                issues.append(
                    WireIssue(
                        reason="stub",
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

    stub_threshold = getattr(args, "stub_threshold", 1.27)
    issues = find_cleanup_candidates(sch, stub_threshold=stub_threshold)

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
    overlap_count = sum(1 for i in issues if i.reason == "overlap")
    stub_count = sum(1 for i in issues if i.reason == "stub")

    if args.format == "json":
        data = {
            "dry_run": args.dry_run,
            "removed": len(issues) if not args.dry_run else 0,
            "zero_length": zero_count,
            "dangling": dangling_count,
            "duplicate": duplicate_count,
            "overlap": overlap_count,
            "stub": stub_count,
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
    if overlap_count:
        print(f"  Overlap: {overlap_count}")
    if dangling_count:
        print(f"  Dangling: {dangling_count}")
    if stub_count:
        print(f"  Stub: {stub_count}")
    print()

    reason_labels = {
        "zero_length": "zero-length",
        "dangling": "dangling",
        "duplicate": "duplicate",
        "overlap": "overlap",
        "stub": "stub",
    }
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
    parser.add_argument(
        "--stub-threshold",
        type=float,
        default=1.27,
        dest="stub_threshold",
        help="Max length (mm) for single-end-dangling stubs to remove (default: 1.27, 0 to disable)",
    )

    args = parser.parse_args(argv)
    return run_cleanup_wires(args)


if __name__ == "__main__":
    sys.exit(main())
