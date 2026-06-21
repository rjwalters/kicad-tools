"""DRC rule implementations.

This subpackage contains the base rule class and individual rule
implementations for the pure Python DRC checker.
"""

from .base import DRC_TOLERANCE, DRCRule
from .clearance import ClearanceRule, SegmentZoneClearanceRule, ViaZoneClearanceRule
from .diffpair_clearance_intra import DiffPairClearanceIntraRule
from .diffpair_length_skew import DiffPairLengthSkewRule
from .diffpair_routing_continuity import DiffPairRoutingContinuityRule
from .dimensions import DimensionRules
from .edge import EdgeClearanceRule
from .impedance import ImpedanceRule, NetImpedanceSpec
from .match_group_length_skew import MatchGroupLengthSkewRule
from .silkscreen import (
    check_all_silkscreen,
    check_silk_edge_clearance,
    check_silk_over_copper,
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
    "SegmentZoneClearanceRule",
    "ViaZoneClearanceRule",
    "DiffPairClearanceIntraRule",
    "DiffPairLengthSkewRule",
    "DiffPairRoutingContinuityRule",
    "DimensionRules",
    "EdgeClearanceRule",
    "ImpedanceRule",
    "MatchGroupLengthSkewRule",
    "NetImpedanceSpec",
    "SinglePadNetRule",
    "SolderMaskPadRules",
    "ViaInPadRule",
    "check_all_silkscreen",
    "check_silk_edge_clearance",
    "check_silk_over_copper",
    "check_silkscreen_line_width",
    "check_silkscreen_over_pads",
    "check_silkscreen_text_height",
    "ZoneFillRule",
]
