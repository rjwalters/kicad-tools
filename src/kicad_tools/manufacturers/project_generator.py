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

import copy
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from .base import DesignRules

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting

logger = logging.getLogger(__name__)

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
# this generator then emitted) SILENTLY SUPPRESSES ``via_diameter`` /
# ``annular_width`` reporting from the other custom DRU rules, reproduced in
# tests/test_drc_constraints_export.py.  The cause (#4999, re-verified in
# #6150): ``solder_mask_margin`` is not a KiCad constraint keyword, so
# kicad-cli discards the WHOLE ``.kicad_dru`` -- every custom rule -- without
# an error.  The generator no longer emits it.  Net effect at the time: a
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

# Value emitted for the project key ``board.design_settings.rules.
# min_silk_clearance`` (Issues #5059, #5704).
#
# This is a fixed legacy value. It is NOT the manufacturer's silkscreen floor,
# and it is deliberately not derived from any ``DesignRules`` field:
#
# * KiCad ignores this key for silk-to-pad gaps today. It was measured on
#   kicad-cli 10.0.1, 10.0.5 and 10.0.6 (the CI-pinned image): a 0.085 mm
#   silk-to-pad gap produced no finding with the key at 0.15 mm or at 2.0 mm,
#   while a positive control showed the same project ``rules`` block was
#   loaded. The factory floor (``DesignRules.min_silk_to_pad_clearance_mm``,
#   0.15 mm for JLCPCB) reaches native DRC through the explicit
#   ``Silk to Pad`` rule in the ``.kicad_dru``. See
#   ``tests/test_effective_silk_clearance_5059.py``.
# * Until #5704 the key was fed from ``min_solder_mask_clearance_mm``. That
#   was a name/source mismatch: a mask clearance was used as a silk clearance.
#   Every profile sets that field to 0.05 mm, so the value is kept at 0.05 mm.
#   Keeping it leaves the ~24 committed ``.kicad_pro`` artifacts under
#   ``boards/`` and ``tests/fixtures/`` byte-identical. This was an operator
#   ruling on #5704.
#
# If a future KiCad starts gating silk-to-pad gaps on this key, 0.05 mm
# becomes too lax a floor. Re-point the key to
# ``min_silk_to_pad_clearance_mm`` at that point. Four of the six profiles
# declare no silk floor (``None``), so that change needs an explicit fallback
# for them.
PROJECT_MIN_SILK_CLEARANCE_MM = 0.05


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
        # Fixed legacy value, deliberately NOT derived from ``rules`` -- see
        # ``PROJECT_MIN_SILK_CLEARANCE_MM`` above (#5059, #5704).  KiCad
        # ignores this key for silk-to-pad gaps today; the factory floor is
        # carried by the ``Silk to Pad`` rule in the ``.kicad_dru``.
        "min_silk_clearance": PROJECT_MIN_SILK_CLEARANCE_MM,
        "min_text_thickness": rules.min_silkscreen_width_mm,
        "min_text_height": rules.min_silkscreen_height_mm,
    }


#: ``design_settings.rules`` keys :func:`build_project_rules` writes.
_PROJECT_RULE_KEYS: tuple[str, ...] = (
    "min_clearance",
    "min_track_width",
    "min_via_diameter",
    "min_microvia_diameter",
    "min_via_annular_width",
    "min_through_hole_diameter",
    "min_via_hole",
    "min_microvia_drill",
    "min_hole_to_hole",
    "min_copper_edge_clearance",
    "min_silk_clearance",
    "min_text_thickness",
    "min_text_height",
)

#: ``design_settings.defaults`` keys :func:`merge_project_rules` writes.
_PROJECT_DEFAULT_KEYS: tuple[str, ...] = (
    "track_min_width",
    "clearance_min",
    "via_min_diameter",
    "via_min_drill",
)


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


#: Name of the native project text variable that selects the board-rule
#: preservation mode.  See :func:`preserve_board_rules_mode`.
PRESERVE_BOARD_RULES_VARIABLE = "KCT_PRESERVE_BOARD_RULES"

#: ``KCT_PRESERVE_BOARD_RULES`` modes (Issues #5023, #6191).
#:
#: ``"stricter"``  -- unset / empty (the default since #6191): numeric minima
#:     become ``max(authored, profile)``; severities are overwritten; an
#:     untouched template ``Default`` netclass is still relaxed.
#: ``"full"``      -- ``1`` / ``true`` / ``yes`` / ``on``: the #5023 opt-in.
#:     Stricter minima AND authored severities are kept, every netclass
#:     (template-signature ``Default`` included) is preserved, and the DRU
#:     carries one ``Reviewed clearance`` rule per netclass.
#: ``"off"``       -- ``0`` / ``false`` / ``no`` / ``off``: the legacy
#:     overwrite.  Profile values replace every minimum.
PRESERVE_MODE_STRICTER = "stricter"
PRESERVE_MODE_FULL = "full"
PRESERVE_MODE_OFF = "off"

_PRESERVE_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_PRESERVE_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def preserve_board_rules_mode(project_data: dict, override: str | None = None) -> str:
    """Return the board-rule preservation mode a project selects.

    Reads the ``KCT_PRESERVE_BOARD_RULES`` native project text variable
    (case-insensitive, surrounding whitespace ignored), or ``override`` when
    the caller passes one -- see the ``preserve_board_rules`` argument of
    :func:`merge_project_rules`:

    * unset or empty -> :data:`PRESERVE_MODE_STRICTER` (default, #6191)
    * ``1`` / ``true`` / ``yes`` / ``on`` -> :data:`PRESERVE_MODE_FULL` (#5023)
    * ``0`` / ``false`` / ``no`` / ``off`` -> :data:`PRESERVE_MODE_OFF`

    Any other value logs a warning and falls back to the default
    :data:`PRESERVE_MODE_STRICTER` -- the mode that can never loosen an
    authored minimum.

    Args:
        project_data: Parsed ``.kicad_pro`` data.
        override: Value to use instead of the project's text variable, with
            the same spellings.  ``None`` reads the project.

    Returns:
        One of the ``PRESERVE_MODE_*`` constants.
    """
    if override is not None:
        raw: object = override
    else:
        text_variables = project_data.get("text_variables")
        raw = (
            text_variables.get(PRESERVE_BOARD_RULES_VARIABLE)
            if isinstance(text_variables, dict)
            else None
        )
    value = "" if raw is None else str(raw).strip().lower()
    if value == "":
        return PRESERVE_MODE_STRICTER
    if value in _PRESERVE_TRUE_VALUES:
        return PRESERVE_MODE_FULL
    if value in _PRESERVE_FALSE_VALUES:
        return PRESERVE_MODE_OFF
    logger.warning(
        "Unrecognised %s=%r; using the default (keep stricter authored minima). "
        "Use 1/true to also keep authored severities, or 0/false for the legacy "
        "profile overwrite.",
        PRESERVE_BOARD_RULES_VARIABLE,
        raw,
    )
    return PRESERVE_MODE_STRICTER


