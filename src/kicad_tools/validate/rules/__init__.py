"""DRC rule implementations.

This subpackage contains the base rule class and individual rule
implementations for the pure Python DRC checker.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import DRC_TOLERANCE, DRCRule
    from .clearance import ClearanceRule, SegmentZoneClearanceRule, ViaZoneClearanceRule
    from .connector_access import ConnectorEdgeAccessRule
    from .dangling_copper import DanglingCopperRule
    from .diffpair_clearance_intra import DiffPairClearanceIntraRule
    from .diffpair_length_skew import DiffPairLengthSkewRule
    from .diffpair_routing_continuity import DiffPairRoutingContinuityRule
    from .dimensions import DimensionRules
    from .edge import EdgeClearanceRule
    from .impedance import ImpedanceRule, NetImpedanceSpec
    from .match_group_length_skew import MatchGroupLengthSkewRule
    from .schematic_fields import check_schematic_fields
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
    from .zone_fill import IsolatedCopperRule, ZoneFillRule

# Resolve only the requested public export. DRCChecker still imports and runs
# its full set of rules when a caller requests a real DRC pass.
_EXPORT_MODULES: dict[str, str] = {
    "DRC_TOLERANCE": ".base",
    "DRCRule": ".base",
    "ClearanceRule": ".clearance",
    "SegmentZoneClearanceRule": ".clearance",
    "ViaZoneClearanceRule": ".clearance",
    "ConnectorEdgeAccessRule": ".connector_access",
    "DanglingCopperRule": ".dangling_copper",
    "DiffPairClearanceIntraRule": ".diffpair_clearance_intra",
    "DiffPairLengthSkewRule": ".diffpair_length_skew",
    "DiffPairRoutingContinuityRule": ".diffpair_routing_continuity",
    "DimensionRules": ".dimensions",
    "EdgeClearanceRule": ".edge",
    "ImpedanceRule": ".impedance",
    "NetImpedanceSpec": ".impedance",
    "MatchGroupLengthSkewRule": ".match_group_length_skew",
    "check_schematic_fields": ".schematic_fields",
    "check_all_silkscreen": ".silkscreen",
    "check_silk_edge_clearance": ".silkscreen",
    "check_silk_over_copper": ".silkscreen",
    "check_silkscreen_line_width": ".silkscreen",
    "check_silkscreen_over_pads": ".silkscreen",
    "check_silkscreen_text_height": ".silkscreen",
    "SinglePadNetRule": ".single_pad_net",
    "SolderMaskPadRules": ".solder_mask",
    "ViaInPadRule": ".via_in_pad",
    "IsolatedCopperRule": ".zone_fill",
    "ZoneFillRule": ".zone_fill",
}

__all__ = [
    "DRC_TOLERANCE",
    "DRCRule",
    "ClearanceRule",
    "SegmentZoneClearanceRule",
    "ViaZoneClearanceRule",
    "ConnectorEdgeAccessRule",
    "DanglingCopperRule",
    "DiffPairClearanceIntraRule",
    "DiffPairLengthSkewRule",
    "DiffPairRoutingContinuityRule",
    "DimensionRules",
    "EdgeClearanceRule",
    "ImpedanceRule",
    "IsolatedCopperRule",
    "MatchGroupLengthSkewRule",
    "NetImpedanceSpec",
    "SinglePadNetRule",
    "SolderMaskPadRules",
    "ViaInPadRule",
    "check_all_silkscreen",
    "check_schematic_fields",
    "check_silk_edge_clearance",
    "check_silk_over_copper",
    "check_silkscreen_line_width",
    "check_silkscreen_over_pads",
    "check_silkscreen_text_height",
    "ZoneFillRule",
]


def __getattr__(name: str) -> Any:
    """Load a public export on first access, preserving its original identity."""
    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
