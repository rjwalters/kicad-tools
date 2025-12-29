"""
OSHPark manufacturer profile.

Design rules and capabilities for OSHPark PCB fabrication.
Note: OSHPark is PCB-only, no assembly services.
Source: https://docs.oshpark.com/design-tools/
"""

from .base import (
    DesignRules,
    ManufacturerProfile,
)

# OSHPark Design Rules
# Standard 2-layer service (purple boards)

OSHPARK_2LAYER_STANDARD = DesignRules(
    min_trace_width_mm=0.1524,  # 6 mil
    min_clearance_mm=0.1524,  # 6 mil
    min_via_drill_mm=0.254,  # 10 mil
    min_via_diameter_mm=0.508,  # 20 mil (10 mil annular)
    min_annular_ring_mm=0.127,  # 5 mil
    min_hole_diameter_mm=0.254,  # 10 mil
    max_hole_diameter_mm=6.35,  # 250 mil
    min_copper_to_edge_mm=0.381,  # 15 mil
    min_hole_to_edge_mm=0.381,  # 15 mil
    min_silkscreen_width_mm=0.127,  # 5 mil
    min_silkscreen_height_mm=0.762,  # 30 mil
    min_solder_mask_dam_mm=0.102,  # 4 mil
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.0,
)

# OSHPark 4-layer service
OSHPARK_4LAYER = DesignRules(
    min_trace_width_mm=0.127,  # 5 mil
    min_clearance_mm=0.127,  # 5 mil
    min_via_drill_mm=0.254,  # 10 mil
    min_via_diameter_mm=0.508,  # 20 mil
    min_annular_ring_mm=0.127,  # 5 mil
    min_hole_diameter_mm=0.254,
    max_hole_diameter_mm=6.35,
    min_copper_to_edge_mm=0.381,
    min_hole_to_edge_mm=0.381,
    min_silkscreen_width_mm=0.127,
    min_silkscreen_height_mm=0.762,
    min_solder_mask_dam_mm=0.102,
    board_thickness_mm=1.6,
    outer_copper_oz=1.0,
    inner_copper_oz=0.5,
)

# OSHPark Swift service (2-layer, faster turnaround)
OSHPARK_SWIFT = DesignRules(
    min_trace_width_mm=0.127,  # 5 mil
    min_clearance_mm=0.127,  # 5 mil
    min_via_drill_mm=0.254,
    min_via_diameter_mm=0.508,
    min_annular_ring_mm=0.127,
    min_hole_diameter_mm=0.254,
    max_hole_diameter_mm=6.35,
    min_copper_to_edge_mm=0.254,  # Tighter for Swift
    min_hole_to_edge_mm=0.254,
    min_silkscreen_width_mm=0.127,
    min_silkscreen_height_mm=0.762,
    min_solder_mask_dam_mm=0.102,
    board_thickness_mm=0.8,  # 0.8mm for Swift
    outer_copper_oz=1.0,
    inner_copper_oz=0.0,
)

# OSHPark After Dark (black solder mask)
OSHPARK_AFTERDARK = DesignRules(
    min_trace_width_mm=0.1524,  # 6 mil
    min_clearance_mm=0.1524,  # 6 mil
    min_via_drill_mm=0.254,
    min_via_diameter_mm=0.508,
    min_annular_ring_mm=0.127,
    min_hole_diameter_mm=0.254,
    max_hole_diameter_mm=6.35,
    min_copper_to_edge_mm=0.381,
    min_hole_to_edge_mm=0.381,
    min_silkscreen_width_mm=0.127,
    min_silkscreen_height_mm=0.762,
    min_solder_mask_dam_mm=0.102,
    board_thickness_mm=1.6,
    outer_copper_oz=2.0,  # 2oz copper
    inner_copper_oz=0.0,
)

# Complete OSHPark Profile
# Note: No assembly, no parts library
OSHPARK_PROFILE = ManufacturerProfile(
    id="oshpark",
    name="OSHPark",
    website="https://oshpark.com",
    design_rules={
        "2layer_1oz": OSHPARK_2LAYER_STANDARD,
        "2layer_standard": OSHPARK_2LAYER_STANDARD,
        "4layer_1oz": OSHPARK_4LAYER,
        "swift": OSHPARK_SWIFT,
        "afterdark": OSHPARK_AFTERDARK,
        "2layer_2oz": OSHPARK_AFTERDARK,  # After Dark is 2oz
    },
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
