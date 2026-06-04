#!/usr/bin/env python3
"""
Set footprint assignments for symbols in KiCad schematics.

Supports single-ref mode, batch mode via JSON/CSV mapping files,
and hierarchical schematic traversal.

Usage:
    # Single symbol
    kicad-tools sch set-footprint board.kicad_sch --ref U2 \\
        --footprint "Package_TO_SOT_SMD:SOT-23-5"

    # Batch mode with JSON mapping
    kicad-tools sch set-footprint board.kicad_sch \\
        --map footprint-map.json

    # Dry run
    kicad-tools sch set-footprint board.kicad_sch --ref R1 \\
        --footprint "Resistor_SMD:R_0805_2012Metric" --dry-run
"""

import json
import re
import sys
from pathlib import Path

from kicad_tools.cli.lib_footprints import _count_pads
from kicad_tools.cli.modify_schematic import (
    create_backup,
    find_symbol_text_range,
    set_footprint_text,
)
from kicad_tools.core.sexp_file import load_footprint
from kicad_tools.footprints.library_path import (
    LibraryPaths,
    detect_kicad_library_path,
    parse_library_id,
)
from kicad_tools.schema import Schematic


def _find_subsheet_files(text: str, schematic_dir: Path) -> list[Path]:
    """Extract sub-sheet file paths from schematic text.

    Looks for (sheet ... (property "Sheetfile" "filename.kicad_sch" ...)) blocks.
    """
    subsheets = []
    pattern = re.compile(r'\(property "Sheetfile" "([^"]+)"')
    for match in pattern.finditer(text):
        subsheet_path = schematic_dir / match.group(1)
        if subsheet_path.exists():
            subsheets.append(subsheet_path)
    return subsheets


def _collect_schematic_files(root_path: Path) -> list[Path]:
    """Recursively collect all schematic files in the hierarchy.

    Returns list starting with root, followed by sub-sheets (depth-first).
    """
    visited: set[Path] = set()
    result: list[Path] = []

    def _walk(sch_path: Path) -> None:
        resolved = sch_path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        result.append(sch_path)

        try:
            text = sch_path.read_text(encoding="utf-8")
        except OSError:
            return

        for sub in _find_subsheet_files(text, sch_path.parent):
            _walk(sub)

    _walk(root_path)
    return result


def _load_mapping(map_path: Path) -> dict[str, str]:
    """Load a reference-to-footprint mapping from JSON or CSV file.

    JSON format: {"R1": "Resistor_SMD:R_0805", "C1": "Capacitor_SMD:C_0603"}
    CSV format (no header): R1,Resistor_SMD:R_0805
    """
    text = map_path.read_text(encoding="utf-8").strip()

    # Try JSON first
    if text.startswith("{"):
        mapping = json.loads(text)
        if not isinstance(mapping, dict):
            raise ValueError(f"JSON mapping must be an object, got {type(mapping).__name__}")
        return mapping

    # Fall back to CSV (ref,footprint per line)
    mapping: dict[str, str] = {}
    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",", 1)
        if len(parts) != 2:
            raise ValueError(f"Line {line_num}: expected 'REF,FOOTPRINT', got: {line}")
        ref, fp = parts[0].strip(), parts[1].strip()
        if not ref:
            raise ValueError(f"Line {line_num}: empty reference")
        mapping[ref] = fp
    return mapping


def _footprint_pad_count(paths: LibraryPaths, footprint: str) -> int | None:
    """Resolve a ``Library:Footprint`` (or bare name) to its pad count.

    Returns ``None`` when the footprint file cannot be located -- e.g. no
    library is installed, an unknown library/footprint, or a parse error.
    Validation callers treat ``None`` as "cannot validate" and skip silently.
    """
    if not paths.found:
        return None

    lib, name = parse_library_id(footprint)
    fp_path: Path | None = None
    if lib:
        fp_path = paths.get_footprint_file(lib, name, fallback_search=True)
    else:
        fp_path = paths.find_footprint_by_name(name)

    if fp_path is None:
        return None

    try:
        sexp = load_footprint(fp_path)
        return _count_pads(sexp)
    except Exception:
        return None


