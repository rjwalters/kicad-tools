"""
Seeed Fusion manufacturer profile.

Design rules and capabilities for Seeed Studio Fusion PCB fabrication and assembly.
Source: https://www.seeedstudio.com/fusion_pcb.html
        https://support.seeedstudio.com/knowledgebase/articles/447362-fusion-pcb-specification
"""

from .base import (
    AssemblyCapabilities,
    ManufacturerProfile,
    PartsLibrary,
    load_design_rules_from_yaml,
)

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("seeed")

# Seeed Fusion Assembly Capabilities
SEEED_ASSEMBLY = AssemblyCapabilities(
    min_component_pitch_mm=0.4,
    min_bga_pitch_mm=0.5,
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
        "TSSOP-8",
        "TSSOP-14",
        "TSSOP-16",
        "TSSOP-20",
        "TSSOP-24",
        "TSSOP-28",
        "SSOP-8",
        "SSOP-16",
        "SSOP-20",
        "SSOP-24",
        "SSOP-28",
        "QFN-16",
        "QFN-20",
        "QFN-24",
        "QFN-32",
        "QFN-48",
        "LQFP-32",
        "LQFP-48",
        "LQFP-64",
        "LQFP-100",
        "TQFP-32",
        "TQFP-44",
        "TQFP-48",
        "TQFP-64",
        "BGA",
        "TO-252",
        "TO-263",
    ],
    supports_double_sided=True,
    supports_bga=True,
    supports_fine_pitch=True,
)

# Seeed Open Parts Library (OPL)
SEEED_OPL = PartsLibrary(
    name="Seeed OPL",
    search_url_template="https://www.seeedstudio.com/catalogsearch/result/?q={part_number}",
    catalog_url="https://www.seeedstudio.com/opl.html",
    tiers={
        "opl": {
            "description": "Seeed Open Parts Library - locally stocked",
            "lead_time_days": 7,
            "setup_fee_usd": 0,
        },
        "shenzhen_opl": {
            "description": "Shenzhen OPL - sourced from local distributor",
            "lead_time_days": 10,
            "setup_fee_usd": 0,
        },
        "external": {
            "description": "External sourcing - customer supplied or global",
            "lead_time_days": 20,
            "setup_fee_usd": 0,
        },
    },
)

# Complete Seeed Fusion Profile
SEEED_PROFILE = ManufacturerProfile(
    id="seeed",
    name="Seeed Fusion",
    website="https://www.seeedstudio.com/fusion.html",
    design_rules=_DESIGN_RULES,
    assembly=SEEED_ASSEMBLY,
    parts_library=SEEED_OPL,
    lead_times={
        "pcb_standard": 6,
        "pcb_expedited": 3,
        "pcba_opl": 7,
        "pcba_shenzhen": 10,
        "pcba_external": 20,
    },
    bom_format="seeed",
    supported_layers=[1, 2, 4, 6, 8],
    pricing_model="per_pcb",
)


def get_profile() -> ManufacturerProfile:
    """Get the Seeed Fusion manufacturer profile."""
    return SEEED_PROFILE
