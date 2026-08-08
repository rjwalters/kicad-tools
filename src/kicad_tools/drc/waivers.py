"""Apply ``.kct_waivers.json`` waivers to a kicad-cli DRC report (Issue #4691).

``kct check --waivers`` (Issue #4417) waives findings from kct's *own* rule
engine.  This module carries the **same** schema-v2 sidecar -- same loader,
same discovery probe, same exact-set matching semantics -- over to the
``kicad-cli pcb drc`` cross-gate consumed by ``kct drc``, so both engines speak
one waiver vocabulary and a board with a documented, intentional violation can
still produce a "0 unwaived errors" verdict on the cross-gate side.

Two normalizations bridge the two engines:

* **Rule key.**  ``waiver.rule`` is matched against KiCad's own violation type
  string (``DRCViolation.type_str``) verbatim -- no translation table.  For
  ``courtyards_overlap`` the two engines coincide by design; clearance-family
  ids diverge (KiCad ``clearance`` vs kct ``clearance_pad_pad``), which is
  fine: one shared sidecar simply carries entries keyed per engine.  An entry
  keyed to the *other* engine's rule id necessarily reads "unused" on this
  side, which is why unused entries are advisory (never gating).

* **Item set.**  The kicad-cli JSON parser keeps each involved item's free-text
  *description* (``"Footprint C52"``, ``"Pad 6 [<no net>] of U3 on F.Cu"``) and
  discards its ``uuid``, so descriptions are normalized to reference
  designators via :func:`kicad_tools.drc.violation.extract_item_refs` before
  matching.  A waiver therefore names refs (``["C52", "U10"]``) exactly as it
  does for ``kct check``.

Unlike the ``kct check`` path, unused-waiver advisories are **not** injected
into ``DRCReport.violations``: that list is the parsed cross-gate report and
synthetic entries would distort its by-type/by-category tables and its
``--format json`` shape.  They are returned in :class:`WaiverApplication`
instead and rendered by ``kct drc`` in a dedicated advisory block.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate.rules.waivers import Waiver, Waivers

from .report import DRCReport
from .violation import DRCViolation, extract_item_refs


@dataclass
class WaiverApplication:
    """Outcome of applying waivers to a DRC report.

    Attributes:
        waived: The violations that a waiver entry matched (already marked
            ``waived=True`` in place on the report).
        unused: Waiver entries that matched no violation -- stale entries, or
            entries keyed to the other engine's rule ids.  Advisory only.
    """

    waived: list[DRCViolation] = field(default_factory=list)
    unused: list[Waiver] = field(default_factory=list)

    @property
    def waived_count(self) -> int:
        """Number of findings matched by a waiver entry."""
        return len(self.waived)

    @property
    def unused_messages(self) -> list[str]:
        """Human-readable advisory lines for each unused waiver entry."""
        return [_unused_message(entry) for entry in self.unused]

    def to_dict_list(self) -> list[dict]:
        """JSON-serializable view of the unused entries (for ``--format json``)."""
        return [
            {
                "rule": entry.rule,
                "items": sorted(entry.items),
                "nets": sorted(entry.nets),
                "reason": entry.reason,
                "issue": entry.issue,
                "message": _unused_message(entry),
            }
            for entry in self.unused
        ]


def _scope_str(entry: Waiver) -> str:
    parts = []
    if entry.items:
        parts.append(f"items={sorted(entry.items)}")
    if entry.nets:
        parts.append(f"nets={sorted(entry.nets)}")
    return ", ".join(parts)


def _unused_message(entry: Waiver) -> str:
    return (
        f"Waiver for rule {entry.rule!r} ({_scope_str(entry)}) matched no violation "
        f"(tracking {entry.issue}); the underlying defect may already be resolved, "
        "the refs may have changed, or the entry may be keyed to another engine's "
        "rule id."
    )


def apply_waivers_to_report(report: DRCReport, waivers: Waivers) -> WaiverApplication:
    """Mark every waiver-matched violation in ``report`` as waived, in place.

    Matching is exact-set and order-insensitive on the *normalized* ref set
    (a 2-item waiver does not waive a 3-item finding) and, when the entry names
    ``nets``, on the violation's net set.  The first matching entry wins.

    Waived violations keep their underlying ``severity`` (so severity-keyed
    consumers such as the ``kct audit`` manufacturing gate stay blocking) but
    report ``is_error is False``, which is what the ``kct drc`` exit gate reads.

    Args:
        report: Parsed DRC report; its violations are mutated in place.
        waivers: Loaded schema-v2 waiver entries.

    Returns:
        A :class:`WaiverApplication` describing what was waived and which
        entries went unused.
    """
    result = WaiverApplication()
    if not waivers.entries:
        return result

    used: set[int] = set()
    for violation in report.violations:
        if violation.waived:
            continue
        refs = extract_item_refs(violation.items)
        nets = frozenset(violation.nets)
        for idx, entry in enumerate(waivers.entries):
            if not entry.matches_normalized(violation.type_str, refs, nets):
                continue
            violation.waived = True
            violation.waiver_reason = entry.reason
            violation.waiver_issue = entry.issue
            used.add(idx)
            result.waived.append(violation)
            break

    result.unused = [entry for idx, entry in enumerate(waivers.entries) if idx not in used]
    return result
