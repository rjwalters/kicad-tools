"""
PCB design tools for KiCad.

This module provides tools for *creating and editing* PCB content:
- PCB editing with design rule enforcement
- Footprint pad position lookup
- PCB block generation (common layouts like LDO, oscillator, etc.)
- Manufacturer design rule helpers (Seeed Fusion, etc.)

For *reading and parsing* existing PCB files, use ``kicad_tools.schema.pcb``.

Block-based layout is available via the ``blocks`` subpackage:
    from kicad_tools.pcb.blocks import MCUBlock, LDOBlock, PCBLayout
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
from .exporter import KiCadPCBExporter, update_pcb_placements
from .footprints import (
    Footprint,
    FootprintLibrary,
    PadInfo,
    get_footprint_pads,
    get_library,
)
from .layout import PCBLayout

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
    "Footprint",
    "FootprintLibrary",
    "PadInfo",
    "get_library",
    "get_footprint_pads",
    # Layout
    "PCBLayout",
    # Exporter
    "KiCadPCBExporter",
    "update_pcb_placements",
]
