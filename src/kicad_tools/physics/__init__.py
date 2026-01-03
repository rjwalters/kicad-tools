"""Physics-based calculations for PCB signal integrity.

This module provides analytical electromagnetic calculations for
transmission line impedance, crosstalk estimation, and timing analysis.

Stackup Analysis:
    >>> from kicad_tools.physics import Stackup
    >>> from kicad_tools.schema.pcb import PCB
    >>>
    >>> # Parse stackup from KiCad board
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> stackup = Stackup.from_pcb(pcb)
    >>>
    >>> # Or use manufacturer preset
    >>> stackup = Stackup.jlcpcb_4layer()
    >>>
    >>> # Query layer properties
    >>> h = stackup.get_dielectric_height("F.Cu")
    >>> er = stackup.get_dielectric_constant("F.Cu")
    >>> print(f"Height to reference: {h}mm, epsilon_r: {er}")

Material Properties:
    >>> from kicad_tools.physics import FR4_STANDARD, ROGERS_4350B
    >>>
    >>> print(f"FR4 epsilon_r: {FR4_STANDARD.epsilon_r}")
    >>> print(f"Rogers loss tangent: {ROGERS_4350B.loss_tangent}")

Note:
    This module focuses on analytical calculations that are fast enough
    for agent iteration (microseconds per calculation). For full-wave
    electromagnetic simulation, use dedicated tools like openEMS or HFSS.
"""

from .constants import (
    COPPER_1OZ,
    COPPER_2OZ,
    COPPER_CONDUCTIVITY,
    # Copper weights
    COPPER_HALF_OZ,
    FR4_HIGH_TG,
    FR4_STANDARD,
    ISOLA_370HR,
    ROGERS_4003C,
    ROGERS_4350B,
    # Physical constants
    SPEED_OF_LIGHT,
    CopperWeight,
    # Dielectric materials
    DielectricMaterial,
    copper_thickness_from_oz,
    # Lookup functions
    get_material,
    get_material_or_default,
)
from .stackup import (
    LayerType,
    Stackup,
    StackupLayer,
)

__all__ = [
    # Constants
    "SPEED_OF_LIGHT",
    "COPPER_CONDUCTIVITY",
    # Copper
    "CopperWeight",
    "COPPER_HALF_OZ",
    "COPPER_1OZ",
    "COPPER_2OZ",
    "copper_thickness_from_oz",
    # Materials
    "DielectricMaterial",
    "FR4_STANDARD",
    "FR4_HIGH_TG",
    "ROGERS_4350B",
    "ROGERS_4003C",
    "ISOLA_370HR",
    "get_material",
    "get_material_or_default",
    # Stackup
    "LayerType",
    "StackupLayer",
    "Stackup",
]
