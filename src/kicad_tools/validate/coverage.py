"""Per-check coverage states for ``kct check`` -- "unknown is not pass" (Issue #5946).

A check category that reports zero findings can mean three different things:

``checked``
    The check ran against inputs it could actually evaluate.  Zero findings
    really is a pass.

``skipped:<reason>``
    The check did not apply, by choice or by construction: deselected with
    ``--only`` / ``--skip``, an opt-in check that was not requested, or nothing
    to verify (for example no declared ampacity targets, or no differential
    pairs on the board).  Not a claim either way, and not a gap.

``unknown:<reason>``
    The check *should* have something to say about this board but could not
    evaluate it: a missing input (no net-class map while the board has
    differential-pair nets, no schematic for the field lint), unusable
    evidence (copper zones with no committed fill, so the zone-clearance rules
    measured nothing), or an unreadable sidecar.  Zero findings here is **not**
    a pass.

Each entry also carries ``blocking``: whether the category guards fabrication
or electrical correctness (it can emit error-severity findings).  Sign-off
(``kct readiness`` / ``/kct:tapeout``) refuses to sign off while any
*blocking* category is ``unknown``.  The ``kct check`` exit code itself is
unchanged: coverage is reported, and sign-off decides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .checker import DRCChecker

CHECKED = "checked"
SKIPPED = "skipped"
UNKNOWN = "unknown"

# Categories that can emit error-severity (fabrication / electrical) findings.
# Advisory categories (silkscreen, pad_grid, pin1_marker, doc_drift,
# sch_fields, ...) are deliberately absent: an unknown advisory check is
# reported but never blocks sign-off.
BLOCKING_CATEGORIES: frozenset[str] = frozenset(
    {
        "ampacity",
        "path_ampacity",
        "clearance",
        "physical_copper_gap",
        "connectivity",
        "segment_zone",
        "via_zone",
        "copper_sliver",
        "courtyard_overlap",
        "diffpair_clearance_intra",
        "diffpair_length_skew",
        "diffpair_routing_continuity",
        "dimensions",
        "edge",
        "impedance",
        "match_group_length_skew",
        "netclass_clearance_copper",
        "netclass_floor",
        "netlist",
        "solder_mask",
        "mask_to_copper",
        "via_in_pad",
        "zones",
    }
)


@dataclass(frozen=True)
class Coverage:
    """Coverage state of one check category."""

    state: str
    reason: str | None = None
    blocking: bool = False

    @property
    def status(self) -> str:
        """``checked`` or ``<state>:<reason>`` -- the compact form."""
        if self.state == CHECKED or not self.reason:
            return self.state
        return f"{self.state}:{self.reason}"

    @property
    def is_blocking_unknown(self) -> bool:
        return self.state == UNKNOWN and self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "state": self.state,
            "reason": self.reason,
            "blocking": self.blocking,
        }


def checked(category: str) -> Coverage:
    return Coverage(CHECKED, None, category in BLOCKING_CATEGORIES)


def skipped(category: str, reason: str) -> Coverage:
    return Coverage(SKIPPED, reason, category in BLOCKING_CATEGORIES)


def unknown(category: str, reason: str) -> Coverage:
    return Coverage(UNKNOWN, reason, category in BLOCKING_CATEGORIES)


def _declares_ampacity(net_class_map: Any) -> bool:
    if not net_class_map:
        return False
    return any(getattr(nc, "target_ampacity", None) is not None for nc in net_class_map.values())


def _unfilled_pour_zones(checker: DRCChecker) -> int:
    count = 0
    for zone in getattr(checker.pcb, "zones", []) or []:
        if getattr(zone, "keepout", None) is not None:
            continue  # rule area, not copper
        if not getattr(zone, "net_name", ""):
            continue  # unconnected pour: no foreign-net clearance to measure
        if not getattr(zone, "filled_polygons", None):
            count += 1
    return count


def _diff_pair_candidates(checker: DRCChecker) -> int:
    try:
        from kicad_tools.router.diffpair import detect_differential_pairs

        names = {num: net.name for num, net in checker.pcb.nets.items() if net.name}
        return len(detect_differential_pairs(names))
    except Exception:  # noqa: BLE001 - detection is advisory input to coverage only
        return 0


def precheck(
    category: str,
    checker: DRCChecker,
    *,
    pcb_path: Path | None = None,
    sch_path: Path | None = None,
    drc_only: bool = False,
) -> Coverage | None:
    """Coverage for a *selected* category whose inputs make it vacuous.

    Returns ``None`` when the category can evaluate the board normally (the
    caller records ``checked`` once it has run), otherwise the ``skipped`` /
    ``unknown`` state explaining why its result says nothing.  The check is
    still invoked by the caller either way, so findings are unchanged.
    """
    ncm = getattr(checker, "net_class_map", None)

    if category == "ampacity" and not _declares_ampacity(ncm):
        return skipped(category, "no_declared_targets")
    if category == "path_ampacity" and not getattr(checker, "current_path_specs", None):
        return skipped(category, "no_declared_current_paths")
    if (
        category == "physical_copper_gap"
        and getattr(checker, "physical_copper_gap_mm", None) is None
    ):
        return skipped(category, "opt_in:--physical-copper-gap")
    if category == "match_group_length_skew" and ncm is None:
        return skipped(category, "no_net_class_map")
    if category in ("diffpair_length_skew", "diffpair_routing_continuity") and ncm is None:
        pairs = _diff_pair_candidates(checker)
        if pairs:
            return unknown(category, f"needs_input:net_class_map ({pairs} diff-pair net(s))")
        return skipped(category, "no_diff_pairs_detected")
    if category in ("segment_zone", "via_zone"):
        unfilled = _unfilled_pour_zones(checker)
        if unfilled:
            return unknown(category, f"zone_fills_absent ({unfilled} unfilled zone(s))")
    if category == "sch_fields":
        if drc_only:
            return skipped(category, "drc_only")
        if sch_path is None:
            return unknown(category, "needs_input:schematic")
    if category == "doc_drift" and pcb_path is None:
        return unknown(category, "needs_input:pcb_path")
    if category == "netclass_clearance_copper":
        if pcb_path is None:
            return unknown(category, "needs_input:pcb_path")
        pro = pcb_path.with_suffix(".kicad_pro")
        if not pro.is_file():
            return skipped(category, "no_kicad_pro")
        try:
            json.loads(pro.read_text())
        except (OSError, ValueError):
            return unknown(category, "unreadable_kicad_pro")
    if category == "netclass_floor":
        if pcb_path is None:
            return unknown(category, "needs_input:pcb_path")
        pro = pcb_path.with_suffix(".kicad_pro")
        if not pro.is_file():
            return skipped(category, "no_kicad_pro")
        try:
            json.loads(pro.read_text())
        except (OSError, ValueError):
            return unknown(category, "unreadable_kicad_pro")
    return None


def blocking_unknowns(coverage: dict[str, Any]) -> list[str]:
    """Names of blocking categories reported ``unknown`` in a JSON coverage map."""
    names = []
    for name, entry in (coverage or {}).items():
        if isinstance(entry, dict) and entry.get("state") == UNKNOWN and entry.get("blocking"):
            names.append(name)
    return sorted(names)


def coverage_to_dict(coverage: dict[str, Coverage]) -> dict[str, dict[str, Any]]:
    return {name: cov.to_dict() for name, cov in sorted(coverage.items())}
