"""Stable keys, evidence hashes and evidence-bound waivers for mistakes (Issue #6006).

``kct detect-mistakes`` findings get the same two identities ``kct check``
findings carry (Issue #5946, :mod:`kicad_tools.validate.evidence`):

``key``
    ``rule_id|items|nets|layer``.  The rule id is the check's
    :func:`~kicad_tools.explain.mistakes.mistake_rule_id`
    (``mistake.bypass_cap_distance``).  A mistake names its subjects in one
    free-form ``components`` list -- reference designators, pad labels, net
    names and sometimes a copper layer -- so each entry is sorted into the
    key's fields: a name that is a net on the board goes to ``nets``, a
    copper-layer name (``F.Cu``) to ``layer``, everything else to ``items``.

``evidence_hash``
    The ``kct check`` evidence recipe (:func:`evidence_payload`: rounded
    location, the placement and pads of every footprint named, the local pad
    membership of every net named, and the key's multiplicity) plus the
    check's structured :attr:`~kicad_tools.explain.mistakes.Mistake.measurements`
    (``{"distance_mm": 13.3}``, ``{"min_width_mm": 0.25, "segment_count":
    2.0}``), each rounded by its check to the precision the explanation
    quotes.  A trace widened from 0.25 mm to 0.28 mm -- still too narrow --
    changes the hash and makes a waiver reviewed at 0.25 mm go **stale**.

    Only measured values are hashed.  The explanation text is not: rewording
    it, or the digits it happens to contain (reference designators, pin
    numbers, net names such as ``+3V3``, boilerplate like "1A at 1oz"), never
    invalidates a waiver.  Policy thresholds (``MAX_BYPASS_DISTANCE_MM``)
    are not hashed either, so tuning one does not make reviewed waivers
    stale; a waiver records that a human accepted *this* measurement.

    The hash carries its own compound version prefix,
    :data:`~kicad_tools.validate.evidence.MISTAKE_EVIDENCE_HASH_PREFIX`
    (``ev2.m1:``): the ``kct check`` evidence version plus the measurement
    recipe version.  Changing the shared evidence recipe (``ev2`` -> ``ev3``)
    or only the measurement recipe (``m1`` -> ``m2``) reports existing
    mistake waivers as ``outdated_evidence_version``; the latter leaves every
    ``kct check`` waiver untouched.

Waivers are the keyed version-3 entries of the shared ``.kct_waivers.json``
/ ``<board>.kct-waivers.json`` sidecar, applied by the very same
:func:`~kicad_tools.validate.rules.waivers.apply_waivers` (stale and unused
handling included).  Each command applies only its own entries
(:data:`~kicad_tools.validate.rules.waivers.MISTAKE_RULE_PREFIX`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.validate.evidence import (
    MISTAKE_EVIDENCE_HASH_PREFIX,
    EvidenceContext,
    evidence_payload,
    finding_key,
)
from kicad_tools.validate.violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.waivers import Waivers

    from .mistakes import Mistake

# The re-waive command named by stale / unused advisories.
WAIVE_COMMAND = "kct detect-mistakes --waive"

_COPPER_LAYER = re.compile(r"^(F|B|In\d+)\.Cu$")


def _board_net_names(pcb: PCB | None) -> frozenset[str]:
    if pcb is None:
        return frozenset()
    names = {getattr(net, "name", "") for net in getattr(pcb, "nets", {}).values()}
    # Pads carry their net name even when the board's net table is sparse.
    for fp in getattr(pcb, "footprints", []) or []:
        for pad in getattr(fp, "pads", []) or []:
            if getattr(pad, "net_name", ""):
                names.add(pad.net_name)
    names.discard("")
    return frozenset(names)


def split_components(
    components: Iterable[str], net_names: frozenset[str] | set[str]
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """Sort a mistake's ``components`` into ``(items, nets, layer)``."""
    items: list[str] = []
    nets: list[str] = []
    layer: str | None = None
    for raw in components:
        name = str(raw)
        if name in net_names:
            nets.append(name)
        elif layer is None and _COPPER_LAYER.match(name):
            layer = name
        else:
            items.append(name)
    return tuple(items), tuple(nets), layer


def board_origin(pcb: PCB | None) -> tuple[float, float]:
    """The board-origin offset of ``pcb`` (``(0, 0)`` when unknown)."""
    origin = getattr(pcb, "board_origin", None) if pcb is not None else None
    try:
        return (float(origin[0]), float(origin[1])) if origin else (0.0, 0.0)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def sheet_location(
    mistake: Mistake, origin: tuple[float, float] = (0.0, 0.0)
) -> tuple[float, float] | None:
    """The mistake's location in sheet (KiCad file) coordinates.

    Mistake checks measure on the loaded :class:`PCB`, whose footprint
    positions are *board-relative* (the Edge.Cuts origin is subtracted at
    load time); ``kct check`` findings are in the file's sheet coordinates.
    Adding the origin back puts both on one frame -- the one KiCad shows.
    """
    if mistake.location is None:
        return None
    return (float(mistake.location[0]) + origin[0], float(mistake.location[1]) + origin[1])


