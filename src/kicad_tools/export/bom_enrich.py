"""
BOM enrichment with LCSC part numbers.

Populates missing LCSC part numbers in BOM items by searching the
LCSC/JLCPCB parts catalog using component values and footprints.

Uses the existing ``PartSuggester`` engine which handles:
- Value + footprint search term construction
- Package-size filtering
- Basic > Preferred > Extended part ranking
- Confidence scoring
- LCSC API rate limiting and caching
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..cost.suggest import PartSuggester

if TYPE_CHECKING:
    from ..schema.bom import BOMItem

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentEntry:
    """Record of an LCSC enrichment for a single BOM group."""

    value: str
    footprint: str
    references: list[str]
    lcsc_part: str  # The assigned LCSC part number (empty if unmatched)
    source: str  # "schematic" | "auto" | "unmatched"
    confidence: float = 0.0
    part_type: str = ""  # "Basic" | "Pref" | "Ext" | ""
    error: str = ""


@dataclass
class EnrichmentReport:
    """Summary of BOM enrichment results."""

    entries: list[EnrichmentEntry] = field(default_factory=list)

    @property
    def total_groups(self) -> int:
        """Total number of component groups processed."""
        return len(self.entries)

    @property
    def already_populated(self) -> int:
        """Groups that already had LCSC numbers from the schematic."""
        return len([e for e in self.entries if e.source == "schematic"])

    @property
    def auto_matched(self) -> int:
        """Groups that were auto-matched via LCSC search."""
        return len([e for e in self.entries if e.source == "auto"])

    @property
    def unmatched(self) -> int:
        """Groups that could not be matched."""
        return len([e for e in self.entries if e.source == "unmatched"])

    @property
    def unmatched_entries(self) -> list[EnrichmentEntry]:
        """Get the entries that could not be matched."""
        return [e for e in self.entries if e.source == "unmatched"]

    def summary_lines(self) -> list[str]:
        """Return human-readable summary lines."""
        lines = [
            f"LCSC enrichment: {self.auto_matched} auto-matched, "
            f"{self.already_populated} from schematic, "
            f"{self.unmatched} unmatched"
        ]
        if self.unmatched_entries:
            lines.append("Unmatched parts:")
            for entry in self.unmatched_entries:
                refs = ", ".join(entry.references)
                reason = f" ({entry.error})" if entry.error else ""
                lines.append(f"  {entry.value} [{entry.footprint}] ({refs}){reason}")
        return lines


def enrich_bom_lcsc(
    items: list[BOMItem],
    *,
    prefer_basic: bool = True,
    min_stock: int = 100,
) -> EnrichmentReport:
    """
    Populate missing LCSC part numbers on BOM items in-place.

    For each unique (value, footprint) group that lacks an LCSC number,
    searches the LCSC catalog using :class:`PartSuggester` and writes
    the best match back onto every ``BOMItem`` in that group.

    Items that already have an ``lcsc`` value are left untouched.

    Args:
        items: List of BOM items to enrich (modified in place).
        prefer_basic: Prefer JLCPCB Basic parts (no extra assembly fee).
        min_stock: Minimum stock level to consider a part viable.

    Returns:
        EnrichmentReport summarising what was matched.
    """
    report = EnrichmentReport()

    # Group items by (value, footprint) to avoid duplicate searches
    groups: dict[tuple[str, str], list[BOMItem]] = {}
    for item in items:
        if getattr(item, "dnp", False):
            continue
        if getattr(item, "is_virtual", False):
            continue
        key = (item.value, item.footprint)
        groups.setdefault(key, []).append(item)

    with PartSuggester(
        prefer_basic=prefer_basic,
        min_stock=min_stock,
    ) as suggester:
        for (value, footprint), group_items in groups.items():
            refs = [it.reference for it in group_items]

            # Check if any item in the group already has an LCSC number
            existing_lcsc = ""
            for it in group_items:
                if it.lcsc:
                    existing_lcsc = it.lcsc
                    break

            if existing_lcsc:
                # Propagate existing LCSC to all items in the group
                for it in group_items:
                    if not it.lcsc:
                        it.lcsc = existing_lcsc
                report.entries.append(
                    EnrichmentEntry(
                        value=value,
                        footprint=footprint,
                        references=refs,
                        lcsc_part=existing_lcsc,
                        source="schematic",
                    )
                )
                continue

            # Use the first reference for type hinting (R1 -> resistor, etc.)
            first_ref = refs[0] if refs else ""

            suggestion = suggester.suggest_for_component(
                reference=first_ref,
                value=value,
                footprint=footprint,
                existing_lcsc=None,
            )

            if suggestion.has_suggestion:
                best = suggestion.best_suggestion
                assert best is not None  # guarded by has_suggestion
                lcsc = best.lcsc_part

                # Write back to all items in the group
                for it in group_items:
                    it.lcsc = lcsc

                report.entries.append(
                    EnrichmentEntry(
                        value=value,
                        footprint=footprint,
                        references=refs,
                        lcsc_part=lcsc,
                        source="auto",
                        confidence=best.confidence,
                        part_type=best.type_str,
                    )
                )
                logger.info(
                    "Auto-matched %s [%s] -> %s (%s, confidence=%.2f)",
                    value,
                    footprint,
                    lcsc,
                    best.type_str,
                    best.confidence,
                )
            else:
                report.entries.append(
                    EnrichmentEntry(
                        value=value,
                        footprint=footprint,
                        references=refs,
                        lcsc_part="",
                        source="unmatched",
                        error=suggestion.error or "no matching parts found",
                    )
                )
                logger.warning(
                    "No LCSC match for %s [%s] (%s): %s",
                    value,
                    footprint,
                    ", ".join(refs),
                    suggestion.error or "no matching parts found",
                )

    return report
