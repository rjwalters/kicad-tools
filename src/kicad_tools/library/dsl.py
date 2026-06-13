"""
Compressed footprint DSL for LLM-friendly generation.

Parses compact footprint specification strings and returns Footprint objects
by delegating to the existing parametric generators.

Grammar:
    {package}[pin_count][_p{pitch}][_w{width}][_h{height}][_thermalpad]

Examples:
    - Passives: "0402", "0603", "1206"
    - SOT: "sot23", "sot23-5", "sot223"
    - SOIC: "soic8", "soic14", "soic8_p1.27mm"
    - QFP: "qfp48_p0.5mm", "qfp100_p0.5mm_w14mm"
    - QFN: "qfn16_w3mm", "qfn24_p0.5mm"
    - BGA: "bga100_p0.8mm", "bga256_p0.8mm"
    - DFN: "dfn8_w3mm_p0.5mm"
    - DIP: "dip8", "dip16"

Usage:
    from kicad_tools.library.dsl import parse_footprint_dsl

    fp = parse_footprint_dsl("soic8")
    fp = parse_footprint_dsl("qfp48_p0.5mm")
    fp = parse_footprint_dsl("0402")
"""

from __future__ import annotations

import math
import re

from kicad_tools.library.footprint import Footprint
from kicad_tools.library.generators import (
    create_bga,
    create_chip,
    create_dfn,
    create_dip,
    create_qfn,
    create_qfp,
    create_soic,
    create_sot,
)
from kicad_tools.library.generators.standards import (
    CHIP_SIZES,
)

# Maps DSL SOT names to SOT_STANDARDS keys
_SOT_ALIASES: dict[str, str] = {
    "sot23": "SOT-23",
    "sot23-3": "SOT-23",
    "sot23-5": "SOT-23-5",
    "sot23-6": "SOT-23-6",
    "sot223": "SOT-223",
    "sot89": "SOT-89",
}

# Regex to extract optional modifiers like _p0.5mm, _w3mm, _h5mm, _thermalpad
_MODIFIER_PITCH = re.compile(r"_p([\d.]+)(?:mm)?", re.IGNORECASE)
_MODIFIER_WIDTH = re.compile(r"_w([\d.]+)(?:mm)?", re.IGNORECASE)
_MODIFIER_HEIGHT = re.compile(r"_h([\d.]+)(?:mm)?", re.IGNORECASE)
_MODIFIER_THERMALPAD = re.compile(r"_thermalpad", re.IGNORECASE)

# Package prefix pattern: letters followed by digits
_PACKAGE_PATTERN = re.compile(
    r"^([a-z]+)([\d]+(?:-[\d]+)?)(.*)$",
    re.IGNORECASE,
)

# Valid package prefixes recognized by the DSL
_VALID_PREFIXES = {"soic", "qfp", "lqfp", "tqfp", "qfn", "bga", "dfn", "dip", "sot"}


def _parse_modifiers(modifier_str: str) -> dict:
    """Extract pitch, width, height, and thermalpad from modifier string."""
    mods: dict = {}

    m = _MODIFIER_PITCH.search(modifier_str)
    if m:
        mods["pitch"] = float(m.group(1))

    m = _MODIFIER_WIDTH.search(modifier_str)
    if m:
        mods["width"] = float(m.group(1))

    m = _MODIFIER_HEIGHT.search(modifier_str)
    if m:
        mods["height"] = float(m.group(1))

    if _MODIFIER_THERMALPAD.search(modifier_str):
        mods["thermalpad"] = True

    return mods


def _is_passive_size(spec: str) -> bool:
    """Check if the spec is a bare passive size code (all digits, 4 chars)."""
    # Strip any modifiers first
    base = spec.split("_")[0]
    return base in CHIP_SIZES


def _build_chip(spec: str) -> Footprint:
    """Build a chip/passive footprint from DSL spec."""
    size = spec.split("_")[0]
    if size not in CHIP_SIZES:
        available = ", ".join(sorted(CHIP_SIZES.keys()))
        raise ValueError(f"Unknown chip size '{size}'. Valid sizes: {available}")
    return create_chip(size=size)


def _build_sot(spec_lower: str) -> Footprint:
    """Build a SOT footprint from DSL spec."""
    base = spec_lower.split("_")[0]
    variant_key = _SOT_ALIASES.get(base)
    if variant_key is None:
        available = ", ".join(sorted(_SOT_ALIASES.keys()))
        raise ValueError(f"Unknown SOT variant '{base}'. Valid DSL names: {available}")
    return create_sot(variant=variant_key)


def _build_soic(pins: int, mods: dict) -> Footprint:
    """Build a SOIC footprint from DSL spec."""
    kwargs: dict = {"pins": pins}
    if "pitch" in mods:
        kwargs["pitch"] = mods["pitch"]
    return create_soic(**kwargs)


def _build_qfp(pins: int, mods: dict) -> Footprint:
    """Build a QFP footprint from DSL spec."""
    kwargs: dict = {"pins": pins}
    if "pitch" in mods:
        kwargs["pitch"] = mods["pitch"]
    if "width" in mods:
        kwargs["body_size"] = mods["width"]
    return create_qfp(**kwargs)


