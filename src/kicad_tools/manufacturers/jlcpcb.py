"""
JLCPCB manufacturer profile.

Design rules and capabilities for JLCPCB PCB fabrication and assembly.
Source: https://jlcpcb.com/capabilities/pcb-capabilities
        https://github.com/ayberkozgur/jlcpcb-design-rules-stackups
"""

from .base import (
    AssemblyCapabilities,
    DesignRules,
    ManufacturerProfile,
    PartsLibrary,
)

# JLCPCB Design Rules by layer count and copper weight
# Source: Official JLCPCB capabilities + community-verified rules

JLCPCB_2LAYER_1OZ = DesignRules(
    min_trace_width_mm=0.127,  # 5 mil
    min_clearance_mm=0.127,  # 5 mil
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.15,
    min_hole_diameter_mm=0.3,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.3,
    min_hole_to_edge_mm=0.5,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.0,  # No inner layers
)

JLCPCB_2LAYER_2OZ = DesignRules(
    min_trace_width_mm=0.2032,  # 8 mil (wider for 2oz)
    min_clearance_mm=0.2032,  # 8 mil
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.15,
    min_hole_diameter_mm=0.3,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.3,
    min_hole_to_edge_mm=0.5,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=2.0,
    inner_copper_oz=0.0,
)

JLCPCB_4LAYER_1OZ = DesignRules(
    min_trace_width_mm=0.1016,  # 4 mil
    min_clearance_mm=0.1016,  # 4 mil
    min_via_drill_mm=0.2,
    min_via_diameter_mm=0.45,
    min_annular_ring_mm=0.125,
    min_hole_diameter_mm=0.2,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.3,
    min_hole_to_edge_mm=0.4,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.5,
)

JLCPCB_4LAYER_2OZ = DesignRules(
    min_trace_width_mm=0.2032,  # 8 mil outer, 4 mil inner
    min_clearance_mm=0.1016,  # 4 mil
    min_via_drill_mm=0.2,
    min_via_diameter_mm=0.45,
    min_annular_ring_mm=0.125,
    min_hole_diameter_mm=0.2,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.3,
    min_hole_to_edge_mm=0.4,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=2.0,
    inner_copper_oz=0.5,
)

JLCPCB_6LAYER_1OZ = DesignRules(
    min_trace_width_mm=0.0889,  # 3.5 mil
    min_clearance_mm=0.0889,  # 3.5 mil
    min_via_drill_mm=0.2,
    min_via_diameter_mm=0.45,
    min_annular_ring_mm=0.125,
    min_hole_diameter_mm=0.2,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.3,
    min_hole_to_edge_mm=0.4,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.5,
)

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
    design_rules={
        "2layer_1oz": JLCPCB_2LAYER_1OZ,
        "2layer_2oz": JLCPCB_2LAYER_2OZ,
        "4layer_1oz": JLCPCB_4LAYER_1OZ,
        "4layer_2oz": JLCPCB_4LAYER_2OZ,
        "6layer_1oz": JLCPCB_6LAYER_1OZ,
    },
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
