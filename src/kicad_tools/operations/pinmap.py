"""
Pin mapping and comparison operations.

Provides tools for comparing symbol pinouts and generating
mappings for symbol replacement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.sexp import SExp, parse_sexp


@dataclass
class Pin:
    """Represents a symbol pin."""

    number: str
    name: str
    pin_type: str
    position: Tuple[float, float] = (0.0, 0.0)
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
        power_positive = ["VCC", "VDD", "PVDD", "AVDD", "DVDD", "GVDD", "VBG"]
        if any(p in name_upper for p in power_positive):
            return "power_positive"

        power_ground = ["GND", "PGND", "AGND", "EP"]
        if any(p in name_upper for p in power_ground):
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
        status = ["FAULT", "CLIP", "OTW", "SD", "MUTE", "RESET"]
        if any(p in name_upper for p in status):
            return "status_control"

        # Oscillator pins
        if any(p in name_upper for p in ["OSC", "FREQ"]):
            return "oscillator"

        # Configuration pins
        config = ["GAIN", "M1", "M2", "HEAD", "PLIMIT", "OC_ADJ"]
        if any(p in name_upper for p in config):
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
    source_pins: List[Pin]
    target_pins: List[Pin]
    mappings: List[PinMapping] = field(default_factory=list)
    unmatched_target: List[Pin] = field(default_factory=list)

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

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "source": self.source_name,
            "target": self.target_name,
            "source_pin_count": len(self.source_pins),
            "target_pin_count": len(self.target_pins),
            "matched_count": self.matched_count,
            "match_percentage": self.match_percentage,
            "mappings": [
                {
                    "source_number": m.source_pin.number,
                    "source_name": m.source_pin.name,
                    "target_number": m.target_pin.number if m.target_pin else None,
                    "target_name": m.target_pin.name if m.target_pin else None,
                    "confidence": m.confidence,
                    "reason": m.match_reason,
                }
                for m in self.mappings
            ],
            "unmatched_target": [
                {"number": p.number, "name": p.name, "type": p.pin_type}
                for p in self.unmatched_target
            ],
        }


def extract_pins_from_sexp(symbol: SExp, recursive: bool = True) -> List[Pin]:
    """Extract pins from a symbol S-expression.

    Args:
        symbol: The symbol S-expression node
        recursive: If True, also search nested sub-symbols (unit variants)
    """
    pins: List[Pin] = []
    seen_numbers: set = set()

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

    def extract_from_node(node: SExp) -> None:
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

            pin_type = type_map.get(pin_type_raw, pin_type_raw)

            pins.append(
                Pin(
                    number=number,
                    name=name or "",
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


def load_symbol_from_file(path: str | Path) -> Tuple[str, List[Pin]]:
    """Load a symbol from a .kicad_sym file."""
    path = Path(path)
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
    pins = extract_pins_from_sexp(main_symbol)

    return name, pins


def load_symbol_from_schematic(sch_path: str | Path, lib_id: str) -> Tuple[str, List[Pin]]:
    """Load an embedded symbol from a schematic's lib_symbols section."""
    path = Path(sch_path)
    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_sch":
        raise ValueError(f"Not a schematic: {path}")

    lib_symbols = sexp.find("lib_symbols")
    if not lib_symbols:
        raise ValueError(f"No lib_symbols section in: {path}")

    # Find the requested symbol
    for sym in lib_symbols.find_all("symbol"):
        sym_name = sym.get_string(0) or ""
        if sym_name == lib_id:
            pins = extract_pins_from_sexp(sym)
            return lib_id, pins

    raise ValueError(f"Symbol '{lib_id}' not found in schematic lib_symbols")


def match_pins(
    source_pins: List[Pin], target_pins: List[Pin]
) -> Tuple[List[PinMapping], List[Pin]]:
    """
    Match source pins to target pins using multiple strategies.

    Returns (mappings, unmatched_target_pins)
    """
    mappings: List[PinMapping] = []
    used_targets: set = set()

    # Build lookup structures for target pins
    target_by_name = {p.name: p for p in target_pins}
    target_by_normalized: Dict[str, List[Pin]] = {}
    for p in target_pins:
        norm = p.normalized_name
        if norm not in target_by_normalized:
            target_by_normalized[norm] = []
        target_by_normalized[norm].append(p)

    target_by_number = {p.number: p for p in target_pins}
    target_by_category: Dict[str, List[Pin]] = {}
    for p in target_pins:
        cat = p.function_category
        if cat not in target_by_category:
            target_by_category[cat] = []
        target_by_category[cat].append(p)

    for src in source_pins:
        mapping: Optional[PinMapping] = None

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
                            src,
                            tgt,
                            0.4,
                            f"Same pin number + category ({src.function_category})",
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


def compare_symbols(source_path: str | Path, target_path: str | Path) -> MappingResult:
    """
    Compare two symbol library files and generate pin mapping.

    Args:
        source_path: Path to source .kicad_sym file
        target_path: Path to target .kicad_sym file

    Returns:
        MappingResult with pin comparisons and suggestions
    """
    source_name, source_pins = load_symbol_from_file(source_path)
    target_name, target_pins = load_symbol_from_file(target_path)

    mappings, unmatched_target = match_pins(source_pins, target_pins)

    return MappingResult(
        source_name=source_name,
        target_name=target_name,
        source_pins=source_pins,
        target_pins=target_pins,
        mappings=mappings,
        unmatched_target=unmatched_target,
    )


def compare_schematic_symbols(
    sch_path: str | Path, source_lib_id: str, target_lib_id: str
) -> MappingResult:
    """
    Compare two symbols from a schematic's embedded lib_symbols.

    Args:
        sch_path: Path to .kicad_sch file
        source_lib_id: Library ID of source symbol
        target_lib_id: Library ID of target symbol

    Returns:
        MappingResult with pin comparisons and suggestions
    """
    source_name, source_pins = load_symbol_from_schematic(sch_path, source_lib_id)
    target_name, target_pins = load_symbol_from_schematic(sch_path, target_lib_id)

    mappings, unmatched_target = match_pins(source_pins, target_pins)

    return MappingResult(
        source_name=source_name,
        target_name=target_name,
        source_pins=source_pins,
        target_pins=target_pins,
        mappings=mappings,
        unmatched_target=unmatched_target,
    )
