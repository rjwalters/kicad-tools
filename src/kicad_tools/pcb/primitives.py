"""
PCB primitive elements for block layout.

This module provides primitive PCB elements used within blocks:
- Pad: A pad/connection point
- Port: External connection point on a block's boundary
- TraceSegment: A segment of copper trace
- Via: A via connecting layers
"""

from dataclasses import dataclass

from .geometry import Layer, Point


@dataclass
class Pad:
    """A pad/connection point."""

    name: str
    position: Point
    layer: Layer = Layer.F_CU
    net: str | None = None

    # Pad geometry (for actual pads, not just ports)
    shape: str = "circle"  # circle, rect, oval
    size: tuple[float, float] = (0.8, 0.8)  # mm
    drill: float | None = None  # For through-hole


@dataclass
class Port:
    """
    External connection point on a block's boundary.

    A port is a virtual pad that represents where external traces
    should connect to this block. The actual physical connection
    might be to a component pad inside the block.
    """

    name: str
    position: Point  # Position relative to block origin
    layer: Layer = Layer.F_CU
    direction: str = "inout"  # in, out, inout, power
    net: str | None = None  # Net name when connected

    # What this port connects to inside the block
    internal_pad: str | None = None  # e.g., "U1.VDD" or "C12.1"


@dataclass
class TraceSegment:
    """A segment of copper trace."""

    start: Point
    end: Point
    width: float = 0.25  # mm
    layer: Layer = Layer.F_CU
    net: str | None = None


@dataclass
class Via:
    """A via connecting layers."""

    position: Point
    drill: float = 0.3  # mm
    size: float = 0.6  # mm (annular ring outer diameter)
    layers: tuple[Layer, Layer] = (Layer.F_CU, Layer.B_CU)
    net: str | None = None


__all__ = ["Pad", "Port", "TraceSegment", "Via"]
