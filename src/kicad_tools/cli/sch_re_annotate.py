#!/usr/bin/env python3
"""
Re-annotate reference designators in KiCad schematics.

Renumbers reference designators sequentially across the hierarchy,
closing gaps and making numbering contiguous.

Usage:
    # Preview changes
    kicad-tools sch re-annotate board.kicad_sch --dry-run

    # Renumber all refs starting from 1
    kicad-tools sch re-annotate board.kicad_sch

    # Only renumber resistors and capacitors
    kicad-tools sch re-annotate board.kicad_sch --prefix R,C

    # Start numbering from 100
    kicad-tools sch re-annotate board.kicad_sch --start-from 100

    # Restart numbering per sheet
    kicad-tools sch re-annotate board.kicad_sch --per-sheet
"""

import re
import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import create_backup
from kicad_tools.cli.sch_set_footprint import _collect_schematic_files

# Prefixes for power/flag symbols that should be excluded from renumbering
EXCLUDED_PREFIXES = {"#PWR", "#FLG", "#SYM"}


def _parse_reference(ref: str) -> tuple[str, int | None, str]:
    """Parse a reference designator into (prefix, number, unit_suffix).

    Examples:
        "R1"   -> ("R", 1, "")
        "U1A"  -> ("U", 1, "A")
        "C32"  -> ("C", 32, "")
        "#PWR01" -> ("#PWR", 1, "")
        "R?"   -> ("R", None, "")
    """
    # Match prefix (letters, optionally starting with #), number, optional unit letter
    m = re.match(r'^(#?[A-Za-z]+)(\d+)([A-Za-z]?)$', ref)
    if m:
        return m.group(1), int(m.group(2)), m.group(3)
    # Unannotated reference like "R?"
    m = re.match(r'^(#?[A-Za-z]+)\?$', ref)
    if m:
        return m.group(1), None, ""
    return ref, None, ""


def _extract_symbols_from_text(text: str) -> list[dict]:
    """Extract symbol references and their positions from schematic text.

    Returns list of dicts with keys: reference, prefix, number, unit_suffix,
    position_y, position_x, lib_id.
    """
    # Match symbol instance blocks (not lib_symbols)
    symbol_pattern = re.compile(
        r'\(symbol\n'
        r'\t\t\(lib_id "([^"]+)"\)'
        r'.*?'
        r'\(at ([\d.]+) ([\d.]+)',
        re.DOTALL,
    )

    ref_pattern = re.compile(r'\(property "Reference" "([^"]+)"')
    symbols = []

    # Find all symbol blocks that have lib_id (instances, not lib definitions)
    block_pattern = re.compile(
        r'\t\(symbol\n'
        r'\t\t\(lib_id "[^"]+"\)'
        r'.*?'
        r'(?:\t\t\(instances\n.*?\t\t\)\n\t\)|\t\t\(pin "[^"]+"\n\t\t\t\(uuid "[^"]+"\)\n\t\t\)\n\t\))',
        re.DOTALL,
    )

    for match in block_pattern.finditer(text):
        block = match.group(0)
        lib_match = re.search(r'\(lib_id "([^"]+)"\)', block)
        at_match = re.search(r'\(at ([\d.eE+-]+) ([\d.eE+-]+)', block)
        ref_match = ref_pattern.search(block)

        if not ref_match or not lib_match:
            continue

        ref = ref_match.group(1)
        lib_id = lib_match.group(1)
        x = float(at_match.group(1)) if at_match else 0.0
        y = float(at_match.group(2)) if at_match else 0.0

        prefix, number, unit_suffix = _parse_reference(ref)

        symbols.append({
            "reference": ref,
            "prefix": prefix,
            "number": number,
            "unit_suffix": unit_suffix,
            "position_x": x,
            "position_y": y,
            "lib_id": lib_id,
        })

    return symbols


def _apply_reference_rename(text: str, old_ref: str, new_ref: str) -> str:
    """Rename a reference designator in schematic text.

    Updates both:
    - (property "Reference" "OLD" ...) -> (property "Reference" "NEW" ...)
    - (reference "OLD") -> (reference "NEW") in instances blocks
    """
    # Update property "Reference" value
    text = re.sub(
        r'(\(property "Reference" ")' + re.escape(old_ref) + r'"',
        r'\g<1>' + new_ref + '"',
        text,
    )

    # Update (reference "OLD") in instances blocks
    text = re.sub(
        r'(\(reference ")' + re.escape(old_ref) + r'"\)',
        r'\g<1>' + new_ref + '")',
        text,
    )

    return text