def _template_netclass_signature() -> tuple[float, float, float, float]:
    from kicad_tools.core.project_file import DEFAULT_NETCLASS_DEFINITION

    return tuple(  # type: ignore[return-value]
        float(DEFAULT_NETCLASS_DEFINITION[key]) for key in _NETCLASS_SIGNATURE_KEYS
    )


#: Keys compared, in order, against :data:`TEMPLATE_DEFAULT_NETCLASS_SIGNATURES`.
_NETCLASS_SIGNATURE_KEYS: tuple[str, str, str, str] = (
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
)

#: ``(clearance, track_width, via_diameter, via_drill)`` tuples, in mm, of
#: ``Default`` netclasses that come from a template rather than a designer
#: (Issue #6191).  Under the default ``stricter`` mode a ``Default`` netclass
#: that matches one of these exactly (within :data:`_SIGNATURE_TOLERANCE_MM`)
#: is treated as unauthored and relaxed to the profile, exactly as before
#: #6191.  Without this exemption every board whose project was stamped from a
#: template would have its stock 0.20 mm clearance "preserved" on
#: regeneration -- the external softstart board went from 10 to 23 kicad-cli
#: DRC errors that way.  ``KCT_PRESERVE_BOARD_RULES=1`` still preserves these.
#:
#: * kct's own template, derived from
#:   :data:`kicad_tools.core.project_file.DEFAULT_NETCLASS_DEFINITION`
#:   (currently ``(0.15, 0.25, 0.6, 0.3)``).
#: * kct's pre-#5654 template ``(0.2, 0.25, 0.6, 0.3)``.
#: * KiCad 5/6 stock ``(0.2, 0.25, 0.8, 0.4)``.
#: * KiCad 7+ stock ``(0.2, 0.2, 0.6, 0.3)``.
#:
#: Deliberately no equivalent heuristic exists for ``design_settings.rules``:
#: values there are authored (#6191 ruling).
TEMPLATE_DEFAULT_NETCLASS_SIGNATURES: tuple[tuple[float, float, float, float], ...] = (
    _template_netclass_signature(),
    (0.2, 0.25, 0.6, 0.3),
    (0.2, 0.25, 0.8, 0.4),
    (0.2, 0.2, 0.6, 0.3),
)

_SIGNATURE_TOLERANCE_MM = 1e-6


def is_template_default_netclass(netclass: dict) -> bool:
    """Return True when ``netclass`` matches a known template ``Default``.

    See :data:`TEMPLATE_DEFAULT_NETCLASS_SIGNATURES`.  A netclass missing any
    of the four signature keys, or carrying a non-numeric value, never
    matches.
    """
    values: list[float] = []
    for key in _NETCLASS_SIGNATURE_KEYS:
        value = netclass.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        values.append(float(value))
    return any(
        all(abs(v - s) <= _SIGNATURE_TOLERANCE_MM for v, s in zip(values, signature, strict=True))
        for signature in TEMPLATE_DEFAULT_NETCLASS_SIGNATURES
    )


def merge_project_rules(
    project_data: dict,
    rules: DesignRules,
    *,
    preserve_board_rules: str | None = None,
) -> dict:
    """Apply DRC constraints + severities onto an existing project dict.

    Preserves unrelated keys.  How profile minima combine with the project's
    authored minima is selected by the native project text variable
    ``KCT_PRESERVE_BOARD_RULES`` (see :func:`preserve_board_rules_mode`):

    * **unset / empty (default, #6191)** -- every numeric minimum in
      ``design_settings.rules``, ``design_settings.defaults`` and the
      ``Default`` netclass becomes ``max(authored, profile)``: a stricter
      authored value wins, and the profile still tightens an authored value
      below the fab floor.  Severities in ``_NON_BLOCKING_SEVERITIES`` are
      overwritten.  A ``Default`` netclass that exactly matches a template
      signature (:data:`TEMPLATE_DEFAULT_NETCLASS_SIGNATURES`) counts as
      unauthored and is relaxed to the profile.
    * **``1`` / ``true``** (#5023) -- as the default, but authored severities
      are kept too and a template-signature ``Default`` netclass is preserved.
    * **``0`` / ``false``** -- the pre-#6191 legacy overwrite: profile values
      replace every minimum, including stricter authored ones.

    The text variable survives native project editing, which makes the
    policy part of the reviewed source.

    Args:
        project_data: Parsed ``.kicad_pro`` data (mutated in place).
        rules: Manufacturer design rules to apply.
        preserve_board_rules: Caller override for the project's
            ``KCT_PRESERVE_BOARD_RULES`` (same spellings); ``None`` reads the
            project.  A script that deliberately applies a reviewed process
            *looser* than rules an earlier kct pass wrote (board 04's paid
            0.15 mm drilling option) passes ``"0"``, because under the default
            mode that earlier, stricter value would otherwise stick.

    Returns:
        The same ``project_data`` dict, mutated.
    """
    board = project_data.setdefault("board", {})
    settings = board.setdefault("design_settings", {})

    mode = preserve_board_rules_mode(project_data, preserve_board_rules)
    keep_minima = mode != PRESERVE_MODE_OFF

    def apply_minima(target: dict, values: dict, keep: bool = keep_minima) -> None:
        for key, value in values.items():
            previous = target.get(key)
            target[key] = (
                max(previous, value)
                if keep and isinstance(previous, (int, float)) and not isinstance(previous, bool)
                else value
            )

    apply_minima(settings.setdefault("rules", {}), build_project_rules(rules))

    severities = settings.setdefault("rule_severities", {})
    for key, value in _NON_BLOCKING_SEVERITIES.items():
        if mode == PRESERVE_MODE_FULL:
            severities.setdefault(key, value)
        else:
            severities[key] = value

    defaults = settings.setdefault("defaults", {})
    apply_minima(
        defaults,
        {
            "track_min_width": rules.min_trace_width_mm,
            "clearance_min": rules.min_clearance_mm,
            "via_min_diameter": rules.min_via_diameter_mm,
            "via_min_drill": rules.min_via_drill_mm,
        },
    )

    # Relax the applied Default-netclass clearance/track/via to the profile
    # so the kicad-cli ``clearance`` test (which reads the applied netclass
    # clearance, not min_clearance) does not flag the stock 0.20mm default.
    # Under the default mode an authored (non-template) Default netclass
    # keeps its stricter values (#6191).
    net_settings = project_data.setdefault("net_settings", {})
    classes = net_settings.setdefault("classes", [])
    default_cls = next((c for c in classes if c.get("name") == "Default"), None)
    if default_cls is None:
        classes.insert(0, build_default_netclass(rules))
    else:
        keep_default_cls = keep_minima and not (
            mode == PRESERVE_MODE_STRICTER and is_template_default_netclass(default_cls)
        )
        apply_minima(
            default_cls,
            {
                "clearance": rules.min_clearance_mm,
                "track_width": rules.min_trace_width_mm,
                "via_diameter": rules.min_via_diameter_mm,
                "via_drill": rules.min_via_drill_mm,
            },
            keep_default_cls,
        )

    return project_data


