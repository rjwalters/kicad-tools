"""Netclass-default vs manufacturer-floor check (Issue #5875).

The post-routing ``dimension_*`` rules in :mod:`.dimensions` compare
*measured* copper (segment widths, via drill/diameter) against the active
manufacturer profile's floors.  They can only fire once copper exists, so an
**unrouted** import whose ``.kicad_pro`` declares a netclass below the floor
reports nothing at all -- even though every trace and via the netclass will
ever produce is guaranteed to violate the floor.

This was found by the tscircuit interop gate (Issue #5847): every project
``circuit-json-to-kicad`` emits carries exactly one netclass at
``track_width=0.1``, ``clearance=0.1``, ``via_diameter=0.3``,
``via_drill=0.2`` -- below JLCPCB's 0.127 mm trace/space floor on two axes
and implying a 0.05 mm annular ring against a 0.13-0.15 mm minimum.  The
fab-blocking fact is knowable at import time; surfacing it only after a
routing run is the worst possible ordering for an agent importing an
upstream project.

So this module checks the *declaration* rather than the geometry: it reads
``net_settings.classes`` from the sibling ``.kicad_pro`` and compares each
declared class's ``track_width``, ``clearance``, ``via_diameter``,
``via_drill`` and implied annular ring ``(via_diameter - via_drill) / 2``
against the same :class:`~kicad_tools.manufacturers.DesignRules` instance
``DimensionRules`` already reads.  There is deliberately no second floor
table (the router-side ``router/mfr_limits.py`` table serves a different
purpose) -- the floors are whatever ``--mfr`` resolved, so non-JLCPCB
profiles resolve their own.

Five rule ids, all in the fab-blocking Manufacturing category (registered in
``DRCChecker.RULE_CATEGORY``), mirroring the ``dimension_*`` family they
pre-empt:

* ``netclass_track_width`` -- class ``track_width`` below the trace floor
* ``netclass_clearance`` -- class ``clearance`` below the space floor
* ``netclass_via_diameter`` -- class ``via_diameter`` below the via OD floor
* ``netclass_via_drill`` -- class ``via_drill`` below the via drill floor
* ``netclass_annular_ring`` -- implied annular ring below the ring floor

**``track_width`` / ``clearance`` are always errors**: every board has
copper, and the netclass clearance is what KiCad's own DRC enforces, so an
under-floor value licenses illegal spacing on any board.  **The three via
findings are errors only where a via can exist at all** (the question the
issue left open): a single-copper-layer board cannot carry one, so they
degrade to warnings there and are errors on any board with 2+ copper
layers.

Because the floors are whatever ``--mfr`` resolved -- including a board's
own reviewed fabrication overrides, which ``check_cmd`` has already applied
to ``checker.design_rules`` -- a board whose process legitimately permits a
tighter netclass (e.g. board 04's reviewed paid 0.15 mm drill option)
reports nothing.

The check is dispatched as a ``check_cmd``-local category (like
``doc_drift`` / ``sch_fields``) because it needs the PCB *path* to locate
the sibling ``.kicad_pro``; it is deliberately NOT a :class:`DRCChecker`
method and NOT part of ``CHECK_ALL_METHODS``.

Carve-outs, all silent passes (never a crash, never a spurious finding):
a missing ``.kicad_pro``, unreadable/malformed JSON, a project that declares
no ``net_settings.classes`` at all, and a class whose field is absent or
non-numeric.  Findings are sorted by (class name, rule id) so repeated runs
are byte-identical for CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules

RULE_TRACK_WIDTH = "netclass_track_width"
RULE_CLEARANCE = "netclass_clearance"
RULE_VIA_DIAMETER = "netclass_via_diameter"
RULE_VIA_DRILL = "netclass_via_drill"
RULE_ANNULAR_RING = "netclass_annular_ring"

#: Every rule id this module can emit, in declaration order.
NETCLASS_FLOOR_RULE_IDS: tuple[str, ...] = (
    RULE_TRACK_WIDTH,
    RULE_CLEARANCE,
    RULE_VIA_DIAMETER,
    RULE_VIA_DRILL,
    RULE_ANNULAR_RING,
)


def _as_float(value: Any) -> float | None:
    """Coerce a ``.kicad_pro`` netclass field to a float, or ``None``.

    KiCad writes these as JSON numbers, but a hand-edited or
    third-party-generated project can carry a string, ``null``, or omit the
    key entirely.  An uncheckable field is skipped rather than guessed at.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _patterns_for_class(patterns: list[dict[str, str]], class_name: str) -> tuple[str, ...]:
    """Net patterns assigned to ``class_name`` by ``netclass_patterns``.

    Note the JSON key is ``netclass_patterns`` (not ``net_class_patterns``,
    as the issue body wrote it) -- that is the key
    :func:`kicad_tools.core.project_file.get_netclass_patterns` reads.
    """
    matched: list[str] = []
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        if entry.get("netclass") != class_name:
            continue
        pattern = entry.get("pattern")
        if isinstance(pattern, str) and pattern:
            matched.append(pattern)
    return tuple(sorted(set(matched)))