def to_violation(
    mistake: Mistake,
    net_names: frozenset[str] | set[str],
    origin: tuple[float, float] = (0.0, 0.0),
) -> DRCViolation:
    """Adapt ``mistake`` to a :class:`DRCViolation` for the shared machinery.

    The adapter carries everything the key / evidence recipe reads, with the
    location in sheet coordinates like a ``kct check`` finding (see
    :func:`sheet_location`); the mistake's text stays on the
    :class:`Mistake`.  An unknown severity is mapped to ``"warning"`` rather
    than rejected.
    """
    items, nets, layer = split_components(mistake.components, net_names)
    severity = mistake.severity if mistake.severity in ("error", "warning", "info") else "warning"
    location = sheet_location(mistake, origin)
    return DRCViolation(
        rule_id=mistake.rule_id or f"mistake.{mistake.category.value}",
        severity=severity,
        message=mistake.title,
        location=location,
        layer=layer,
        items=items,
        nets=nets,
    )


def _measurement_value(value: float) -> float:
    number = float(value)
    # Normalize -0.0 so the canonical JSON is stable.
    return 0.0 if number == 0 else number


def measurements(mistake: Mistake) -> dict[str, float]:
    """The check's structured measurements, canonicalized for hashing.

    Values are already rounded by the check to the precision it reports;
    here they are only coerced to ``float`` (so ``2`` and ``2.0`` hash the
    same) and keyed in sorted order.
    """
    return {
        str(name): _measurement_value(value)
        for name, value in sorted((mistake.measurements or {}).items())
    }


def mistake_evidence_hash(
    mistake: Mistake,
    violation: DRCViolation,
    context: EvidenceContext,
    *,
    multiplicity: int = 1,
) -> str:
    """Hash the local evidence of ``mistake`` (see module docstring)."""
    payload = evidence_payload(violation, context, multiplicity=multiplicity)
    payload["measurements"] = measurements(mistake)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return MISTAKE_EVIDENCE_HASH_PREFIX + digest


def annotate_mistakes(mistakes: list[Mistake], pcb: PCB | None) -> list[DRCViolation]:
    """Attach ``key`` and ``evidence_hash`` to every mistake, in place.

    Returns the :class:`DRCViolation` adapters (same order), already carrying
    their evidence hash, for :func:`apply_mistake_waivers` /
    :func:`~kicad_tools.validate.rules.waivers.write_keyed_waivers`.
    """
    from dataclasses import replace

    net_names = _board_net_names(pcb)
    context = EvidenceContext(pcb)
    origin = board_origin(pcb)
    adapters = [to_violation(m, net_names, origin) for m in mistakes]
    counts = Counter(v.key for v in adapters)
    annotated: list[DRCViolation] = []
    for m, v in zip(mistakes, adapters, strict=True):
        digest = mistake_evidence_hash(m, v, context, multiplicity=counts[v.key])
        m.key = finding_key(v.rule_id, v.items, v.nets, v.layer)
        m.evidence_hash = digest
        annotated.append(replace(v, evidence_hash=digest))
    return annotated


@dataclass
class MistakeWaiverResult:
    """Outcome of :func:`apply_mistake_waivers`."""

    advisories: list[DRCViolation] = field(default_factory=list)

    @property
    def stale(self) -> list[DRCViolation]:
        from kicad_tools.validate.rules.waivers import WAIVER_STALE_RULE_ID

        return [a for a in self.advisories if a.rule_id == WAIVER_STALE_RULE_ID]

    @property
    def unused(self) -> list[DRCViolation]:
        from kicad_tools.validate.rules.waivers import WAIVER_UNUSED_RULE_ID

        return [a for a in self.advisories if a.rule_id == WAIVER_UNUSED_RULE_ID]


def apply_mistake_waivers(
    mistakes: list[Mistake],
    adapters: list[DRCViolation],
    waivers: Waivers,
) -> MistakeWaiverResult:
    """Apply evidence-bound waivers to ``mistakes`` in place (Issue #6006).

    Runs :func:`~kicad_tools.validate.rules.waivers.apply_waivers` over the
    adapters (so matching, stale and unused semantics are exactly
    ``kct check``'s) and copies each verdict back onto its mistake.  The
    ``waiver_stale`` / ``waiver_unused`` advisories are returned rather than
    turned into mistakes: they are about the sidecar, not the board.
    """
    from kicad_tools.validate.rules.waivers import apply_waivers

    if len(mistakes) != len(adapters):  # pragma: no cover - programming error
        raise ValueError("mistakes and adapters must align")
    results = DRCResults(violations=list(adapters))
    apply_waivers(results, waivers, waive_command=WAIVE_COMMAND)
    # apply_waivers rebuilds the list in order, one entry per input finding,
    # then appends its advisories.
    rebuilt = results.violations
    for m, v in zip(mistakes, rebuilt[: len(adapters)], strict=True):
        if v.waived:
            m.waived = True
            m.waiver_reason = v.waiver_reason
            m.waiver_issue = v.waiver_issue
        if v.stale_waiver_hash is not None:
            m.stale_waiver_hash = v.stale_waiver_hash
            m.stale_waiver_reason = v.stale_waiver_reason
    return MistakeWaiverResult(advisories=list(rebuilt[len(adapters) :]))
