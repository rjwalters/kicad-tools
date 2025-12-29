#!/usr/bin/env python3
"""
Modify KiCad schematic files.

Provides operations to add, delete, and replace symbols in schematics.
Uses text-based modifications to preserve original formatting.
Always creates a backup before modifying.

Usage:
    # Delete a symbol
    python3 scripts/kicad/modify-schematic.py schematic.kicad_sch --delete U1

    # List what would be deleted (dry run)
    python3 scripts/kicad/modify-schematic.py schematic.kicad_sch --delete U1 --dry-run

    # Replace a symbol's library reference
    python3 scripts/kicad/modify-schematic.py schematic.kicad_sch --replace U1 --new-lib "TPA3116D2:TPA3116D2"

    # Add embedded lib_symbol from file
    python3 scripts/kicad/modify-schematic.py schematic.kicad_sch --add-lib-symbol lib/symbols/TPA3116D2.kicad_sym

    # Update symbol value
    python3 scripts/kicad/modify-schematic.py schematic.kicad_sch --set-value U1 "TPA3116D2"
"""

import argparse
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from kicad_tools.core.sexp import SExp, parse_sexp

KICAD_SCRIPTS = Path(__file__).resolve().parent


def generate_uuid() -> str:
    """Generate a KiCad-compatible UUID."""
    return str(uuid.uuid4())


# Text-based modification functions (preserve original formatting)


def find_symbol_text_range(text: str, reference: str) -> Optional[tuple[int, int, dict]]:
    """
    Find the text range of a symbol instance by reference.

    Returns (start, end, info) or None if not found.
    """
    # Pattern to match symbol blocks with the given reference
    # We need to find (symbol ... (property "Reference" "U1" ...) ...)

    # First find all symbol instance blocks (not lib_symbols)
    symbol_pattern = re.compile(
        r"\n(\t\(symbol\n"
        r'\t\t\(lib_id "[^"]+"\)'
        r".*?"
        r"\t\t\(instances\n.*?\t\t\)\n"
        r"\t\))",
        re.DOTALL,
    )

    for match in symbol_pattern.finditer(text):
        block = match.group(1)
        # Check if this block has the reference we want
        ref_pattern = re.compile(r'\(property "Reference" "' + re.escape(reference) + r'"')
        if ref_pattern.search(block):
            # Extract info
            lib_id_match = re.search(r'\(lib_id "([^"]+)"\)', block)
            pos_match = re.search(r"\(at ([\d.]+) ([\d.]+)", block)
            uuid_match = re.search(r'\(uuid "([^"]+)"\)', block)
            # Value property spans multiple lines - just capture the quoted value
            value_match = re.search(r'\(property "Value" "([^"]*)"', block)

            info = {
                "lib_id": lib_id_match.group(1) if lib_id_match else "",
                "position": (float(pos_match.group(1)), float(pos_match.group(2)))
                if pos_match
                else (0, 0),
                "uuid": uuid_match.group(1) if uuid_match else "",
                "value": value_match.group(1) if value_match else "",
            }

            return (match.start(), match.end(), info)

    return None


def delete_symbol_text(text: str, reference: str) -> tuple[str, bool, str]:
    """
    Delete a symbol from the schematic text.

    Returns (modified_text, success, message).
    """
    result = find_symbol_text_range(text, reference)

    if not result:
        return text, False, f"Symbol '{reference}' not found"

    start, end, info = result

    # Remove the symbol block
    modified = text[:start] + text[end:]

    return modified, True, f"Deleted: {reference} ({info['lib_id']}) at {info['position']}"


def set_value_text(text: str, reference: str, new_value: str) -> tuple[str, bool, str]:
    """
    Set a symbol's Value property using text replacement.

    Returns (modified_text, success, message).
    """
    result = find_symbol_text_range(text, reference)

    if not result:
        return text, False, f"Symbol '{reference}' not found"

    start, end, info = result
    block = text[start:end]
    old_value = info["value"]

    # Replace the value property in this block (handles multi-line)
    # Match (property "Value" "..." without requiring closing paren on same line
    value_pattern = re.compile(r'(\(property "Value" )"[^"]*"')
    new_block = value_pattern.sub(rf'\g<1>"{new_value}"', block, count=1)

    if new_block == block:
        return text, False, f"Could not find Value property on {reference}"

    modified = text[:start] + new_block + text[end:]

    return modified, True, f"Changed {reference} value: '{old_value}' -> '{new_value}'"


