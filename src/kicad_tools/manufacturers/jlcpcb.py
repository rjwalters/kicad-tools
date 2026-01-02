"""
JLCPCB manufacturer profile.

Design rules and capabilities for JLCPCB PCB fabrication and assembly.
Source: https://jlcpcb.com/capabilities/pcb-capabilities
        https://github.com/ayberkozgur/jlcpcb-design-rules-stackups
"""

from .base import (
    AssemblyCapabilities,
    ManufacturerProfile,
    PartsLibrary,
    load_design_rules_from_yaml,
)

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("jlcpcb")

# JLCPCB Assembly Capabilities
JLCPCB_ASSEMBLY = AssemblyCapabilities(
    min_component_pitch_mm=0.35,
    min_bga_pitch_mm=0.4,
    max_component_height_mm=25.0,
    supported_packages=[
        "0201",
        "0402",
        "0603",
        "0805",
        "1206",
        "1210",
        "2010",
        "2512",
        "SOT-23",
        "SOT-223",
        "SOT-363",
        "SOT-89",
        "SOT-323",
        "SOIC-8",
        "SOIC-14",
        "SOIC-16",
        "SOP-8",
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
        "QFN-64",
        "LQFP-32",
        "LQFP-48",
        "LQFP-64",
        "LQFP-100",
        "LQFP-144",
        "TQFP-32",
        "TQFP-44",
        "TQFP-48",
        "TQFP-64",
        "TQFP-100",
        "BGA",
        "WLCSP",
        "TO-252",
        "TO-263",
        "DPAK",
        "D2PAK",
    ],
    supports_double_sided=True,
    supports_bga=True,
    supports_fine_pitch=True,
)

# LCSC Parts Library
LCSC_LIBRARY = PartsLibrary(
    name="LCSC",
    search_url_template="https://www.lcsc.com/search?q={part_number}",
    catalog_url="https://jlcpcb.com/parts",
    tiers={
        "basic": {
            "description": "In-stock at JLCPCB warehouse",
            "lead_time_days": 3,
            "setup_fee_usd": 0,
        },
        "extended": {
            "description": "Sourced from LCSC",
            "lead_time_days": 7,
            "setup_fee_usd": 3.0,  # Per unique part
        },
        "global": {
            "description": "Global sourcing (DigiKey, Mouser, etc.)",
            "lead_time_days": 14,
            "setup_fee_usd": 0,
        },
    },
)

# Complete JLCPCB Profile
JLCPCB_PROFILE = ManufacturerProfile(
    id="jlcpcb",
    name="JLCPCB",
    website="https://jlcpcb.com",
    design_rules=_DESIGN_RULES,
    assembly=JLCPCB_ASSEMBLY,
    parts_library=LCSC_LIBRARY,
    lead_times={
        "pcb_standard": 5,
        "pcb_expedited": 2,
        "pcba_basic": 7,
        "pcba_extended": 14,
        "pcba_global": 21,
    },
    bom_format="jlcpcb",
    supported_layers=[1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20],
    pricing_model="per_pcb",
)


def get_profile() -> ManufacturerProfile:
    """Get the JLCPCB manufacturer profile."""
    return JLCPCB_PROFILE
