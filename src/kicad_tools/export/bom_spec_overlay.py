"""
Overlay BOM fields from a .kct project spec onto BOM items.

Reads ``bom_entries`` from a :class:`~kicad_tools.spec.schema.ProjectSpec`
and writes MPN / LCSC fields onto matching :class:`~kicad_tools.schema.bom.BOMItem`
instances **in place**.  Items that match a spec entry are tagged with
``source="spec"`` in the returned :class:`SpecOverlayReport` so that
downstream enrichment (e.g. LCSC auto-match) can skip them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..schema.bom import BOMItem
    from ..spec.schema import BOMEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reference-range expansion
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def expand_ref_range(ref_spec: str) -> list[str]:
    """Expand a reference designator or dash-separated range.

    Examples::

        expand_ref_range("U1")        -> ["U1"]
        expand_ref_range("Q1-Q4")     -> ["Q1", "Q2", "Q3", "Q4"]
        expand_ref_range("R10-R12")   -> ["R10", "R11", "R12"]

    If the string cannot be parsed as a range it is returned as-is in a
    single-element list.
    """
    if "-" not in ref_spec:
        return [ref_spec.strip()]

    parts = ref_spec.split("-", 1)
    start_match = _REF_RE.match(parts[0].strip())
    end_match = _REF_RE.match(parts[1].strip())

    if not start_match or not end_match:
        # Not a valid range -- return the raw string
        return [ref_spec.strip()]

    prefix_start = start_match.group(1)
    prefix_end = end_match.group(1)

    if prefix_start != prefix_end:
        # Different prefixes (e.g. "R1-C4") -- not a valid range
        return [ref_spec.strip()]

    num_start = int(start_match.group(2))
    num_end = int(end_match.group(2))

    if num_start > num_end:
        return [ref_spec.strip()]

    return [f"{prefix_start}{n}" for n in range(num_start, num_end + 1)]


# ---------------------------------------------------------------------------
# Overlay report
# ---------------------------------------------------------------------------


@dataclass
class SpecOverlayEntry:
    """Record of a single spec-to-BOM overlay application."""

    reference: str
    mpn: str
    lcsc: str
    matched: bool  # True if a BOM item with this reference was found


@dataclass
class SpecOverlayReport:
    """Summary of spec overlay results."""

    entries: list[SpecOverlayEntry] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def matched(self) -> int:
        return len([e for e in self.entries if e.matched])

    @property
    def unmatched(self) -> int:
        return len([e for e in self.entries if not e.matched])

    @property
    def unmatched_refs(self) -> list[str]:
        return [e.reference for e in self.entries if not e.matched]

    def summary_lines(self) -> list[str]:
        lines = [
            f"Spec overlay: {self.matched} applied, {self.unmatched} unmatched"
        ]
        if self.unmatched_refs:
            lines.append(
                f"  Unmatched spec refs: {', '.join(self.unmatched_refs)}"
            )
        return lines


# ---------------------------------------------------------------------------
# Spec auto-detection
# ---------------------------------------------------------------------------


def find_spec_file(project_dir: Path) -> Path | None:
    """Locate a ``.kct`` spec file in *project_dir*.

    Prefers ``project.kct``; falls back to the first ``.kct`` found.
    Warns if multiple ``.kct`` files exist and none is named ``project.kct``.

    Returns ``None`` when no spec file is found.
    """
    project_kct = project_dir / "project.kct"
    if project_kct.is_file():
        return project_kct

    kct_files = sorted(project_dir.glob("*.kct"))
    if not kct_files:
        return None

    if len(kct_files) > 1:
        logger.warning(
            "Multiple .kct files found in %s; using %s. "
            "Rename the primary spec to 'project.kct' to suppress this warning.",
            project_dir,
            kct_files[0].name,
        )

    return kct_files[0]


# ---------------------------------------------------------------------------
# Core overlay function
# ---------------------------------------------------------------------------


def apply_spec_overlay(
    items: list[BOMItem],
    bom_entries: list[BOMEntry],
) -> SpecOverlayReport:
    """Apply spec BOM entries onto *items* in place.

    For each :class:`BOMEntry`, the function expands reference ranges and
    looks for a ``BOMItem`` with a matching ``reference``.  When found the
    item's ``mpn`` and ``lcsc`` fields are overwritten with the spec values
    (if the spec value is non-empty / non-None).

    Args:
        items: BOM items to mutate.
        bom_entries: Entries from the project spec.

    Returns:
        A :class:`SpecOverlayReport` summarising what was applied.
    """
    report = SpecOverlayReport()

    # Build lookup: reference -> BOMItem
    ref_to_item: dict[str, BOMItem] = {item.reference: item for item in items}

    for entry in bom_entries:
        refs = expand_ref_range(entry.ref)
        for ref in refs:
            item = ref_to_item.get(ref)
            if item is None:
                logger.warning(
                    "Spec BOM entry ref '%s' does not match any schematic component",
                    ref,
                )
                report.entries.append(
                    SpecOverlayEntry(
                        reference=ref,
                        mpn=entry.part,
                        lcsc=entry.lcsc or "",
                        matched=False,
                    )
                )
                continue

            # Overlay MPN
            if entry.part:
                item.mpn = entry.part

            # Overlay LCSC
            if entry.lcsc:
                item.lcsc = entry.lcsc

            report.entries.append(
                SpecOverlayEntry(
                    reference=ref,
                    mpn=entry.part,
                    lcsc=entry.lcsc or "",
                    matched=True,
                )
            )
            logger.info(
                "Spec overlay: %s -> mpn=%s lcsc=%s",
                ref,
                entry.part,
                entry.lcsc or "(none)",
            )

    return report