def set_lib_id_text(text: str, reference: str, new_lib_id: str) -> tuple[str, bool, str]:
    """
    Change a symbol's library reference using text replacement.

    Returns (modified_text, success, message).
    """
    result = find_symbol_text_range(text, reference)

    if not result:
        return text, False, f"Symbol '{reference}' not found"

    start, end, info = result
    block = text[start:end]
    old_lib_id = info["lib_id"]

    # Replace the lib_id in this block
    lib_id_pattern = re.compile(r'(\(lib_id )"[^"]*"')
    new_block = lib_id_pattern.sub(rf'\1"{new_lib_id}"', block, count=1)

    if new_block == block:
        return text, False, f"Could not find lib_id on {reference}"

    modified = text[:start] + new_block + text[end:]

    return modified, True, f"Changed {reference} lib_id: '{old_lib_id}' -> '{new_lib_id}'"


def add_lib_symbol_text(text: str, lib_file: Path) -> tuple[str, bool, str]:
    """
    Add a library symbol from a file to the lib_symbols section.

    Returns (modified_text, success, message).
    """
    if not lib_file.exists():
        return text, False, f"Library file not found: {lib_file}"

    # Read the library file
    lib_text = lib_file.read_text(encoding="utf-8")

    # Extract symbol definitions from the library
    # Find (symbol "NAME" ...) blocks
    symbol_pattern = re.compile(r'\n(\t\(symbol "[^"]+".*?\n\t\))', re.DOTALL)

    symbols_to_add = []
    for match in symbol_pattern.finditer(lib_text):
        sym_block = match.group(1)
        # Get symbol name
        name_match = re.search(r'\(symbol "([^"]+)"', sym_block)
        if name_match:
            symbols_to_add.append((name_match.group(1), sym_block))

    if not symbols_to_add:
        return text, False, f"No symbols found in library: {lib_file}"

    # Find lib_symbols section end
    lib_symbols_end = re.search(r"\n\t\(lib_symbols\n.*?\n\t\)", text, re.DOTALL)
    if not lib_symbols_end:
        return text, False, "Schematic has no lib_symbols section"

    # Check which symbols already exist
    added = []
    skipped = []

    insert_pos = lib_symbols_end.end() - 3  # Before the closing "\n\t)"
    insert_text = ""

    for name, block in symbols_to_add:
        # Check if already exists
        if re.search(r'\(symbol "' + re.escape(name) + r'"', text):
            skipped.append(name)
        else:
            insert_text += block
            added.append(name)

    if not added:
        if skipped:
            return text, True, f"Skipped (already exist): {', '.join(skipped)}"
        return text, False, "No symbols to add"

    modified = text[:insert_pos] + insert_text + text[insert_pos:]

    msg_parts = []
    if added:
        msg_parts.append(f"Added: {', '.join(added)}")
    if skipped:
        msg_parts.append(f"Skipped: {', '.join(skipped)}")

    return modified, True, "; ".join(msg_parts)


def regen_uuids_text(text: str, reference: str) -> tuple[str, bool, str]:
    """
    Regenerate UUIDs for a symbol and its pins using text replacement.

    Returns (modified_text, success, message).
    """
    result = find_symbol_text_range(text, reference)

    if not result:
        return text, False, f"Symbol '{reference}' not found"

    start, end, info = result
    block = text[start:end]

    # Find and replace all UUIDs in this block
    uuid_pattern = re.compile(r'(\(uuid )"[^"]+"')

    uuid_count = len(uuid_pattern.findall(block))

    def uuid_replacer(match):
        return match.group(1) + f'"{generate_uuid()}"'

    new_block = uuid_pattern.sub(uuid_replacer, block)

    if new_block == block:
        return text, False, f"No UUIDs found on {reference}"

    modified = text[:start] + new_block + text[end:]

    return modified, True, f"Regenerated {uuid_count} UUIDs for {reference}"


def find_symbol_indices(sexp: SExp, reference: str) -> list[int]:
    """Find indices of symbol nodes matching reference."""
    indices = []
    for i, val in enumerate(sexp.values):
        if isinstance(val, SExp) and val.tag == "symbol":
            # Check if this is a symbol instance (has lib_id), not lib_symbols
            if val.find("lib_id"):
                # Find Reference property
                for prop in val.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref = prop.get_string(1) or ""
                        if ref == reference:
                            indices.append(i)
                        break
    return indices


def find_lib_symbol_index(sexp: SExp, lib_id: str) -> Optional[int]:
    """Find index of a lib_symbol in lib_symbols section."""
    lib_symbols = sexp.find("lib_symbols")
    if not lib_symbols:
        return None

    for i, val in enumerate(lib_symbols.values):
        if isinstance(val, SExp) and val.tag == "symbol":
            name = val.get_string(0) or ""
            if name == lib_id:
                return i
    return None


