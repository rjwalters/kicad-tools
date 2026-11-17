"""Board-ID / revision silkscreen DRC rule (Issue #8532).

A fabricated board with no revision on its silkscreen cannot be told apart
from the previous spin.  This rule looks for silkscreen text -- board-level
``gr_text`` or user text inside a footprint -- naming the board revision:
either the title block's ``rev`` value, or (when that is empty) a generic
``rev`` / ``vN`` token.  ``kct pcb add-board-info`` writes
``{project} {rev} {date}`` and so satisfies it.

Rule ID generated:
    - board_id_missing: no silkscreen text names the revision.

A fully unpopulated board (no populated side, as for ``fiducials_missing``)
raises nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

BOARD_ID_MISSING_RULE_ID = "board_id_missing"

_SILK_LAYERS = frozenset({"F.SilkS", "B.SilkS"})
# Fallback when the title block carries no revision to look for.
_GENERIC_REV = re.compile(r"(?<![A-Za-z0-9])(rev\.?\s*[A-Za-z0-9]|v\s*\d)", re.IGNORECASE)


def _silk_texts(pcb: PCB) -> Iterator[str]:
    for text in pcb.texts:
        if text.layer in _SILK_LAYERS and not text.hidden:
            yield text.text
    for fp in pcb.footprints:
        for ftext in fp.texts:
            if ftext.text_type == "user" and ftext.layer in _SILK_LAYERS and not ftext.hidden:
                yield ftext.text


class BoardIdRule(DRCRule):
    """Check that the silkscreen names the board revision."""

    rule_id = BOARD_ID_MISSING_RULE_ID
    name = "Board ID"
    description = "Checks that silkscreen text names the board revision"

    def __init__(self, *, severity: str = "warning") -> None:
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"invalid severity {severity!r}")
        self.severity = severity

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return a single ``board_id_missing`` warning when no silk text names the revision."""
        del design_rules
        from kicad_tools.cli.pcb_add_fiducials import _populated_sides

        results = DRCResults()
        results.rules_checked = 1
        if not _populated_sides(pcb):
            return results
        rev = (pcb.revision or "").strip()
        rev_re = re.compile(
            r"(?<![A-Za-z0-9])(?:rev\.?\s*)?" + re.escape(rev) + r"(?![A-Za-z0-9])", re.IGNORECASE
        )
        for content in _silk_texts(pcb):
            if rev:
                if rev_re.search(content):
                    return results
            elif _GENERIC_REV.search(content):
                return results
        what = f"revision {pcb.revision!r}" if rev else "a revision"
        results.add(
            DRCViolation(
                rule_id=BOARD_ID_MISSING_RULE_ID,
                severity=self.severity,
                message=(
                    f"no silkscreen text names {what} -- run `kct pcb add-board-info`, "
                    f"or waive via .kct_waivers.json"
                ),
            )
        )
        return results
