"""
FlashPCB manufacturer profile.

Design rules and capabilities for FlashPCB PCB fabrication and assembly.
FlashPCB is a USA-based manufacturer offering PCB fabrication and PCBA services.
Source: https://www.flashpcb.com/capabilities
"""

from .base import (
    AssemblyCapabilities,
    ManufacturerProfile,
    load_design_rules_from_yaml,
)

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("flashpcb")

# Assembly capabilities for FlashPCB instant quote tier
# Note: No parts library like LCSC - components must be sourced separately
_ASSEMBLY = AssemblyCapabilities(
    min_component_pitch_mm=0.508,  # 20 mil pad pitch
    min_bga_pitch_mm=0.5,  # BGA available via email quote only
    max_component_height_mm=25.0,
    supported_packages=[
        "0201",
        "0402",
        "0603",
        "0805",
        "1206",
        "1210",
        "SOT-23",
        "SOT-223",
        "SOT-363",
        "SOIC-8",
        "SOIC-14",
        "SOIC-16",
        "TSSOP-14",
        "TSSOP-16",
        "TSSOP-20",
        "TSSOP-28",
        "QFN",
        "QFP",
        "LQFP",
    ],
    supports_double_sided=True,
    supports_bga=False,  # BGA requires email quote, not instant
    supports_fine_pitch=False,  # <0.5mm pitch requires email quote
)

# Complete FlashPCB Profile
FLASHPCB_PROFILE = ManufacturerProfile(
    id="flashpcb",
    name="FlashPCB",
    website="https://www.flashpcb.com",
    design_rules=_DESIGN_RULES,
    assembly=_ASSEMBLY,
    parts_library=None,  # No integrated parts library
    lead_times={
        "pcb_expedited": 3,  # 3 day turnaround
        "pcb_standard": 5,  # 5 day turnaround
        "pcb_economy": 10,  # 10 day turnaround
        "pcba_standard": 10,  # Estimate for assembly
    },
    bom_format="generic",
    supported_layers=[2, 4],  # Instant quote tier only
    pricing_model="per_pcb",
)


def get_profile() -> ManufacturerProfile:
    """Get the FlashPCB manufacturer profile."""
    return FLASHPCB_PROFILE