def get_symbol_info(sexp: SExp, index: int) -> dict:
    """Extract information about a symbol at given index."""
    sym = sexp.values[index]
    if not isinstance(sym, SExp):
        return {}

    info = {
        "index": index,
        "lib_id": "",
        "position": (0, 0),
        "uuid": "",
        "reference": "",
        "value": "",
        "footprint": "",
        "pin_count": 0,
    }

    if lid := sym.find("lib_id"):
        info["lib_id"] = lid.get_string(0) or ""

    if at := sym.find("at"):
        info["position"] = (at.get_float(0) or 0, at.get_float(1) or 0)

    if u := sym.find("uuid"):
        info["uuid"] = u.get_string(0) or ""

    for prop in sym.find_all("property"):
        name = prop.get_string(0) or ""
        val = prop.get_string(1) or ""
        if name == "Reference":
            info["reference"] = val
        elif name == "Value":
            info["value"] = val
        elif name == "Footprint":
            info["footprint"] = val

    info["pin_count"] = len(sym.find_all("pin"))

    return info


def delete_symbol(sexp: SExp, reference: str, dry_run: bool = False) -> tuple[bool, str]:
    """
    Delete a symbol from the schematic.

    Returns (success, message).
    """
    indices = find_symbol_indices(sexp, reference)

    if not indices:
        return False, f"Symbol '{reference}' not found"

    if len(indices) > 1:
        return False, f"Multiple symbols found with reference '{reference}' ({len(indices)})"

    info = get_symbol_info(sexp, indices[0])

    if dry_run:
        return True, f"Would delete: {reference} ({info['lib_id']}) at {info['position']}"

    # Remove the symbol
    del sexp.values[indices[0]]

    return True, f"Deleted: {reference} ({info['lib_id']}) at {info['position']}"


def set_symbol_value(
    sexp: SExp, reference: str, new_value: str, dry_run: bool = False
) -> tuple[bool, str]:
    """
    Set a symbol's Value property.

    Returns (success, message).
    """
    indices = find_symbol_indices(sexp, reference)

    if not indices:
        return False, f"Symbol '{reference}' not found"

    sym = sexp.values[indices[0]]
    old_value = ""

    for prop in sym.find_all("property"):
        if prop.get_string(0) == "Value":
            old_value = prop.get_string(1) or ""
            if dry_run:
                return True, f"Would change {reference} value: '{old_value}' -> '{new_value}'"
            # Update the value
            prop.set_value(1, new_value)
            return True, f"Changed {reference} value: '{old_value}' -> '{new_value}'"

    return False, f"Value property not found on {reference}"


def set_symbol_lib_id(
    sexp: SExp, reference: str, new_lib_id: str, dry_run: bool = False
) -> tuple[bool, str]:
    """
    Change a symbol's library reference.

    Returns (success, message).
    """
    indices = find_symbol_indices(sexp, reference)

    if not indices:
        return False, f"Symbol '{reference}' not found"

    sym = sexp.values[indices[0]]

    if lid := sym.find("lib_id"):
        old_lib_id = lid.get_string(0) or ""
        if dry_run:
            return True, f"Would change {reference} lib_id: '{old_lib_id}' -> '{new_lib_id}'"
        lid.set_value(0, new_lib_id)
        return True, f"Changed {reference} lib_id: '{old_lib_id}' -> '{new_lib_id}'"

    return False, f"lib_id not found on {reference}"


