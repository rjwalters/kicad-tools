#!/usr/bin/env python3
"""
Repair missing project instances in KiCad hierarchical schematics.

Finds all symbols that lack ``(instances ...)`` blocks for the current
project, assigns non-conflicting reference designators, and writes the
instances blocks into the sub-sheet files.

Usage:
    # Preview changes
    kicad-tools sch repair-instances board.kicad_sch --dry-run

    # Fix all missing instances
    kicad-tools sch repair-instances board.kicad_sch

    # Fix with backup
    kicad-tools sch repair-instances board.kicad_sch --backup
"""

import re
import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import create_backup
from kicad_tools.cli.sch_re_annotate import (
    _detect_indent,
    _detect_project_info,
    _parse_reference,
    _format_reference,
    _apply_uuid_reference_rename,
)
from kicad_tools.cli.sch_set_footprint import _collect_schematic_files

# Whitespace token for regex: matches one or more tabs or spaces
_WS = r'[ \t]+'


def _extract_symbols_with_instance_info(
    text: str, project_name: str
) -> list[dict]:
    """Extract symbols and whether they have instances for the given project.

    Uses a bracket-depth approach to find complete symbol blocks, which is
    more robust than the regex-only approach used in ``sch_re_annotate``.

    Returns list of dicts with keys: reference, prefix, number, unit_suffix,
    lib_id, uuid, has_project_instance.
    """
    symbols = []
    ref_pattern = re.compile(r'\(property "Reference" "([^"]+)"')

    # Find symbol block starts: lines matching <ws>(symbol\n<ws>(lib_id "...")
    start_pattern = re.compile(
        r'(?:^|\n)(' + _WS + r')\(symbol\n' + _WS + r'\(lib_id "([^"]+)"\)',
        re.DOTALL,
    )

    for start_match in start_pattern.finditer(text):
        indent = start_match.group(1)
        lib_id = start_match.group(2)
        block_start = start_match.start()

        # Find the end of this symbol block by tracking parenthesis depth
        # Start from the opening '(' of '(symbol'
        paren_start = text.index("(symbol", block_start)
        depth = 0
        i = paren_start
        block_end = len(text)
        while i < len(text):
            ch = text[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    block_end = i + 1
                    break
            elif ch == '"':
                # Skip over quoted strings to avoid counting parens inside
                i += 1
                while i < len(text) and text[i] != '"':
                    if text[i] == '\\':
                        i += 1  # skip escaped char
                    i += 1
            i += 1

        block = text[paren_start:block_end]

        ref_match = ref_pattern.search(block)
        if not ref_match:
            continue

        ref = ref_match.group(1)

        # Skip power symbols
        if lib_id.startswith("power:"):
            continue

        # Find the symbol-level uuid (the last uuid in the block, or the
        # one that's a direct child of the symbol -- not inside a pin)
        # Strategy: find all uuids, the symbol-level one is typically the
        # one right before (instances) or at the end of the block.
        # We search for (uuid "...") that is NOT inside a (pin ...) block.
        # Simple approach: find the uuid that appears after all pin blocks.
        all_uuids = list(re.finditer(r'\(uuid "([^"]+)"\)', block))
        # The symbol-level uuid is typically the last one before instances,
        # or the last one in the block if no instances.
        # Pin uuids are nested inside (pin "X" (uuid "...")) blocks.
        # The symbol uuid is at indent level 2 (direct child of symbol).
        sym_uuid = ""
        for uuid_m in reversed(all_uuids):
            # Check if this uuid is inside a (pin ...) block
            preceding = block[:uuid_m.start()]
            # Count unmatched (pin openings
            pin_opens = len(re.findall(r'\(pin\b', preceding))
            pin_closes = preceding.count(')')  # rough estimate
            # Better approach: check if (pin appears after the last )
            # before our uuid position
            last_paren_close = preceding.rfind(')')
            last_pin_open = preceding.rfind('(pin ')
            if last_pin_open > last_paren_close:
                # uuid is inside a pin block
                continue
            sym_uuid = uuid_m.group(1)
            break

        if not sym_uuid and all_uuids:
            sym_uuid = all_uuids[-1].group(1)

        prefix, number, unit_suffix = _parse_reference(ref)

        # Check if this symbol has an instances block with our project
        has_instances_block = '(instances' in block
        has_project = (
            has_instances_block
            and f'(project "{project_name}"' in block
        )

        symbols.append({
            "reference": ref,
            "prefix": prefix,
            "number": number,
            "unit_suffix": unit_suffix,
            "lib_id": lib_id,
            "uuid": sym_uuid,
            "has_project_instance": has_project,
        })

    return symbols


def _find_symbol_block(text: str, sym_uuid: str) -> tuple[int, int] | None:
    """Find the start and end positions of the symbol block containing *sym_uuid*.

    Uses bracket-depth parsing for robustness.
    Returns (start, end) positions or None if not found.
    """
    uuid_str = f'(uuid "{sym_uuid}")'
    uuid_pos = text.find(uuid_str)
    if uuid_pos == -1:
        return None

    # Walk backward to find the start of this symbol block
    start_pattern = re.compile(_WS + r'\(symbol\n')
    block_start = -1
    for m in start_pattern.finditer(text, 0, uuid_pos):
        block_start = m.start()
    if block_start == -1:
        return None

    # Find the opening paren of (symbol
    paren_start = text.index("(symbol", block_start)

    # Walk forward tracking depth to find closing paren
    depth = 0
    i = paren_start
    while i < len(text):
        ch = text[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return paren_start, i + 1
        elif ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == '\\':
                    i += 1
                i += 1
        i += 1

    return None


def _insert_project_instance(
    text: str,
    sym_uuid: str,
    project_name: str,
    instance_path: str,
    new_ref: str,
    unit: int = 1,
) -> str:
    """Insert a project instance entry into a symbol's block.

    If the symbol has an ``(instances)`` block, appends a new
    ``(project ...)`` entry.  If not, creates an ``(instances)`` block
    before the symbol's closing parenthesis.

    Uses bracket-depth parsing for robustness with varying file layouts.
    """
    bounds = _find_symbol_block(text, sym_uuid)
    if bounds is None:
        return text

    start, end = bounds
    block = text[start:end]

    ind = _detect_indent(text)
    i2 = ind * 2
    i3 = ind * 3
    i4 = ind * 4
    i5 = ind * 5

    project_entry = (
        f'{i3}(project "{project_name}"\n'
        f'{i4}(path "{instance_path}"\n'
        f'{i5}(reference "{new_ref}")\n'
        f'{i5}(unit {unit})\n'
        f'{i4})\n'
        f'{i3})\n'
    )

    # Check if block already has (instances
    instances_pos = block.find('(instances')
    if instances_pos != -1:
        # Check if this project already exists
        if f'(project "{project_name}"' in block:
            return text

        # Find the closing ) of the (instances block
        # Walk from instances_pos tracking depth
        depth = 0
        i = instances_pos
        while i < len(block):
            ch = block[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    # Insert the new project entry before this closing )
                    insert_pos = start + i
                    # Find the start of the line containing this )
                    line_start = text.rfind('\n', 0, insert_pos) + 1
                    return (
                        text[:line_start]
                        + project_entry
                        + text[line_start:]
                    )
            elif ch == '"':
                i += 1
                while i < len(block) and block[i] != '"':
                    if block[i] == '\\':
                        i += 1
                    i += 1
            i += 1

        return text  # Couldn't find instances closing

    # No instances block - insert one before the symbol's closing )
    # The closing ) of the symbol block is at position `end - 1`
    # We want to insert before the line containing that closing paren
    closing_paren_pos = end - 1
    line_start = text.rfind('\n', 0, closing_paren_pos) + 1

    instances_block = (
        f'{i2}(instances\n'
        f'{project_entry}'
        f'{i2})\n'
    )

    return text[:line_start] + instances_block + text[line_start:]


def _collect_existing_refs(
    all_file_symbols: dict[Path, list[dict]],
) -> dict[str, set[int]]:
    """Collect all existing reference numbers per prefix across all files.

    Only counts symbols that already have project instances (i.e. already
    annotated in the project).
    """
    existing: dict[str, set[int]] = {}
    for symbols in all_file_symbols.values():
        for sym in symbols:
            if sym["has_project_instance"] and sym["number"] is not None:
                existing.setdefault(sym["prefix"], set()).add(sym["number"])
    return existing


def run_repair_instances(
    schematic_path: Path,
    dry_run: bool = False,
    backup: bool = True,
    format: str = "text",
) -> int:
    """Run the repair-instances operation.

    Args:
        schematic_path: Path to root .kicad_sch file.
        dry_run: If True, show what would change without modifying files.
        backup: If True, create backups before modifying.
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

    # Detect project info
    project_name, root_uuid, file_instance_paths = _detect_project_info(
        schematic_path, all_files
    )

    if not root_uuid:
        print("Error: Could not detect project UUID from root schematic", file=sys.stderr)
        return 1

    # Phase 1: Collect all symbols across hierarchy with instance info
    file_symbols: dict[Path, list[dict]] = {}
    file_texts: dict[Path, str] = {}

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            return 1
        file_texts[sch_file] = text
        file_symbols[sch_file] = _extract_symbols_with_instance_info(
            text, project_name
        )

    # Phase 2: Find symbols missing project instances
    missing: list[dict] = []  # [{file, sym}, ...]
    for sch_file in all_files:
        for sym in file_symbols[sch_file]:
            if not sym["has_project_instance"]:
                missing.append({"file": sch_file, "sym": sym})

    if not missing:
        if format == "text":
            print("All symbols have project instances. No repairs needed.")
        elif format == "json":
            import json
            print(json.dumps({"repairs": [], "total": 0, "dry_run": dry_run}))
        return 0

    # Phase 3: Assign reference designators for unannotated symbols
    # Collect existing refs to avoid conflicts
    existing_refs = _collect_existing_refs(file_symbols)

    # Also collect refs from the missing symbols that are already annotated
    # (have a number but just lack the instances block)
    for entry in missing:
        sym = entry["sym"]
        if sym["number"] is not None:
            existing_refs.setdefault(sym["prefix"], set()).add(sym["number"])

    # Track counters for assigning new refs
    counters: dict[str, int] = {}

    def _next_available(prefix: str) -> int:
        n = counters.get(prefix, 1)
        reserved = existing_refs.get(prefix, set())
        while n in reserved:
            n += 1
        counters[prefix] = n + 1
        # Also reserve so subsequent calls don't reuse
        reserved.add(n)
        return n

    repairs: list[dict] = []
    for entry in missing:
        sym = entry["sym"]
        sch_file = entry["file"]

        if sym["number"] is not None:
            # Already annotated, just needs instances block
            new_ref = sym["reference"]
        else:
            # Unannotated (R?, C?, etc.) - assign next available
            new_number = _next_available(sym["prefix"])
            new_ref = _format_reference(
                sym["prefix"], new_number, sym["unit_suffix"]
            )

        instance_path = file_instance_paths.get(sch_file, "")

        repairs.append({
            "file": sch_file,
            "uuid": sym["uuid"],
            "lib_id": sym["lib_id"],
            "old_ref": sym["reference"],
            "new_ref": new_ref,
            "instance_path": instance_path,
            "needs_rename": sym["number"] is None,
        })

    # Phase 4: Output
    if format == "json":
        import json
        output = {
            "repairs": [
                {
                    "file": str(r["file"]),
                    "uuid": r["uuid"],
                    "lib_id": r["lib_id"],
                    "old_ref": r["old_ref"],
                    "new_ref": r["new_ref"],
                    "instance_path": r["instance_path"],
                }
                for r in repairs
            ],
            "total": len(repairs),
            "dry_run": dry_run,
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"Found {len(repairs)} symbol(s) missing project instances:")
        print()
        # Group by file for display
        by_file: dict[Path, list[dict]] = {}
        for r in repairs:
            by_file.setdefault(r["file"], []).append(r)

        for sch_file, file_repairs in by_file.items():
            print(f"  {sch_file.name}:")
            for r in file_repairs:
                if r["needs_rename"]:
                    print(
                        f"    {r['old_ref']} -> {r['new_ref']}"
                        f"  ({r['lib_id']})"
                    )
                else:
                    print(
                        f"    {r['new_ref']}"
                        f"  ({r['lib_id']}) [add instances block]"
                    )
        print()

    if dry_run:
        if format == "text":
            print("Dry run: no changes made.")
        return 0

    # Phase 5: Apply changes
    modified_files: dict[Path, str] = {}

    for r in repairs:
        sch_file = r["file"]
        text = modified_files.get(sch_file, file_texts[sch_file])

        # If unannotated, rename the reference first
        if r["needs_rename"]:
            text = _apply_uuid_reference_rename(
                text, r["uuid"], r["new_ref"]
            )

        # Add the project instance block
        if r["instance_path"]:
            text = _insert_project_instance(
                text,
                r["uuid"],
                project_name,
                r["instance_path"],
                r["new_ref"],
            )

        modified_files[sch_file] = text

    # Phase 6: Write modified files
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
            f"Repaired {len(repairs)} instance(s) "
            f"across {len(modified_files)} file(s)."
        )

    return 0


def main(argv: list[str] | None = None):
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Repair missing project instances in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--backup", action="store_true",
        help="Create backup before modifying",
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)

    return run_repair_instances(
        schematic_path=args.schematic,
        dry_run=args.dry_run,
        backup=args.backup,
        format=args.format,
    )


if __name__ == "__main__":
    sys.exit(main())
