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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kicad_tools.core.sexp import SExp, parse_sexp

KICAD_SCRIPTS = Path(__file__).resolve().parent


@dataclass
class Pin:
    """Represents a symbol pin."""

    number: str
    name: str
    pin_type: str
    position: tuple[float, float] = (0, 0)
    orientation: int = 0

    @property
    def normalized_name(self) -> str:
        """Normalize pin name for matching."""
        name = self.name.upper()
        # Remove suffixes like _39, _40
        name = re.sub(r"_\d+$", "", name)
        # Normalize common variations
        name = name.replace("~{", "").replace("}", "")  # Active low markers
        name = name.replace("/", "_")
        name = name.replace("+", "P").replace("-", "N")
        return name

    @property
    def function_category(self) -> str:
        """Categorize pin by function."""
        name_upper = self.name.upper()

        # Power pins
        if any(p in name_upper for p in ["VCC", "VDD", "PVDD", "AVDD", "DVDD", "GVDD", "VBG"]):
            return "power_positive"
        if any(p in name_upper for p in ["GND", "PGND", "AGND", "EP"]):
            return "power_ground"

        # Bootstrap pins
        if "BST" in name_upper:
            return "bootstrap"

        # Audio inputs
        if any(p in name_upper for p in ["INPUT", "INP", "INN", "IN_"]):
            return "audio_input"

        # Audio outputs
        if "OUT" in name_upper:
            return "audio_output"

        # Control/status pins
        if any(p in name_upper for p in ["FAULT", "CLIP", "OTW", "SD", "MUTE", "RESET"]):
            return "status_control"

        # Oscillator pins
        if any(p in name_upper for p in ["OSC", "FREQ"]):
            return "oscillator"

        # Configuration pins
        if any(p in name_upper for p in ["GAIN", "M1", "M2", "HEAD", "PLIMIT", "OC_ADJ"]):
            return "configuration"

        # No connect
        if name_upper in ["NC", "N/C", "N.C."]:
            return "no_connect"

        return "other"


@dataclass
class PinMapping:
    """Represents a mapping between source and target pins."""

    source_pin: Pin
    target_pin: Optional[Pin]
    confidence: float  # 0.0 to 1.0
    match_reason: str

    @property
    def is_matched(self) -> bool:
        return self.target_pin is not None


@dataclass
class MappingResult:
    """Complete mapping analysis between two symbols."""

    source_name: str
    target_name: str
    source_pins: list[Pin]
    target_pins: list[Pin]
    mappings: list[PinMapping] = field(default_factory=list)
    unmatched_target: list[Pin] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for m in self.mappings if m.is_matched)

    @property
    def unmatched_source_count(self) -> int:
        return sum(1 for m in self.mappings if not m.is_matched)

    @property
    def match_percentage(self) -> float:
        if not self.mappings:
            return 0.0
        return (self.matched_count / len(self.mappings)) * 100


def extract_pins_from_symbol(symbol: SExp, recursive: bool = True) -> list[Pin]:
    """Extract pins from a symbol S-expression.

    Args:
        symbol: The symbol S-expression node
        recursive: If True, also search nested sub-symbols (unit variants)
    """
    pins = []
    seen_numbers = set()

    def extract_from_node(node: SExp):
        for pin_node in node.find_all("pin"):
            # Get pin type (first value after tag)
            pin_type_raw = pin_node.get_string(0) or "unspecified"

            # Get pin name and number
            name_node = pin_node.find("name")
            number_node = pin_node.find("number")
            at_node = pin_node.find("at")

            name = name_node.get_string(0) if name_node else ""
            number = number_node.get_string(0) if number_node else ""

            # Skip duplicate pin numbers (from multiple units)
            if number in seen_numbers:
                continue
            seen_numbers.add(number)

            position = (0.0, 0.0)
            orientation = 0
            if at_node:
                position = (at_node.get_float(0) or 0, at_node.get_float(1) or 0)
                orientation = int(at_node.get_float(2) or 0)

            # Map KiCad pin type to readable string
            type_map = {
                "input": "Input",
                "output": "Output",
                "bidirectional": "Bidirectional",
                "tri_state": "Tri-State",
                "passive": "Passive",
                "free": "Free",
                "unspecified": "Unspecified",
                "power_in": "Power Input",
                "power_out": "Power Output",
                "open_collector": "Open Collector",
                "open_emitter": "Open Emitter",
                "no_connect": "No Connect",
            }
            pin_type = type_map.get(pin_type_raw, pin_type_raw)

            pins.append(
                Pin(
                    number=number,
                    name=name,
                    pin_type=pin_type,
                    position=position,
                    orientation=orientation,
                )
            )

        # Recursively search nested symbols (sub-units like Symbol_0_1, Symbol_1_1)
        if recursive:
            for sub_sym in node.find_all("symbol"):
                extract_from_node(sub_sym)

    extract_from_node(symbol)

    return sorted(pins, key=lambda p: (int(p.number) if p.number.isdigit() else 999, p.number))


