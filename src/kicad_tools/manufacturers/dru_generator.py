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

import re
from typing import TYPE_CHECKING, Sequence

from .base import DesignRules

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting

# ---------------------------------------------------------------------------
# Managed fab-floors block (Issue #4600)
# ---------------------------------------------------------------------------

# Sentinel markers delimiting the kct-owned fab-floor rules inside a
# ``.kicad_dru`` sidecar.  On re-emit only the text BETWEEN these markers is
# replaced; everything else in the file -- hand-written custom rules, the
# ``kct creepage export-rules`` managed block (which uses its own, distinct
# markers; see ``kicad_tools.creepage.export_rules.DRU_BLOCK_BEGIN``) -- is
# preserved verbatim, so the two managed blocks coexist in one file.
DRU_FLOORS_BLOCK_BEGIN = "# BEGIN kct fab floors (Issue #4600) -- managed, do not edit"
DRU_FLOORS_BLOCK_END = "# END kct fab floors"

# KiCad ``.kicad_dru`` files start with a version header.
DRU_VERSION_HEADER = "(version 1)"


def _strip_version_header(content: str) -> str:
    """Drop a leading ``(version N)`` line so a merged file keeps exactly one."""
    return re.sub(r"^\s*\(version[^)]*\)\s*\n?", "", content, count=1)


def merge_dru_floors(existing: str | None, dru_content: str) -> str:
    """Merge :func:`generate_dru` output into ``.kicad_dru`` content (#4600).

    The tier-floor rules are wrapped in a sentinel-delimited managed block and
    merged into ``existing`` with the same semantics as the creepage
    exporter's ``merge_dru_block`` (Issue #4508) -- one merge dialect, two
    distinct marker pairs:

    * No existing content -> ``(version 1)`` + the managed block.
    * Existing fab-floors block present -> replace only the text between the
      sentinels.
    * Existing content without a fab-floors block (a fully user-owned file,
      or one holding the creepage managed block) -> **append** the block,
      preserving every existing byte, and prepend a version header only if
      the file lacks one.  A pre-existing file is therefore never clobbered
      or deleted -- the previous blanket ``write_text`` silently destroyed
      HV creepage rules and hand-written custom rules.

    The merged result contains exactly one leading ``(version 1)`` header
    (the header inside ``dru_content`` is stripped before wrapping).

    Idempotent: re-merging identical inputs yields byte-identical output.

    Args:
        existing: Current ``.kicad_dru`` file content, or ``None`` when the
            file does not exist yet.
        dru_content: Fresh :func:`generate_dru` output (with its leading
            version header) to install as the managed block.

    Returns:
        The full merged ``.kicad_dru`` file content.
    """
    body = _strip_version_header(dru_content).strip("\n")
    block = f"{DRU_FLOORS_BLOCK_BEGIN}\n{body}\n{DRU_FLOORS_BLOCK_END}"

    if existing is None or not existing.strip():
        return f"{DRU_VERSION_HEADER}\n\n{block}\n"

    pattern = re.compile(
        re.escape(DRU_FLOORS_BLOCK_BEGIN) + r".*?" + re.escape(DRU_FLOORS_BLOCK_END),
        re.DOTALL,
    )
    if pattern.search(existing):
        merged = pattern.sub(lambda _m: block, existing)
        return merged if merged.endswith("\n") else merged + "\n"

    # Append; ensure a version header exists (mirrors ``merge_dru_block``).
    prefix = existing if existing.endswith("\n") else existing + "\n"
    if not re.search(r"^\s*\(version\b", existing, re.MULTILINE):
        prefix = f"{DRU_VERSION_HEADER}\n" + prefix
    return f"{prefix}\n{block}\n"


def generate_dru(
    rules: DesignRules,
    manufacturer_name: str = "",
    net_classes: Sequence[NetClassRouting] | None = None,
) -> str:
    """
    Generate KiCad .kicad_dru file content from a DesignRules instance.

    Produces rules with KiCad condition expressions where appropriate to
    scope constraints to the correct object types (e.g., via drill vs pad
    drill, silk-on-silk clearance).

    Args:
        rules: DesignRules instance containing manufacturer constraints.
        manufacturer_name: Optional manufacturer name for rule labels.
        net_classes: Optional sequence of net-class routing configs.  For
            each class whose ``target_ampacity`` is set, two net-scoped
            minimum-width rules (external + internal layers) are appended,
            with the widths derived via IPC-2221 from ``rules.outer_copper_oz``
            / ``rules.inner_copper_oz`` (see
            :func:`kicad_tools.physics.ampacity.width_for_current`).  When
            ``None`` (or when no class sets ``target_ampacity``), the output
            is byte-for-byte identical to the board-wide rule set --
            preserving the drift-prevention pass-through contract.

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

    # --- Ampacity-derived net-scoped minimum trace widths (#4216) ---
    #
    # For each net class carrying a target current, emit two net-scoped
    # min-width rules: one for external copper (F.Cu / B.Cu, k=0.048) and
    # one for internal copper (all other copper layers, k=0.024).  The
    # widths come from the IPC-2221 closed-form inversion using the board's
    # copper weights (outer for external, inner for internal).
    #
    # KiCad's custom-rule condition grammar scopes to a net class via the
    # ``A.NetClass`` token (KiCad 7/8/9 "Custom Design Rules" reference).
    # These rules are appended only when at least one class sets
    # ``target_ampacity``; otherwise the output is byte-for-byte identical to
    # the board-wide rule set above (drift-prevention pass-through).
    if net_classes:
        from kicad_tools.physics.ampacity import width_for_current

        for nc in net_classes:
            if nc.target_ampacity is None:
                continue

            external_width_mm = width_for_current(
                nc.target_ampacity,
                copper_weight_oz=rules.outer_copper_oz,
                layer="external",
            )
            internal_width_mm = width_for_current(
                nc.target_ampacity,
                copper_weight_oz=rules.inner_copper_oz,
                layer="internal",
            )

            # External copper: front and back layers.
            lines.append(
                f'(rule "Ampacity Min Width ({nc.name}, external){label_suffix}"\n'
                f"  (condition \"A.NetClass == '{nc.name}' && A.Type == 'track'"
                f" && (A.Layer == 'F.Cu' || A.Layer == 'B.Cu')\")\n"
                f"  (constraint track_width (min {external_width_mm:.4f}mm)))"
            )
            # Internal copper: any track that is not on an external layer.
            lines.append(
                f'(rule "Ampacity Min Width ({nc.name}, internal){label_suffix}"\n'
                f"  (condition \"A.NetClass == '{nc.name}' && A.Type == 'track'"
                f" && A.Layer != 'F.Cu' && A.Layer != 'B.Cu'\")\n"
                f"  (constraint track_width (min {internal_width_mm:.4f}mm)))"
            )

    return "\n".join(lines) + "\n"
