"""DRC rule implementations.

This subpackage contains the base rule class and individual rule
implementations for the pure Python DRC checker.
"""

from .base import DRCRule
from .clearance import ClearanceRule
from .dimensions import DimensionRules
from .edge import EdgeClearanceRule
from .impedance import ImpedanceRule, NetImpedanceSpec
from .silkscreen import (
    check_all_silkscreen,
    check_silkscreen_line_width,
    check_silkscreen_over_pads,
    check_silkscreen_text_height,
)

__all__ = [
    "DRCRule",
    "ClearanceRule",
    "DimensionRules",
    "EdgeClearanceRule",
    "ImpedanceRule",
    "NetImpedanceSpec",
    "check_all_silkscreen",
    "check_silkscreen_line_width",
    "check_silkscreen_over_pads",
    "check_silkscreen_text_height",
]
