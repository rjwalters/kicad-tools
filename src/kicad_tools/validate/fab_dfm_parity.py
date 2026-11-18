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
* ``count_rule_ids`` (issue #8940) is the finding-attribution key: the exact
  ``rule_id`` s whose findings count toward that row's kct error/warning
  counts in the parity comparison.  It must be a *partition* -- no rule id
  may appear in two rows -- and every entry must be an id ``kct check``
  actually emits; tests enforce both.  An entry may carry a ``#band``
  qualifier (``physical_copper_gap#trace`` / ``#pad``, issue #8972): it
  matches only findings graded against that fab band (see
  :func:`attribution_key`), which is how one rule id spanning the trace and
  pad fab bands (issue #8938) divides across the ``trace_spacing`` /
  ``pad_spacing`` rows; the bare id still matches the bandless emit sites
  (the legacy opt-in pass, zone-fill ``info`` findings), which stay on
  ``pad_spacing``.  A ``#corner_class`` qualifier works the same way
  (``sharp_trace_corner#bend``, issue #9037).  ``kct_rules`` is coverage *provenance*
  at mixed granularity (mostly category ids) and is deliberately NOT the
  attribution key.  Ids deliberately unattributed: ``*_incomplete`` run-state
  infos, ``clearance_net0_bridge`` and the zone-clearance ids (the fab does
  not judge zone-fill clearance), the kct-only silk advisories
  (``silk_over_mask_opening``, ``silk_edge_clearance``, ``silk_overlap``,
  ``silk_ref_obscured``, ``silkscreen_text_height``), and the
  ``sharp_trace_corner#junction`` population (issue #9037: 3-or-more-segment
  vertices are kct-only over-reporting under the #8848 operator ruling --
  the fab flagged none of them).
  ``count_rule_ids`` adds no parity *claim*; re-keying an existing row's
  attribution (as #8972 did) changes the row's content, which moves
  :data:`TABLE_VERSION` (and the stanza header) with it automatically.
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
* Do not hand-edit a version: :data:`TABLE_VERSION` is a content hash of the
  rows (see :func:`compute_table_version`), so it changes by itself on any row
  change and two PRs editing different rows cannot collide on it (issue #9242).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .violations import DRCViolation

Status = Literal["covered", "partial", "not_covered"]

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
    #: Finding attribution key (issue #8940): the exact ``rule_id`` s whose
    #: findings count toward this row's kct error/warning counts.  Empty for
    #: rows kct does not measure at all (rendered "n/a", not 0).  Must form a
    #: partition across rows -- see the module docstring.
    count_rule_ids: tuple[str, ...] = ()
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
        # Non-pad copper spacing: the pair-type clearance ids with no pad on
        # either side (segment/via gaps -- the fab's trace_spacing
        # population), plus the trace-band-graded fab-view findings
        # (``#trace`` qualified key, issue #8972).
        (
            "clearance_segment_segment",
            "clearance_segment_via",
            "clearance_via_via",
            "physical_copper_gap#trace",
        ),
        "net-aware `clearance` always; the net-agnostic fab-view pass "
        "(`physical_copper_gap`, #8843) adds same-net copper but runs only when the "
        "effective --mfr is exactly `jlcpcb` -- a `jlcpcb-tier*` profile gets "
        "`clearance` alone. Observed 2 danger (0.08 mm). Since #8938 the pass "
        "judges gaps with no pad and no zone fill on either side at "
        "trace_spacing's recorded bands from the resolved stackup's "
        "dfm_thresholds entry (previously hardcoded 0.10/0.15 mm constants): "
        "below the danger edge is an error; with no recorded warning_mm there "
        "is no warning band, and gaps at or above the danger edge are not "
        "reported (the fab recorded 0 danger / 0 warning for trace_spacing on "
        "the 4-layer 1 oz #8938 board). Since #8972 those trace-band findings "
        "count toward this row, not `pad_spacing`.",
    ),
    ParityRow(
        "pad_spacing",
        ("clearance", "physical_copper_gap"),
        "partial",
        True,
        # Pad-involving clearance ids plus the pad populations of the
        # physical pass: pad-band-graded and mask-bridge findings via the
        # ``#pad`` qualified key, and the bare id for the bandless emit
        # sites (the legacy opt-in pass, zone-fill `info` findings) so the
        # partition stays total (issue #8972).
        (
            "clearance_pad_pad",
            "clearance_pad_segment",
            "clearance_pad_via",
            "physical_copper_gap",
            "physical_copper_gap#pad",
        ),
        "as `trace_spacing`, plus the 2 oz pad mask-bridge check inside "
        "`physical_copper_gap`; that check runs on outer layers only (#8913) and "
        "judges a declared 2 oz outer weight only, otherwise reporting "
        "`physical_copper_gap_incomplete` at info. Observed 8 danger. Since "
        "#8938: gaps with a pad on either side (no zone fill) are judged at "
        "pad_spacing's recorded bands, and a same-net gap below the danger "
        "edge is an ERROR -- the fab cannot see nets and flags it regardless "
        "(the 4-layer 1 oz board's 6 danger are same-net via/track at "
        "0.065-0.099 mm from a pad edge); the finding is labelled `same_net` "
        "so the class can be waived deliberately. Reported, not reproduced "
        "(#9034): on one further 4-layer board (not committed) kct reported "
        "19 errors against the fab's 6 danger + 1 warning; the surplus cause "
        "is unmeasured, but kct emits at most one finding per copper-source "
        "pair per layer (closest measurement wins -- measured on the #9034 "
        "fixture), so it cannot come from per-edge inflation within a pair. "
        "No fab-sourced edge justifies a threshold change, so the stricter "
        "same-net error stays (#8938). Zone-fill (pour) gaps are "
        "kct's own advisory only and report at info, not tiered: JLCDFM does "
        "not judge pour clearance -- 1,516 pour-to-pad gaps at 0.10-0.13 mm "
        "went unflagged on the #8938 board -- so kct's warning count for this "
        "category exceeds the fab's by that population by design.",
    ),
    ParityRow(
        "solder_mask_opening_exposing_trace",
        ("mask_to_copper",),
        "partial",
        True,
        ("mask_to_copper",),
        "`mask_to_copper` is default-on for an explicit --mfr `jlcpcb*` since #8842 "
        "(interim 0.09 mm danger / 0.15 mm warning); a bare `kct check` with no "
        "--mfr does not engage it, and the native path needs an unmodified saved "
        "source. Observed 5 danger, all same-net: a track running beside, along, or "
        "leaving and jogging beside its own pad at 0-0.08 mm (#8914); those shapes "
        "stay errors at the 0.09 mm band. Since #8858 one shape is `within_own_pad` "
        "at info: a straight track entering its own plain pad once, at 45 degrees or "
        "steeper, exposing nothing beyond the pad and leaving no more copper under "
        "mask beside the opening than that one crossing (or a track lying wholly on "
        "the pad). The fab did not flag plain entries (#8914; 655 entries on the "
        "consumer board, 0 flagged). On boards 00-07 (0 mm expansion) that was 963 "
        "of 1,542 errors. Pass-through, along-edge and straddling tracks, shallow "
        "entries, arcs, vias and pours touching the pad, expansion rings wider than "
        "the construction budget, and different-net copper keep their verdict. "
        "Since #8914 an assessment that could not complete reports "
        "`mask_to_copper_incomplete` at info, so zero findings no longer read as a "
        "clean board.",
    ),
    ParityRow(
        "tht_to_smd",
        ("tht_smd_spacing",),
        "partial",
        True,
        ("tht_smd_spacing",),
        "`tht_smd_spacing` (#8843) judges plated through-hole component pads plus, "
        "since #8914, vias whose drill is at least 1.0 mm (the fab classes a hole "
        "by size: the three top-side dangers kct missed on the #8914 board were all "
        "1.3-1.42 mm wire-anchor vias). Since #8925 an anchor via is also "
        "judged against any exposed (untented) via annulus -- the fab's `smd` side "
        "counts a small exposed via pad (#8914 item 5); THT component pads are not "
        "paired with via annuli, and tented annuli are skipped (the fab's exact "
        "tenting test is unverified). Still skipped: same-footprint pad pairs and "
        "sub-1.0 mm fan-out vias as the hole side. A "
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
        # The geometric pass (silk over exposed copper) and the text-over-pad
        # heuristic; NOT silk_over_mask_opening (no pad involved).
        ("silk_over_copper", "silkscreen_over_pad"),
        "silk_over_copper reported 3 warnings where JLCDFM reported 54 danger.",
    ),
    ParityRow(
        "silkscreen_to_hole",
        ("silk_hole_overlap",),
        "partial",
        True,
        ("silk_hole_overlap",),
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
        (),
        "no rule: no copper-weight-aware annular-ring floor exists yet. Observed 100 warnings.",
    ),
    ParityRow(
        "negative_soldermask_expansion",
        ("mask_negative_expansion",),
        "covered",
        False,
        ("mask_negative_expansion",),
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
        ("mask_multi_segment",),
        "`mask_multi_segment` (#8847) warns on every opening whose geometry is more "
        "than one disjoint piece; same default-on saved-source snapshot as "
        "`mask_negative_expansion`. The recorded 21 warnings were not re-measured.",
    ),
    ParityRow(
        "unconnected_trace_end",
        ("dangling_copper",),
        "covered",
        False,
        # Both dangling-end ids the row's rule emits (track ends and
        # under-bonded vias are both unconnected-end defects).
        ("track_dangling", "via_dangling"),
        "parity with the observed 5 warnings.",
    ),
    ParityRow(
        "sharp_trace_corner",
        ("sharp_trace_corner",),
        "covered",
        False,
        # Only the two-segment open-air bend is the fab-equivalent subset
        # (issue #9037); the junction population (``#junction``) is kct-only
        # over-reporting, reported by operator ruling on PR #8848 and left
        # deliberately unattributed so the tallies cannot read as fab-aligned.
        ("sharp_trace_corner#bend",),
        "`sharp_trace_corner` (#8840) reports all 3 observed vertices (pinned in its "
        "tests). Only its two-segment open-air bend population maps to JLCDFM's "
        "category (``#bend``): the fab flagged 3 of the 8 bends on that board, and "
        "no criterion separates the other 5 (leg length and width do not), so they "
        "are recorded as unexplained, not covered (#9037). The rule's junction "
        "findings (3+ segments at the vertex) are kct-only over-reporting -- 39 on "
        "that board, 0 flagged -- carried as warnings under the #8848 operator "
        "ruling, separable since #9037 via ``corner_class='junction'`` and not "
        "attributed here. A vertex inside same-net pad copper is not reported "
        "since #8848 (the board's 5 in-pad findings should be gone; pinned by "
        "tests, not re-measured on the board).",
    ),
    ParityRow(
        "missing_plated_through_hole",
        ("missing_plated_hole",),
        "partial",
        False,
        ("missing_plated_hole",),
        "`missing_plated_hole` (#8840) flags same-net top/bottom SMD pads whose "
        "centres coincide (0.05 mm) with no same-net plated hole inside both; "
        "overlapping pads with offset centres are not checked. The observed 2 "
        "warnings were not re-measured.",
    ),
    ParityRow(
        "silkscreen_line_width",
        ("silkscreen",),
        "partial",
        False,
        # The two >= 0.15 mm stroke/thickness floors this row's note names;
        # silkscreen_text_height is a different (unknown-bucket) floor.
        ("silkscreen_line_width", "silkscreen_text_thickness"),
        "`silkscreen_line_width` (every silk graphic primitive) and "
        "`silkscreen_text_thickness` (text) enforce JLCPCB's published >= 0.15 mm. "
        "Not covered: JLCDFM flags silk that meets that floor (#8844: 63 "
        "warnings on gerbers with no silk stroke below 0.150 mm). How JLCDFM "
        "selects the items it flags is unknown (#8943).",
    ),
    ParityRow(
        "fiducial",
        ("fiducials",),
        "covered",
        False,
        ("fiducials_missing",),
        "checked; no finding on the board.",
    ),
)


def compute_table_version(rows: Sequence[ParityRow]) -> str:
    """Content-derived identifier for a parity table (issue #9242).

    The first 8 hex chars of the SHA-256 of the rows' canonical JSON.  Every
    :class:`ParityRow` field is included (``dataclasses.asdict``, not
    :meth:`ParityRow.to_dict`, which omits ``count_rule_ids``), keys are
    sorted and tuples serialize as lists, so the value is identical across
    runs, processes and Python versions and changes iff a row changes.
    """
    canonical = json.dumps([asdict(r) for r in rows], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


#: Content hash of :data:`PARITY_TABLE`; derived, never hand-edited, so two PRs
#: touching different rows cannot claim the same number.  Row history lives in
#: git and ``changelog.d/``.
TABLE_VERSION: str = compute_table_version(PARITY_TABLE)


@dataclass(frozen=True)
class RowComparison:
    """One row's kct-vs-fab count comparison (issue #8940).

    ``None`` counts mean "not measured": ``kct_*`` is ``None`` when the row
    has no ``count_rule_ids`` (kct has no rule at all, so 0 would read as
    "clean"); ``fab_*`` is ``None`` when the recorded result carries no entry
    for the category.
    """

    category: str
    kct_errors: int | None
    kct_warnings: int | None
    fab_danger: int | None
    fab_warning: int | None
    #: The row's headline signal (AC 4): the fab recorded danger, kct covers
    #: the category, and kct still reports zero errors.
    fab_only_danger: bool = False

    def to_dict(self) -> dict:
        return {
            "kct_errors": self.kct_errors,
            "kct_warnings": self.kct_warnings,
            "fab_danger": self.fab_danger,
            "fab_warning": self.fab_warning,
            "fab_only_danger": self.fab_only_danger,
        }


@dataclass(frozen=True)
class ParityReport:
    """Result of :func:`evaluate_parity` (the ``fab_dfm_parity`` report section)."""

    manufacturer: str
    rows: tuple[ParityRow, ...]
    #: The JLCDFM parity categories that are danger-capable and whose
    #: kct-vs-fab comparison is unavailable: either ``not_covered`` by this
    #: static table (#9062) or backed by a kct rule that did not complete
    #: this run (#9234, via :func:`evaluate_parity`'s ``incomplete_rules``).
    #: The static subset alone -- the one that drives the ``fab_dfm_parity``
    #: entry in ``meta_checks.incomplete_categories`` and the verdict -- is
    #: :attr:`table_incomplete_categories`; the run-state subset is
    #: :attr:`run_incomplete_categories`.
    incomplete_categories: tuple[str, ...]
    #: Per-category kct-vs-fab counts; ``None`` unless a recorded result was
    #: supplied (issue #8940).
    comparisons: tuple[RowComparison, ...] | None = None
    #: Recorded-result categories with no parity-table row (issue #8940);
    #: surfaced instead of silently dropped.
    unmapped_recorded: tuple[str, ...] = ()

    @property
    def table_incomplete_categories(self) -> tuple[str, ...]:
        """The static ``not_covered`` subset (#9062's table-only contract)."""
        return tuple(
            r.category for r in self.rows if r.danger_capable and r.status == "not_covered"
        )

    @property
    def run_incomplete_categories(self) -> tuple[str, ...]:
        """Categories named only because their kct rule did not complete (#9234)."""
        table = set(self.table_incomplete_categories)
        return tuple(c for c in self.incomplete_categories if c not in table)

    @property
    def table_incomplete(self) -> bool:
        """A danger-capable row kct has no rule for (#8845, run-independent)."""
        return bool(self.table_incomplete_categories)

    @property
    def incomplete(self) -> bool:
        return bool(self.incomplete_categories)

    def to_dict(self) -> dict:
        data: dict[str, object] = {
            "table_version": TABLE_VERSION,
            "source": SOURCE,
            "retrieved": RETRIEVED,
            "manufacturer": self.manufacturer,
            "rows": [r.to_dict() for r in self.rows],
            "incomplete_categories": list(self.incomplete_categories),
        }
        if self.comparisons is not None:
            data["comparison"] = {c.category: c.to_dict() for c in self.comparisons}
        if self.unmapped_recorded:
            data["unmapped_recorded_categories"] = list(self.unmapped_recorded)
        return data


class RecordedResultError(ValueError):
    """A recorded fab DFM result was unreadable or malformed (issue #8940)."""


def load_recorded_result(path: str | Path) -> dict[str, dict[str, int]]:
    """Read and validate a recorded fab DFM result (issue #8940).

    The file must be JSON in the shape of
    ``tests/fixtures/fab_dfm_parity/jlcdfm_*.json``: an object with a
    ``counts`` mapping of category name to ``{"danger": int, "warning": int}``.
    Returns just the ``counts`` mapping.  Raises :class:`RecordedResultError`
    with an operator-readable message on any problem -- never a silent fall
    back to the uncompared stanza.
    """
    import json

    p = Path(path)
    try:
        text = p.read_text()
    except OSError as exc:
        raise RecordedResultError(f"cannot read {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RecordedResultError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RecordedResultError(f"{p}: expected a JSON object, got {type(data).__name__}")
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise RecordedResultError(f"{p}: no 'counts' object at the top level")
    for category, entry in counts.items():
        if not isinstance(category, str) or not isinstance(entry, dict):
            raise RecordedResultError(f"{p}: counts entry {category!r} is not an object")
        for key in ("danger", "warning"):
            value = entry.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RecordedResultError(
                    f"{p}: counts[{category!r}][{key!r}] must be a non-negative integer, "
                    f"got {value!r}"
                )
    return counts


def applies_to(manufacturer: str | None) -> bool:
    """True when the parity table applies to *manufacturer* (JLCPCB family only)."""
    if not manufacturer:
        return False
    m = manufacturer.lower()
    return m == _MFR_FAMILY or m.startswith(_MFR_FAMILY + "-")


def attribution_key(v: DRCViolation) -> str:
    """The finding-attribution key a ``count_rule_ids`` entry matches against.

    ``rule_id#fab_band`` for a fab-band-graded finding (issue #8972:
    ``physical_copper_gap`` is one rule id spanning the trace and pad fab
    bands of issue #8938, and the band it was graded against is carried on
    :attr:`DRCViolation.fab_band`), ``rule_id#corner_class`` for a classified
    ``sharp_trace_corner`` finding (issue #9037: the bend population maps to
    JLCDFM's category, the junction population is kct-only), the bare
    ``rule_id`` otherwise.  The qualifier is attribution-only -- the rule
    id, and so ``DRCViolation.key`` / every ``.kct_waivers.json`` entry, is
    unchanged.
    """
    if v.fab_band:
        return f"{v.rule_id}#{v.fab_band}"
    if v.corner_class:
        return f"{v.rule_id}#{v.corner_class}"
    return v.rule_id


def evaluate_parity(
    manufacturer: str | None,
    table: tuple[ParityRow, ...] = PARITY_TABLE,
    *,
    findings: Sequence[DRCViolation] = (),
    recorded_counts: Mapping[str, Mapping[str, int]] | None = None,
    incomplete_rules: Sequence[str] = (),
) -> ParityReport | None:
    """Evaluate the parity table for *manufacturer*; ``None`` when it does not apply.

    A danger-capable ``not_covered`` row makes the report ``incomplete``;
    ``partial`` rows are listed but never trigger it.  With
    *incomplete_rules* (#9234) -- kct rule ids whose check did not complete
    this run, e.g. ``("mask_to_copper",)`` -- every danger-capable row that
    counts findings from one of those rules (``count_rule_ids``, ignoring any
    ``#band`` qualifier) is also named in ``incomplete_categories``: the
    fab-parity comparison for that category is unavailable on this run.  That
    run-state subset does not by itself reach ``meta_checks`` or the verdict
    (see :attr:`ParityReport.table_incomplete`), which stay keyed on the
    static ``not_covered`` rows as in #9062.

    With *recorded_counts* (issue #8940) the report also carries a
    :class:`RowComparison` per row: kct error/warning counts over *findings*
    attributed by each row's ``count_rule_ids`` (the same ``is_error`` /
    ``is_warning`` predicates the headline totals use, so the tallies cannot
    disagree) beside the fab's recorded danger/warning counts.  Rows kct does
    not measure get ``None`` kct counts, not zero.
    """
    if not applies_to(manufacturer):
        return None
    rule_gaps = {rule.split("#", 1)[0] for rule in incomplete_rules}
    incomplete = tuple(
        r.category
        for r in table
        if r.danger_capable
        and (
            r.status == "not_covered"
            or any(rule.split("#", 1)[0] in rule_gaps for rule in r.count_rule_ids)
        )
    )
    comparisons: tuple[RowComparison, ...] | None = None
    unmapped: tuple[str, ...] = ()
    if recorded_counts is not None:
        claimed = {r.category for r in table}
        unmapped = tuple(sorted(set(recorded_counts) - claimed))
        comps = []
        for r in table:
            kct_errors: int | None
            kct_warnings: int | None
            if r.count_rule_ids:
                ids = frozenset(r.count_rule_ids)
                kct_errors = sum(1 for v in findings if attribution_key(v) in ids and v.is_error)
                kct_warnings = sum(
                    1 for v in findings if attribution_key(v) in ids and v.is_warning
                )
            else:
                kct_errors = None
                kct_warnings = None
            recorded = recorded_counts.get(r.category)
            fab_danger = int(recorded["danger"]) if recorded is not None else None
            fab_warning = int(recorded["warning"]) if recorded is not None else None
            comps.append(
                RowComparison(
                    r.category,
                    kct_errors,
                    kct_warnings,
                    fab_danger,
                    fab_warning,
                    fab_only_danger=(
                        fab_danger is not None
                        and fab_danger > 0
                        and kct_errors is not None
                        and kct_errors == 0
                    ),
                )
            )
        comparisons = tuple(comps)
    return ParityReport(
        str(manufacturer), table, incomplete, comparisons=comparisons, unmapped_recorded=unmapped
    )


def format_parity_stanza(report: ParityReport) -> str:
    """Text rendering for ``kct check`` (printed after the Coverage stanza).

    Without a recorded result the output is byte-identical to the pre-#8940
    stanza (AC 2); with one, each row gains ``| kct ... | fab ...`` count
    columns, ``fab danger, kct 0 errors`` rows are visibly marked (AC 4), and
    rows kct cannot measure read ``n/a`` rather than 0 (AC 7).
    """
    lines = [
        "",
        f"Fab DFM parity (JLCDFM, table v{TABLE_VERSION}, {RETRIEVED}): "
        f"{len(report.table_incomplete_categories)} danger-capable categor"
        f"{'y' if len(report.table_incomplete_categories) == 1 else 'ies'} not covered",
    ]
    comparisons = (
        {c.category: c for c in report.comparisons} if report.comparisons is not None else None
    )
    for r in report.rows:
        rules = ", ".join(r.kct_rules) or "-"
        flag = " [danger-capable]" if r.danger_capable else ""
        line = f"  {r.status.replace('_', ' '):<11} {r.category}{flag}: {rules}"
        if comparisons is not None:
            c = comparisons[r.category]
            if c.kct_errors is None:
                kct = "n/a (not measured)"
            else:
                kct = f"{c.kct_errors} err, {c.kct_warnings} warn"
            fab = (
                "not recorded"
                if c.fab_danger is None
                else f"{c.fab_danger} danger, {c.fab_warning} warn"
            )
            line += f" | kct {kct} | fab {fab}"
            if c.fab_only_danger:
                line += " | ** fab danger, kct 0 errors **"
        lines.append(line)
    if report.unmapped_recorded:
        lines.append(
            "  recorded categories not in the parity table: " + ", ".join(report.unmapped_recorded)
        )
    if report.table_incomplete:
        lines.append(
            "  INCOMPLETE: kct has no rule for the categories above; a pass here does "
            "not mean JLCDFM is clean (use --allow-incomplete to exit 0)."
        )
    if report.run_incomplete_categories:
        # Run-state incompleteness (#9234): kct HAS a rule for these
        # categories, but it did not complete this run, so the counts beside
        # them are not comparable and a zero must not read as parity.
        lines.append(
            "  INCOMPLETE (this run): "
            + ", ".join(report.run_incomplete_categories)
            + ": the kct check behind this category did not complete, so its "
            "kct-vs-fab comparison is unavailable (see "
            "meta_checks.incomplete_categories; use --allow-incomplete to exit 0)"
        )
    return "\n".join(lines)
