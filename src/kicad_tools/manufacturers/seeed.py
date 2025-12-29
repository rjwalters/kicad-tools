"""
Seeed Fusion manufacturer profile.

Design rules and capabilities for Seeed Studio Fusion PCB fabrication and assembly.
Source: https://www.seeedstudio.com/fusion_pcb.html
        https://support.seeedstudio.com/knowledgebase/articles/447362-fusion-pcb-specification
"""

from .base import (
    AssemblyCapabilities,
    DesignRules,
    ManufacturerProfile,
    PartsLibrary,
)

# Seeed Fusion Design Rules (conservative values for reliable manufacturing)
# These are slightly more conservative than JLCPCB to ensure compatibility

SEEED_2LAYER_1OZ = DesignRules(
    min_trace_width_mm=0.1524,  # 6 mil
    min_clearance_mm=0.1524,  # 6 mil
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.15,
    min_hole_diameter_mm=0.3,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.5,
    min_hole_to_edge_mm=0.5,
    min_silkscreen_width_mm=0.15,  # 6 mil
    min_silkscreen_height_mm=0.8,  # 32 mil
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.0,
)

SEEED_2LAYER_2OZ = DesignRules(
    min_trace_width_mm=0.2,  # 8 mil for 2oz
    min_clearance_mm=0.2,  # 8 mil
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.15,
    min_hole_diameter_mm=0.3,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.5,
    min_hole_to_edge_mm=0.5,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=2.0,
    inner_copper_oz=0.0,
)

SEEED_4LAYER_1OZ = DesignRules(
    min_trace_width_mm=0.1524,  # 6 mil (conservative)
    min_clearance_mm=0.1524,  # 6 mil
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.15,
    min_hole_diameter_mm=0.3,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.5,
    min_hole_to_edge_mm=0.5,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.5,
)

SEEED_4LAYER_2OZ = DesignRules(
    min_trace_width_mm=0.2,  # 8 mil for 2oz outer
    min_clearance_mm=0.2,  # 8 mil
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.15,
    min_hole_diameter_mm=0.3,
    max_hole_diameter_mm=6.3,
    min_copper_to_edge_mm=0.5,
    min_hole_to_edge_mm=0.5,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_height_mm=0.8,
    min_solder_mask_dam_mm=0.1,
    board_thickness_mm=1.6,
    outer_copper_oz=2.0,
    inner_copper_oz=1.0,
)

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
    design_rules={
        "2layer_1oz": SEEED_2LAYER_1OZ,
        "2layer_2oz": SEEED_2LAYER_2OZ,
        "4layer_1oz": SEEED_4LAYER_1OZ,
        "4layer_2oz": SEEED_4LAYER_2OZ,
    },
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
