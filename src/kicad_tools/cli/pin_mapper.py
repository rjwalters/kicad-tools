#!/usr/bin/env python3
"""
Compare two KiCad symbols and generate pin mappings for replacement.

Analyzes pin names, types, and functions to suggest how to rewire
when replacing one symbol with another.

Usage:
    # Compare two symbol library files
    python3 scripts/kicad/pin-mapper.py lib/TPA3251.kicad_sym lib/TPA3116D2.kicad_sym

    # Compare symbols from schematic's embedded lib_symbols
    python3 scripts/kicad/pin-mapper.py schematic.kicad_sch --from "Amplifier_Audio:TPA3251" --to "TPA3116D2:TPA3116D2"

    # Output as JSON for programmatic use
    python3 scripts/kicad/pin-mapper.py source.kicad_sym target.kicad_sym --json

    # Show only unmatched pins
    python3 scripts/kicad/pin-mapper.py source.kicad_sym target.kicad_sym --unmatched
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.operations.pinmap import (
    MappingResult,
    Pin,
    load_symbol_from_file,
    load_symbol_from_schematic,
    match_pins,
)


def generate_mapping(
    source_name: str, source_pins: list[Pin], target_name: str, target_pins: list[Pin]
) -> MappingResult:
    """Generate complete mapping analysis."""
    mappings, unmatched_target = match_pins(source_pins, target_pins)

    return MappingResult(
        source_name=source_name,
        target_name=target_name,
        source_pins=source_pins,
        target_pins=target_pins,
        mappings=mappings,
        unmatched_target=unmatched_target,
    )


def print_mapping_report(
    result: MappingResult, show_all: bool = True, show_unmatched_only: bool = False
):
    """Print human-readable mapping report."""
    print(f"\n{'=' * 70}")
    print(f"PIN MAPPING: {result.source_name} -> {result.target_name}")
    print(f"{'=' * 70}")
    print(f"Source pins: {len(result.source_pins)}")
    print(f"Target pins: {len(result.target_pins)}")
    print(f"Matched: {result.matched_count} ({result.match_percentage:.1f}%)")
    print(f"Unmatched source: {result.unmatched_source_count}")
    print(f"Unmatched target: {len(result.unmatched_target)}")

    # Matched pins
    matched = [m for m in result.mappings if m.is_matched]
    if matched and not show_unmatched_only:
        print(f"\n{'-' * 70}")
        print("MATCHED PINS")
        print(f"{'-' * 70}")
        print(f"{'Source':<20} {'Target':<20} {'Conf':<6} {'Reason':<24}")
        print(f"{'-' * 20} {'-' * 20} {'-' * 6} {'-' * 24}")

        # Group by confidence
        for m in sorted(matched, key=lambda x: -x.confidence):
            src_str = f"{m.source_pin.number}:{m.source_pin.name}"
            tgt_str = f"{m.target_pin.number}:{m.target_pin.name}"
            conf_str = f"{m.confidence * 100:.0f}%"
            print(f"{src_str:<20} {tgt_str:<20} {conf_str:<6} {m.match_reason:<24}")

    # Unmatched source pins
    unmatched_src = [m for m in result.mappings if not m.is_matched]
    if unmatched_src:
        print(f"\n{'-' * 70}")
        print("UNMATCHED SOURCE PINS (need manual mapping or removal)")
        print(f"{'-' * 70}")
        print(f"{'Pin':<8} {'Name':<20} {'Type':<15} {'Category':<15}")
        print(f"{'-' * 8} {'-' * 20} {'-' * 15} {'-' * 15}")
        for m in unmatched_src:
            p = m.source_pin
            print(f"{p.number:<8} {p.name:<20} {p.pin_type:<15} {p.function_category:<15}")

    # Unmatched target pins
    if result.unmatched_target:
        print(f"\n{'-' * 70}")
        print("UNMATCHED TARGET PINS (new pins in target symbol)")
        print(f"{'-' * 70}")
        print(f"{'Pin':<8} {'Name':<20} {'Type':<15} {'Category':<15}")
        print(f"{'-' * 8} {'-' * 20} {'-' * 15} {'-' * 15}")
        for p in result.unmatched_target:
            print(f"{p.number:<8} {p.name:<20} {p.pin_type:<15} {p.function_category:<15}")

    # Summary by category
    if show_all:
        print(f"\n{'-' * 70}")
        print("SUMMARY BY FUNCTION CATEGORY")
        print(f"{'-' * 70}")

        categories = {p.function_category for p in result.source_pins}
        categories.update(p.function_category for p in result.target_pins)

        for cat in sorted(categories):
            src_count = sum(1 for p in result.source_pins if p.function_category == cat)
            tgt_count = sum(1 for p in result.target_pins if p.function_category == cat)
            matched_count = sum(
                1 for m in result.mappings if m.is_matched and m.source_pin.function_category == cat
            )
            print(f"{cat:<20}: source={src_count}, target={tgt_count}, matched={matched_count}")


def mapping_to_dict(result: MappingResult) -> dict:
    """Convert mapping result to dictionary for JSON output."""
    return {
        "source": {
            "name": result.source_name,
            "pin_count": len(result.source_pins),
        },
        "target": {
            "name": result.target_name,
            "pin_count": len(result.target_pins),
        },
        "statistics": {
            "matched": result.matched_count,
            "unmatched_source": result.unmatched_source_count,
            "unmatched_target": len(result.unmatched_target),
            "match_percentage": result.match_percentage,
        },
        "mappings": [
            {
                "source_pin": m.source_pin.number,
                "source_name": m.source_pin.name,
                "target_pin": m.target_pin.number if m.target_pin else None,
                "target_name": m.target_pin.name if m.target_pin else None,
                "confidence": m.confidence,
                "reason": m.match_reason,
            }
            for m in result.mappings
        ],
        "unmatched_target": [
            {
                "pin": p.number,
                "name": p.name,
                "type": p.pin_type,
                "category": p.function_category,
            }
            for p in result.unmatched_target
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Compare two KiCad symbols and generate pin mappings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        help="Source symbol file (.kicad_sym) or schematic (.kicad_sch)",
    )
    parser.add_argument("target", type=Path, nargs="?", help="Target symbol file (.kicad_sym)")

    parser.add_argument(
        "--from",
        dest="from_lib",
        type=str,
        help="Source lib_id when using schematic (e.g., 'Amplifier_Audio:TPA3251')",
    )
    parser.add_argument("--to", dest="to_lib", type=str, help="Target lib_id or symbol file")

    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--unmatched", action="store_true", help="Show only unmatched pins")
    parser.add_argument("--brief", action="store_true", help="Show brief summary only")

    args = parser.parse_args()

    # Determine source
    source_name = None
    source_pins = None

    if args.from_lib and args.source:
        # Load from schematic's lib_symbols
        if args.source.suffix == ".kicad_sch":
            source_name, source_pins = load_symbol_from_schematic(args.source, args.from_lib)
        else:
            print(f"Error: --from requires a schematic file, got: {args.source}")
            return 1
    elif args.source and args.source.suffix == ".kicad_sym":
        source_name, source_pins = load_symbol_from_file(args.source)
    else:
        parser.print_help()
        print("\nError: Must provide source symbol file or schematic with --from")
        return 1

    # Determine target
    target_name = None
    target_pins = None

    if args.to_lib:
        # Check if it's a file path or lib_id
        to_path = Path(args.to_lib)
        if to_path.exists() and to_path.suffix == ".kicad_sym":
            target_name, target_pins = load_symbol_from_file(to_path)
        elif args.source and args.source.suffix == ".kicad_sch":
            target_name, target_pins = load_symbol_from_schematic(args.source, args.to_lib)
        else:
            print(f"Error: Cannot find target symbol: {args.to_lib}")
            return 1
    elif args.target:
        if args.target.suffix == ".kicad_sym":
            target_name, target_pins = load_symbol_from_file(args.target)
        else:
            print(f"Error: Target must be a .kicad_sym file: {args.target}")
            return 1
    else:
        parser.print_help()
        print("\nError: Must provide target symbol file or --to lib_id")
        return 1

    # Generate mapping
    result = generate_mapping(source_name, source_pins, target_name, target_pins)

    # Output
    if args.json:
        print(json.dumps(mapping_to_dict(result), indent=2))
    elif args.brief:
        print(f"{result.source_name} -> {result.target_name}")
        print(
            f"  Matched: {result.matched_count}/{len(result.source_pins)} ({result.match_percentage:.1f}%)"
        )
        print(f"  Unmatched source: {result.unmatched_source_count}")
        print(f"  Unmatched target: {len(result.unmatched_target)}")
    else:
        print_mapping_report(
            result, show_all=not args.unmatched, show_unmatched_only=args.unmatched
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
