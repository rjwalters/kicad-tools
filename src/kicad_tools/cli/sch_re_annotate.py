#!/usr/bin/env python3
"""
Re-annotate reference designators in KiCad schematics.

Renumbers reference designators sequentially across the hierarchy,
closing gaps and making numbering contiguous.  Also annotates
unannotated components (``R?``, ``C?``, etc.) and creates missing
``(instances)`` blocks for multi-project schematics.

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

# Whitespace token for regex: matches one or more tabs or spaces
_WS = r'[ \t]+'


def _detect_indent(text: str) -> str:
    """Detect the indentation unit used in a schematic file.

    Looks at the first indented line to determine whether the file uses
    tabs or spaces (and how many spaces per level).

    Returns:
        The single-level indent string (e.g. ``'\\t'`` or ``'  '``).
    """
    for line in text.splitlines():
        if line and line[0] in (' ', '\t'):
            # Count leading whitespace
            stripped = line.lstrip(' \t')
            leading = line[:len(line) - len(stripped)]
            if '\t' in leading:
                return '\t'
            # Assume the first indented line is at depth 1
            # Common space counts: 2 or 4
            return leading
    return '\t'


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
    position_y, position_x, lib_id, uuid, has_project_instance.
    """
    ref_pattern = re.compile(r'\(property "Reference" "([^"]+)"')
    symbols = []

    # Find all symbol blocks that have lib_id (instances, not lib definitions)
    # Use _WS instead of literal \t to support both tab and space indentation
    block_pattern = re.compile(
        _WS + r'\(symbol\n'
        + _WS + r'\(lib_id "[^"]+"\)'
        r'.*?'
        r'(?:' + _WS + r'\(instances\n.*?' + _WS + r'\)\n' + _WS + r'\)'
        r'|' + _WS + r'\(pin "[^"]+"\n' + _WS + r'\(uuid "[^"]+"\)\n' + _WS + r'\)\n' + _WS + r'\))',
        re.DOTALL,
    )

    for match in block_pattern.finditer(text):
        block = match.group(0)
        lib_match = re.search(r'\(lib_id "([^"]+)"\)', block)
        at_match = re.search(r'\(at ([\d.eE+-]+) ([\d.eE+-]+)', block)
        ref_match = ref_pattern.search(block)
        uuid_match = re.search(r'\(uuid "([^"]+)"\)', block)

        if not ref_match or not lib_match:
            continue

        ref = ref_match.group(1)
        lib_id = lib_match.group(1)
        sym_uuid = uuid_match.group(1) if uuid_match else ""
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
            "uuid": sym_uuid,
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


def _apply_uuid_reference_rename(text: str, sym_uuid: str, new_ref: str) -> str:
    """Rename a reference designator for a specific symbol identified by UUID.

    This is needed for unannotated components (``R?``, ``C?``) where the
    reference string is shared by multiple components in the same file.
    Targets the symbol block containing the given UUID.
    """
    # Strategy: find the symbol block boundaries first, then modify within.
    # Symbol blocks start with <indent>(symbol\n and end with <indent>).
    # We find the block that contains our UUID and replace within it.
    uuid_str = f'(uuid "{sym_uuid}")'
    uuid_pos = text.find(uuid_str)
    if uuid_pos == -1:
        return text

    # Walk backward to find the start of this symbol block using regex
    # to support both tab and space indentation
    start_pattern = re.compile(_WS + r'\(symbol\n')
    block_start = -1
    for m in start_pattern.finditer(text, 0, uuid_pos):
        block_start = m.start()
    if block_start == -1:
        return text

    # Walk forward to find the end of this symbol block (\n<indent>) at depth 1)
    end_pattern = re.compile(r'\n' + _WS + r'\)')
    end_match = end_pattern.search(text, uuid_pos)
    if end_match is None:
        return text
    block_end = end_match.end()

    block = text[block_start:block_end]

    # Replace the Reference property within this specific block
    new_block = re.sub(
        r'(\(property "Reference" ")[^"]+"',
        r'\g<1>' + new_ref + '"',
        block,
        count=1,
    )

    return text[:block_start] + new_block + text[block_end:]