#: ``meta`` key recording which ``net_settings`` classes and
#: ``netclass_assignments`` entries kct declared from the router's net-class
#: map (Issue #6390).  Everything *not* listed here is authored and is never
#: replaced, loosened or removed by :func:`apply_router_netclasses`.
ROUTER_NETCLASSES_META_KEY = "kct_router_netclasses"

#: ``meta`` key recording the differential pairs the router coupled-routed
#: whose names KiCad's ``isCoupledDiffPair()`` does not recognise (Issue
#: #6429): a sorted list of ``[P, N]`` net-name pairs.  The router-class
#: ``.kicad_dru`` exemption and ``kct check``'s ``netclass_clearance_copper``
#: both read it, through
#: :func:`~kicad_tools.manufacturers.netclass_dru.is_router_coupled_pair`.
#: KiCad-named pairs (``USB_D+``/``USB_D-``, ``TX_P``/``TX_N``) are never
#: listed, so boards that only have those carry no key at all.
ROUTER_DIFF_PAIRS_META_KEY = "kct_router_diff_pairs"

#: ``meta`` key holding a digest of the project content kct wrote (Issue
#: #6390, AC5).  See :func:`stamp_kct_project` / :func:`is_kct_owned_project`.
SIDECAR_DIGEST_META_KEY = "kct_sidecar_sha256"

