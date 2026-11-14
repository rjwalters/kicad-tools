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
* Every rule named in ``kct_rules`` of a ``covered`` / ``partial`` row must be
  something ``kct check`` can actually emit -- a real category
  (``cli/check_categories.py``) or a finer rule id registered in
  ``DRCChecker.RULE_CATEGORY`` (``silk_hole_overlap`` under ``silkscreen``); a
  test enforces it so the table cannot drift from the code.
* The reverse drift -- a rule that shipped while its row still says
  ``not_covered`` -- is caught by ``FAB_CATEGORY_RULES`` in
  ``tests/test_fab_dfm_parity.py`` (issue #8902).  That mapping names the rule
  id each category *will* be covered by, including ids that do not exist yet,
  so registering one fails the test until this row is flipped.  A rule PR that
  forgets its row therefore cannot merge green; add the new id to the mapping
  in the same PR.
* Bump :data:`TABLE_VERSION` on any row change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["covered", "partial", "not_covered"]

#: Version of the table below; bump on any row change.
TABLE_VERSION = 3

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
        ("clearance", "physical_copper_gap"),
        "partial",
        True,
        "net-aware `clearance` always; the net-agnostic fab-view pass "
        "(`physical_copper_gap`, #8843) adds same-net copper but runs only when the "
        "effective --mfr is exactly `jlcpcb` -- a `jlcpcb-tier*` profile gets "
        "`clearance` alone. Observed 2 danger (0.08 mm).",
    ),
    ParityRow(
        "pad_spacing",
        ("clearance", "physical_copper_gap"),
        "partial",
        True,
        "as `trace_spacing`, plus the 2 oz pad mask-bridge check inside "
        "`physical_copper_gap`; that check runs on outer layers only (#8913) and "
        "judges a declared 2 oz outer weight only, otherwise reporting "
        "`physical_copper_gap_incomplete` at info. Observed 8 danger.",
    ),
    ParityRow(
        "solder_mask_opening_exposing_trace",
        ("mask_to_copper",),
        "partial",
        True,
        "`mask_to_copper` is default-on for an explicit --mfr `jlcpcb*` since #8842 "
        "(interim 0.09 mm danger / 0.15 mm warning); a bare `kct check` with no "
        "--mfr does not engage it, and the native path needs an unmodified saved "
        "source. Observed 5 danger. The rule has no same-net exclusion and traces "
        "are in its comparison set, so the fab's same-net track-in-pad-opening "
        "items are in scope; since #8914 an assessment that could not complete "
        "reports `mask_to_copper_incomplete` at info, so zero findings no longer "
        "read as a clean board. Whether the #8914 report's zero count was an "
        "incomplete run or a measurement gap is still unresolved.",
    ),
    ParityRow(
        "tht_to_smd",
        ("tht_smd_spacing",),
        "partial",
        True,
        "`tht_smd_spacing` (#8843) judges plated through-hole component pads plus, "
        "since #8914, vias whose drill is at least 1.0 mm (the fab classes a hole "
        "by size: the three top-side dangers kct missed on the #8914 board were all "
        "1.3-1.42 mm wire-anchor vias). Still skipped: same-footprint pad pairs, "
        "sub-1.0 mm fan-out vias, and the fab's via-to-via pairing (its `smd` side "
        "also counts a small exposed via pad: #8914 item 5, tracked in #8925). A "
        "pad shape the rule "
        "cannot measure now reports `tht_smd_spacing_incomplete` at info instead of "
        "vanishing. Fab-view gated -- runs only when the effective --mfr is exactly "
        "`jlcpcb`. Observed 5 danger.",
    ),
    ParityRow(
        "silkscreen_to_pad",
        ("silkscreen",),
        "partial",
        True,
        "silk_over_copper reported 3 warnings where JLCDFM reported 54 danger.",
    ),
    ParityRow(
        "silkscreen_to_hole",
        ("silk_hole_overlap",),
        "partial",
        True,
        "`silk_hole_overlap` (#8844) flags silk that *overlaps* a via/PTH/NPTH drill "
        "circle, tented vias included -- a rule id under the `silkscreen` category. "
        "Threshold is 0 mm, so a silk-to-hole gap narrower than JLCDFM's clearance "
        "but not touching is still unchecked, and text is tested as its bounding box "
        "rather than glyph outlines. Observed 75 danger, not reproduced against the "
        "live tool.",
    ),
    ParityRow(
        "annular_ring",
        (),
        "not_covered",
        False,
        "no rule: no copper-weight-aware annular-ring floor exists yet. Observed 100 warnings.",
    ),
    ParityRow(
        "negative_soldermask_expansion",
        ("mask_negative_expansion",),
        "covered",
        False,
        "`mask_negative_expansion` (#8847) warns on every opening whose effective "
        "margin is < 0 mm; default-on pure geometry read from the saved source, so "
        "an unsaved or edited board reports coverage unknown rather than a pass. "
        "The recorded 4 warnings were not re-measured.",
    ),
    ParityRow(
        "soldermask_opening_multiple_segments",
        ("mask_multi_segment",),
        "covered",
        False,
        "`mask_multi_segment` (#8847) warns on every opening whose geometry is more "
        "than one disjoint piece; same default-on saved-source snapshot as "
        "`mask_negative_expansion`. The recorded 21 warnings were not re-measured.",
    ),
    ParityRow(
        "unconnected_trace_end",
        ("dangling_copper",),
        "covered",
        False,
        "parity with the observed 5 warnings.",
    ),
    ParityRow(
        "sharp_trace_corner",
        (),
        "not_covered",
        False,
        "no rule; `sharp_trace_corner` is in flight on #8840. Observed 3 warnings.",
    ),
    ParityRow(
        "missing_plated_through_hole",
        (),
        "not_covered",
        False,
        "no rule; `missing_plated_hole` is in flight on #8840. Observed 2 warnings.",
    ),
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
