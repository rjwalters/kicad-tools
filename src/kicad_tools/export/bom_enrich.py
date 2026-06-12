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
from ..parts.cache import PartsCache
from ..parts.lcsc import LCSCForbiddenError
from .lcsc_value_check import check_lcsc_against_cache, find_value_mismatch

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
    source: str  # "schematic" | "auto" | "cache" | "unmatched"
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
    def spec_populated(self) -> int:
        """Groups that already had LCSC numbers from the project spec."""
        return len([e for e in self.entries if e.source == "spec"])

    @property
    def auto_matched(self) -> int:
        """Groups that were auto-matched via LCSC search."""
        return len([e for e in self.entries if e.source == "auto"])

    @property
    def cache_matched(self) -> int:
        """Groups matched from stale cache when API was unavailable."""
        return len([e for e in self.entries if e.source == "cache"])

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
        parts = [f"{self.auto_matched} auto-matched"]
        if self.cache_matched:
            parts.append(f"{self.cache_matched} from cache")
        if self.spec_populated:
            parts.append(f"{self.spec_populated} from spec")
        parts.append(f"{self.already_populated} from schematic")
        parts.append(f"{self.unmatched} unmatched")
        lines = [f"LCSC enrichment: {', '.join(parts)}"]
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
    spec_refs: set[str] | None = None,
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
        spec_refs: Set of reference designators whose LCSC was populated
            by the project spec overlay.  These are reported with
            ``source="spec"`` instead of ``"schematic"``.

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

    api_forbidden = False
    forbidden_error = "JLCPCB API unavailable (403)"

    def _try_cache_fallback(
        cache: PartsCache | None,
        value: str,
        footprint: str,
        refs: list[str],
        group_items: list[BOMItem],
    ) -> EnrichmentEntry:
        """Attempt to resolve a part from the enrichment cache.

        Returns an ``EnrichmentEntry`` with ``source="cache"`` on hit,
        or ``source="unmatched"`` on miss.
        """
        if cache is not None:
            match = cache.get_enrichment_match(value, footprint, ignore_expiry=True)
            if match is not None:
                lcsc = match["lcsc_part"]

                # Validate the cached part's known parametric value
                # against the requested BOM value before trusting a
                # stale entry (issue #3590: C1525/100nF assigned to a
                # 16nF row from a poisoned cache entry).
                first_ref = refs[0] if refs else ""
                mismatch = check_lcsc_against_cache(cache, lcsc, value, first_ref)
                if mismatch is not None:
                    cache.delete_enrichment_match(value, footprint)
                    logger.warning(
                        "Rejecting cached LCSC match %s for %s [%s]: %s "
                        "-- evicted poisoned cache entry",
                        lcsc,
                        value,
                        footprint,
                        mismatch.describe(),
                    )
                    return EnrichmentEntry(
                        value=value,
                        footprint=footprint,
                        references=refs,
                        lcsc_part="",
                        source="unmatched",
                        error=(f"cached match {lcsc} rejected: {mismatch.describe()}"),
                    )

                for it in group_items:
                    it.lcsc = lcsc
                # WARNING, not INFO: ignore_expiry=True is a degraded
                # mode that explicitly accepts stale data.
                logger.warning(
                    "Cache fallback %s [%s] -> %s "
                    "(stale cache; API unavailable -- verify before fab)",
                    value,
                    footprint,
                    lcsc,
                )
                return EnrichmentEntry(
                    value=value,
                    footprint=footprint,
                    references=refs,
                    lcsc_part=lcsc,
                    source="cache",
                    confidence=match["confidence"],
                    part_type=match["part_type"],
                )
        return EnrichmentEntry(
            value=value,
            footprint=footprint,
            references=refs,
            lcsc_part="",
            source="unmatched",
            error=forbidden_error,
        )

    with PartSuggester(
        prefer_basic=prefer_basic,
        min_stock=min_stock,
    ) as suggester:
        # Obtain the parts cache from the underlying LCSC client so we
        # can store and retrieve enrichment matches.
        cache: PartsCache | None = None
        client = suggester._get_client()
        if client is not None:
            cache = client.cache

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

                # Determine source: if any ref in the group was set by spec,
                # report as "spec"; otherwise "schematic".
                _spec_refs = spec_refs or set()
                source = "spec" if any(r in _spec_refs for r in refs) else "schematic"
                report.entries.append(
                    EnrichmentEntry(
                        value=value,
                        footprint=footprint,
                        references=refs,
                        lcsc_part=existing_lcsc,
                        source=source,
                    )
                )
                continue

            # If the API is known to be forbidden, try the cache before
            # marking as unmatched.
            if api_forbidden:
                report.entries.append(
                    _try_cache_fallback(cache, value, footprint, refs, group_items)
                )
                continue

            # Use the first reference for type hinting (R1 -> resistor, etc.)
            first_ref = refs[0] if refs else ""

            try:
                suggestion = suggester.suggest_for_component(
                    reference=first_ref,
                    value=value,
                    footprint=footprint,
                    existing_lcsc=None,
                )
            except LCSCForbiddenError:
                # API is globally unavailable -- emit a single warning and
                # fall back to cache for this and all remaining groups.
                api_forbidden = True
                logger.warning(
                    "JLCPCB API returned 403 Forbidden -- "
                    "falling back to enrichment cache for remaining groups. "
                    "Use --no-auto-lcsc to suppress."
                )
                report.entries.append(
                    _try_cache_fallback(cache, value, footprint, refs, group_items)
                )
                continue

            if suggestion.has_suggestion:
                best = suggestion.best_suggestion
                assert best is not None  # guarded by has_suggestion
                lcsc = best.lcsc_part

                # Validate the suggested part's value against the BOM
                # value before applying/caching (issue #3590: a wrong
                # API match cached without validation poisons every
                # later offline export).
                mismatch = find_value_mismatch(value, first_ref, part_description=best.description)
                if mismatch is None:
                    mismatch = check_lcsc_against_cache(cache, lcsc, value, first_ref)
                if mismatch is not None:
                    logger.warning(
                        "Rejecting LCSC auto-match %s for %s [%s]: %s",
                        lcsc,
                        value,
                        footprint,
                        mismatch.describe(),
                    )
                    report.entries.append(
                        EnrichmentEntry(
                            value=value,
                            footprint=footprint,
                            references=refs,
                            lcsc_part="",
                            source="unmatched",
                            error=(f"auto-match {lcsc} rejected: {mismatch.describe()}"),
                        )
                    )
                    continue

                # Write back to all items in the group
                for it in group_items:
                    it.lcsc = lcsc

                # Store the match in the enrichment cache for offline use
                if cache is not None:
                    cache.put_enrichment_match(
                        value,
                        footprint,
                        lcsc,
                        confidence=best.confidence,
                        part_type=best.type_str,
                    )

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
