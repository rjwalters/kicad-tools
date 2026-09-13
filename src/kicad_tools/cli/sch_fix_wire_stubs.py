"""Repair exact-grid wire stubs only when the extension is unambiguous and clear."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kicad_tools.cli.sch_json import add_format_flag, record, run_with_json_summary
from kicad_tools.schema import Schematic
from kicad_tools.sexp import SExp
from kicad_tools.validate.sch_wire_stub import (
    _COORD_EPS,
    DEFAULT_GRID_MM,
    DEFAULT_MAX_STUB_GRIDS,
    WireStubFinding,
    _find_grid_aligned_pin,
    find_wire_stubs,
)

Point = tuple[float, float]


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    cross = (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])
    return abs(cross) <= _COORD_EPS * max(1, abs(b[0] - a[0]), abs(b[1] - a[1])) and all(
        min(a[i], b[i]) - _COORD_EPS <= p[i] <= max(a[i], b[i]) + _COORD_EPS for i in (0, 1)
    )


def _intersects(a: Point, b: Point, c: Point, d: Point) -> bool:
    def cross(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    if any(
        (_on_segment(c, a, b), _on_segment(d, a, b), _on_segment(a, c, d), _on_segment(b, c, d))
    ):
        return True
    return cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0


def _load_design(path: Path) -> tuple[dict[Path, Schematic], dict[Path, bytes]]:
    sheets: dict[Path, Schematic] = {}
    snapshots: dict[Path, bytes] = {}

    def visit(source: Path) -> None:
        source = source.resolve(strict=True)
        if source in sheets:
            return
        before = source.read_bytes()
        sch = Schematic.load(source)
        if source.read_bytes() != before:
            raise ValueError(f"Schematic changed while loading: {source}")
        snapshots[source] = before
        sheets[source] = sch
        for sheet in sch.sheets:
            if not sheet.filename:
                raise ValueError(f"Missing sheet filename in {source}")
            visit(source.parent / sheet.filename)

    visit(path)
    return sheets, snapshots


def _check_library_geometry(sch: Schematic, lib_id: str, seen: set[str] | None = None) -> None:
    """Reject body styles/common units the schema pin model cannot distinguish."""
    seen = set() if seen is None else seen
    if lib_id in seen:
        raise ValueError(f"Circular library inheritance: {lib_id}")
    seen.add(lib_id)
    node = sch.get_lib_symbol(lib_id)
    if node is None:
        raise ValueError(f"Missing library definition: {lib_id}")
    for unit in node.find_all("symbol"):
        if not unit.find_all("pin"):
            continue
        suffix = (unit.get_string(0) or "").rsplit("_", 2)
        if len(suffix) != 3 or suffix[1:] != ["1", "1"]:
            raise ValueError(f"Unsupported pin unit or body style in {lib_id}")
    base = node.find("extends")
    if base is not None:
        name = base.get_string(0)
        definitions = sch.lib_symbols
        matches = (
            []
            if definitions is None
            else [
                candidate.get_string(0)
                for candidate in definitions.find_all("symbol")
                if candidate.get_string(0) == name
                or (candidate.get_string(0) or "").split(":")[-1] == name
            ]
        )
        if len(matches) != 1 or matches[0] is None:
            raise ValueError(f"Missing or ambiguous library base: {name}")
        _check_library_geometry(sch, matches[0], seen)


def _pins_and_libs(sch: Schematic):
    libs = {}
    pins: list[tuple[str, str, float, float]] = []
    for symbol in sch.symbols:
        _check_library_geometry(sch, symbol.lib_id)
        lib = sch.get_lib_symbol_resolved(symbol.lib_id)
        if lib is None or (lib.extends and not lib.pins):
            raise ValueError(f"Missing or unresolved library symbol: {symbol.lib_id}")
        if lib.units != 1 or symbol.unit != 1:
            raise ValueError(f"Unsupported multi-unit geometry: {symbol.lib_id}")
        if len({pin.number for pin in lib.pins}) != len(lib.pins):
            raise ValueError(f"Ambiguous duplicate pin numbers: {symbol.lib_id}")
        libs[symbol.lib_id] = lib
        # Resolve eagerly: the read-only detector intentionally suppresses errors;
        # repair must fail before changing any sheet if connectivity is unknown.
        positions = lib.get_all_pin_positions(
            instance_pos=symbol.position,
            instance_rot=symbol.rotation,
            mirror=symbol.mirror,
            snap_to_grid=False,
        )
        pins.extend((symbol.reference or "", number, x, y) for number, (x, y) in positions.items())
    return pins, libs


def _wire_node(sch: Schematic, finding: WireStubFinding) -> tuple[SExp | None, str]:
    if not finding.wire_uuid:
        return None, "missing wire UUID"
    nodes = [
        node
        for node in sch.sexp.find_all("wire")
        if (uuid := node.find("uuid")) is not None and uuid.get_string(0) == finding.wire_uuid
    ]
    if len(nodes) != 1:
        return None, "wire UUID is missing or duplicated"
    if len(nodes[0].find_all("uuid")) != 1:
        return None, "duplicated wire UUID fields"
    pts = nodes[0].find_all("pts")
    if len(pts) != 1:
        return None, "malformed wire points"
    xy = pts[0].find_all("xy")
    if len(xy) != 2 or finding.endpoint_index not in (0, 1):
        return None, "invalid endpoint identity"
    for node, expected in zip(xy, (finding.wire_start, finding.wire_end), strict=True):
        if len(node.children) != 2 or tuple(child.value for child in node.children) != expected:
            return None, "stale wire endpoints"
    if (finding.wire_start, finding.wire_end)[finding.endpoint_index] != finding.dangling_endpoint:
        return None, "stale dangling endpoint"
    return xy[finding.endpoint_index], ""


def _unsafe_reason(sch: Schematic, finding: WireStubFinding, pins) -> str:
    a, b = finding.dangling_endpoint, finding.candidate_pin_position
    if not any(
        ref + "." + number == finding.candidate_pin_ref and (x, y) == b
        for ref, number, x, y in pins
    ):
        return "candidate is not an exact physical pin position"
    other = (finding.wire_start, finding.wire_end)[1 - finding.endpoint_index]
    axis = 0 if finding.axis == "x" else 1
    # Exact collinearity avoids bending or shortening even a slightly off-axis wire.
    if (
        other[1 - axis] != a[1 - axis]
        or b[1 - axis] != a[1 - axis]
        or (a[axis] - other[axis]) * (b[axis] - a[axis]) <= 0
    ):
        return "not a straight outward extension"
    matches = [
        _find_grid_aligned_pin(a, [pin], DEFAULT_GRID_MM, DEFAULT_MAX_STUB_GRIDS) for pin in pins
    ]
    nearest = [m for m in matches if m is not None and m[4] <= finding.grid_steps_short]
    if len(nearest) != 1:
        return "ambiguous nearest pin"
    for ref, num, x, y in pins:
        if (ref + "." + num, (x, y)) == (finding.candidate_pin_ref, b):
            continue
        if _on_segment((x, y), a, b):
            return "extension touches another pin"
    anchors = [
        item.position
        for collection in (
            sch.junctions,
            sch.labels,
            sch.hierarchical_labels,
            sch.global_labels,
            sch.no_connects,
        )
        for item in collection
    ]
    # Hierarchical sheet pins are electrical anchors too.
    for sheet in sch.sexp.find_all("sheet"):
        for pin in sheet.find_all("pin"):
            at = pin.find("at")
            if at is not None:
                x, y = at.get_float(0), at.get_float(1)
                if x is None or y is None:
                    raise ValueError("Malformed sheet pin position")
                anchors.append((x, y))
    if any(_on_segment(p, a, b) for p in anchors):
        return "extension touches a junction, label, sheet pin or no-connect"
    for wire in sch.wires:
        if wire.uuid != finding.wire_uuid and _intersects(a, b, wire.start, wire.end):
            return "extension intersects another wire"
    return ""


def _save_changes(changed: dict[Path, Schematic], snapshots: dict[Path, bytes]) -> None:
    """Stage all sheets before replacing; roll back completed writes on failure.

    Recheck every dependency immediately before writing. This detects concurrent
    editor saves during planning, although filesystem replacement is not a lock.
    """
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, sch in changed.items():
            for collection in (staged, backups):
                fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                os.close(fd)
                collection[path] = Path(name)
                collection[path].chmod(stat.S_IMODE(path.stat().st_mode))
            backups[path].write_bytes(snapshots[path])
            sch.invalidate_cache()
            try:
                sch.save(staged[path])
            finally:
                # The atomic writer retains its own temporary on failure. This
                # destination is already private staging, so we own its cleanup.
                staged[path].with_suffix(staged[path].suffix + ".tmp").unlink(missing_ok=True)
            # Saving atomically replaces the staging inode; restore source mode
            # on the completed file before publishing it over the original.
            staged[path].chmod(stat.S_IMODE(path.stat().st_mode))
        for path, original in snapshots.items():
            if path.read_bytes() != original:
                raise ValueError(f"Schematic changed since planning: {path}")
        for path, temporary in staged.items():
            if path.read_bytes() != snapshots[path]:
                raise ValueError(f"Schematic changed before replacement: {path}")
            os.replace(temporary, path)
            replaced.append(path)
    except Exception as exc:
        failures = []
        for path in reversed(replaced):
            try:
                os.replace(backups[path], path)
            except OSError as rollback_error:
                failures.append(f"{path}: {rollback_error}; original saved at {backups[path]}")
        if failures:
            # Retain backup files for recovery, and never claim full rollback.
            backups.clear()
            raise OSError(f"{exc}; rollback incomplete: {'; '.join(failures)}") from exc
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            temporary.unlink(missing_ok=True)


def run_fix_wire_stubs(args) -> int:
    planned: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    record(planned=planned, applied=applied, skipped=skipped)
    try:
        sheets, snapshots = _load_design(Path(args.schematic))
        edits = []
        for path, sch in sheets.items():
            pins, libs = _pins_and_libs(sch)
            candidates = []
            for finding in find_wire_stubs([(str(path), sch)], libs):
                node, reason = _wire_node(sch, finding)
                if not reason:
                    reason = _unsafe_reason(sch, finding, pins)
                candidates.append((finding, node, reason))
            for i, (finding, node, reason) in enumerate(candidates):
                if not reason:
                    for j, (other, _, other_reason) in enumerate(candidates):
                        if (
                            i != j
                            and not other_reason
                            and _intersects(
                                finding.dangling_endpoint,
                                finding.candidate_pin_position,
                                other.dangling_endpoint,
                                other.candidate_pin_position,
                            )
                        ):
                            reason = "incompatible simultaneous extensions"
                            break
                action = asdict(finding)
                if reason:
                    action["reason"] = reason
                    skipped.append(action)
                    print(f"Skipped {path.name} {finding.wire_uuid}: {reason}")
                else:
                    planned.append(action)
                    edits.append((path, sch, node, finding, action))
                    print(
                        f"Planned {path.name} {finding.wire_uuid}[{finding.endpoint_index}]: "
                        f"{finding.dangling_endpoint} -> {finding.candidate_pin_position} "
                        f"({finding.candidate_pin_ref})"
                    )
        if not args.dry_run:
            changed = {}
            for path, sch, node, finding, action in edits:
                assert node is not None
                node.children[:] = [
                    SExp(value=value, _original_str=repr(value))
                    for value in finding.candidate_pin_position
                ]
                changed[path] = sch
            _save_changes(changed, snapshots)
            applied.extend(action for _, _, _, _, action in edits)
        print(f"{len(planned)} planned, {len(applied)} applied, {len(skipped)} skipped")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        record(error=str(exc))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", help="Root .kicad_sch file (includes child sheets)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without writing")
    add_format_flag(parser)
    args = parser.parse_args(argv)
    return run_with_json_summary(
        "fix-wire-stubs",
        args.schematic,
        lambda: run_fix_wire_stubs(args),
        output_format=args.format,
        dry_run=args.dry_run,
    )