_CLEARANCE_EPSILON_MM = 1e-9


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def router_netclass_provenance(project_data: dict) -> tuple[list[str], list[str]]:
    """``(class names, net names)`` kct declared from the router map (#6390)."""
    meta = project_data.get("meta")
    record = meta.get(ROUTER_NETCLASSES_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(record, dict):
        return [], []
    classes = [c for c in record.get("classes", []) if isinstance(c, str)]
    nets = [n for n in record.get("assignments", []) if isinstance(n, str)]
    return classes, nets


def recorded_router_coupled_pairs(project_data: dict) -> list[tuple[str, str]]:
    """``(P, N)`` pairs recorded under ``meta.kct_router_diff_pairs`` (#6429)."""
    meta = project_data.get("meta")
    record = meta.get(ROUTER_DIFF_PAIRS_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(record, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in record:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and all(isinstance(name, str) and name for name in item)
            and item[0] != item[1]
        ):
            pairs.append((item[0], item[1]))
    return pairs


def apply_router_coupled_pairs(
    project_data: dict,
    pairs: Iterable[tuple[str, str]],
    net_names: Iterable[str] | None = None,
) -> dict:
    """Record the router's coupled pairs KiCad does not recognise (Issue #6429).

    Replaces any earlier record (a pair no longer engaged is dropped).  Only
    pairs for which :func:`~kicad_tools.manufacturers.netclass_dru.is_coupled_diff_pair`
    is false in both orders are kept -- KiCad already exempts the others by
    name -- and, when ``net_names`` is given, only pairs whose two nets are
    on the board.  An empty result removes the key, so a board whose pairs
    are all KiCad-named stays byte-identical.
    """
    from .netclass_dru import is_coupled_diff_pair

    present = None if net_names is None else set(net_names)
    record = sorted(
        {
            (p, n)
            for p, n in pairs
            if p
            and n
            and p != n
            and not is_coupled_diff_pair(p, n)
            and not is_coupled_diff_pair(n, p)
            and (present is None or (p in present and n in present))
        }
    )
    meta = project_data.get("meta")
    if isinstance(meta, dict):
        meta.pop(ROUTER_DIFF_PAIRS_META_KEY, None)
    if record:
        project_data.setdefault("meta", {})[ROUTER_DIFF_PAIRS_META_KEY] = [
            [p, n] for p, n in record
        ]
    return project_data


def _remove_router_netclasses(project_data: dict) -> None:
    """Drop the classes / assignments a previous kct pass declared (#6390)."""
    classes, nets = router_netclass_provenance(project_data)
    meta = project_data.get("meta")
    if isinstance(meta, dict):
        meta.pop(ROUTER_NETCLASSES_META_KEY, None)
    settings = project_data.get("net_settings")
    if not isinstance(settings, dict):
        return
    if classes and isinstance(settings.get("classes"), list):
        settings["classes"] = [
            c for c in settings["classes"] if not (isinstance(c, dict) and c.get("name") in classes)
        ]
    assignments = settings.get("netclass_assignments")
    if nets and isinstance(assignments, dict):
        for net in nets:
            assignments.pop(net, None)


def _authored_netclass_members(project_data: dict, net_names: list[str]) -> set[str]:
    """Nets an authored pattern or explicit assignment already classifies.

    Raises :class:`~kicad_tools.core.project_clearance.ProjectClearanceError`
    when the authored netclasses cannot be evaluated safely (an unsupported
    pattern, a malformed block): kct must not guess which nets those match.
    """
    from kicad_tools.core.netclass_diagnostics import diagnose_netclasses
    from kicad_tools.core.project_clearance import resolve_project_clearances

    resolve_project_clearances(project_data, net_names)
    settings = project_data.get("net_settings") or {}
    members: set[str] = set()
    assignments = settings.get("netclass_assignments")
    if isinstance(assignments, dict):
        members.update(net for net, value in assignments.items() if value)
    report = diagnose_netclasses(project_data, list(net_names))
    for row in report["patterns"]:
        members.update(row.get("matches") or [])
    return members


def apply_router_netclasses(
    project_data: dict,
    net_class_map: Mapping[str, NetClassRouting],
    net_names: Iterable[str] | None = None,
) -> dict:
    """Declare the router's stricter net classes in a project (Issue #6390).

    ``kct route`` routes each net at its :class:`NetClassRouting` clearance
    (``Power`` 0.2 mm, ``Clock`` 0.2 mm, ...), but until #6390 those classes
    only reached ``net_class_map.json``: the routed ``.kicad_pro`` declared
    ``Default`` alone, so neither ``kicad-cli pcb drc`` nor ``kct check``'s
    ``netclass_clearance_copper`` rule held the copper to them.

    This writes every router class whose clearance exceeds the project's
    ``Default`` clearance into ``net_settings.classes`` (a clone of
    ``Default`` with ``name`` and ``clearance`` overridden) and assigns its
    nets by exact name in ``netclass_assignments`` -- never patterns, whose
    native-verified subset cannot express names like ``+5V`` -- in the shape
    the block's schema reads (schema 3 strings, otherwise arrays; see
    :func:`~kicad_tools.core.project_clearance.netclass_assignment_value`).

    Authored intent always wins:

    * the classes / assignments a previous kct pass declared (recorded under
      ``meta.kct_router_netclasses``) are replaced, everything else is left
      alone;
    * a net an authored pattern or assignment already classifies keeps its
      authored class;
    * a router class whose name collides with an authored class of a
      different clearance is skipped;
    * a project whose authored netclasses cannot be evaluated (e.g. an
      unsupported pattern) gets no router classes at all.

    Each skip prints a warning on stderr.  Deterministic: the same inputs
    always produce the same project (classes and nets sorted by name).

    Args:
        project_data: Parsed ``.kicad_pro`` (mutated in place), normally
            already merged by :func:`merge_project_rules` so ``Default``
            carries the fabrication floor.
        net_class_map: The router's effective ``{net_name: NetClassRouting}``
            map.  An empty map removes the previously declared classes.
        net_names: The board's nets; map entries for other nets are ignored.
            ``None`` uses every map key.

    Returns:
        The same ``project_data`` dict.
    """
    _remove_router_netclasses(project_data)
    names = set(net_class_map) if net_names is None else set(net_names) & set(net_class_map)
    names.discard("")
    settings = project_data.setdefault("net_settings", {})
    classes = settings.setdefault("classes", [])
    default_cls = next(
        (c for c in classes if isinstance(c, dict) and c.get("name") == "Default"), None
    )
    floor = default_cls.get("clearance") if isinstance(default_cls, dict) else None
    if isinstance(floor, bool) or not isinstance(floor, (int, float)):
        floor = 0.0

    wanted: dict[str, float] = {}
    members: dict[str, list[str]] = {}
    for net in sorted(names):
        routing = net_class_map[net]
        clearance = float(getattr(routing, "clearance", 0.0) or 0.0)
        class_name = getattr(routing, "name", "")
        if not class_name or class_name == "Default" or clearance <= floor + _CLEARANCE_EPSILON_MM:
            continue
        # A class name the map gives two clearances keeps the stricter one.
        wanted[class_name] = max(wanted.get(class_name, 0.0), clearance)
        members.setdefault(class_name, []).append(net)
    if not wanted:
        return project_data

    try:
        authored = _authored_netclass_members(project_data, sorted(names))
    except ValueError as exc:
        _warn(
            f"router net classes not declared in the project ({', '.join(sorted(wanted))}): "
            f"its authored netclasses cannot be evaluated safely ({exc})"
        )
        return project_data

    authored_classes = {
        c.get("name"): c for c in classes if isinstance(c, dict) and isinstance(c.get("name"), str)
    }
    declared: list[str] = []
    assigned: list[str] = []
    assignments = settings.get("netclass_assignments")
    if not isinstance(assignments, dict):
        assignments = {}
    from kicad_tools.core.project_clearance import netclass_assignment_value

    new_classes: list[dict] = []
    for class_name in sorted(wanted):
        clearance = wanted[class_name]
        existing = authored_classes.get(class_name)
        if existing is not None:
            value = existing.get("clearance")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or abs(value - clearance) > _CLEARANCE_EPSILON_MM
            ):
                _warn(
                    f"router net class {class_name!r} ({clearance:g} mm) not declared: the "
                    f"project already defines {class_name!r} with clearance {value!r}"
                )
                continue
        nets = [n for n in members[class_name] if n not in authored]
        skipped = [n for n in members[class_name] if n in authored]
        if skipped:
            _warn(
                f"router net class {class_name!r} ({clearance:g} mm) not applied to "
                f"{', '.join(skipped)}: an authored netclass already covers "
                f"{'it' if len(skipped) == 1 else 'them'}"
            )
        if not nets:
            continue
        if existing is None:
            entry = dict(default_cls) if isinstance(default_cls, dict) else {}
            entry.pop("priority", None)
            entry["name"] = class_name
            entry["clearance"] = clearance
            new_classes.append(entry)
            declared.append(class_name)
        for net in nets:
            assignments[net] = netclass_assignment_value(settings, class_name)
            assigned.append(net)

    if not assigned:
        return project_data
    classes.extend(new_classes)
    settings["netclass_assignments"] = dict(sorted(assignments.items()))
    project_data.setdefault("meta", {})[ROUTER_NETCLASSES_META_KEY] = {
        "classes": declared,
        "assignments": sorted(assigned),
    }
    return project_data


#: ``design_settings`` keys whose values kct's fabrication-floor merge owns.
#: :func:`_owned_digest` ignores them, so a reviewed script that adjusts a
#: floor after kct wrote the project (board 04's paid-drill annular ring)
#: does not turn kct's own artifact into "authored" content.
def _floor_owned_view(project_data: dict) -> dict:
    view = copy.deepcopy(project_data)
    meta = view.get("meta")
    if isinstance(meta, dict):
        meta.pop(SIDECAR_DIGEST_META_KEY, None)
    settings = view.get("board", {}).get("design_settings")
    if isinstance(settings, dict):
        for block, keys in (
            ("rules", _PROJECT_RULE_KEYS),
            ("defaults", _PROJECT_DEFAULT_KEYS),
            ("rule_severities", tuple(_NON_BLOCKING_SEVERITIES)),
        ):
            section = settings.get(block)
            if isinstance(section, dict):
                for key in keys:
                    section.pop(key, None)
    classes = view.get("net_settings", {}).get("classes")
    if isinstance(classes, list):
        for cls in classes:
            if isinstance(cls, dict) and cls.get("name") == "Default":
                for key in _NETCLASS_SIGNATURE_KEYS:
                    cls.pop(key, None)
    return view


def _owned_digest(project_data: dict) -> str:
    canonical = json.dumps(_floor_owned_view(project_data), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp_kct_project(project_data: dict) -> dict:
    """Record that kct wrote ``project_data`` (Issue #6390, AC5).

    Stores a digest of everything except the fabrication floors kct itself
    manages under ``meta.kct_sidecar_sha256``.  A later renamed-output route
    may then replace the file even though it no longer equals its source:
    :func:`is_kct_owned_project` proves nobody changed anything but kct's own
    floors since.  A hand edit anywhere else (a netclass, a pattern, a
    severity of your own) breaks the digest and the conflict check refuses
    the overwrite exactly as before.

    Behaviour change: an edit confined to the excluded keys -- the
    ``rules.min_*`` keys of ``_PROJECT_RULE_KEYS``, the ``defaults`` keys of
    ``_PROJECT_DEFAULT_KEYS``, the two non-blocking ``rule_severities`` and
    ``Default``'s clearance/track/via signature (``_NETCLASS_SIGNATURE_KEYS``)
    -- on a renamed output no longer conflicts: it is silently overwritten
    from the source on the next route, *even when the edit tightened a
    floor*.  Make such a change in the source project (or the reviewed rules
    the source carries), not in the renamed output.
    """
    meta = project_data.setdefault("meta", {})
    # Re-insert last so the key order (and so the bytes) never depends on
    # whether an earlier pass already stamped the file.
    meta.pop(SIDECAR_DIGEST_META_KEY, None)
    meta[SIDECAR_DIGEST_META_KEY] = _owned_digest(project_data)
    return project_data


def is_kct_owned_project(project_data: object) -> bool:
    """True when ``project_data`` carries a valid :func:`stamp_kct_project` digest."""
    if not isinstance(project_data, dict):
        return False
    meta = project_data.get("meta")
    digest = meta.get(SIDECAR_DIGEST_META_KEY) if isinstance(meta, dict) else None
    return isinstance(digest, str) and digest == _owned_digest(project_data)


def _strip_managed_dru_blocks(text: str) -> str:
    """``.kicad_dru`` content outside every kct-managed block, whitespace-normalised."""
    from .dru_generator import DRU_FLOORS_BLOCK_BEGIN, DRU_FLOORS_BLOCK_END
    from .keepout_dru import DRU_KEEPOUT_BLOCK_BEGIN, DRU_KEEPOUT_BLOCK_END
    from .netclass_dru import (
        DRU_NETCLASS_BLOCK_BEGIN,
        DRU_NETCLASS_BLOCK_END,
        DRU_PAIR_GAP_BLOCK_BEGIN,
        DRU_PAIR_GAP_BLOCK_END,
    )

    for begin, end in (
        (DRU_FLOORS_BLOCK_BEGIN, DRU_FLOORS_BLOCK_END),
        (DRU_KEEPOUT_BLOCK_BEGIN, DRU_KEEPOUT_BLOCK_END),
        (DRU_NETCLASS_BLOCK_BEGIN, DRU_NETCLASS_BLOCK_END),
        (DRU_PAIR_GAP_BLOCK_BEGIN, DRU_PAIR_GAP_BLOCK_END),
    ):
        text = re.sub(re.escape(begin) + r".*?" + re.escape(end), "", text, flags=re.DOTALL)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and line != "(version 1)")


_NET_NAME_RE = re.compile(r'\(net\s+(?:\d+\s+)?"((?:[^"\\]|\\.)*)"')


def board_net_names(pcb_path: str | Path) -> set[str] | None:
    """Net names declared or referenced in a ``.kicad_pcb`` (``None`` if unreadable).

    A text scan, not a parse: it covers both the legacy ``(net N "name")``
    and the KiCad 10 ``(net "name")`` spellings and is cheap on large boards.
    """
    try:
        text = Path(pcb_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {
        match.group(1).replace('\\"', '"').replace("\\\\", "\\")
        for match in _NET_NAME_RE.finditer(text)
    }


def generate_project_dru(
    rules: DesignRules,
    project_data: dict,
    *,
    manufacturer_id: str = "",
    net_classes: Sequence[NetClassRouting] | None = None,
    preserve_board_rules: str | None = None,
) -> str:
    """Render the factory-floor ``.kicad_dru`` text, honouring project minima.

    Custom DRU constraints override project minima during native zone fill
    and DRC, so the stricter authored project minima must be carried into
    BOTH representations (#5023, #6191).  The mode comes from the
    ``KCT_PRESERVE_BOARD_RULES`` text variable (see
    :func:`preserve_board_rules_mode`):

    * **unset / empty (default)** -- each DRU scalar floor (track width,
      clearance, via drill/diameter, annular ring, copper-to-edge) becomes
      ``max(profile, native project minimum)``.  A ``Reviewed clearance -
      <class>`` rule is emitted only for netclasses whose clearance is
      strictly greater than that clearance floor; a rule at or below the
      floor is redundant and, because later matching KiCad rules win, could
      only shadow more specific rules.
    * **``1`` / ``true``** -- the same floors, plus one ``Reviewed clearance``
      rule for every netclass (ascending clearance, so the stricter class
      wins where two classes meet), exactly as #5023 shipped.
    * **``0`` / ``false``** -- plain profile floors, no reviewed rules.

    Args:
        rules: Manufacturer design rules.
        project_data: Parsed ``.kicad_pro`` data (normally already merged by
            :func:`merge_project_rules`); not mutated.
        manufacturer_id: Manufacturer label for the DRU header.
        net_classes: Optional net-class routing configs forwarded to
            :func:`~kicad_tools.manufacturers.dru_generator.generate_dru`.
        preserve_board_rules: Caller override for the project's
            ``KCT_PRESERVE_BOARD_RULES``; see :func:`merge_project_rules`.

    Returns:
        The ``.kicad_dru`` text.
    """
    from .dru_generator import generate_dru

    mode = preserve_board_rules_mode(project_data, preserve_board_rules)
    dru_rules = rules
    preserved_clearances = ""
    if mode != PRESERVE_MODE_OFF:
        native = project_data.get("board", {}).get("design_settings", {}).get("rules", {})

        def native_floor(key: str) -> float:
            value = native.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0
            return value

        mapping = {
            "min_trace_width_mm": "min_track_width",
            "min_clearance_mm": "min_clearance",
            "min_via_drill_mm": "min_via_hole",
            "min_via_diameter_mm": "min_via_diameter",
            "min_annular_ring_mm": "min_via_annular_width",
            "min_copper_to_edge_mm": "min_copper_edge_clearance",
        }
        dru_rules = replace(
            rules,
            **{
                field: max(getattr(rules, field), native_floor(key))
                for field, key in mapping.items()
            },
        )
        # Later matching rules win in KiCad. Ascending order preserves
        # the stricter class when two differently classified nets meet.
        from .netclass_dru import netclass_condition

        router_classes = set(router_netclass_provenance(project_data)[0])
        coupled_pairs = recorded_router_coupled_pairs(project_data)
        classes = project_data.get("net_settings", {}).get("classes", [])
        for cls in sorted(classes, key=lambda c: c.get("clearance", 0)):
            clearance = max(dru_rules.min_clearance_mm, cls.get("clearance", 0))
            if mode == PRESERVE_MODE_STRICTER and clearance <= dru_rules.min_clearance_mm:
                continue
            # Issue #6390: a router-declared class does not govern pad pairs
            # inside one footprint (see ``netclass_dru.netclass_condition``).
            # Issue #6429: plus the router's recorded coupled pairs.
            condition = netclass_condition(
                cls["name"], router=cls["name"] in router_classes, extra_pairs=coupled_pairs
            )
            preserved_clearances += (
                f"\n(rule {json.dumps('Reviewed clearance - ' + cls['name'])}\n"
                f"  (condition {json.dumps(condition)})\n"
                f"  (constraint clearance (min {clearance}mm)))\n"
            )

    return (
        generate_dru(dru_rules, manufacturer_name=manufacturer_id, net_classes=net_classes)
        + preserved_clearances
    )


def apply_netclass_dru_rules(
    dru_text: str,
    rules: DesignRules,
    project_data: dict,
    *,
    preserve_board_rules: str | None = None,
) -> str:
    """Merge the router net-class managed block into ``.kicad_dru`` text (#6390).

    The block carries rules only under ``KCT_PRESERVE_BOARD_RULES=0``; in the
    other modes :func:`generate_project_dru`'s reviewed rules already cover
    the router's classes and a stale block is removed.  See
    :mod:`kicad_tools.manufacturers.netclass_dru`.
    """
    from .netclass_dru import merge_netclass_block, netclass_block_rules

    mode = preserve_board_rules_mode(project_data, preserve_board_rules)
    block_rules = netclass_block_rules(
        project_data, rules.min_clearance_mm, mode_off=mode == PRESERVE_MODE_OFF
    )
    return (
        merge_netclass_block(dru_text, block_rules, recorded_router_coupled_pairs(project_data))
        or dru_text
    )


@lru_cache(maxsize=1)
def _installed_kicad_cli_version() -> str | None:
    """Resolve the installed ``kicad-cli``'s raw version string, or ``None``.

    Cached: ``write_drc_constraints`` runs once per exported board (and the
    export/route/check paths each call it), so an uncached lookup would spawn
    a ``kicad-cli version`` subprocess per call for a value that cannot change
    within a process.  Tests that stub the lookup must call
    ``_installed_kicad_cli_version.cache_clear()``.

    Returns ``None`` -- never raises -- when ``kicad-cli`` is absent or
    unreadable: a version we cannot determine must not produce a warning.
    """
    try:
        from kicad_tools.cli.runner import find_kicad_cli
        from kicad_tools.export.gerber import get_kicad_cli_version

        cli = find_kicad_cli()
        if cli is None:
            return None
        return get_kicad_cli_version(cli)
    except Exception:  # pragma: no cover - defensive; this is a warning path
        return None


def _warn_if_smd_pad_clearance_is_inert(rules: DesignRules, dru_path: Path) -> None:
    """Warn when the just-written ``SMD Pad Clearance`` rule cannot fire (#5724).

    The rule is emitted unconditionally whenever the profile declares a
    different-net SMD pad floor, but KiCad 10.0.0/10.0.1 do not give a pad its
    parent footprint's ``Reference``, so the rule's scope is permanently false
    *and no ``drc_rule_error`` is raised* -- ``kicad-cli pcb drc`` simply
    reports a clean board.  Without this warning a user on an affected KiCad
    gets a green DFM verdict from a sidecar ``kct`` just wrote for them.

    Warning-only and best-effort by design: the sidecar content is unchanged,
    and an absent/unparseable ``kicad-cli`` stays silent rather than guessing.
    """
    if rules.min_smd_pad_clearance_mm is None:
        # The rule is not emitted at all for this profile/tier -- nothing to
        # warn about (mirrors the emission gate in ``generate_dru``).
        return

    from .dru_generator import smd_pad_clearance_inert_reason

    reason = smd_pad_clearance_inert_reason(_installed_kicad_cli_version())
    if reason is None:
        return
    logger.warning(
        "%s: %s (floor: %.4g mm different-net SMD pad clearance)",
        dru_path,
        reason,
        rules.min_smd_pad_clearance_mm,
    )


def write_drc_constraints(
    pcb_path: str | Path,
    rules: DesignRules,
    *,
    manufacturer_id: str = "",
    layers: int | None = None,
    copper_oz: float | None = None,
    write_dru: bool = True,
    net_classes: Sequence[NetClassRouting] | None = None,
    source_pcb_path: str | Path | None = None,
    preserve_board_rules: str | None = None,
    router_net_classes: Mapping[str, NetClassRouting] | None = None,
    router_coupled_pairs: Iterable[tuple[str, str]] | None = None,
    router_pair_gaps: Iterable[tuple[str, str, float]] | None = None,
    _board_path: str | Path | None = None,
) -> list[Path]:
    """Emit DRC-constraint sources next to a routed ``.kicad_pcb``.

    Writes (or updates) ``<board>.kicad_pro`` so ``kicad-cli pcb drc``
    auto-loads the relaxed built-in minimums, and -- by default -- a
    companion ``<board>.kicad_dru`` for the rule families the project
    schema can't express.  An existing ``.kicad_pro`` is preserved and
    only the constraint/severity entries are merged; stricter authored
    minima win unless the project opts out with
    ``KCT_PRESERVE_BOARD_RULES=0`` (see :func:`merge_project_rules`, #6191).

    The ``.kicad_dru`` half has matching preserve-and-merge semantics
    (Issue #4600): the tier-floor rules land inside a sentinel-delimited
    managed block (see
    :func:`~kicad_tools.manufacturers.dru_generator.merge_dru_floors`) and
    re-emitting replaces only that block.  Hand-written custom rules and
    the ``kct creepage export-rules`` managed block (#4508) outside the
    fab-floors markers survive verbatim; a pre-existing user-owned file
    is never clobbered -- the block is merged in alongside its content.

    After writing the ``.kicad_dru`` this logs a ``WARNING`` when the
    locally installed ``kicad-cli`` is below
    :data:`~kicad_tools.manufacturers.dru_generator.SMD_PAD_CLEARANCE_MIN_KICAD_VERSION`
    and the profile declares a different-net SMD pad floor: KiCad
    10.0.0/10.0.1 evaluate the emitted ``SMD Pad Clearance`` rule as
    permanently false *without* raising a rule error, so a native DRC run
    would report a clean board (Issue #5724).  The emitted content is
    identical either way -- this is a diagnostic only, and it stays silent
    when ``kicad-cli`` cannot be located or its version cannot be parsed.

    The ``.kicad_dru`` also gets one explicit ``intersectsArea`` disallow
    rule per keepout rule area on the board, in its own managed block
    (Issue #6039): ``kicad-cli`` 10.0.1 does not enforce rule areas on its
    own.  See :mod:`kicad_tools.manufacturers.keepout_dru`.

    Router net classes (Issue #6390): when ``router_net_classes`` is given,
    the router's classes stricter than ``Default`` are declared in the
    ``.kicad_pro`` with exact-name ``netclass_assignments`` (see
    :func:`apply_router_netclasses`), so both DRC engines enforce them.  The
    ``.kicad_dru`` gets the matching ``NetClass`` clearance rules -- through
    the reviewed rules of :func:`generate_project_dru`, or under
    ``KCT_PRESERVE_BOARD_RULES=0`` through their own managed block (see
    :mod:`kicad_tools.manufacturers.netclass_dru`).  ``None`` keeps whatever
    a previous route declared.

    Router coupled pairs (Issue #6429): ``router_coupled_pairs`` lists the
    ``(P, N)`` pairs the router coupled-routed.  Those KiCad's
    ``isCoupledDiffPair()`` does not recognise by name are recorded under
    ``meta.kct_router_diff_pairs`` (:func:`apply_router_coupled_pairs`) and
    added, by exact net name, to the router-class clearance exemption, so
    both engines skip their intra-pair gap.  ``None`` keeps whatever a
    previous route recorded.

    Router pair gaps (Issue #6443): ``router_pair_gaps`` lists ``(P, N,
    gap_mm)`` for the pairs the router coupled, each gap resolved by
    :meth:`~kicad_tools.router.rules.DesignRules.pair_gap` -- the value the
    router laid the pair at.  Each pair KiCad recognises by name gets one
    native ``diff_pair_gap (min ..)`` rule in its own managed block (see
    :func:`~kicad_tools.manufacturers.netclass_dru.pair_gap_block_rules`);
    an empty list removes a stale block, and ``None`` leaves whatever a
    previous route wrote.

    The written ``.kicad_pro`` is stamped (:func:`stamp_kct_project`).  On a
    renamed output an existing destination is replaced only when it equals
    the source, equals the merged result, or still carries a valid kct stamp
    (nothing but kct's own floors changed since kct wrote it); an existing
    ``.kicad_dru`` only when it differs from the source in kct-managed blocks
    alone.  Anything else is a ``DRC sidecar conflict``.

    Args:
        pcb_path: Path to the routed board.
        router_net_classes: The router's effective ``{net: NetClassRouting}``
            map (Issue #6390); see above.
        router_coupled_pairs: The router's coupled-routed ``(P, N)`` pairs
            (Issue #6429); see above.
        router_pair_gaps: The router's ``(P, N, gap_mm)`` coupled-pair gaps
            (Issue #6443); see above.
        preserve_board_rules: Caller override for the project's
            ``KCT_PRESERVE_BOARD_RULES`` text variable (same spellings),
            applied to both sidecars without being written into the project;
            ``None`` (default) reads the project.  See
            :func:`merge_project_rules`.
        _board_path: Internal -- board to read keepout rule areas from when
            ``pcb_path`` is a staged sibling with no ``.kicad_pcb`` beside it.
        source_pcb_path: Original board when routing to a renamed destination.
            Copy authored project/DRU before merging floors; reject conflicting
            destination files before writing either sidecar. Source is read-only.
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
    if source_pcb_path is not None and Path(source_pcb_path).resolve() != pcb_path.resolve():
        # Render from authored source in isolation, then validate BOTH destination
        # sidecars before changing either. Existing source copies and identical
        # prior outputs are accepted; unrelated destination content is a conflict.
        source = Path(source_pcb_path)
        suffixes = [".kicad_pro", ".kicad_dru"] if write_dru else [".kicad_pro"]
        with tempfile.TemporaryDirectory(prefix="kct-route-rules-") as temporary:
            staged = Path(temporary) / pcb_path.name
            for suffix in suffixes:
                authored, destination = source.with_suffix(suffix), pcb_path.with_suffix(suffix)
                if authored.exists():
                    if destination.exists() and destination.samefile(authored):
                        raise ValueError(
                            f"DRC sidecar conflict: {destination} aliases source {authored}"
                        )
                    if suffix == ".kicad_pro":
                        data = json.loads(authored.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError(f"Invalid source project: {authored}")
                    shutil.copyfile(authored, staged.with_suffix(suffix))
                elif destination.exists():
                    shutil.copyfile(destination, staged.with_suffix(suffix))
            rendered = write_drc_constraints(
                staged,
                rules,
                manufacturer_id=manufacturer_id,
                layers=layers,
                copper_oz=copper_oz,
                write_dru=write_dru,
                net_classes=net_classes,
                preserve_board_rules=preserve_board_rules,
                router_net_classes=router_net_classes,
                router_coupled_pairs=router_coupled_pairs,
                router_pair_gaps=router_pair_gaps,
                _board_path=pcb_path,
            )
            for result in rendered:
                authored = source.with_suffix(result.suffix)
                destination = pcb_path.with_suffix(result.suffix)
                if authored.exists() and destination.exists():
                    read = (
                        (lambda p: json.loads(p.read_text(encoding="utf-8")))
                        if result.suffix == ".kicad_pro"
                        else (lambda p: p.read_bytes())
                    )
                    current = read(destination)
                    if current in (read(authored), read(result)):
                        continue
                    # Issue #6390 (AC5): a destination kct itself wrote may be
                    # regenerated even when an earlier route derived it from a
                    # different source -- but only when nothing outside kct's
                    # own floors / managed blocks was edited since.
                    if result.suffix == ".kicad_pro":
                        if is_kct_owned_project(current):
                            continue
                    elif _strip_managed_dru_blocks(
                        destination.read_text(encoding="utf-8", errors="replace")
                    ) == _strip_managed_dru_blocks(
                        authored.read_text(encoding="utf-8", errors="replace")
                    ):
                        continue
                    raise ValueError(
                        f"DRC sidecar conflict: {destination} differs from source {authored} and its merged rules"
                    )
            for result in rendered:
                shutil.copyfile(result, pcb_path.with_suffix(result.suffix))
            return [pcb_path.with_suffix(result.suffix) for result in rendered]
    project_name = pcb_path.stem
    pro_path = pcb_path.with_suffix(".kicad_pro")
    written: list[Path] = []

    if pro_path.exists():
        try:
            project_data = json.loads(pro_path.read_text(encoding="utf-8"))
            merge_project_rules(project_data, rules, preserve_board_rules=preserve_board_rules)
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

    board = Path(_board_path) if _board_path is not None else pcb_path
    if router_net_classes is not None:
        apply_router_netclasses(
            project_data,
            router_net_classes,
            board_net_names(board) if board.exists() else None,
        )
    if router_coupled_pairs is not None:
        apply_router_coupled_pairs(
            project_data,
            router_coupled_pairs,
            board_net_names(board) if board.exists() else None,
        )
    stamp_kct_project(project_data)
    pro_path.write_text(json.dumps(project_data, indent=2), encoding="utf-8")
    written.append(pro_path)

    if write_dru:
        from .dru_generator import merge_dru_floors

        dru_path = pcb_path.with_suffix(".kicad_dru")
        try:
            existing_dru: str | None = (
                dru_path.read_text(encoding="utf-8") if dru_path.exists() else None
            )
        except OSError:
            # Unreadable existing file -- nothing recoverable to preserve;
            # write cleanly (mirrors the corrupt-``.kicad_pro`` fallback).
            existing_dru = None
        from .keepout_dru import apply_keepout_rules

        merged_dru = merge_dru_floors(
            existing_dru,
            generate_project_dru(
                rules,
                project_data,
                manufacturer_id=manufacturer_id,
                net_classes=net_classes,
                preserve_board_rules=preserve_board_rules,
            ),
            path=dru_path,
        )
        # Issue #6039: explicit per-rule-area keepout rules, since headless
        # kicad-cli does not enforce rule areas on its own.
        if board.exists():
            merged_dru = apply_keepout_rules(merged_dru, board) or merged_dru
        # Issue #6390: the router's classes under KCT_PRESERVE_BOARD_RULES=0.
        merged_dru = apply_netclass_dru_rules(
            merged_dru, rules, project_data, preserve_board_rules=preserve_board_rules
        )
        # Issue #6443: one native diff_pair_gap rule per router-coupled pair.
        if router_pair_gaps is not None:
            from .netclass_dru import merge_pair_gap_block, pair_gap_block_rules

            gap_rules = pair_gap_block_rules(
                router_pair_gaps, board_net_names(board) if board.exists() else None
            )
            merged = merge_pair_gap_block(merged_dru, gap_rules)
            merged_dru = merged if merged is not None else merged_dru
        dru_path.write_text(merged_dru, encoding="utf-8")
        written.append(dru_path)
        # The sidecar is on disk now; tell the user if the engine they have
        # installed will silently ignore its SMD pad floor (#5724).
        _warn_if_smd_pad_clearance_is_inert(rules, dru_path)

    return written