def run_re_annotate(
    schematic_path: Path,
    dry_run: bool = False,
    backup: bool = True,
    prefixes: list[str] | None = None,
    start_from: int = 1,
    per_sheet: bool = False,
    format: str = "text",
) -> int:
    """Run the re-annotate operation.

    Args:
        schematic_path: Path to root .kicad_sch file.
        dry_run: If True, show mapping without modifying files.
        backup: If True, create backups before modifying.
        prefixes: If set, only renumber these prefixes (e.g., ["R", "C"]).
        start_from: Starting number for each prefix (default 1).
        per_sheet: If True, restart numbering per sheet.
        format: Output format ("text" or "json").

    Returns:
        0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Collect all schematic files in hierarchy order
    all_files = _collect_schematic_files(schematic_path)

    if not all_files:
        print("Error: No schematic files found", file=sys.stderr)
        return 1

    # Phase 1: Collect all symbols across hierarchy
    file_symbols: dict[Path, list[dict]] = {}
    file_texts: dict[Path, str] = {}

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            return 1
        file_texts[sch_file] = text
        file_symbols[sch_file] = _extract_symbols_from_text(text)

    # Phase 2: Build rename mapping
    # Group symbols by prefix, maintaining file/position order
    # For multi-unit components, all units share the same base ref number

    if per_sheet:
        # Per-sheet mode: restart numbering for each file
        mapping = _build_per_sheet_mapping(
            all_files, file_symbols, prefixes, start_from
        )
    else:
        # Continuous mode: number across all files
        mapping = _build_continuous_mapping(
            all_files, file_symbols, prefixes, start_from
        )

    if not mapping:
        print("No references to renumber.")
        return 0

    # Filter out identity mappings
    effective_mapping = {old: new for old, new in mapping.items() if old != new}

    if not effective_mapping:
        print("All references are already sequential. No changes needed.")
        return 0

    # Phase 3: Output mapping / apply changes
    if format == "json":
        import json
        output = {
            "mappings": [
                {"old": old, "new": new}
                for old, new in sorted(effective_mapping.items())
            ],
            "total": len(effective_mapping),
            "dry_run": dry_run,
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"Re-annotation mapping ({len(effective_mapping)} changes):")
        print()

        # Group by prefix for display
        by_prefix: dict[str, list[tuple[str, str]]] = {}
        for old, new in sorted(effective_mapping.items()):
            prefix, _, _ = _parse_reference(old)
            by_prefix.setdefault(prefix, []).append((old, new))

        for prefix in sorted(by_prefix):
            changes = by_prefix[prefix]
            print(f"  {prefix}:")
            for old, new in changes:
                print(f"    {old} -> {new}")
        print()

    if dry_run:
        if format == "text":
            print("Dry run: no changes made.")
        return 0

    # Phase 4: Apply the full mapping to each file
    # We need a two-pass rename to avoid collisions (e.g., R1->R2 and R2->R3).
    # First rename all to temporary unique refs, then rename to final refs.
    modified_files: dict[Path, str] = {}

    for sch_file in all_files:
        text = file_texts[sch_file]
        syms = file_symbols[sch_file]
        refs_in_file = {s["reference"] for s in syms}

        # Find which mappings apply to this file
        file_mapping = {
            old: new for old, new in mapping.items() if old in refs_in_file
        }
        file_effective = {
            old: new for old, new in file_mapping.items() if old != new
        }

        if not file_effective:
            continue

        # Two-pass rename to avoid collisions
        # Pass 1: rename to temporary placeholders
        temp_mapping: dict[str, str] = {}
        for i, old_ref in enumerate(file_effective):
            temp_ref = f"__REANNOTATE_TEMP_{i}__"
            temp_mapping[old_ref] = temp_ref

        current_text = text
        for old_ref, temp_ref in temp_mapping.items():
            current_text = _apply_reference_rename(current_text, old_ref, temp_ref)

        # Pass 2: rename from temp to final
        for old_ref, temp_ref in temp_mapping.items():
            new_ref = file_effective[old_ref]
            current_text = _apply_reference_rename(current_text, temp_ref, new_ref)

        modified_files[sch_file] = current_text

    # Phase 5: Write modified files
    if modified_files:
        for sch_file, new_text in modified_files.items():
            if backup:
                bak = create_backup(sch_file)
                if format == "text":
                    print(f"  Backup: {bak.name}")
            try:
                sch_file.write_text(new_text, encoding="utf-8")
            except OSError as e:
                print(f"Error writing {sch_file}: {e}", file=sys.stderr)
                return 1

    if format == "text":
        print(
            f"Re-annotated {len(effective_mapping)} reference(s) "
            f"across {len(modified_files)} file(s)."
        )

    return 0


def _build_continuous_mapping(
    all_files: list[Path],
    file_symbols: dict[Path, list[dict]],
    prefixes: list[str] | None,
    start_from: int,
) -> dict[str, str]:
    """Build a continuous mapping across all files.

    Symbols are ordered by file order, then by position (top-to-bottom,
    left-to-right) within each file. Multi-unit components share a base
    number.
    """
    # Collect all symbols in order
    ordered_symbols: list[dict] = []
    for sch_file in all_files:
        syms = file_symbols[sch_file]
        # Sort by position: top-to-bottom (Y ascending), left-to-right (X ascending)
        syms_sorted = sorted(syms, key=lambda s: (s["position_y"], s["position_x"]))
        ordered_symbols.extend(syms_sorted)

    return _assign_numbers(ordered_symbols, prefixes, start_from)


def _build_per_sheet_mapping(
    all_files: list[Path],
    file_symbols: dict[Path, list[dict]],
    prefixes: list[str] | None,
    start_from: int,
) -> dict[str, str]:
    """Build a per-sheet mapping, restarting numbering per file."""
    mapping: dict[str, str] = {}

    for sch_file in all_files:
        syms = file_symbols[sch_file]
        syms_sorted = sorted(syms, key=lambda s: (s["position_y"], s["position_x"]))
        sheet_mapping = _assign_numbers(syms_sorted, prefixes, start_from)
        mapping.update(sheet_mapping)

    return mapping


def _assign_numbers(
    symbols: list[dict],
    prefixes: list[str] | None,
    start_from: int,
) -> dict[str, str]:
    """Assign sequential numbers to symbols, handling multi-unit components.

    Multi-unit components (e.g., U1A and U1B) share the same base reference
    number but differ by their unit suffix.
    """
    mapping: dict[str, str] = {}
    # Track next number per prefix
    counters: dict[str, int] = {}
    # Track base number assignments for multi-unit: (prefix, old_number) -> new_number
    multi_unit_assigned: dict[tuple[str, int | None], int] = {}

    for sym in symbols:
        ref = sym["reference"]
        prefix = sym["prefix"]
        number = sym["number"]
        unit_suffix = sym["unit_suffix"]

        # Skip excluded prefixes (power symbols, flags)
        if prefix in EXCLUDED_PREFIXES:
            continue

        # Skip if prefix filter is active and this prefix isn't included
        if prefixes is not None and prefix not in prefixes:
            continue

        # Skip unannotated refs
        if number is None:
            continue

        # Handle multi-unit components
        if unit_suffix:
            key = (prefix, number)
            if key in multi_unit_assigned:
                new_number = multi_unit_assigned[key]
            else:
                new_number = counters.get(prefix, start_from)
                counters[prefix] = new_number + 1
                multi_unit_assigned[key] = new_number
            new_ref = f"{prefix}{new_number}{unit_suffix}"
        else:
            # Check if this is a multi-unit component without suffix
            # (unit > 1 would share the same ref)
            new_number = counters.get(prefix, start_from)
            counters[prefix] = new_number + 1
            new_ref = f"{prefix}{new_number}"

        mapping[ref] = new_ref

    return mapping


def main(argv: list[str] | None = None):
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Re-annotate reference designators in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview changes without modifying files"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    parser.add_argument(
        "--prefix",
        help="Comma-separated list of prefixes to renumber (e.g., R,C,U)",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=1,
        help="Starting number for each prefix (default: 1)",
    )
    parser.add_argument(
        "--per-sheet",
        action="store_true",
        help="Restart numbering per sheet instead of continuous",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)

    prefix_list = None
    if args.prefix:
        prefix_list = [p.strip() for p in args.prefix.split(",")]

    return run_re_annotate(
        schematic_path=args.schematic,
        dry_run=args.dry_run,
        backup=args.backup,
        prefixes=prefix_list,
        start_from=args.start_from,
        per_sheet=args.per_sheet,
        format=args.format,
    )


if __name__ == "__main__":
    sys.exit(main())