def add_lib_symbol_from_file(sexp: SExp, lib_file: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    Add a library symbol from a .kicad_sym file to the schematic's lib_symbols section.

    Returns (success, message).
    """
    if not lib_file.exists():
        return False, f"Library file not found: {lib_file}"

    # Parse the symbol library
    lib_text = lib_file.read_text(encoding="utf-8")
    lib_sexp = parse_sexp(lib_text)

    if lib_sexp.tag != "kicad_symbol_lib":
        return False, f"Not a symbol library: {lib_file}"

    # Get symbols from library
    lib_symbols_to_add = lib_sexp.find_all("symbol")
    if not lib_symbols_to_add:
        return False, f"No symbols found in library: {lib_file}"

    # Find or create lib_symbols section in schematic
    sch_lib_symbols = sexp.find("lib_symbols")
    if not sch_lib_symbols:
        return False, "Schematic has no lib_symbols section"

    added = []
    skipped = []

    for sym in lib_symbols_to_add:
        sym_name = sym.get_string(0) or ""
        if not sym_name:
            continue

        # Check if already exists
        existing = find_lib_symbol_index(sexp, sym_name)
        if existing is not None:
            skipped.append(sym_name)
            continue

        if dry_run:
            added.append(sym_name)
            continue

        # Add to lib_symbols
        sch_lib_symbols.values.append(sym)
        added.append(sym_name)

    msg_parts = []
    if added:
        action = "Would add" if dry_run else "Added"
        msg_parts.append(f"{action}: {', '.join(added)}")
    if skipped:
        msg_parts.append(f"Skipped (already exist): {', '.join(skipped)}")

    if not added and not skipped:
        return False, "No symbols to add"

    return True, "; ".join(msg_parts)


def regenerate_symbol_uuids(sexp: SExp, reference: str, dry_run: bool = False) -> tuple[bool, str]:
    """
    Regenerate UUIDs for a symbol and its pins.

    Returns (success, message).
    """
    indices = find_symbol_indices(sexp, reference)

    if not indices:
        return False, f"Symbol '{reference}' not found"

    sym = sexp.values[indices[0]]

    if dry_run:
        pin_count = len(sym.find_all("pin"))
        return True, f"Would regenerate UUIDs for {reference} (1 symbol + {pin_count} pins)"

    # Regenerate symbol UUID
    if u := sym.find("uuid"):
        u.set_value(0, generate_uuid())

    # Regenerate pin UUIDs
    pin_count = 0
    for pin in sym.find_all("pin"):
        if u := pin.find("uuid"):
            u.set_value(0, generate_uuid())
            pin_count += 1

    return True, f"Regenerated UUIDs for {reference}: 1 symbol + {pin_count} pins"


def list_symbols(sexp: SExp) -> list[dict]:
    """List all symbol instances in schematic."""
    symbols = []
    for i, val in enumerate(sexp.values):
        if isinstance(val, SExp) and val.tag == "symbol":
            if val.find("lib_id"):  # Instance, not lib definition
                info = get_symbol_info(sexp, i)
                symbols.append(info)
    return symbols


def create_backup(path: Path) -> Path:
    """Create a backup of the file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.parent / f"{path.stem}_backup_{timestamp}{path.suffix}"
    shutil.copy2(path, backup_path)
    return backup_path


def main():
    parser = argparse.ArgumentParser(
        description="Modify KiCad schematic files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to KiCad schematic file (.kicad_sch)")

    # Operations
    ops = parser.add_argument_group("Operations")
    ops.add_argument(
        "--delete", "-d", type=str, metavar="REF", help="Delete symbol by reference (e.g., U1)"
    )
    ops.add_argument(
        "--set-value", nargs=2, metavar=("REF", "VALUE"), help="Set symbol value property"
    )
    ops.add_argument(
        "--set-lib-id", nargs=2, metavar=("REF", "LIB_ID"), help="Change symbol library reference"
    )
    ops.add_argument(
        "--add-lib-symbol",
        type=Path,
        metavar="FILE",
        help="Add library symbol from .kicad_sym file",
    )
    ops.add_argument(
        "--regen-uuids", type=str, metavar="REF", help="Regenerate UUIDs for symbol and pins"
    )

    # Options
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Don't create backup before modifying"
    )
    parser.add_argument("--list", "-l", action="store_true", help="List all symbols in schematic")
    parser.add_argument(
        "--output", "-o", type=Path, help="Write to different file instead of modifying in place"
    )

    args = parser.parse_args()

    # Validate schematic
    if not args.schematic.exists():
        print(f"Error: Schematic not found: {args.schematic}")
        return 1

    if args.schematic.suffix != ".kicad_sch":
        print(f"Error: Not a schematic file: {args.schematic}")
        return 1

    # Load schematic
    try:
        text = args.schematic.read_text(encoding="utf-8")
        sexp = parse_sexp(text)
    except Exception as e:
        print(f"Error loading schematic: {e}")
        return 1

    if sexp.tag != "kicad_sch":
        print(f"Error: Not a valid schematic (tag: {sexp.tag})")
        return 1

    # List mode
    if args.list:
        symbols = list_symbols(sexp)
        if not symbols:
            print("No symbol instances found")
            return 0

        print(f"\nSymbols in {args.schematic.name}:")
        print(f"{'Ref':<8} {'Value':<20} {'Library':<35} {'Position':<15}")
        print("-" * 80)
        for s in sorted(symbols, key=lambda x: x["reference"]):
            pos = f"({s['position'][0]:.1f}, {s['position'][1]:.1f})"
            print(f"{s['reference']:<8} {s['value']:<20} {s['lib_id']:<35} {pos:<15}")
        return 0

    # Check if any operation specified
    operations = [
        args.delete,
        args.set_value,
        args.set_lib_id,
        args.add_lib_symbol,
        args.regen_uuids,
    ]
    if not any(operations):
        parser.print_help()
        print("\nError: No operation specified")
        return 1

    # Track modifications using text-based approach (preserves formatting)
    modified_text = text
    modified = False
    results = []

    # Execute operations using text-based functions
    if args.delete:
        if args.dry_run:
            result = find_symbol_text_range(modified_text, args.delete)
            if result:
                _, _, info = result
                msg = f"Would delete: {args.delete} ({info['lib_id']}) at {info['position']}"
                results.append(("Delete", True, msg))
            else:
                results.append(("Delete", False, f"Symbol '{args.delete}' not found"))
        else:
            modified_text, success, msg = delete_symbol_text(modified_text, args.delete)
            results.append(("Delete", success, msg))
            if success:
                modified = True

    if args.set_value:
        ref, value = args.set_value
        if args.dry_run:
            result = find_symbol_text_range(modified_text, ref)
            if result:
                _, _, info = result
                msg = f"Would change {ref} value: '{info['value']}' -> '{value}'"
                results.append(("Set Value", True, msg))
            else:
                results.append(("Set Value", False, f"Symbol '{ref}' not found"))
        else:
            modified_text, success, msg = set_value_text(modified_text, ref, value)
            results.append(("Set Value", success, msg))
            if success:
                modified = True

    if args.set_lib_id:
        ref, lib_id = args.set_lib_id
        if args.dry_run:
            result = find_symbol_text_range(modified_text, ref)
            if result:
                _, _, info = result
                msg = f"Would change {ref} lib_id: '{info['lib_id']}' -> '{lib_id}'"
                results.append(("Set Lib ID", True, msg))
            else:
                results.append(("Set Lib ID", False, f"Symbol '{ref}' not found"))
        else:
            modified_text, success, msg = set_lib_id_text(modified_text, ref, lib_id)
            results.append(("Set Lib ID", success, msg))
            if success:
                modified = True

    if args.add_lib_symbol:
        if args.dry_run:
            # Just check what would be added
            lib_text = (
                args.add_lib_symbol.read_text(encoding="utf-8")
                if args.add_lib_symbol.exists()
                else ""
            )
            import re as re_mod

            symbols = re_mod.findall(r'\(symbol "([^"]+)"', lib_text)
            if symbols:
                existing = [
                    s
                    for s in symbols
                    if re_mod.search(r'\(symbol "' + re_mod.escape(s) + r'"', modified_text)
                ]
                new = [s for s in symbols if s not in existing]
                if new:
                    results.append(("Add Lib Symbol", True, f"Would add: {', '.join(new)}"))
                elif existing:
                    results.append(
                        ("Add Lib Symbol", True, f"Skipped (already exist): {', '.join(existing)}")
                    )
            else:
                results.append(("Add Lib Symbol", False, "No symbols found in library"))
        else:
            modified_text, success, msg = add_lib_symbol_text(modified_text, args.add_lib_symbol)
            results.append(("Add Lib Symbol", success, msg))
            if success:
                modified = True

    if args.regen_uuids:
        if args.dry_run:
            result = find_symbol_text_range(modified_text, args.regen_uuids)
            if result:
                _, _, info = result
                msg = f"Would regenerate UUIDs for {args.regen_uuids}"
                results.append(("Regen UUIDs", True, msg))
            else:
                results.append(("Regen UUIDs", False, f"Symbol '{args.regen_uuids}' not found"))
        else:
            modified_text, success, msg = regen_uuids_text(modified_text, args.regen_uuids)
            results.append(("Regen UUIDs", success, msg))
            if success:
                modified = True

    # Print results
    print(f"\n{'=' * 60}")
    print(f"SCHEMATIC MODIFICATION {'(DRY RUN)' if args.dry_run else ''}")
    print(f"{'=' * 60}")
    print(f"File: {args.schematic.name}")

    for op_name, success, msg in results:
        status = "OK" if success else "FAILED"
        print(f"\n[{status}] {op_name}: {msg}")

    # Save if modified
    if modified and not args.dry_run:
        output_path = args.output or args.schematic

        # Create backup
        if not args.no_backup and output_path == args.schematic:
            backup = create_backup(args.schematic)
            print(f"\nBackup: {backup.name}")

        # Write modified text directly (preserves original formatting)
        try:
            output_path.write_text(modified_text, encoding="utf-8")
            print(f"Saved: {output_path.name}")
        except Exception as e:
            print(f"Error saving: {e}")
            return 1

    elif args.dry_run:
        print("\n(No changes made - dry run)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
