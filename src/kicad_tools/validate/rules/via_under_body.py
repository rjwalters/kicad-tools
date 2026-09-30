"""Via-under-package-body DRC rule.

A board can pass KiCad's DRC with vias tucked under the body of a QFN, DFN,
SON or LGA package: they clear every pad, sit inside the courtyard (which
KiCad does not check against vias), and are therefore invisible to both the
clearance and via-in-pad rules.  Once the part is assembled those vias
cannot be probed, reworked or inspected, and an untented via under a
bottom-terminated package can wick solder or short to the exposed body.

This rule flags any via whose copper (a disc of ``via.size``) overlaps the
*package body* outline of a selected footprint:

* **Body outline** comes from
  :func:`kicad_tools.geometry.package_body.package_body_polygon` -- the
  ``F.Fab``/``B.Fab`` outline on the footprint's side, falling back to the
  courtyard when no fab outline exists.  Footprints with neither are
  skipped.
* **Footprint selection** defaults to bottom-terminated packages whose
  footprint id matches :data:`DEFAULT_FOOTPRINT_PATTERN` -- the packages
  whose body sits flush on the board, hiding (and potentially shorting to)
  anything beneath it.  Leaded QFPs and BGAs are deliberately **not** in the
  default: vias under a stand-off QFP body (power/ground stitching) and
  dog-bone fan-out under a BGA are routine -- the committed board-07
  LQFP-144 alone carries 48 -- and flagging them would drown the finding.
  Pass a custom ``footprint_pattern`` (e.g.
  ``DEFAULT_FOOTPRINT_PATTERN + "|QFP|BGA"``) or ``include_references`` to
  opt them in.  String patterns are compiled case-insensitively, which is
  why the default anchors ``SON`` as a package token rather than matching
  it inside names like ``Panasonic`` or ``Resonator``.
* **Thermal-pad vias** -- vias on the net of the footprint's own exposed
  pad whose centre lies inside that pad's copper -- are the intended
  exception and are allowed by default (``allow_thermal_pad_vias=True``).
  A via-in-pad finding for them, if the fab cannot do it, still comes from
  the separate ``via_in_pad`` rule.
* Vias that do not reach the footprint's outer copper layer (blind/buried
  vias on the far side) are not "under" the body and are skipped.

Distinct from ``via_in_pad`` (drill inside a same-net SMD pad) and
``courtyard_overlap`` (footprint-vs-footprint).  Severity defaults to
``warning`` and the rule id is classified advisory, so plain ``kct check``
exit codes are unchanged; ``--strict`` blocks on it.  Individual findings are
waivable via ``.kct_waivers.json`` with ``items=[<via ref>, <footprint ref>]``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from statistics import median
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.geometry.package_body import footprint_side, package_body_polygon

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Via

VIA_UNDER_BODY_RULE_ID = "via_under_body"

# Footprint ids (``Lib:Name``) whose package body is checked by default:
# bottom-terminated packages where a hidden via is a real inspection /
# rework / short risk.  Matched case-insensitively with ``re.search`` (so
# ``Package_DFN_QFN``, ``Package_SON:WSON-8...``, ``Package_LGA`` and
# lower-case vendor ids such as ``qfn-16...`` all select).
#
# ``SON`` is anchored as a package token -- start of id or a non-letter,
# up to two prefix letters (W/V/U/X/HU/TD...SON; ``X2SON`` matches via the
# non-letter branch), and no trailing letter.  Unanchored, the
# case-insensitive search would hit ``son`` inside vendor/part names
# (Degson, Panasonic, SeikoEpson, Resonator, Johanson, Winson, Aosong):
# 138 non-bottom-terminated KiCad stock footprints such as crystals,
# resonators and power inductors.  Anchored, the pattern selects exactly
# the same 964 KiCad 10 stock footprints as the old case-sensitive
# ``QFN|DFN|SON|LGA``.
DEFAULT_FOOTPRINT_PATTERN = r"QFN|DFN|LGA|(?:^|[^A-Z])[A-Z]{0,2}SON(?![A-Z])"

# An SMD pad at least this many times the footprint's median copper-pad area
# is treated as an exposed (thermal) pad.
_EXPOSED_PAD_AREA_RATIO = 4.0

# Pad numbers KiCad libraries and vendor footprints use for exposed pads.
_EXPOSED_PAD_NUMBERS = frozenset({"EP", "PAD", "TAB"})


def _has_copper(pad: Pad) -> bool:
    return any(layer.endswith(".Cu") for layer in pad.layers)


def exposed_pads(footprint: Footprint) -> list[Pad]:
    """Return the footprint's exposed / thermal pads.

    A pad qualifies when it is an SMD copper pad and either carries a
    conventional exposed-pad number (``EP``, ``PAD``, ``TAB``) or its area is
    at least ``_EXPOSED_PAD_AREA_RATIO`` times the median copper-pad area of
    the footprint (KiCad's own QFN/DFN libraries number the EP ``N+1``, so
    the size test is what catches them).

    A thermal pad split into several smaller same-net pads (common for
    paste windowing) is also recognised: when the *combined* area of the
    larger-than-median SMD copper pads sharing a (non-zero) net reaches the
    same ratio, every pad in that cluster is returned.
    """
    copper = [p for p in footprint.pads if p.type == "smd" and _has_copper(p)]
    if not copper:
        return []
    areas = [p.size[0] * p.size[1] for p in copper]
    typical = median(areas) if len(copper) >= 3 else None
    threshold = _EXPOSED_PAD_AREA_RATIO * typical if typical is not None and typical > 0 else None
    # Thermal pads split into several same-net pads: group the
    # larger-than-median pads by net and test their combined area.  Ordinary
    # same-net signal pins (e.g. several GND pins) are median-sized, so they
    # never add up to a phantom exposed pad.
    split_ep: set[int] = set()
    if threshold is not None and typical is not None:
        clusters: dict[int, list[int]] = {}
        for i, (pad, area) in enumerate(zip(copper, areas, strict=True)):
            if pad.net_number and area > typical:
                clusters.setdefault(pad.net_number, []).append(i)
        for members in clusters.values():
            if len(members) >= 2 and sum(areas[i] for i in members) >= threshold:
                split_ep.update(members)
    result = []
    for i, (pad, area) in enumerate(zip(copper, areas, strict=True)):
        oversized = threshold is not None and area >= threshold
        if oversized or i in split_ep or pad.number.upper() in _EXPOSED_PAD_NUMBERS:
            result.append(pad)
    return result


class ViaUnderBodyRule(DRCRule):
    """Flag vias whose copper lies under a selected package body.

    Rule IDs generated:
        - via_under_body: a via's copper overlaps the Fab (or courtyard
          fallback) body outline of a selected footprint.
    """

    rule_id = VIA_UNDER_BODY_RULE_ID
    name = "Via Under Package Body"
    description = (
        "Detects vias hidden under the body of bottom-terminated (QFN/DFN/SON/LGA) packages "
        "(not probeable or inspectable after assembly)"
    )

    def __init__(
        self,
        *,
        footprint_pattern: str | re.Pattern[str] | None = DEFAULT_FOOTPRINT_PATTERN,
        include_references: Iterable[str] = (),
        exclude_references: Iterable[str] = (),
        allow_thermal_pad_vias: bool = True,
        fallback_to_courtyard: bool = True,
        severity: str = "warning",
    ) -> None:
        """Configure the rule.

        Args:
            footprint_pattern: Regex searched against the footprint id
                (``Lib:Name``) to select packages to check.  A ``str`` is
                compiled case-insensitively; a pre-compiled ``re.Pattern``
                is used exactly as given (its own flags).  ``None``
                disables pattern selection (only ``include_references`` are
                checked).
            include_references: References always checked, regardless of
                ``footprint_pattern``.
            exclude_references: References never checked.
            allow_thermal_pad_vias: When True, vias on the net of the
                footprint's own exposed pad whose centre is inside that pad
                are not reported.
            fallback_to_courtyard: Use the courtyard as the body outline for
                footprints without a Fab outline.
            severity: Severity of emitted violations (``error``,
                ``warning`` or ``info``).
        """
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"invalid severity {severity!r}")
        if isinstance(footprint_pattern, str):
            footprint_pattern = re.compile(footprint_pattern, re.IGNORECASE)
        self.footprint_pattern = footprint_pattern
        self.include_references = frozenset(include_references)
        self.exclude_references = frozenset(exclude_references)
        self.allow_thermal_pad_vias = allow_thermal_pad_vias
        self.fallback_to_courtyard = fallback_to_courtyard
        self.severity = severity

    def selects(self, footprint: Footprint) -> bool:
        """Return True if ``footprint`` is subject to this rule."""
        ref = footprint.reference
        if ref in self.exclude_references:
            return False
        if ref in self.include_references:
            return True
        return self.footprint_pattern is not None and bool(
            self.footprint_pattern.search(footprint.name)
        )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check every via against the body outline of selected footprints.

        Args:
            pcb: The PCB to check.
            design_rules: Unused; the rule is geometric only.

        Returns:
            DRCResults with one ``via_under_body`` violation per
            (via, footprint) pair.
        """
        del design_rules
        results = DRCResults()
        results.rules_checked = 1

        bodies: list[tuple[Footprint, Any, str, list[tuple[int, Any]]]] = []
        for footprint in pcb.footprints:
            if not self.selects(footprint):
                continue
            if not bodies:
                require_shapely("via-under-body check")
            body, source = package_body_polygon(
                footprint, fallback_to_courtyard=self.fallback_to_courtyard
            )
            if body is None or source is None:
                continue
            bodies.append((footprint, body, source, self._thermal_pads(footprint)))

        if not bodies:
            return results

        from shapely.geometry import Point  # type: ignore[import-untyped]

        for via in pcb.vias:
            radius = via.size / 2.0
            if radius <= DRC_TOLERANCE:
                continue
            center = Point(via.position)
            for footprint, body, source, thermal in bodies:
                copper_layer = f"{footprint_side(footprint)}.Cu"
                if via.layers and copper_layer not in via.layers:
                    continue
                if body.distance(center) >= radius - DRC_TOLERANCE:
                    continue
                if self._is_thermal_pad_via(via, center, thermal):
                    continue
                results.add(self._make_violation(via, footprint, source))

        return results

    def _thermal_pads(self, footprint: Footprint) -> list[tuple[int, Any]]:
        """Return ``(net_number, copper_polygon)`` for each exposed pad."""
        if not self.allow_thermal_pad_vias:
            return []
        from .clearance import _pad_polygon

        pads = []
        for pad in exposed_pads(footprint):
            if pad.net_number == 0:
                continue
            polygon = _pad_polygon(pad, footprint)
            if polygon is not None:
                pads.append((pad.net_number, polygon))
        return pads

    @staticmethod
    def _is_thermal_pad_via(via: Via, center: Any, thermal: list[tuple[int, Any]]) -> bool:
        return any(via.net_number == net and polygon.covers(center) for net, polygon in thermal)

    def _make_violation(self, via: Via, footprint: Footprint, source: str) -> DRCViolation:
        via_ref = f"Via-{via.uuid[:8]}" if via.uuid else "Via"
        net_name = via.net_name or ""
        net_text = f" (net '{net_name}')" if net_name else ""
        outline = "fab outline" if source == "fab" else "courtyard (no fab outline)"
        return DRCViolation(
            rule_id=VIA_UNDER_BODY_RULE_ID,
            severity=self.severity,
            message=(
                f"{via_ref}{net_text} (size {via.size:.3f}mm) is under the package body of {footprint.reference} "
                f"({footprint.name}, {outline}); it cannot be probed, inspected "
                f"or reworked after assembly -- move it outside the body or "
                f"waive via .kct_waivers.json if intentional"
            ),
            location=(round(via.position[0], 3), round(via.position[1], 3)),
            layer=footprint.layer,
            actual_value=round(via.size, 4),
            items=(via_ref, footprint.reference),
            nets=(net_name,) if net_name else (),
        )
