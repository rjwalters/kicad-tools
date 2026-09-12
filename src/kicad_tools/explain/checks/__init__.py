"""PCB design mistake checks.

This package contains individual check implementations for detecting
common PCB design mistakes.
"""

from .acid_trap import AcidTrapCheck
from .bom_health import BomFieldHealthCheck
from .bypass import BypassCapDistanceCheck
from .connectivity import LedSeriesResistorCheck, PullUpResistorCheck
from .crystal import CrystalNoiseProximityCheck, CrystalTraceLengthCheck
from .decoupling import MissingDecouplingCapCheck
from .differential import DifferentialPairSkewCheck
from .power import PowerTraceWidthCheck
from .thermal import ThermalPadConnectionCheck
from .tombstoning import TombstoningRiskCheck
from .via import ViaInPadCheck

__all__ = [
    "BypassCapDistanceCheck",
    "CrystalTraceLengthCheck",
    "CrystalNoiseProximityCheck",
    "DifferentialPairSkewCheck",
    "PowerTraceWidthCheck",
    "ThermalPadConnectionCheck",
    "ViaInPadCheck",
    "AcidTrapCheck",
    "TombstoningRiskCheck",
    "MissingDecouplingCapCheck",
    "PullUpResistorCheck",
    "LedSeriesResistorCheck",
    "BomFieldHealthCheck",
]
