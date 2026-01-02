"""
OSHPark manufacturer profile.

Design rules and capabilities for OSHPark PCB fabrication.
Note: OSHPark is PCB-only, no assembly services.
Source: https://docs.oshpark.com/design-tools/
"""

from .base import (
    ManufacturerProfile,
    load_design_rules_from_yaml,
)

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("oshpark")

# Complete OSHPark Profile
# Note: No assembly, no parts library
OSHPARK_PROFILE = ManufacturerProfile(
    id="oshpark",
    name="OSHPark",
    website="https://oshpark.com",
    design_rules=_DESIGN_RULES,
    assembly=None,  # PCB only - no assembly
    parts_library=None,  # No parts library
    lead_times={
        "pcb_standard": 12,  # ~12 calendar days
        "pcb_swift": 5,  # ~5 calendar days
    },
    bom_format="generic",
    supported_layers=[2, 4],
    pricing_model="per_sqin",  # $5/sq.in for 2-layer, $10/sq.in for 4-layer
)


def get_profile() -> ManufacturerProfile:
    """Get the OSHPark manufacturer profile."""
    return OSHPARK_PROFILE