def load_symbol_from_file(path: Path) -> tuple[str, list[Pin]]:
    """Load a symbol from a .kicad_sym file."""
    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_symbol_lib":
        raise ValueError(f"Not a symbol library: {path}")

    # Get first symbol
    symbols = sexp.find_all("symbol")
    if not symbols:
        raise ValueError(f"No symbols found in: {path}")

    # Use first symbol (skip sub-units)
    main_symbol = None
    for sym in symbols:
        name = sym.get_string(0) or ""
        if not re.search(r"_\d+_\d+$", name):  # Skip unit variants like Symbol_1_1
            main_symbol = sym
            break

    if not main_symbol:
        main_symbol = symbols[0]

    name = main_symbol.get_string(0) or path.stem
    pins = extract_pins_from_symbol(main_symbol)

    return name, pins


def load_symbol_from_schematic(sch_path: Path, lib_id: str) -> tuple[str, list[Pin]]:
    """Load an embedded symbol from a schematic's lib_symbols section."""
    text = sch_path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_sch":
        raise ValueError(f"Not a schematic: {sch_path}")

    lib_symbols = sexp.find("lib_symbols")
    if not lib_symbols:
        raise ValueError(f"No lib_symbols section in: {sch_path}")

    # Find the requested symbol
    for sym in lib_symbols.find_all("symbol"):
        sym_name = sym.get_string(0) or ""
        if sym_name == lib_id:
            pins = extract_pins_from_symbol(sym)
            return lib_id, pins

    raise ValueError(f"Symbol '{lib_id}' not found in schematic lib_symbols")