def _detect_project_info(
    root_path: Path,
    all_files: list[Path],
) -> tuple[str, str, dict[Path, str]]:
    """Detect project name, root UUID, and sheet UUID paths.

    For multi-project schematics, the project name is derived from the
    root schematic filename.  The instance path for each sub-sheet is
    ``/<root_uuid>/<sheet_uuid>``.

    Returns:
        Tuple of (project_name, root_uuid, file_to_instance_path_map).
    """
    project_name = root_path.stem
    root_text = root_path.read_text(encoding="utf-8")

    # Find root schematic UUID (first uuid at top level)
    root_uuid_match = re.search(r'^' + _WS + r'\(uuid "([^"]+)"\)', root_text, re.MULTILINE)
    root_uuid = root_uuid_match.group(1) if root_uuid_match else ""

    # Build map of sub-sheet file -> instance path
    # Parse sheet blocks from root: (sheet ... (uuid "XXX") ... (property "Sheetfile" "file.kicad_sch"))
    file_paths: dict[Path, str] = {
        root_path: f"/{root_uuid}",
    }

    sheet_pattern = re.compile(
        r'\(sheet\b.*?\(uuid "([^"]+)"\).*?\(property "Sheetfile" "([^"]+)".*?\)',
        re.DOTALL,
    )
    for m in sheet_pattern.finditer(root_text):
        sheet_uuid = m.group(1)
        sheet_file = m.group(2)
        sheet_path = root_path.parent / sheet_file
        file_paths[sheet_path] = f"/{root_uuid}/{sheet_uuid}"

    return project_name, root_uuid, file_paths


