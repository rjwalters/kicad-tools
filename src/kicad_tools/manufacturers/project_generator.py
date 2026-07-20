"""
KiCad project file (.kicad_pro) DRC-constraint generator.

``kicad-cli pcb drc`` auto-loads ``<board>.kicad_pro`` (same basename and
directory as the ``.kicad_pcb``) and reads its
``board.design_settings.rules`` block for the *built-in* minimum
constraints (min track width, min via diameter, min through-hole drill,
min clearance, ...).  When no project file is present next to the board,
kicad-cli falls back to KiCad's hard-coded defaults (min track 0.20mm,
min via Ø0.50mm, min through-hole 0.30mm, min clearance 0.20mm) -- which
are *stricter* than the capabilities modern fabs (e.g. jlcpcb-tier1)
actually offer.  The result is hundreds of false ``track_width`` /
``via_diameter`` / ``drill_out_of_range`` / ``clearance`` errors on boards
that are genuinely manufacturable.

This module builds the ``design_settings.rules`` block from a
:class:`~kicad_tools.manufacturers.base.DesignRules` instance so the
emitted project file relaxes the built-in minimums to the target
manufacturer profile.  A ``.kicad_dru`` (generated separately by
:func:`~kicad_tools.manufacturers.dru_generator.generate_dru`) is *not*
sufficient on its own: KiCad applies the *most restrictive* of built-in +
custom rules, so a custom ``track_width min 0.15`` will not relax the
built-in 0.20 default -- the built-in minimum lives in
``design_settings.rules`` and must be set there.

Usage::

    from kicad_tools.manufacturers import get_profile
    from kicad_tools.manufacturers.project_generator import (
        build_project_rules,
        write_drc_constraints,
    )

    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)
    pro_dict = build_project_rules(rules)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .base import DesignRules

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting

# Severities for DRC rule categories that are *not* manufacturability
# blockers.  ``lib_footprint_mismatch`` is a library-sync artifact (the
# board's embedded footprint differs from the on-disk library copy) and
# ``isolated_copper`` is a cosmetic zone-island warning -- neither stops a
# fab from building the board, so they must not read as blocking errors.
_NON_BLOCKING_SEVERITIES: dict[str, str] = {
    "lib_footprint_mismatch": "ignore",
    "isolated_copper": "warning",
}

# Built-in via floors and the micro-via split (Issues #3734, #3736).
#
# KiCad's built-in ``via_diameter`` / ``annular_width`` / ``hole_size``
# checks read ``design_settings.rules`` and fire on every via, including the
# ``(via micro ...)`` structures the router's ``--micro-via-in-pad-fallback``
# emits for fine-pitch escape (e.g. LQFP-48 0.5 mm pitch, where a 0.6 mm via
# cannot fit between adjacent pads).  jlcpcb-tier1's Capability+ tier supports
# these micro vias natively, so the kct-check engine flatly exempts
# ``via_type == "micro"`` from the standard floors (see
# validate/rules/dimensions.py) and we mirror that here.
#
# #3734 lowered the *built-in* ``min_via_*`` floor to the micro minimum for
# ALL vias and relied on the ``A.Via_Type != 'Micro'`` guarded ``.kicad_dru``
# rules as the standard-via backstop.  #3736 (board-04 judge) found that
# backstop is non-functional under kicad-cli 10.0.1: any custom
# ``solder_mask_margin`` rule (the unconditional "Solder Mask Clearance" rule
# this generator always emits) SILENTLY SUPPRESSES ``via_diameter`` /
# ``annular_width`` reporting from the other custom DRU rules -- a kicad-cli
# bug, reproduced in tests/test_drc_constraints_export.py.  Net effect: a
# genuinely sub-spec STANDARD via passed kicad-cli silently.
#
# Fix: keep the BUILT-IN ``min_via_diameter`` / ``min_via_hole`` at the
# manufacturer's STANDARD floor (built-in checks are NOT masked by the
# solder-mask quirk, so they catch sub-spec standard vias independently) and
# exempt micro vias from those two checks via KiCad's dedicated
# ``min_microvia_diameter`` / ``min_microvia_drill`` keys.
#
# ``annular_width`` is the one exception: KiCad 10.0.1 has NO micro-via
# annular key (``min_microvia_annular_width`` is silently ignored), so the
# built-in ``annular_width`` floor applies to micro vias too.  Holding it at
# the standard floor would falsely flag board-04's legitimate micro vias, so
# ``min_via_annular_width`` stays at the micro floor; standard-via annular is
# enforced by ``kct check --mfr`` (the primary CI gate, with its own
# micro-via exemption) plus the guarded DRU "Annular Ring" rule.  This is a
# documented kicad-cli limitation, not a coverage gap in the primary gate.
_MICRO_VIA_FLOOR_DIAMETER_MM = 0.2
_MICRO_VIA_FLOOR_ANNULAR_MM = 0.05
_MICRO_VIA_FLOOR_HOLE_MM = 0.1


def build_default_netclass(rules: DesignRules) -> dict:
    """Build the ``Default`` netclass entry for ``net_settings.classes``.

    KiCad's ``clearance`` DRC test uses the *applied* netclass clearance
    (not ``design_settings.rules.min_clearance``, which only bounds the
    minimum a netclass may declare).  When the board carries no netclass
    of its own, kicad-cli falls back to the project's ``Default`` netclass
    -- whose stock clearance is 0.20mm.  Setting it from the profile here
    is what actually silences the false ``clearance`` errors on boards
    routed to tighter fab capabilities.

    Args:
        rules: Manufacturer design rules to translate.

    Returns:
        A KiCad ``Default`` netclass definition dict.
    """
    return {
        "bus_width": 12,
        "clearance": rules.min_clearance_mm,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": rules.min_trace_width_mm,
        "line_style": 0,
        # Micro vias use the micro-via process floor, not the standard
        # through-via size: KiCad's ``via_diameter`` DRC check measures a
        # ``(via micro ...)`` against the netclass ``microvia_diameter``,
        # so leaving this at the 0.6 mm standard size flags every
        # fine-pitch micro via (Issue #3734).  The standard through-via
        # size remains ``via_diameter`` below.
        "microvia_diameter": _MICRO_VIA_FLOOR_DIAMETER_MM,
        "microvia_drill": _MICRO_VIA_FLOOR_HOLE_MM,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": rules.min_trace_width_mm,
        "via_diameter": rules.min_via_diameter_mm,
        "via_drill": rules.min_via_drill_mm,
        "wire_width": 6,
    }


def build_project_rules(rules: DesignRules) -> dict[str, float]:
    """Build the ``board.design_settings.rules`` mapping from a profile.

    Maps :class:`DesignRules` fields onto the KiCad project schema keys
    that ``kicad-cli pcb drc`` reads as built-in minimum constraints.

    Args:
        rules: Manufacturer design rules to translate.

    Returns:
        Dict suitable for ``project["board"]["design_settings"]["rules"]``.
    """
    return {
        "min_clearance": rules.min_clearance_mm,
        "min_track_width": rules.min_trace_width_mm,
        # Standard via diameter / hole floors stay at the manufacturer
        # minimum so KiCad's built-in checks independently catch sub-spec
        # STANDARD vias (the #3734 DRU backstop is masked by the
        # solder_mask_margin quirk -- see _MICRO_VIA_FLOOR_* above).  Micro
        # vias are exempted from these two built-in checks via the dedicated
        # ``min_microvia_diameter`` / ``min_microvia_drill`` keys.
        "min_via_diameter": rules.min_via_diameter_mm,
        "min_microvia_diameter": _MICRO_VIA_FLOOR_DIAMETER_MM,
        # ``annular_width`` has no micro-via key in KiCad 10.0.1, so this
        # floor applies to micro vias too -- it must stay at the micro
        # minimum to avoid false positives on legitimate micro vias.
        # Standard-via annular is enforced by ``kct check --mfr`` + the
        # guarded DRU "Annular Ring" rule.
        "min_via_annular_width": _MICRO_VIA_FLOOR_ANNULAR_MM,
        "min_through_hole_diameter": rules.min_hole_diameter_mm,
        "min_via_hole": rules.min_via_drill_mm,
        "min_microvia_drill": _MICRO_VIA_FLOOR_HOLE_MM,
        "min_hole_to_hole": rules.min_hole_to_hole_mm,
        "min_copper_edge_clearance": rules.min_copper_to_edge_mm,
        "min_silk_clearance": rules.min_solder_mask_clearance_mm,
        "min_text_thickness": rules.min_silkscreen_width_mm,
        "min_text_height": rules.min_silkscreen_height_mm,
    }


def build_project_data(
    rules: DesignRules,
    project_name: str,
    manufacturer_id: str = "",
    layers: int | None = None,
    copper_oz: float | None = None,
) -> dict:
    """Build a complete minimal ``.kicad_pro`` dict with DRC constraints.

    The returned project carries the ``board.design_settings.rules`` block
    (relaxing KiCad's built-in minimums to the profile) plus
    ``rule_severities`` that downgrade non-manufacturability noise.

    Args:
        rules: Manufacturer design rules to translate.
        project_name: Base name (without extension) recorded in ``meta``.
        manufacturer_id: Optional manufacturer id stored in ``meta``.
        layers: Optional copper-layer count stored in ``meta``.
        copper_oz: Optional copper weight stored in ``meta``.

    Returns:
        Project data dict ready to be JSON-serialized to ``.kicad_pro``.
    """
    meta: dict = {"filename": f"{project_name}.kicad_pro", "version": 1}
    if manufacturer_id:
        meta["manufacturer"] = manufacturer_id
    if layers is not None:
        meta["layers"] = layers
    if copper_oz is not None:
        meta["copper_oz"] = copper_oz

    return {
        "meta": meta,
        "board": {
            "design_settings": {
                "rules": build_project_rules(rules),
                "rule_severities": dict(_NON_BLOCKING_SEVERITIES),
                "defaults": {
                    "track_min_width": rules.min_trace_width_mm,
                    "clearance_min": rules.min_clearance_mm,
                    "via_min_diameter": rules.min_via_diameter_mm,
                    "via_min_drill": rules.min_via_drill_mm,
                },
            }
        },
        "net_settings": {
            "classes": [build_default_netclass(rules)],
            "meta": {"version": 3},
        },
        "schematic": {"meta": {"version": 1}},
        "sheets": [],
        "text_variables": {},
    }


def merge_project_rules(
    project_data: dict,
    rules: DesignRules,
) -> dict:
    """Apply DRC constraints + severities onto an existing project dict.

    Preserves any unrelated keys already present in the project file and
    only overwrites the constraint/severity entries this module owns.

    Args:
        project_data: Parsed ``.kicad_pro`` data (mutated in place).
        rules: Manufacturer design rules to apply.

    Returns:
        The same ``project_data`` dict, mutated.
    """
    board = project_data.setdefault("board", {})
    settings = board.setdefault("design_settings", {})

    settings.setdefault("rules", {}).update(build_project_rules(rules))

    severities = settings.setdefault("rule_severities", {})
    severities.update(_NON_BLOCKING_SEVERITIES)

    defaults = settings.setdefault("defaults", {})
    defaults["track_min_width"] = rules.min_trace_width_mm
    defaults["clearance_min"] = rules.min_clearance_mm
    defaults["via_min_diameter"] = rules.min_via_diameter_mm
    defaults["via_min_drill"] = rules.min_via_drill_mm

    # Relax the applied Default-netclass clearance/track/via to the profile
    # so the kicad-cli ``clearance`` test (which reads the applied netclass
    # clearance, not min_clearance) does not flag the stock 0.20mm default.
    net_settings = project_data.setdefault("net_settings", {})
    classes = net_settings.setdefault("classes", [])
    default_cls = next((c for c in classes if c.get("name") == "Default"), None)
    if default_cls is None:
        classes.insert(0, build_default_netclass(rules))
    else:
        default_cls["clearance"] = rules.min_clearance_mm
        default_cls["track_width"] = rules.min_trace_width_mm
        default_cls["via_diameter"] = rules.min_via_diameter_mm
        default_cls["via_drill"] = rules.min_via_drill_mm

    return project_data


def write_drc_constraints(
    pcb_path: str | Path,
    rules: DesignRules,
    *,
    manufacturer_id: str = "",
    layers: int | None = None,
    copper_oz: float | None = None,
    write_dru: bool = True,
    net_classes: Sequence[NetClassRouting] | None = None,
) -> list[Path]:
    """Emit DRC-constraint sources next to a routed ``.kicad_pcb``.

    Writes (or updates) ``<board>.kicad_pro`` so ``kicad-cli pcb drc``
    auto-loads the relaxed built-in minimums, and -- by default -- a
    companion ``<board>.kicad_dru`` for the rule families the project
    schema can't express.  An existing ``.kicad_pro`` is preserved and
    only the constraint/severity entries are overwritten.

    Args:
        pcb_path: Path to the routed board.
        rules: Manufacturer design rules to translate.
        manufacturer_id: Optional manufacturer id (for ``.kicad_pro`` meta
            and ``.kicad_dru`` labels).
        layers: Optional copper-layer count (stored in ``meta``).
        copper_oz: Optional copper weight (stored in ``meta``).
        write_dru: When True (default), also emit the ``.kicad_dru``.
        net_classes: Optional net-class routing configs threaded through to
            :func:`~kicad_tools.manufacturers.dru_generator.generate_dru`.
            When any class declares a ``target_ampacity`` the emitted
            ``.kicad_dru`` carries the matching net-scoped minimum-width
            rules, so ``kicad-cli`` enforces the same ampacity floors
            ``kct check`` evaluated.  When ``None`` the ``.kicad_dru`` is
            byte-for-byte identical to the board-wide rule set (#4216).

    Returns:
        List of paths written.
    """
    pcb_path = Path(pcb_path)
    project_name = pcb_path.stem
    pro_path = pcb_path.with_suffix(".kicad_pro")
    written: list[Path] = []

    if pro_path.exists():
        try:
            project_data = json.loads(pro_path.read_text(encoding="utf-8"))
            merge_project_rules(project_data, rules)
            if manufacturer_id:
                project_data.setdefault("meta", {})["manufacturer"] = manufacturer_id
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable existing project -- overwrite cleanly.
            project_data = build_project_data(
                rules,
                project_name,
                manufacturer_id=manufacturer_id,
                layers=layers,
                copper_oz=copper_oz,
            )
    else:
        project_data = build_project_data(
            rules,
            project_name,
            manufacturer_id=manufacturer_id,
            layers=layers,
            copper_oz=copper_oz,
        )

    pro_path.write_text(json.dumps(project_data, indent=2), encoding="utf-8")
    written.append(pro_path)

    if write_dru:
        from .dru_generator import generate_dru

        dru_path = pcb_path.with_suffix(".kicad_dru")
        dru_path.write_text(
            generate_dru(rules, manufacturer_name=manufacturer_id, net_classes=net_classes),
            encoding="utf-8",
        )
        written.append(dru_path)

    return written
