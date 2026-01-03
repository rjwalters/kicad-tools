"""
PCB Blocks - Virtual Components for Hierarchical PCB Layout.

This module extends the circuit block concept to PCB layout, treating
groups of components as "virtual components" with:
- Internal component placement (relative positions)
- Internal routing (pre-routed critical traces)
- External ports (connection points on the block boundary)

This enables a "divide and conquer" approach to PCB layout:
1. Define blocks with internal placement + routing
2. Place blocks on PCB
3. Route inter-block connections (simpler problem)

Usage:
    from kicad_tools.pcb.blocks import PCBBlock, MCUBlock, LDOBlock

    # Create MCU block with bypass caps pre-placed and pre-routed
    mcu = MCUBlock(
        mcu_footprint="QFP-20_4x4mm",
        bypass_caps=["C12", "C13"],
    )

    # Place on PCB at position
    mcu.place(x=100, y=50, rotation=0)

    # Get port positions for inter-block routing
    vdd_port = mcu.port("VDD")
    pa0_port = mcu.port("PA0")
"""

# Re-export geometry types
from ..geometry import Layer, Point, Rectangle

# Re-export placement types
from ..placement import ComponentPlacement, get_footprint_pads

# Re-export primitive types
from ..primitives import Pad, Port, TraceSegment, Via

# Block classes
from .base import PCBBlock
from .led import LEDBlock
from .mcu import MCUBlock
from .oscillator import OscillatorBlock
from .power import LDOBlock


def __getattr__(name: str):
    """Lazy import for layout and exporter to avoid circular imports."""
    if name == "PCBLayout":
        from ..layout import PCBLayout

        return PCBLayout
    if name == "KiCadPCBExporter":
        from ..exporter import KiCadPCBExporter

        return KiCadPCBExporter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Geometry
    "Point",
    "Rectangle",
    "Layer",
    # Primitives
    "Pad",
    "Port",
    "TraceSegment",
    "Via",
    # Placement
    "ComponentPlacement",
    "get_footprint_pads",
    # Layout (lazy import)
    "PCBLayout",
    # Exporter (lazy import)
    "KiCadPCBExporter",
    # Base class
    "PCBBlock",
    # Block types
    "MCUBlock",
    "LDOBlock",
    "OscillatorBlock",
    "LEDBlock",
]