def match_pins(
    source_pins: list[Pin], target_pins: list[Pin]
) -> tuple[list[PinMapping], list[Pin]]:
    """
    Match source pins to target pins using multiple strategies.

    Returns (mappings, unmatched_target_pins)
    """
    mappings = []
    used_targets = set()

    # Build lookup structures for target pins
    target_by_name = {p.name: p for p in target_pins}
    target_by_normalized = {}
    for p in target_pins:
        norm = p.normalized_name
        if norm not in target_by_normalized:
            target_by_normalized[norm] = []
        target_by_normalized[norm].append(p)

    target_by_number = {p.number: p for p in target_pins}
    target_by_category = {}
    for p in target_pins:
        cat = p.function_category
        if cat not in target_by_category:
            target_by_category[cat] = []
        target_by_category[cat].append(p)

    for src in source_pins:
        mapping = None

        # Strategy 1: Exact name match (highest confidence)
        if src.name in target_by_name and src.name not in used_targets:
            tgt = target_by_name[src.name]
            if tgt.number not in used_targets:
                mapping = PinMapping(src, tgt, 1.0, "Exact name match")
                used_targets.add(tgt.number)

        # Strategy 2: Normalized name match
        if not mapping:
            norm_name = src.normalized_name
            if norm_name in target_by_normalized:
                candidates = [
                    p for p in target_by_normalized[norm_name] if p.number not in used_targets
                ]
                if candidates:
                    # Prefer same pin type
                    same_type = [p for p in candidates if p.pin_type == src.pin_type]
                    tgt = same_type[0] if same_type else candidates[0]
                    mapping = PinMapping(src, tgt, 0.8, f"Normalized name match ({norm_name})")
                    used_targets.add(tgt.number)

        # Strategy 3: Same pin number (low confidence)
        if not mapping:
            if src.number in target_by_number:
                tgt = target_by_number[src.number]
                if tgt.number not in used_targets:
                    # Only if same category
                    if src.function_category == tgt.function_category:
                        mapping = PinMapping(
                            src, tgt, 0.4, f"Same pin number + category ({src.function_category})"
                        )
                        used_targets.add(tgt.number)

        # Strategy 4: Function category match (suggestion only)
        if not mapping:
            cat = src.function_category
            if cat in target_by_category and cat != "other":
                candidates = [p for p in target_by_category[cat] if p.number not in used_targets]
                if candidates:
                    tgt = candidates[0]
                    mapping = PinMapping(src, tgt, 0.2, f"Category match ({cat})")
                    # Don't mark as used - these are weak matches

        # No match found
        if not mapping:
            mapping = PinMapping(src, None, 0.0, "No match found")

        mappings.append(mapping)

    # Find unmatched target pins
    unmatched = [p for p in target_pins if p.number not in used_targets]

    return mappings, unmatched


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
    print(f"PIN MAPPING: {result.source_name} → {result.target_name}")
    print(f"{'=' * 70}")
    print(f"Source pins: {len(result.source_pins)}")
    print(f"Target pins: {len(result.target_pins)}")
    print(f"Matched: {result.matched_count} ({result.match_percentage:.1f}%)")
    print(f"Unmatched source: {result.unmatched_source_count}")
    print(f"Unmatched target: {len(result.unmatched_target)}")

    # Matched pins
    matched = [m for m in result.mappings if m.is_matched]
    if matched and not show_unmatched_only:
        print(f"\n{'─' * 70}")
        print("MATCHED PINS")
        print(f"{'─' * 70}")
        print(f"{'Source':<20} {'Target':<20} {'Conf':<6} {'Reason':<24}")
        print(f"{'─' * 20} {'─' * 20} {'─' * 6} {'─' * 24}")

        # Group by confidence
        for m in sorted(matched, key=lambda x: -x.confidence):
            src_str = f"{m.source_pin.number}:{m.source_pin.name}"
            tgt_str = f"{m.target_pin.number}:{m.target_pin.name}"
            conf_str = f"{m.confidence * 100:.0f}%"
            print(f"{src_str:<20} {tgt_str:<20} {conf_str:<6} {m.match_reason:<24}")

    # Unmatched source pins
    unmatched_src = [m for m in result.mappings if not m.is_matched]
    if unmatched_src:
        print(f"\n{'─' * 70}")
        print("UNMATCHED SOURCE PINS (need manual mapping or removal)")
        print(f"{'─' * 70}")
        print(f"{'Pin':<8} {'Name':<20} {'Type':<15} {'Category':<15}")
        print(f"{'─' * 8} {'─' * 20} {'─' * 15} {'─' * 15}")
        for m in unmatched_src:
            p = m.source_pin
            print(f"{p.number:<8} {p.name:<20} {p.pin_type:<15} {p.function_category:<15}")

    # Unmatched target pins
    if result.unmatched_target:
        print(f"\n{'─' * 70}")
        print("UNMATCHED TARGET PINS (new pins in target symbol)")
        print(f"{'─' * 70}")
        print(f"{'Pin':<8} {'Name':<20} {'Type':<15} {'Category':<15}")
        print(f"{'─' * 8} {'─' * 20} {'─' * 15} {'─' * 15}")
        for p in result.unmatched_target:
            print(f"{p.number:<8} {p.name:<20} {p.pin_type:<15} {p.function_category:<15}")

    # Summary by category
    if show_all:
        print(f"\n{'─' * 70}")
        print("SUMMARY BY FUNCTION CATEGORY")
        print(f"{'─' * 70}")

        categories = set(p.function_category for p in result.source_pins)
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
        print(f"{result.source_name} → {result.target_name}")
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
