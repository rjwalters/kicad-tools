"""Physics-based calculations for PCB signal integrity.

This module provides analytical electromagnetic calculations for
transmission line impedance, crosstalk estimation, and timing analysis.

Transmission Line Impedance:
    >>> from kicad_tools.physics import Stackup, TransmissionLine
    >>>
    >>> # Use manufacturer preset stackup
    >>> stackup = Stackup.jlcpcb_4layer()
    >>> tl = TransmissionLine(stackup)
    >>>
    >>> # Calculate microstrip impedance on top layer
    >>> result = tl.microstrip(width_mm=0.2, layer="F.Cu")
    >>> print(f"Z0 = {result.z0:.1f}Ω, εeff = {result.epsilon_eff:.2f}")
    >>>
    >>> # Calculate trace width for target impedance
    >>> width = tl.width_for_impedance(z0_target=50, layer="F.Cu")
    >>> print(f"50Ω requires {width:.3f}mm trace width")
    >>>
    >>> # Stripline on inner layer
    >>> result = tl.stripline(width_mm=0.15, layer="In1.Cu")
    >>> print(f"Stripline Z0 = {result.z0:.1f}Ω")

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
    COPPER_HALF_OZ,
    FR4_HIGH_TG,
    FR4_STANDARD,
    ISOLA_370HR,
    ROGERS_4003C,
    ROGERS_4350B,
    SPEED_OF_LIGHT,
    CopperWeight,
    DielectricMaterial,
    copper_thickness_from_oz,
    get_material,
    get_material_or_default,
)
from .coupled_lines import (
    CoupledLines,
    DifferentialPairResult,
)
from .crosstalk import (
    CrosstalkAnalyzer,
    CrosstalkResult,
)
from .stackup import (
    LayerType,
    Stackup,
    StackupLayer,
)
from .timing import (
    DifferentialPairSkew,
    PropagationResult,
    TimingAnalyzer,
    TimingBudget,
)
from .transmission_line import (
    ImpedanceResult,
    TransmissionLine,
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
    # Transmission Line
    "ImpedanceResult",
    "TransmissionLine",
    # Coupled Lines
    "CoupledLines",
    "DifferentialPairResult",
    # Crosstalk
    "CrosstalkAnalyzer",
    "CrosstalkResult",
    # Timing
    "TimingAnalyzer",
    "PropagationResult",
    "TimingBudget",
    "DifferentialPairSkew",
]
