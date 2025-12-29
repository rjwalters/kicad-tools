"""
PCB design tools for KiCad.

This module provides tools for *creating and editing* PCB content:
- PCB editing with design rule enforcement
- Footprint pad position lookup
- PCB block generation (common layouts like LDO, oscillator, etc.)
- Manufacturer design rule helpers (Seeed Fusion, etc.)

For *reading and parsing* existing PCB files, use ``kicad_tools.schema.pcb``.
"""

from .editor import (
    AudioLayoutRules,
    PCBEditor,
    Point,
    SeeedFusion4Layer,
    Track,
    Via,
    Zone,
)
from .footprints import (
    FootprintLibrary,
    PadInfo,
    get_footprint_pads,
    get_library,
)

__all__ = [
    # Editor
    "Point",
    "Track",
    "Via",
    "Zone",
    "PCBEditor",
    "SeeedFusion4Layer",
    "AudioLayoutRules",
    # Footprints
    "FootprintLibrary",
    "PadInfo",
    "get_library",
    "get_footprint_pads",
]
