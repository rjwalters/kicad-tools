"""BOM part-number health checks.

Covers the ``check_bom_health`` gap called out by issue #4899 (item 4 of
#4880's Konnect ideas audit). This repo has no supplier-API integration, so
"lifecycle" and "stock" status (which Konnect's ``check_bom_health`` also
covers) are genuinely out of scope -- this check is limited to what is
representable from data already present on the PCB: whether every
BOM-eligible component carries a manufacturer/distributor part-number
property.

Follows the #4011 vacuity-guard discipline (mirrored throughout this
module via :class:`~kicad_tools.explain.mistakes.CheckIncomplete`): if
*no* component on the board defines any MPN/LCSC-style property, this
board simply doesn't track part numbers in footprint properties at all,
and flagging every single component as "missing" would be noise, not
signal. In that case the check reports ``incomplete`` rather than a
(false) clean pass or a spam of findings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import CheckIncomplete, Mistake, MistakeCategory

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint

# Property names (case-insensitive) recognized as carrying a sourceable
# part number. Mirrors the field-name mapping already used by
# ``schema.bom.extract_bom_from_pcb`` for consistency.
_PART_NUMBER_PROPERTY_NAMES = frozenset(
    {
        "mpn",
        "mfr_pn",
        "manufacturer_pn",
        "pn",
        "lcsc",
        "lcsc_pn",
        "lcsc part",
        "jlc",
        "jlcpcb",
    }
)


def _part_number(fp: Footprint) -> str:
    for name, value in fp.properties.items():
        if not value:
            continue
        if name.strip().lower() in _PART_NUMBER_PROPERTY_NAMES:
            return value
    return ""


class BomFieldHealthCheck:
    """Check that BOM-eligible components carry a part-number property.

    Skips footprints excluded from the BOM (``exclude_from_bom``) and DNP
    (do-not-populate) components, since neither is actually procured.
    """

    category = MistakeCategory.BOM_HEALTH

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check BOM-eligible components for a part-number property.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for components missing a part number

        Raises:
            CheckIncomplete: If no component on the board defines any
                MPN/LCSC-style property, meaning this board's footprint
                properties don't carry part-number data at all and no
                meaningful per-component verdict can be reached.
        """
        candidates = [fp for fp in pcb.footprints if self._is_bom_candidate(fp)]
        if not candidates:
            return []

        tracked = [fp for fp in candidates if _part_number(fp)]
        if not tracked:
            raise CheckIncomplete(
                "no BOM-eligible component on this board defines an "
                "MPN/LCSC-style property (checked "
                f"{sorted(_PART_NUMBER_PROPERTY_NAMES)}); this board may not "
                "track manufacturer part numbers in footprint properties, so "
                "BOM part-number coverage cannot be assessed"
            )

        mistakes: list[Mistake] = []
        for fp in candidates:
            if _part_number(fp):
                continue
            mistakes.append(
                Mistake(
                    category=MistakeCategory.BOM_HEALTH,
                    severity="warning",
                    title="Component missing manufacturer part number",
                    components=[fp.reference],
                    location=fp.position,
                    explanation=(
                        f"{fp.reference} ({fp.value}) has no MPN/LCSC "
                        "part-number property, while other components on "
                        "this board do. Without a specific part number, this "
                        "component cannot be reliably re-sourced or ordered "
                        "for assembly."
                    ),
                    fix_suggestion=(
                        f"Add an 'MPN' (or distributor-specific, e.g. 'LCSC') "
                        f"property to {fp.reference} with the manufacturer "
                        "part number used elsewhere on this board."
                    ),
                    learn_more_url="docs/mistakes/bom-health.md",
                )
            )
        return mistakes

    def _is_bom_candidate(self, fp: Footprint) -> bool:
        return not fp.exclude_from_bom and not fp.dnp