def _build_qfn(pins: int, mods: dict) -> Footprint:
    """Build a QFN footprint from DSL spec."""
    kwargs: dict = {"pins": pins}
    if "pitch" in mods:
        kwargs["pitch"] = mods["pitch"]
    if "width" in mods:
        kwargs["body_size"] = mods["width"]
    return create_qfn(**kwargs)


def _build_bga(pins: int, mods: dict) -> Footprint:
    """Build a BGA footprint from DSL spec."""
    # BGA needs rows and cols - assume square grid
    side = int(math.isqrt(pins))
    if side * side != pins:
        raise ValueError(
            f"BGA pin count {pins} is not a perfect square. "
            f"BGA DSL requires a square grid (e.g., bga100 = 10x10)."
        )
    kwargs: dict = {"rows": side, "cols": side}
    if "pitch" in mods:
        kwargs["pitch"] = mods["pitch"]
    return create_bga(**kwargs)


def _build_dfn(pins: int, mods: dict) -> Footprint:
    """Build a DFN footprint from DSL spec."""
    kwargs: dict = {"pins": pins}
    if "pitch" in mods:
        kwargs["pitch"] = mods["pitch"]
    if "width" in mods:
        kwargs["body_width"] = mods["width"]
        kwargs["body_length"] = mods["width"]  # Default square if only width given
    if "height" in mods:
        kwargs["body_length"] = mods["height"]
    return create_dfn(**kwargs)


def _build_dip(pins: int, mods: dict) -> Footprint:
    """Build a DIP footprint from DSL spec."""
    kwargs: dict = {"pins": pins}
    if "pitch" in mods:
        kwargs["pitch"] = mods["pitch"]
    return create_dip(**kwargs)


def parse_footprint_dsl(spec: str) -> Footprint:
    """Parse a compact footprint DSL string and return a Footprint object.

    The DSL grammar is:
        {package}[pin_count][_p{pitch}][_w{width}][_h{height}][_thermalpad]

    Args:
        spec: A compact footprint specification string.

    Returns:
        A Footprint object ready for export.

    Raises:
        ValueError: If the spec cannot be parsed or contains invalid parameters.

    Examples:
        >>> fp = parse_footprint_dsl("0402")          # Chip passive
        >>> fp = parse_footprint_dsl("soic8")         # SOIC-8
        >>> fp = parse_footprint_dsl("qfp48_p0.5mm")  # LQFP-48, 0.5mm pitch
        >>> fp = parse_footprint_dsl("sot23-5")       # SOT-23-5
    """
    if not spec or not isinstance(spec, str):
        raise ValueError(
            "Footprint DSL spec must be a non-empty string. "
            "Examples: '0402', 'soic8', 'qfp48_p0.5mm'"
        )

    spec_stripped = spec.strip()
    spec_lower = spec_stripped.lower()

    # 1. Check for passive/chip sizes (all-digit base, e.g. "0402", "0603")
    if _is_passive_size(spec_lower):
        return _build_chip(spec_lower)

    # 2. Check for SOT variants (sot23, sot23-5, sot223, sot89)
    base_lower = spec_lower.split("_")[0]
    if base_lower in _SOT_ALIASES:
        return _build_sot(spec_lower)

    # 3. Parse structured package specs: prefix + pin_count + modifiers
    m = _PACKAGE_PATTERN.match(spec_lower)
    if not m:
        raise ValueError(
            f"Cannot parse footprint DSL spec '{spec}'. "
            f"Expected format: {{package}}{{pin_count}}[_p{{pitch}}][_w{{width}}]. "
            f"Valid prefixes: {', '.join(sorted(_VALID_PREFIXES))}. "
            f"Valid chip sizes: {', '.join(sorted(CHIP_SIZES.keys()))}. "
            f"Examples: 'soic8', 'qfp48_p0.5mm', '0402', 'sot23'"
        )

    prefix = m.group(1)
    pin_str = m.group(2)
    modifier_str = m.group(3)

    # Parse pin count (handles "23-5" style for SOT variants)
    if "-" in pin_str:
        # Already handled above for SOT, but catch any remaining
        raise ValueError(
            f"Cannot parse footprint DSL spec '{spec}'. "
            f"Hyphenated pin counts are only valid for SOT variants."
        )

    try:
        pins = int(pin_str)
    except ValueError:
        raise ValueError(
            f"Invalid pin count '{pin_str}' in spec '{spec}'. Pin count must be a positive integer."
        )

    if pins <= 0:
        raise ValueError(f"Pin count must be positive, got {pins} in spec '{spec}'.")

    mods = _parse_modifiers(modifier_str)

    # Normalize package prefix aliases
    prefix_normalized = prefix
    if prefix in ("lqfp", "tqfp"):
        prefix_normalized = "qfp"

    # Dispatch to the appropriate builder
    builders = {
        "soic": _build_soic,
        "qfp": _build_qfp,
        "qfn": _build_qfn,
        "bga": _build_bga,
        "dfn": _build_dfn,
        "dip": _build_dip,
    }

    builder = builders.get(prefix_normalized)
    if builder is None:
        raise ValueError(
            f"Unknown package prefix '{prefix}' in spec '{spec}'. "
            f"Valid prefixes: {', '.join(sorted(_VALID_PREFIXES))}. "
            f"Examples: 'soic8', 'qfp48_p0.5mm', 'bga100_p0.8mm'"
        )

    return builder(pins, mods)