def _add_project_instance(
    text: str,
    sym_uuid: str,
    project_name: str,
    instance_path: str,
    new_ref: str,
    unit: int = 1,
) -> str:
    """Add a project instance entry to a symbol's (instances) block.

    If the symbol has an ``(instances)`` block, appends a new
    ``(project "name" ...)`` entry.  If the symbol has no
    ``(instances)`` block, creates one before the closing ``\\t)``.

    Args:
        text: Full schematic text.
        sym_uuid: UUID of the target symbol.
        project_name: Project name for the instance entry.
        instance_path: Full hierarchy path (e.g. ``/root-uuid/sheet-uuid``).
        new_ref: Reference designator to use in the instance.
        unit: Unit number (default 1).

    Returns:
        Modified schematic text.
    """
    # Detect the indentation unit used in this file
    ind = _detect_indent(text)
    i2 = ind * 2
    i3 = ind * 3
    i4 = ind * 4
    i5 = ind * 5

    instance_entry = (
        f'{i3}(project "{project_name}"\n'
        f'{i4}(path "{instance_path}"\n'
        f'{i5}(reference "{new_ref}")\n'
        f'{i5}(unit {unit})\n'
        f'{i4})\n'
        f'{i3})\n'
    )

    # Find the symbol block containing this UUID
    # Strategy: find the (instances block within the symbol that contains our UUID,
    # and append the new project entry before the closing )
    block_pattern = re.compile(
        r'(' + _WS + r'\(symbol\n'
        + _WS + r'\(lib_id "[^"]+"\)'
        r'.*?'
        r'\(uuid "' + re.escape(sym_uuid) + r'"\)'
        r'.*?)'
        r'(' + _WS + r'\(instances\n)'
        r'(.*?)'
        r'(' + _WS + r'\)\n' + _WS + r'\))',
        re.DOTALL,
    )

    match = block_pattern.search(text)
    if match:
        # Has instances block — append new project entry before closing )
        before = match.group(1)
        instances_start = match.group(2)
        instances_body = match.group(3)
        instances_end = match.group(4)

        # Check if this project already has an entry
        if f'(project "{project_name}"' in instances_body:
            return text

        new_block = before + instances_start + instances_body + instance_entry + instances_end
        return text[:match.start()] + new_block + text[match.end():]

    # No instances block — find the symbol block and add one before closing )
    # The symbol block ends with pin entries then )
    no_inst_pattern = re.compile(
        r'(' + _WS + r'\(symbol\n'
        + _WS + r'\(lib_id "[^"]+"\)'
        r'.*?'
        r'\(uuid "' + re.escape(sym_uuid) + r'"\)'
        r'.*?'
        r'(?:' + _WS + r'\(pin "[^"]+"\n' + _WS + r'\(uuid "[^"]+"\)\n' + _WS + r'\)\n))'
        r'(' + _WS + r'\))',
        re.DOTALL,
    )

    match = no_inst_pattern.search(text)
    if match:
        instances_block = (
            f'{i2}(instances\n'
            f'{instance_entry}'
            f'{i2})\n'
        )
        return text[:match.start(2)] + instances_block + match.group(2) + text[match.end():]

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

    # Detect project info for multi-project instance handling
    project_name, root_uuid, file_instance_paths = _detect_project_info(
        schematic_path, all_files
    )

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
    # The mapping is now keyed by UUID to handle unannotated refs (R?, C?)
    # that share the same reference string across multiple components.

    if per_sheet:
        uuid_mapping = _build_per_sheet_mapping(
            all_files, file_symbols, prefixes, start_from
        )
    else:
        uuid_mapping = _build_continuous_mapping(
            all_files, file_symbols, prefixes, start_from
        )

    if not uuid_mapping:
        print("No references to renumber.")
        return 0

    # Filter out identity mappings
    effective_mapping = {
        uid: info for uid, info in uuid_mapping.items()
        if info["old"] != info["new"]
    }

    if not effective_mapping:
        print("All references are already sequential. No changes needed.")
        return 0

    # Phase 3: Output mapping / apply changes
    if format == "json":
        import json
        output = {
            "mappings": [
                {"old": info["old"], "new": info["new"], "uuid": uid}
                for uid, info in sorted(
                    effective_mapping.items(), key=lambda x: x[1]["new"]
                )
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
        for uid, info in sorted(
            effective_mapping.items(), key=lambda x: x[1]["new"]
        ):
            prefix, _, _ = _parse_reference(info["new"])
            by_prefix.setdefault(prefix, []).append((info["old"], info["new"]))

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
    # For annotated refs (unique reference strings), use the two-pass rename.
    # For unannotated refs (R?, C?), use UUID-targeted replacement.
    modified_files: dict[Path, str] = {}

    for sch_file in all_files:
        text = file_texts[sch_file]
        syms = file_symbols[sch_file]
        uuids_in_file = {s["uuid"] for s in syms}

        # Partition mappings for this file into annotated vs unannotated
        annotated_mapping: dict[str, str] = {}  # old_ref -> new_ref
        unannotated_entries: list[dict] = []  # [{uuid, new_ref}, ...]

        for uid, info in uuid_mapping.items():
            if uid not in uuids_in_file:
                continue
            if info["unannotated"]:
                unannotated_entries.append({
                    "uuid": uid,
                    "old": info["old"],
                    "new": info["new"],
                })
            else:
                if info["old"] != info["new"]:
                    annotated_mapping[info["old"]] = info["new"]

        if not annotated_mapping and not unannotated_entries:
            continue

        current_text = text

        # Step A: Handle annotated refs with two-pass rename to avoid collisions
        if annotated_mapping:
            temp_mapping: dict[str, str] = {}
            for i, old_ref in enumerate(annotated_mapping):
                temp_ref = f"__REANNOTATE_TEMP_{i}__"
                temp_mapping[old_ref] = temp_ref

            for old_ref, temp_ref in temp_mapping.items():
                current_text = _apply_reference_rename(current_text, old_ref, temp_ref)

            for old_ref, temp_ref in temp_mapping.items():
                new_ref = annotated_mapping[old_ref]
                current_text = _apply_reference_rename(current_text, temp_ref, new_ref)

        # Step B: Handle unannotated refs with UUID-targeted replacement
        instance_path = file_instance_paths.get(sch_file, "")
        for entry in unannotated_entries:
            current_text = _apply_uuid_reference_rename(
                current_text, entry["uuid"], entry["new"]
            )
            # Add project instance entry
            if instance_path:
                current_text = _add_project_instance(
                    current_text,
                    entry["uuid"],
                    project_name,
                    instance_path,
                    entry["new"],
                )

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
) -> dict[str, dict]:
    """Build a continuous UUID-keyed mapping across all files.

    Symbols are ordered by file order, then by position (top-to-bottom,
    left-to-right) within each file. Multi-unit components share a base
    number.

    Returns:
        Dict mapping UUID -> {"old": str, "new": str, "unannotated": bool}.
    """
    ordered_symbols: list[dict] = []
    for sch_file in all_files:
        syms = file_symbols[sch_file]
        syms_sorted = sorted(syms, key=lambda s: (s["position_y"], s["position_x"]))
        ordered_symbols.extend(syms_sorted)

    return _assign_numbers(ordered_symbols, prefixes, start_from)


def _build_per_sheet_mapping(
    all_files: list[Path],
    file_symbols: dict[Path, list[dict]],
    prefixes: list[str] | None,
    start_from: int,
) -> dict[str, dict]:
    """Build a per-sheet UUID-keyed mapping, restarting numbering per file."""
    mapping: dict[str, dict] = {}

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
) -> dict[str, dict]:
    """Assign sequential numbers to symbols, handling multi-unit components.

    Multi-unit components (e.g., U1A and U1B) share the same base reference
    number but differ by their unit suffix.

    Returns:
        Dict mapping UUID -> {"old": str, "new": str, "unannotated": bool}.
    """
    mapping: dict[str, dict] = {}
    # Track next number per prefix
    counters: dict[str, int] = {}
    # Track base number assignments for multi-unit: (prefix, old_number) -> new_number
    multi_unit_assigned: dict[tuple[str, int | None], int] = {}

    for sym in symbols:
        ref = sym["reference"]
        prefix = sym["prefix"]
        number = sym["number"]
        unit_suffix = sym["unit_suffix"]
        sym_uuid = sym.get("uuid") or ref

        # Skip excluded prefixes (power symbols, flags, ground symbols)
        if prefix in EXCLUDED_PREFIXES or prefix.startswith("#"):
            continue

        # Skip if prefix filter is active and this prefix isn't included
        if prefixes is not None and prefix not in prefixes:
            continue

        unannotated = number is None

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
            new_number = counters.get(prefix, start_from)
            counters[prefix] = new_number + 1
            new_ref = f"{prefix}{new_number}"

        mapping[sym_uuid] = {
            "old": ref,
            "new": new_ref,
            "unannotated": unannotated,
        }

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