def check_netclass_floor(
    pcb_path: Path | None,
    design_rules: DesignRules,
    copper_layers: int = 2,
) -> DRCResults:
    """Compare ``.kicad_pro`` netclass defaults against the mfr floors.

    Args:
        pcb_path: Path of the ``.kicad_pcb`` under check.  The sibling
            ``.kicad_pro`` is derived from it; the PCB itself is never
            parsed here.  ``None`` makes this a silent no-op so library
            callers that only exercise PCB-object-scoped checks are
            unaffected.
        design_rules: Resolved design rules from the active ``--mfr``
            profile -- the same instance
            :class:`~kicad_tools.validate.rules.dimensions.DimensionRules`
            reads, so there is exactly one floor table.
        copper_layers: Number of copper layers on the board.  A board with
            fewer than 2 cannot carry a via at all, so the three via
            findings degrade to warnings there.

    Returns:
        DRCResults containing netclass-floor violations.
    """
    results = DRCResults()
    for rule_id in NETCLASS_FLOOR_RULE_IDS:
        results.rules_checked_by_rule[rule_id] = 1
    results.rules_checked = len(NETCLASS_FLOOR_RULE_IDS)

    if pcb_path is None:
        return results

    classes, patterns = _load_netclasses(pcb_path)
    if not classes:
        return results

    # Severity policy for the three via findings (the question the issue
    # left open).  ``track_width`` / ``clearance`` are always errors: every
    # board has copper, and an under-floor netclass clearance is what KiCad
    # itself enforces, so it licenses illegal spacing on any board.  A via
    # declaration is fab-blocking only where a via can exist at all -- a
    # single-copper-layer board cannot carry one, so those three degrade to
    # warnings there and are errors on any board with 2+ copper layers.
    if copper_layers < 2:
        via_severity = "warning"
        via_note = " (1-layer board: no via can exist -- advisory)"
    else:
        via_severity = "error"
        via_note = ""

    findings: list[tuple[tuple[str, str], DRCViolation]] = []
    for netclass in classes:
        if not isinstance(netclass, dict):
            continue
        name = netclass.get("name")
        if not isinstance(name, str) or not name:
            continue

        items = (f"netclass:{name}", *_patterns_for_class(patterns, name))

        track_width = _as_float(netclass.get("track_width"))
        clearance = _as_float(netclass.get("clearance"))
        via_diameter = _as_float(netclass.get("via_diameter"))
        via_drill = _as_float(netclass.get("via_drill"))

        # Implied annular ring -- identical arithmetic to
        # ``DimensionRules._check_via_dimensions``, applied to the declared
        # (diameter, drill) pair instead of a placed via.
        annular_ring: float | None = None
        if via_diameter is not None and via_drill is not None:
            annular_ring = (via_diameter - via_drill) / 2

        # (rule_id, field label, declared value, floor, severity)
        candidates: tuple[tuple[str, str, float | None, float, str], ...] = (
            (
                RULE_TRACK_WIDTH,
                "track_width",
                track_width,
                design_rules.min_trace_width_mm,
                "error",
            ),
            (
                RULE_CLEARANCE,
                "clearance",
                clearance,
                design_rules.min_clearance_mm,
                "error",
            ),
            (
                RULE_VIA_DIAMETER,
                "via_diameter",
                via_diameter,
                design_rules.min_via_diameter_mm,
                via_severity,
            ),
            (
                RULE_VIA_DRILL,
                "via_drill",
                via_drill,
                design_rules.min_via_drill_mm,
                via_severity,
            ),
            (
                RULE_ANNULAR_RING,
                "implied annular ring",
                annular_ring,
                design_rules.min_annular_ring_mm,
                via_severity,
            ),
        )

        for rule_id, field_label, actual, floor, severity in candidates:
            if actual is None or actual + DRC_TOLERANCE >= floor:
                continue
            findings.append(
                (
                    (name, rule_id),
                    DRCViolation(
                        rule_id=rule_id,
                        severity=severity,
                        message=(
                            f"Netclass {name!r} {field_label} {actual:.3f}mm < "
                            f"minimum {floor:.3f}mm" + (via_note if severity == "warning" else "")
                        ),
                        actual_value=actual,
                        required_value=floor,
                        items=items,
                    ),
                )
            )

    for _key, violation in sorted(findings, key=lambda entry: entry[0]):
        results.add(violation)

    return results


def _load_netclasses(
    pcb_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read ``(classes, netclass_patterns)`` from the sibling ``.kicad_pro``.

    Returns two empty lists for every carve-out (no project file, unreadable
    or malformed JSON, no ``net_settings.classes`` declared).  The
    declaration-absent case must NOT fall back to
    :func:`~kicad_tools.core.project_file.get_netclass_definitions`'
    materializing behaviour: that helper *creates* a stock ``Default`` class
    when none exists, which would report findings against a netclass the
    project never declared.
    """
    pro_path = pcb_path.with_suffix(".kicad_pro")
    if not pro_path.is_file():
        return [], []
    try:
        data = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    net_settings = data.get("net_settings")
    if not isinstance(net_settings, dict) or "classes" not in net_settings:
        return [], []

    from kicad_tools.core.project_file import get_netclass_definitions, get_netclass_patterns

    classes = get_netclass_definitions(data)
    patterns = get_netclass_patterns(data)
    if not isinstance(classes, list):
        return [], []
    if not isinstance(patterns, list):
        patterns = []
    return classes, patterns
