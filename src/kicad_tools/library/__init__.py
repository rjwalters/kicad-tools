"""
Library module for KiCad footprint and symbol generation.

This module provides tools for programmatically creating KiCad footprints
and symbols, including parametric generators for common package types.

Usage:
    from kicad_tools.library import Footprint, create_soic, create_chip

    # Create a SOIC-8 footprint
    fp = create_soic(pins=8)
    fp.save("MyFootprints.pretty/SOIC-8_Custom.kicad_mod")

    # Create a chip resistor footprint
    fp = create_chip("0603", prefix="R")
    fp.save("MyFootprints.pretty/R_0603_Custom.kicad_mod")
"""

from .footprint import Footprint, GraphicArc, GraphicCircle, GraphicLine, GraphicRect, Pad
from .generators import (
    create_bga,
    create_bga_standard,
    create_chip,
    create_dfn,
    create_dfn_standard,
    create_dip,
    create_pin_header,
    create_qfn,
    create_qfp,
    create_soic,
    create_sot,
)

__all__ = [
    # Core classes
    "Footprint",
    "Pad",
    "GraphicLine",
    "GraphicRect",
    "GraphicCircle",
    "GraphicArc",
    # Generators
    "create_soic",
    "create_qfp",
    "create_qfn",
    "create_sot",
    "create_chip",
    "create_dip",
    "create_pin_header",
    "create_bga",
    "create_bga_standard",
    "create_dfn",
    "create_dfn_standard",
]
