"""
PCBWay manufacturer profile.

Design rules and capabilities for PCBWay PCB fabrication and assembly.
Source: https://www.pcbway.com/capabilities.html
"""

from .base import (
    AssemblyCapabilities,
    ManufacturerProfile,
    PartsLibrary,
    load_design_rules_from_yaml,
)

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("pcbway")

# PCBWay Assembly Capabilities
PCBWAY_ASSEMBLY = AssemblyCapabilities(
    min_component_pitch_mm=0.35,
    min_bga_pitch_mm=0.4,
    max_component_height_mm=30.0,
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
        "QFN-64",
        "LQFP-32",
        "LQFP-48",
        "LQFP-64",
        "LQFP-100",
        "LQFP-144",
        "LQFP-176",
        "TQFP-32",
        "TQFP-44",
        "TQFP-48",
        "TQFP-64",
        "TQFP-100",
        "TQFP-144",
        "BGA",
        "WLCSP",
        "CSP",
        "TO-252",
        "TO-263",
        "TO-220",
    ],
    supports_double_sided=True,
    supports_bga=True,
    supports_fine_pitch=True,
)

# PCBWay Parts - Global sourcing (no fixed library like LCSC)
PCBWAY_PARTS = PartsLibrary(
    name="Global Sourcing",
    search_url_template="https://www.digikey.com/en/products/result?keywords={part_number}",
    catalog_url=None,  # No fixed catalog
    tiers={
        "turnkey": {
            "description": "PCBWay sources parts (DigiKey, Mouser, LCSC, etc.)",
            "lead_time_days": 10,
            "setup_fee_usd": 0,
        },
        "consignment": {
            "description": "Customer supplies parts",
            "lead_time_days": 5,
            "setup_fee_usd": 0,
        },
    },
)

# Complete PCBWay Profile
PCBWAY_PROFILE = ManufacturerProfile(
    id="pcbway",
    name="PCBWay",
    website="https://www.pcbway.com",
    design_rules=_DESIGN_RULES,
    assembly=PCBWAY_ASSEMBLY,
    parts_library=PCBWAY_PARTS,
    lead_times={
        "pcb_standard": 5,
        "pcb_expedited": 2,
        "pcba_turnkey": 10,
        "pcba_consignment": 5,
    },
    bom_format="generic",
    supported_layers=[1, 2, 4, 6, 8, 10, 12, 14],
    pricing_model="per_pcb",
)


def get_profile() -> ManufacturerProfile:
    """Get the PCBWay manufacturer profile."""
    return PCBWAY_PROFILE
