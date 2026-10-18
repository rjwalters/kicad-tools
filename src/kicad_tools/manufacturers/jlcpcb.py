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
    load_rotation_corrections,
)

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("jlcpcb")

# Load rotation corrections for JLCPCB pick-and-place files
_ROTATION_CORRECTIONS = load_rotation_corrections("jlcpcb")

# JLCPCB Assembly Capabilities -- two PCBA service tiers (Issue #7204).
#
# Source: "PCB Assembly Technical Capabilities" table,
#   https://jlcpcb.com/capabilities/pcb-assembly-capabilities
#   (fetched 2026-10-18; this page changes -- re-check before editing).
#
#   Feature                  Economic PCBA            Standard PCBA
#   Assembly types           single-sided placement   single & double sided
#   Single PCB size          10x10 - 470x500 mm       70x70 - 460x500 mm
#   Edge rails / fiducials   not necessary            necessary
#   Minimum package          0402                     01005
#   Minimum IC pin spacing   0.4 mm                   0.35 mm
#   Minimum BGA spacing      0.5 mm (c-c)             0.3 mm (c-c)
#   Build time               1 - 3 days               >= 4 days
#
# The same page lists *both* tiers as "(SMT/Thru-hole)"; kct therefore includes
# THT parts in the JLCPCB CPL/BOM (operator decision, issue #7239).
_STANDARD_PACKAGES = [
    "01005",
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
]

# Economic's minimum package is 0402, so 01005/0201 are Standard-only.
_ECONOMIC_PACKAGES = [p for p in _STANDARD_PACKAGES if p not in ("01005", "0201")]

JLCPCB_ASSEMBLY_ECONOMIC = AssemblyCapabilities(
    tier_name="Economic PCBA",
    min_component_pitch_mm=0.4,
    min_bga_pitch_mm=0.5,
    max_component_height_mm=25.0,
    supported_packages=_ECONOMIC_PACKAGES,
    supports_double_sided=False,
    supports_bga=True,
    supports_fine_pitch=True,
    min_package="0402",
    min_board_size_mm=(10.0, 10.0),
    requires_rails_and_fiducials=False,
    min_build_days=1,
)

JLCPCB_ASSEMBLY_STANDARD = AssemblyCapabilities(
    tier_name="Standard PCBA",
    min_component_pitch_mm=0.35,
    min_bga_pitch_mm=0.3,
    max_component_height_mm=25.0,
    supported_packages=list(_STANDARD_PACKAGES),
    supports_double_sided=True,
    supports_bga=True,
    supports_fine_pitch=True,
    min_package="01005",
    min_board_size_mm=(70.0, 70.0),
    requires_rails_and_fiducials=True,
    min_build_days=4,
)

# Tiers ordered cheapest first.
JLCPCB_ASSEMBLY_TIERS = [JLCPCB_ASSEMBLY_ECONOMIC, JLCPCB_ASSEMBLY_STANDARD]

# Capability superset (Standard).  Kept under the historical name because
# ``jlcpcb_tier1`` and ``profile.assembly`` consumers use it.
JLCPCB_ASSEMBLY = JLCPCB_ASSEMBLY_STANDARD

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
    assembly_tiers=list(JLCPCB_ASSEMBLY_TIERS),
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
    rotation_corrections=_ROTATION_CORRECTIONS,
    pnp_format_id="jlcpcb",
    gerber_preset_id="jlcpcb",
)


def get_profile() -> ManufacturerProfile:
    """Get the JLCPCB manufacturer profile."""
    return JLCPCB_PROFILE
