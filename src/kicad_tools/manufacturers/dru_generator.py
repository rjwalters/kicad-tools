"""
Centralized KiCad DRC rules (.kicad_dru) file generator.

Generates .kicad_dru file content from a DesignRules dataclass instance,
producing rules with proper KiCad condition expressions to scope rules
to the correct object types and avoid false positives.

Usage:
    from kicad_tools.manufacturers import get_profile
    from kicad_tools.manufacturers.dru_generator import generate_dru

    profile = get_profile("jlcpcb")
    rules = profile.get_design_rules(layers=2, copper_oz=1.0)
    content = generate_dru(rules, manufacturer_name="JLCPCB")
"""

from __future__ import annotations

from .base import DesignRules


def generate_dru(
    rules: DesignRules,
    manufacturer_name: str = "",
) -> str:
    """
    Generate KiCad .kicad_dru file content from a DesignRules instance.

    Produces rules with KiCad condition expressions where appropriate to
    scope constraints to the correct object types (e.g., via drill vs pad
    drill, silk-on-silk clearance).

    Args:
        rules: DesignRules instance containing manufacturer constraints.
        manufacturer_name: Optional manufacturer name for rule labels.

    Returns:
        String content of a valid .kicad_dru file.
    """
    label_suffix = f" - {manufacturer_name}" if manufacturer_name else ""
    lines: list[str] = ["(version 1)"]

    # --- Trace Width ---
    lines.append(
        f'(rule "Trace Width{label_suffix}"\n'
        f"  (condition \"A.Type == 'track'\")\n"
        f"  (constraint track_width (min {rules.min_trace_width_mm}mm)))"
    )

    # --- Clearance (general copper-to-copper) ---
    lines.append(
        f'(rule "Clearance{label_suffix}"\n'
        f"  (constraint clearance (min {rules.min_clearance_mm}mm)))"
    )

    # --- Via Drill ---
    # Issue #3118 / #3734: exempt micro vias from the standard through-via
    # floors.  The router's ``--micro-via-in-pad-fallback`` (and ``kct stitch
    # --micro-via``) emit ``(via micro ...)`` structures that are
    # intentionally smaller than the manufacturer's standard via floor for
    # fine-pitch escape (e.g. LQFP-48 0.5 mm pitch, where a 0.6 mm via cannot
    # fit between adjacent pads).  jlcpcb-tier1's published Capability+ tier
    # supports these micro vias natively, so the kct-check engine flatly
    # exempts ``via_type == "micro"`` (see validate/rules/dimensions.py).
    # Mirror that exemption here so ``kicad-cli pcb drc`` agrees -- otherwise
    # the same micro via is exempt under one engine and a hard error under
    # the other.  KiCad scopes via type via ``A.Via_Type``.
    lines.append(
        f'(rule "Via Drill{label_suffix}"\n'
        f"  (condition \"A.Type == 'via' && A.Via_Type != 'Micro'\")\n"
        f"  (constraint hole_size (min {rules.min_via_drill_mm}mm)))"
    )

    # --- Via Diameter ---
    lines.append(
        f'(rule "Via Diameter{label_suffix}"\n'
        f"  (condition \"A.Type == 'via' && A.Via_Type != 'Micro'\")\n"
        f"  (constraint via_diameter (min {rules.min_via_diameter_mm}mm)))"
    )

    # --- Annular Ring ---
    lines.append(
        f'(rule "Annular Ring{label_suffix}"\n'
        f"  (condition \"A.Via_Type != 'Micro'\")\n"
        f"  (constraint annular_width (min {rules.min_annular_ring_mm}mm)))"
    )

    # --- Copper to Edge ---
    lines.append(
        f'(rule "Copper to Edge{label_suffix}"\n'
        f"  (constraint edge_clearance (min {rules.min_copper_to_edge_mm}mm)))"
    )

    # --- Hole to Edge ---
    lines.append(
        f'(rule "Hole to Edge{label_suffix}"\n'
        f"  (condition \"A.Type == 'via' || A.Type == 'pad'\")\n"
        f"  (constraint hole_clearance (min {rules.min_hole_to_edge_mm}mm)))"
    )

    # --- Silkscreen Width ---
    lines.append(
        f'(rule "Silkscreen Width{label_suffix}"\n'
        f"  (condition \"A.Type == 'text' && A.Layer == 'F.Silkscreen'\")\n"
        f"  (constraint text_thickness (min {rules.min_silkscreen_width_mm}mm)))"
    )

    # --- Silkscreen Height ---
    lines.append(
        f'(rule "Silkscreen Height{label_suffix}"\n'
        f"  (condition \"A.Type == 'text' && A.Layer == 'F.Silkscreen'\")\n"
        f"  (constraint text_height (min {rules.min_silkscreen_height_mm}mm)))"
    )

    # --- Solder Mask Clearance ---
    lines.append(
        f'(rule "Solder Mask Clearance{label_suffix}"\n'
        f"  (constraint solder_mask_margin (min {rules.min_solder_mask_clearance_mm}mm)))"
    )

    # --- Solder Mask Dam (bridge) ---
    lines.append(
        f'(rule "Solder Mask Dam{label_suffix}"\n'
        f"  (constraint physical_hole_clearance (min {rules.min_solder_mask_dam_mm}mm)))"
    )

    return "\n".join(lines) + "\n"
