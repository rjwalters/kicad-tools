"""Silkscreen validation rules.

This module implements DRC checks for silkscreen elements:
- Minimum line width
- Minimum text height
- Silkscreen-over-pad detection
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

# Silkscreen layer names
SILKSCREEN_LAYERS = ("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen")


def is_silkscreen_layer(layer: str) -> bool:
    """Check if a layer is a silkscreen layer."""
    return layer in SILKSCREEN_LAYERS


def check_silkscreen_line_width(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Check silkscreen line width against minimum.

    Checks both:
    - Board-level graphics (gr_line, gr_rect, etc.) on silkscreen layers
    - Footprint graphics (fp_line, fp_rect, etc.) on silkscreen layers

    Args:
        pcb: The PCB to check
        design_rules: Design rules with min_silkscreen_width_mm

    Returns:
        DRCResults containing any violations
    """
    results = DRCResults(rules_checked=1)
    min_width = design_rules.min_silkscreen_width_mm

    # Check board-level graphics
    for graphic in pcb.graphics:
        if not is_silkscreen_layer(graphic.layer):
            continue
        if graphic.stroke_width < min_width and graphic.stroke_width > 0:
            results.add(
                DRCViolation(
                    rule_id="silkscreen_line_width",
                    severity="warning",
                    message=(
                        f"Silkscreen line width {graphic.stroke_width:.2f}mm "
                        f"< minimum {min_width:.2f}mm"
                    ),
                    location=graphic.start,
                    layer=graphic.layer,
                    actual_value=graphic.stroke_width,
                    required_value=min_width,
                    items=(f"gr_{graphic.graphic_type}",),
                )
            )

    # Check footprint graphics
    for footprint in pcb.footprints:
        for graphic in footprint.graphics:
            if not is_silkscreen_layer(graphic.layer):
                continue
            if graphic.stroke_width < min_width and graphic.stroke_width > 0:
                results.add(
                    DRCViolation(
                        rule_id="silkscreen_line_width",
                        severity="warning",
                        message=(
                            f"Silkscreen line width {graphic.stroke_width:.2f}mm "
                            f"< minimum {min_width:.2f}mm on {footprint.reference}"
                        ),
                        location=footprint.position,
                        layer=graphic.layer,
                        actual_value=graphic.stroke_width,
                        required_value=min_width,
                        items=(footprint.reference, f"fp_{graphic.graphic_type}"),
                    )
                )

    return results


def check_silkscreen_text_height(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Check silkscreen text height against minimum.

    Checks both:
    - Board-level text (gr_text) on silkscreen layers
    - Footprint text (fp_text) on silkscreen layers

    Args:
        pcb: The PCB to check
        design_rules: Design rules with min_silkscreen_height_mm

    Returns:
        DRCResults containing any violations
    """
    results = DRCResults(rules_checked=1)
    min_height = design_rules.min_silkscreen_height_mm

    # Check board-level text
    for text in pcb.texts:
        if not is_silkscreen_layer(text.layer):
            continue
        if text.hidden:
            continue
        if text.font_height < min_height:
            results.add(
                DRCViolation(
                    rule_id="silkscreen_text_height",
                    severity="warning",
                    message=(
                        f"Silkscreen text height {text.font_height:.2f}mm "
                        f"< minimum {min_height:.2f}mm"
                    ),
                    location=text.position,
                    layer=text.layer,
                    actual_value=text.font_height,
                    required_value=min_height,
                    items=(text.text[:20] if text.text else "gr_text",),
                )
            )

    # Check footprint text
    for footprint in pcb.footprints:
        for fp_text in footprint.texts:
            if not is_silkscreen_layer(fp_text.layer):
                continue
            if fp_text.hidden:
                continue
            if fp_text.font_height < min_height:
                # Build descriptive item name
                if fp_text.text_type == "reference":
                    item_name = f"{footprint.reference} (reference)"
                elif fp_text.text_type == "value":
                    item_name = f"{footprint.reference} (value)"
                else:
                    item_name = f"{footprint.reference} ({fp_text.text_type})"

                results.add(
                    DRCViolation(
                        rule_id="silkscreen_text_height",
                        severity="warning",
                        message=(
                            f"Silkscreen text height {fp_text.font_height:.2f}mm "
                            f"< minimum {min_height:.2f}mm on {footprint.reference}"
                        ),
                        location=footprint.position,
                        layer=fp_text.layer,
                        actual_value=fp_text.font_height,
                        required_value=min_height,
                        items=(item_name,),
                    )
                )

    return results


def check_silkscreen_over_pads(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Check for silkscreen elements overlapping exposed pads.

    This is a simplified check that warns when silkscreen elements
    exist on the same layer as SMD pads in the footprint. A full
    geometric overlap check would require more complex calculations.

    For now, this checks footprint text that might overlap pads by
    checking if the text position is within the pad area.

    Args:
        pcb: The PCB to check
        design_rules: Design rules (not currently used for this check)

    Returns:
        DRCResults containing any warnings
    """
    results = DRCResults(rules_checked=1)

    for footprint in pcb.footprints:
        # Get exposed pads (SMD pads are always exposed)
        exposed_pads = [pad for pad in footprint.pads if pad.type == "smd"]

        if not exposed_pads:
            continue

        # Determine silkscreen layer for this footprint side
        if footprint.layer == "F.Cu":
            silk_layer = ("F.SilkS", "F.Silkscreen")
        else:
            silk_layer = ("B.SilkS", "B.Silkscreen")

        # Check footprint text elements
        for fp_text in footprint.texts:
            if fp_text.layer not in silk_layer:
                continue
            if fp_text.hidden:
                continue

            # Simple overlap check: see if text center is close to any pad
            # This is a simplified heuristic - full overlap detection would
            # require computing bounding boxes and intersections
            for pad in exposed_pads:
                # Calculate distance from text to pad center
                dx = fp_text.position[0] - pad.position[0]
                dy = fp_text.position[1] - pad.position[1]

                # Text is considered "over" pad if within half the pad size
                pad_half_width = pad.size[0] / 2
                pad_half_height = pad.size[1] / 2

                if abs(dx) < pad_half_width and abs(dy) < pad_half_height:
                    results.add(
                        DRCViolation(
                            rule_id="silkscreen_over_pad",
                            severity="warning",
                            message=(
                                f"Silkscreen text may overlap exposed pad on {footprint.reference}"
                            ),
                            location=footprint.position,
                            layer=fp_text.layer,
                            items=(footprint.reference, f"pad {pad.number}"),
                        )
                    )
                    break  # Only report once per text element

    return results


def check_all_silkscreen(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Run all silkscreen checks.

    Args:
        pcb: The PCB to check
        design_rules: Design rules from manufacturer profile

    Returns:
        DRCResults containing all silkscreen violations
    """
    results = DRCResults()

    results.merge(check_silkscreen_line_width(pcb, design_rules))
    results.merge(check_silkscreen_text_height(pcb, design_rules))
    results.merge(check_silkscreen_over_pads(pcb, design_rules))

    return results