def _build_symbol_pin_counts(schematic_path: Path) -> dict[str, int]:
    """Map symbol reference -> expected pin count for the whole hierarchy.

    Prefers the resolved library-symbol pin count, falling back to the
    instance pin list. Failures are swallowed so validation can degrade to
    "no data" rather than aborting the assignment.
    """
    counts: dict[str, int] = {}
    for sch_file in _collect_schematic_files(schematic_path):
        try:
            sch = Schematic.load(sch_file)
        except Exception:
            continue
        for sym in sch.symbols:
            ref = sym.reference
            if not ref:
                continue
            pin_count: int | None = None
            try:
                lib_sym = sch.get_lib_symbol_resolved(sym.lib_id)
                if lib_sym is not None and lib_sym.pin_count > 0:
                    pin_count = lib_sym.pin_count
            except Exception:
                pin_count = None
            if pin_count is None:
                inst = len(sym.pins)
                if inst > 0:
                    pin_count = inst
            if pin_count is not None:
                counts[ref] = pin_count
    return counts


def run_set_footprint(
    schematic_path: Path,
    ref: str | None = None,
    footprint: str | None = None,
    map_path: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
    validate: bool = True,
    strict: bool = False,
    config_override: str | Path | None = None,
    mapping: dict[str, str] | None = None,
) -> int:
    """Run the set-footprint operation.

    When *validate* is True and a KiCad footprint library is available, each
    assigned footprint's pad count is compared against the symbol's pin count
    and a warning is emitted on mismatch. In single-ref mode (or under
    *strict*), a mismatch makes the command exit non-zero; in batch mode the
    mismatch only warns so existing bulk-assign workflows are unaffected.
    When no library is available, validation is silently skipped.

    The ``mapping`` parameter lets in-process callers (e.g.
    :func:`sch_assign_footprints.run_assign_footprints`) hand a pre-built
    ``{ref: footprint}`` dict directly, skipping the JSON/CSV file round-trip.
    It is treated as the same "batch mode" as ``--map``: validation warnings
    do not abort unless ``strict=True``. ``ref``/``footprint``/``map_path``
    are ignored when ``mapping`` is provided.

    Returns 0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Build the ref -> footprint mapping. Precedence:
    #   1. explicit in-memory ``mapping`` dict (batch mode for in-process callers)
    #   2. ``map_path`` JSON/CSV file (batch mode for CLI users)
    #   3. ``ref`` + ``footprint`` single-symbol mode
    batch_mode: bool
    if mapping is not None:
        if not mapping:
            print("Error: mapping is empty", file=sys.stderr)
            return 1
        batch_mode = True
    elif map_path is not None:
        if not map_path.exists():
            print(f"Error: Mapping file not found: {map_path}", file=sys.stderr)
            return 1
        try:
            mapping = _load_mapping(map_path)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error reading mapping file: {e}", file=sys.stderr)
            return 1
        if not mapping:
            print("Error: Mapping file is empty", file=sys.stderr)
            return 1
        batch_mode = True
    elif ref is not None and footprint is not None:
        mapping = {ref: footprint}
        batch_mode = False
    else:
        print("Error: Provide either --ref/--footprint or --map", file=sys.stderr)
        return 1

    single_ref_mode = not batch_mode

    # --- Pin-count validation (best-effort, before any modification) ---
    if validate:
        paths = detect_kicad_library_path(config_override)
        if paths.found:
            symbol_pins = _build_symbol_pin_counts(schematic_path)
            mismatches: list[str] = []
            for r, fp in mapping.items():
                expected = symbol_pins.get(r)
                if expected is None:
                    continue
                pad_count = _footprint_pad_count(paths, fp)
                if pad_count is None:
                    continue
                if pad_count != expected:
                    mismatches.append(
                        f"  {r}: symbol has {expected} pins but footprint "
                        f"'{fp}' has {pad_count} pads"
                    )
            if mismatches:
                print(
                    "Warning: pin-count mismatch between symbol and footprint:",
                    file=sys.stderr,
                )
                for m in mismatches:
                    print(m, file=sys.stderr)
                if single_ref_mode or strict:
                    print(
                        "Aborting due to pin-count mismatch (use --no-validate to override).",
                        file=sys.stderr,
                    )
                    return 1

    # Collect all schematic files (root + sub-sheets)
    all_files = _collect_schematic_files(schematic_path)

    total_changed = 0
    total_errors = 0
    remaining = dict(mapping)
    modified_files: dict[Path, str] = {}

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            total_errors += 1
            continue

        file_modified = False
        current_text = text

        # Try each remaining reference against this file
        for r in list(remaining.keys()):
            fp = remaining[r]
            result = find_symbol_text_range(current_text, r)
            if result is None:
                continue

            if dry_run:
                _, _, info = result
                old_fp = info.get("footprint", "")
                print(f"  {r}: '{old_fp}' -> '{fp}' (in {sch_file.name})")
                total_changed += 1
                del remaining[r]
            else:
                current_text, success, msg = set_footprint_text(current_text, r, fp)
                if success:
                    print(f"  {msg} (in {sch_file.name})")
                    file_modified = True
                    total_changed += 1
                    del remaining[r]
                else:
                    print(f"  Warning: {msg}", file=sys.stderr)
                    total_errors += 1

        if file_modified:
            modified_files[sch_file] = current_text

    # Write out modified files
    if not dry_run and modified_files:
        for sch_file, new_text in modified_files.items():
            if backup:
                bak = create_backup(sch_file)
                print(f"  Backup: {bak.name}")
            try:
                sch_file.write_text(new_text, encoding="utf-8")
            except OSError as e:
                print(f"Error writing {sch_file}: {e}", file=sys.stderr)
                total_errors += 1

    # Summary
    print()
    if dry_run:
        print(f"Dry run: {total_changed} footprint(s) would be changed")
    else:
        print(f"Changed {total_changed} footprint(s)")

    if remaining:
        print(f"Warning: {len(remaining)} reference(s) not found: {', '.join(sorted(remaining))}")
        total_errors += len(remaining)

    return 1 if total_errors > 0 and total_changed == 0 else 0


def main(argv: list[str] | None = None):
    """CLI entry point for standalone usage."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Set footprint assignments in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument("--ref", help="Symbol reference (e.g., U2, R1)")
    parser.add_argument(
        "--footprint", help="Footprint to assign (e.g., Package_TO_SOT_SMD:SOT-23-5)"
    )
    parser.add_argument(
        "--map",
        dest="map_file",
        type=Path,
        help="Path to JSON or CSV mapping file (ref -> footprint)",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    parser.add_argument("--no-backup", action="store_true", help="Skip creating backup files")
    parser.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        default=True,
        help="Skip pin-count validation against the footprint library",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any pin-count mismatch, even in batch mode",
    )

    args = parser.parse_args(argv)

    # Validate argument combinations
    if args.map_file and (args.ref or args.footprint):
        print("Error: Cannot use --map with --ref/--footprint", file=sys.stderr)
        return 1
    if args.ref and not args.footprint:
        print("Error: --footprint is required when using --ref", file=sys.stderr)
        return 1
    if args.footprint and not args.ref:
        print("Error: --ref is required when using --footprint", file=sys.stderr)
        return 1
    if not args.ref and not args.map_file:
        print("Error: Provide either --ref/--footprint or --map", file=sys.stderr)
        return 1

    return run_set_footprint(
        schematic_path=args.schematic,
        ref=args.ref,
        footprint=args.footprint,
        map_path=args.map_file,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        validate=args.validate,
        strict=args.strict,
    )


if __name__ == "__main__":
    sys.exit(main())
