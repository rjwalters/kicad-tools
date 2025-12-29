"""
Programmatic footprint generation module (EXPERIMENTAL).

.. warning::
    This module is experimental. The ``Footprint.to_sexp()`` method
    is not yet implemented and will raise NotImplementedError.

Provides classes for generating KiCad footprints programmatically,
including common package types (0402, 0603, SOT-23, QFN, etc.).
"""

import warnings
from enum import Enum
from typing import List, Tuple

__all__ = [
    "PadType",
    "PadShape",
    "Layer",
    "Pad",
    "Footprint",
]

# Emit warning on import
warnings.warn(
    "kicad_tools.footprints is experimental. Footprint.to_sexp() is not yet implemented.",
    category=FutureWarning,
    stacklevel=2,
)


class PadType(Enum):
    """Pad mounting type."""

    SMD = "smd"
    THT = "thru_hole"
    NPTH = "np_thru_hole"
    CONNECT = "connect"


class PadShape(Enum):
    """Pad shape."""

    RECT = "rect"
    ROUNDRECT = "roundrect"
    CIRCLE = "circle"
    OVAL = "oval"
    TRAPEZOID = "trapezoid"


class Layer(Enum):
    """PCB layers."""

    F_CU = "F.Cu"
    B_CU = "B.Cu"
    F_PASTE = "F.Paste"
    B_PASTE = "B.Paste"
    F_MASK = "F.Mask"
    B_MASK = "B.Mask"
    F_SILKS = "F.SilkS"
    B_SILKS = "B.SilkS"
    F_CRTYD = "F.CrtYd"
    B_CRTYD = "B.CrtYd"
    EDGE_CUTS = "Edge.Cuts"


class Pad:
    """A copper pad on a footprint."""

    def __init__(
        self,
        number: str,
        pad_type: PadType,
        shape: PadShape,
        position: Tuple[float, float],
        size: Tuple[float, float],
        layers: List[Layer],
        drill: float = 0,
    ):
        self.number = number
        self.pad_type = pad_type
        self.shape = shape
        self.position = position
        self.size = size
        self.layers = layers
        self.drill = drill


class Footprint:
    """
    A KiCad footprint definition.

    Note: Full implementation with generators pending migration from
    hardware/chorus-test-revA/lib/footprint_lib.py
    """

    def __init__(self, name: str):
        self.name = name
        self.pads: List[Pad] = []
        self.silkscreen: List[str] = []
        self.courtyard: List[str] = []

    def add_pad(self, pad: Pad) -> None:
        """Add a pad to the footprint."""
        self.pads.append(pad)

    def to_sexp(self) -> str:
        """Export to KiCad S-expression format."""
        raise NotImplementedError(
            "Footprint.to_sexp() is not yet implemented. This is an experimental feature."
        )
