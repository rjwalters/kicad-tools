"""Stable finding keys and local-evidence hashes for ``kct check`` (Issue #5946).

Two identities are attached to every :class:`~kicad_tools.validate.violations.DRCViolation`:

``key``
    *What* the finding is about: the rule id, the sorted item references, the
    sorted net names and the layer, joined as
    ``rule_id|item,item|net,net|layer``.  The key deliberately excludes the
    location and measured values, so it survives a board edit that moves the
    finding a little or changes its message wording.  ``kct check --diff`` pairs
    findings across two revisions by key, and evidence-bound waivers name the
    finding they acknowledge by key.

``evidence_hash``
    *The local evidence* that produced the finding: its rounded location and
    closest points, its measured and required values, the placement and pad
    geometry (with pad nets) of every footprint it names, and the pad
    membership of every net it names.  When the copper or nets under a finding
    change, the hash changes -- that is what makes an evidence-bound waiver go
    **stale** instead of silently suppressing a finding whose geometry nobody
    has reviewed.

Both are pure functions of the finding (plus the loaded board, for the
footprint / net part of the evidence), so the same board always yields the
same keys and hashes.  Values are rounded (positions to 1 um, measurements to
0.1 um) so float noise from re-serialization cannot flip a hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .violations import DRCResults, DRCViolation

# Field separator inside a finding key.  ``|`` never appears in a KiCad
# reference designator and is vanishingly rare in a net name.
KEY_SEPARATOR = "|"

# Hash prefix so a reader can tell the algorithm at a glance, and so a future
# change to the evidence recipe can be versioned without ambiguity.
EVIDENCE_HASH_PREFIX = "ev1:"

_POS_DIGITS = 3  # 1 um
_VALUE_DIGITS = 4  # 0.1 um

# Separators that split an item descriptor such as ``U1.3`` / ``U1-pad3`` /
# ``U1:3`` / ``U1 pad 3`` into its leading reference designator.
_REF_SPLIT = re.compile(r"[.\-:/ ]")


def finding_key(
    rule_id: str,
    items: tuple[str, ...] | list[str] = (),
    nets: tuple[str, ...] | list[str] = (),
    layer: str | None = None,
) -> str:
    """Build the stable key ``rule_id|items|nets|layer`` for a finding."""
    return KEY_SEPARATOR.join(
        (
            rule_id,
            ",".join(sorted(items)),
            ",".join(sorted(nets)),
            layer or "",
        )
    )


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    # Normalize -0.0 so the canonical JSON is stable.
    return 0.0 if rounded == 0 else rounded


def _point(point: tuple[float, float] | None) -> list[float | None] | None:
    if point is None:
        return None
    return [_round(point[0], _POS_DIGITS), _round(point[1], _POS_DIGITS)]


def _footprint_index(pcb: PCB) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for fp in pcb.footprints:
        ref = getattr(fp, "reference", "")
        if ref and ref not in index:
            index[ref] = fp
    return index


def _resolve_ref(item: str, footprints: dict[str, Any]) -> str | None:
    """Map an item descriptor (``U1``, ``U1.3``, ``U1-pad3``) to a footprint ref."""
    if item in footprints:
        return item
    head = _REF_SPLIT.split(item, maxsplit=1)[0]
    if head in footprints:
        return head
    return None


def _footprint_evidence(fp: Any) -> dict[str, Any]:
    pads = []
    for pad in getattr(fp, "pads", []) or []:
        pads.append(
            [
                str(pad.number),
                pad.net_name or "",
                _round(pad.position[0], _POS_DIGITS),
                _round(pad.position[1], _POS_DIGITS),
                _round(pad.size[0], _POS_DIGITS),
                _round(pad.size[1], _POS_DIGITS),
                _round(getattr(pad, "rotation", 0.0), _VALUE_DIGITS),
            ]
        )
    pads.sort(key=lambda row: (row[0], row[1], str(row[2:])))
    return {
        "at": _point(fp.position),
        "rot": _round(fp.rotation, _VALUE_DIGITS),
        "layer": fp.layer,
        "footprint": getattr(fp, "name", ""),
        "pads": pads,
    }


def _net_members(pcb: PCB) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for fp in pcb.footprints:
        ref = getattr(fp, "reference", "")
        for pad in getattr(fp, "pads", []) or []:
            if pad.net_name:
                members.setdefault(pad.net_name, []).append(f"{ref}.{pad.number}")
    for names in members.values():
        names.sort()
    return members


class EvidenceContext:
    """Per-board lookup tables shared by every hash computed for one run."""

    def __init__(self, pcb: PCB | None) -> None:
        self.footprints: dict[str, Any] = _footprint_index(pcb) if pcb is not None else {}
        self.net_members: dict[str, list[str]] = _net_members(pcb) if pcb is not None else {}
        self.has_board = pcb is not None


def evidence_payload(violation: DRCViolation, context: EvidenceContext | None = None) -> dict:
    """Return the canonical evidence document hashed by :func:`compute_evidence_hash`."""
    ctx = context or EvidenceContext(None)
    payload: dict[str, Any] = {
        "key": violation.key,
        "location": _point(violation.location),
        "closest": [_point(p) for p in violation.closest_locations],
        "actual": _round(violation.actual_value, _VALUE_DIGITS),
        "required": _round(violation.required_value, _VALUE_DIGITS),
    }
    if ctx.has_board:
        footprints: dict[str, Any] = {}
        for item in violation.items:
            ref = _resolve_ref(item, ctx.footprints)
            if ref is not None and ref not in footprints:
                footprints[ref] = _footprint_evidence(ctx.footprints[ref])
        payload["footprints"] = dict(sorted(footprints.items()))
        payload["nets"] = {net: ctx.net_members.get(net, []) for net in sorted(set(violation.nets))}
    return payload


def compute_evidence_hash(violation: DRCViolation, context: EvidenceContext | None = None) -> str:
    """Hash the local evidence of ``violation`` (see module docstring)."""
    canonical = json.dumps(
        evidence_payload(violation, context), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return EVIDENCE_HASH_PREFIX + digest


def annotate_evidence(results: DRCResults, pcb: PCB | None) -> None:
    """Attach an ``evidence_hash`` to every finding in ``results`` that lacks one.

    Only :class:`~kicad_tools.validate.violations.DRCViolation` instances are
    annotated; any other finding object that reached the list (e.g. a test
    double or a foreign model) is left untouched.
    """
    from .violations import DRCViolation

    def _needs(v: object) -> bool:
        return isinstance(v, DRCViolation) and not v.evidence_hash

    if not any(_needs(v) for v in results.violations):
        return
    context = EvidenceContext(pcb)
    results.violations = [
        replace(v, evidence_hash=compute_evidence_hash(v, context)) if _needs(v) else v
        for v in results.violations
    ]
