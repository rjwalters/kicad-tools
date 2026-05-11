"""DRC rule implementations.

This subpackage contains the base rule class and individual rule
implementations for the pure Python DRC checker.
"""

from .base import DRC_TOLERANCE, DRCRule
from .clearance import ClearanceRule
from .diffpair_clearance_intra import DiffPairClearanceIntraRule
from .dimensions import DimensionRules
from .edge import EdgeClearanceRule
from .impedance import ImpedanceRule, NetImpedanceSpec
from .silkscreen import (
    check_all_silkscreen,
    check_silkscreen_line_width,
    check_silkscreen_over_pads,
    check_silkscreen_text_height,
)
from .single_pad_net import SinglePadNetRule
from .solder_mask import SolderMaskPadRules
from .via_in_pad import ViaInPadRule
from .zone_fill import ZoneFillRule

__all__ = [
    "DRC_TOLERANCE",
    "DRCRule",
    "ClearanceRule",
    "DiffPairClearanceIntraRule",
    "DimensionRules",
    "EdgeClearanceRule",
    "ImpedanceRule",
    "NetImpedanceSpec",
    "SinglePadNetRule",
    "SolderMaskPadRules",
    "ViaInPadRule",
    "check_all_silkscreen",
    "check_silkscreen_line_width",
    "check_silkscreen_over_pads",
    "check_silkscreen_text_height",
    "ZoneFillRule",
]
