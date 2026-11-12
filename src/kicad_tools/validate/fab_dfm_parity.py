"""Fab DFM parity table: which JLCDFM categories does ``kct check`` cover? (Issue #8845)

``kct check --mfr jlcpcb`` can pass while JLCPCB's own DFM tool (jlcdfm.com)
reports *danger*-level findings in categories kct has no rule for (epic #8838:
a READY 9/9 board got six danger categories).  This module is the versioned,
honest table of those categories -- one row per JLCDFM category with the kct
rule(s) covering it and a ``covered`` / ``partial`` / ``not_covered`` status --
plus :func:`evaluate_parity`, which turns it into the ``fab_dfm_parity`` report
section and the ``incomplete`` signal for the verdict.

Rules for editing the table:

* A sibling rule PR flips its own row (``not_covered`` -> ``partial`` /
  ``covered``) and fills ``kct_rules``.
* Every rule named in ``kct_rules`` of a ``covered`` / ``partial`` row must be a
  real ``kct check`` category (``cli/check_categories.py``); a test enforces it
  so the table cannot drift from the code.
* Bump :data:`TABLE_VERSION` on any row change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["covered", "partial", "not_covered"]

#: Version of the table below; bump on any row change.
TABLE_VERSION = 1

#: Where the observed categories/thresholds came from (counts are hand-recorded).
SOURCE = "https://github.com/rjwalters/kicad-tools/issues/8838"
RETRIEVED = "2026-11-12"

#: Manufacturer ids the table applies to (``jlcpcb`` and its ``jlcpcb-tier*`` profiles).
_MFR_FAMILY = "jlcpcb"


@dataclass(frozen=True)
class ParityRow:
    """One JLCDFM category and how well kct covers it."""

    category: str
    kct_rules: tuple[str, ...]
    status: Status
    danger_capable: bool
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "kct_rules": list(self.kct_rules),
            "status": self.status,
            "danger_capable": self.danger_capable,
            "note": self.note,
        }


#: One row per JLCDFM category from the #8838 table.  Danger-capable rows are
#: the categories JLCDFM reported at danger level on the observed 2 oz board.
PARITY_TABLE: tuple[ParityRow, ...] = (
    ParityRow(
        "trace_spacing",
        ("clearance",),
        "partial",
        True,
        "net-aware clearance only; same-net near-parallel copper unchecked. "
        "Observed 2 danger (0.08 mm).",
    ),
    ParityRow(
        "pad_spacing",
        ("clearance",),
        "partial",
        True,
        "same-net trace-to-pad proximity and 2 oz mask-bridge spacing unchecked. "
        "Observed 8 danger.",
    ),
    ParityRow(
        "solder_mask_opening_exposing_trace",
        ("mask_to_copper",),
        "partial",
        True,
        "mask_to_copper exists but is opt-in (--mask-copper-config), so it does not "
        "run by default. Observed 5 danger.",
    ),
    ParityRow("tht_to_smd", (), "not_covered", True, "no rule. Observed 5 danger."),
    ParityRow(
        "silkscreen_to_pad",
        ("silkscreen",),
        "partial",
        True,
        "silk_over_copper reported 3 warnings where JLCDFM reported 54 danger.",
    ),
    ParityRow(
        "silkscreen_to_hole",
        (),
        "not_covered",
        True,
        "no rule (silk over tented via holes). Observed 75 danger.",
    ),
    ParityRow("annular_ring", (), "not_covered", False, "floor is not copper-weight aware."),
    ParityRow("negative_soldermask_expansion", (), "not_covered", False, "no rule."),
    ParityRow("soldermask_opening_multiple_segments", (), "not_covered", False, "no rule."),
    ParityRow(
        "unconnected_trace_end",
        ("dangling_copper",),
        "covered",
        False,
        "parity with the observed 5 warnings.",
    ),
    ParityRow("sharp_trace_corner", (), "not_covered", False, "no rule."),
    ParityRow("missing_plated_through_hole", (), "not_covered", False, "no rule."),
    ParityRow(
        "silkscreen_line_width",
        ("silkscreen",),
        "partial",
        False,
        "only a text-thickness floor, not line width.",
    ),
    ParityRow("fiducial", ("fiducials",), "covered", False, "checked; no finding on the board."),
)


@dataclass(frozen=True)
class ParityReport:
    """Result of :func:`evaluate_parity` (the ``fab_dfm_parity`` report section)."""

    manufacturer: str
    rows: tuple[ParityRow, ...]
    incomplete_categories: tuple[str, ...]

    @property
    def incomplete(self) -> bool:
        return bool(self.incomplete_categories)

    def to_dict(self) -> dict:
        return {
            "table_version": TABLE_VERSION,
            "source": SOURCE,
            "retrieved": RETRIEVED,
            "manufacturer": self.manufacturer,
            "rows": [r.to_dict() for r in self.rows],
            "incomplete_categories": list(self.incomplete_categories),
        }


def applies_to(manufacturer: str | None) -> bool:
    """True when the parity table applies to *manufacturer* (JLCPCB family only)."""
    if not manufacturer:
        return False
    m = manufacturer.lower()
    return m == _MFR_FAMILY or m.startswith(_MFR_FAMILY + "-")


def evaluate_parity(
    manufacturer: str | None, table: tuple[ParityRow, ...] = PARITY_TABLE
) -> ParityReport | None:
    """Evaluate the parity table for *manufacturer*; ``None`` when it does not apply.

    A danger-capable ``not_covered`` row makes the report ``incomplete``;
    ``partial`` rows are listed but never trigger it.
    """
    if not applies_to(manufacturer):
        return None
    incomplete = tuple(r.category for r in table if r.danger_capable and r.status == "not_covered")
    return ParityReport(str(manufacturer), table, incomplete)


def format_parity_stanza(report: ParityReport) -> str:
    """Text rendering for ``kct check`` (printed after the Coverage stanza)."""
    lines = [
        "",
        f"Fab DFM parity (JLCDFM, table v{TABLE_VERSION}, {RETRIEVED}): "
        f"{len(report.incomplete_categories)} danger-capable categor"
        f"{'y' if len(report.incomplete_categories) == 1 else 'ies'} not covered",
    ]
    for r in report.rows:
        rules = ", ".join(r.kct_rules) or "-"
        flag = " [danger-capable]" if r.danger_capable else ""
        lines.append(f"  {r.status.replace('_', ' '):<11} {r.category}{flag}: {rules}")
    if report.incomplete:
        lines.append(
            "  INCOMPLETE: kct has no rule for the categories above; a pass here does "
            "not mean JLCDFM is clean (use --allow-incomplete to exit 0)."
        )
    return "\n".join(lines)
