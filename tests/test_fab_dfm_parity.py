"""Fab DFM parity table, verdict flip and recorded-counts bite test (Issue #8845)."""

from __future__ import annotations

import json
from itertools import combinations_with_replacement
from pathlib import Path

import pytest

from kicad_tools.cli.check_categories import CHECK_CATEGORIES
from kicad_tools.validate import fab_dfm_parity as fdp

FIX = Path(__file__).parent / "fixtures" / "fab_dfm_parity"
RECORDED = json.loads((FIX / "jlcdfm_8838_board.json").read_text())["counts"]
ROWS = {r.category: r for r in fdp.PARITY_TABLE}

#: Every recorded real-board case.  #8938 adds the 4-layer 1 oz board beside
#: the original 2 oz one; the table's properties are asserted against the
#: UNION across boards, not either board alone.
RECORDED_BOARDS = (
    "jlcdfm_8838_board.json",
    "jlcdfm_8938_board.json",
)


def _all_recorded_counts():
    """The per-board ``counts`` dicts, folded over every recorded board."""
    return [json.loads((FIX / name).read_text())["counts"] for name in RECORDED_BOARDS]


#: JLCDFM category -> the ``kct check`` rule id(s) that cover it (Issue #8902).
#:
#: Maintained by hand, and deliberately allowed to name ids that do **not**
#: exist yet: :func:`test_registered_rule_forbids_not_covered` fails as soon as
#: a named id becomes a real rule while its ``PARITY_TABLE`` row still says
#: ``not_covered``.  That is the guard -- shipping a rule without flipping its
#: row turns the table's own test red instead of leaving a stale "no rule" note
#: behind (the drift this issue found: ``tht_to_smd`` said "no rule" three PRs
#: after ``tht_smd_spacing`` shipped).
#:
#: An empty tuple means "no rule id is planned for this category yet" and is
#: exempt from the guard; ``annular_ring`` is the only such row (no
#: copper-weight-aware floor is designed).  Rows whose rule is in flight name
#: the id that PR will register.
FAB_CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "trace_spacing": ("clearance", "physical_copper_gap"),
    "pad_spacing": ("clearance", "physical_copper_gap"),
    "solder_mask_opening_exposing_trace": ("mask_to_copper",),
    "tht_to_smd": ("tht_smd_spacing",),
    "silkscreen_to_pad": ("silkscreen",),
    # In flight on #8844 (PR #8854) as a rule id under the ``silkscreen`` category.
    "silkscreen_to_hole": ("silk_hole_overlap",),
    "annular_ring": (),
    "negative_soldermask_expansion": ("mask_negative_expansion",),
    "soldermask_opening_multiple_segments": ("mask_multi_segment",),
    "unconnected_trace_end": ("dangling_copper",),
    # Both in flight on #8840 (PR #8848) as new ``kct check`` categories.
    "sharp_trace_corner": ("sharp_trace_corner",),
    "missing_plated_through_hole": ("missing_plated_hole",),
    "silkscreen_line_width": ("silkscreen",),
    "fiducial": ("fiducials",),
}


def _implemented_rule_ids() -> frozenset[str]:
    """Every id ``kct check`` can emit findings under: categories + rule ids.

    ``CHECK_CATEGORIES`` alone is not enough -- a rule can ship as a finer
    ``rule_id`` inside an existing category (``silk_hole_overlap`` under
    ``silkscreen``), in which case it is registered in
    :data:`DRCChecker.RULE_CATEGORY` (the report-bucket table) instead.
    """
    from kicad_tools.validate.checker import DRCChecker

    return frozenset(CHECK_CATEGORIES) | frozenset(DRCChecker.RULE_CATEGORY)


def _drifted_rows(
    table: tuple[fdp.ParityRow, ...],
    mapping: dict[str, tuple[str, ...]],
    implemented: frozenset[str],
) -> list[str]:
    """Categories whose mapped rules all exist yet whose row says ``not_covered``."""
    rows = {r.category: r for r in table}
    drifted = []
    for category, rules in mapping.items():
        row = rows.get(category)
        if row is None or not rules:
            continue
        if row.status == "not_covered" and all(r in implemented for r in rules):
            drifted.append(category)
    return sorted(drifted)


def test_rules_are_real_check_categories():
    """A covered/partial row must name something ``kct check`` can emit.

    That is a ``CHECK_CATEGORIES`` category *or* a finer rule id registered in
    ``DRCChecker.RULE_CATEGORY`` -- ``silkscreen_to_hole`` is covered by
    ``silk_hole_overlap``, a rule id under the ``silkscreen`` category (#8844).
    """
    implemented = _implemented_rule_ids()
    for r in fdp.PARITY_TABLE:
        if r.status in ("covered", "partial"):
            assert r.kct_rules, r.category
            for rule in r.kct_rules:
                assert rule in implemented, (r.category, rule)


def test_table_matches_recorded_categories():
    seen: set[str] = set()
    for counts in _all_recorded_counts():
        assert set(counts) <= set(ROWS), f"unknown category recorded: {set(counts) - set(ROWS)}"
        seen |= set(counts)
    assert seen == set(ROWS)


def test_danger_capable_matches_recorded_danger():
    """Danger-capable means danger-capable on ANY recorded board (#8938).

    The single-board assertion broke by construction when the 1 oz board
    landed: ``trace_spacing`` is 2 danger there, 0 on the #8938 board, and
    ``silkscreen_to_pad`` 54 vs 0.  The property the table means is the union.
    """
    danger_any = {
        cat for counts in _all_recorded_counts() for cat, c in counts.items() if c["danger"] > 0
    }
    for cat, row in ROWS.items():
        assert row.danger_capable == (cat in danger_any), cat


def test_8938_fixture_records_the_resolved_stackup():
    """Band logic keys on the stackup, so it must be data, not prose (#8938)."""
    fixture = json.loads((FIX / "jlcdfm_8938_board.json").read_text())
    assert fixture["stackup"] == "4layer_1oz"


def test_recorded_danger_not_covered_is_incomplete():
    report = fdp.evaluate_parity("jlcpcb")
    for counts in _all_recorded_counts():
        for cat, c in counts.items():
            if c["danger"] > 0 and ROWS[cat].status == "not_covered":
                assert cat in report.incomplete_categories


def test_recorded_danger_with_zero_kct_errors_fails_the_suite():
    """A category where a recorded board reports danger must be covered by a
    rule that can emit errors (issue #8938).

    The defect this pins: every danger-capable row was ``partial`` with a
    covering rule that downgraded the fab's danger geometry to warnings, so
    kct reported zero errors on a board carrying 58 fab danger items.  The
    rule-side bite (real bands on the measured geometry) lives in
    ``test_physical_copper_gap.py``; this pins the table side.
    """
    for counts in _all_recorded_counts():
        for cat, c in counts.items():
            if c["danger"] > 0:
                row = ROWS[cat]
                assert row.kct_rules, f"{cat}: fab danger but no covering rule"
                assert row.status != "not_covered", cat


def test_fixture_findings_attributed_to_exactly_one_row():
    """AC 9 (replaces the dead ``v.category`` predicate, issue #8940).

    The pre-#8940 test named ``v.category`` -- a field
    ``validate.violations.DRCViolation`` does not have -- and guarded on
    ``covered`` rows with recorded danger, which no recorded board has, so
    its loop body never executed.  What it meant to pin, and what this runs
    for real on every danger fixture board: findings the parity table claims
    are attributed to exactly one row, using the same key the stanza counts
    with (#8972 made that key band-qualified for ``physical_copper_gap``).
    """
    from kicad_tools.cli.check_cmd import build_check_rule_set, run_check_rule_set

    claimed: dict[str, str] = {
        rule: r.category for r in fdp.PARITY_TABLE for rule in r.count_rule_ids
    }
    attributed = 0
    for cat, counts in RECORDED.items():
        if counts["danger"] <= 0:
            continue
        res = run_check_rule_set(build_check_rule_set(FIX / f"{cat}.kicad_pcb"))
        for v in res.violations:
            owner = claimed.get(fdp.attribution_key(v))
            if owner is None:
                continue
            attributed += 1
            # The counting key resolves every claimed finding to one row;
            # ``owner`` existing at all is that assertion (a dict key maps to
            # exactly one value), but keep the row lookup honest too.
            assert owner in ROWS
    assert attributed >= 1, "no fixture finding exercised the attribution key"


@pytest.mark.parametrize("cat", [c for c, n in RECORDED.items() if n["danger"] > 0])
def test_danger_fixture_boards_exist_and_parse(cat):
    from kicad_tools.schema.pcb import PCB

    assert PCB.load(str(FIX / f"{cat}.kicad_pcb"))


def test_not_applicable_to_other_manufacturers():
    assert fdp.evaluate_parity("pcbway") is None
    assert fdp.evaluate_parity(None) is None
    assert fdp.evaluate_parity("jlcpcb-tier1") is not None


def test_all_danger_rows_covered_not_incomplete():
    table = tuple(
        fdp.ParityRow(r.category, ("clearance",), "covered", r.danger_capable)
        for r in fdp.PARITY_TABLE
    )
    assert not fdp.evaluate_parity("jlcpcb", table).incomplete


def test_partial_does_not_trigger_incomplete():
    table = (fdp.ParityRow("x", ("clearance",), "partial", True),)
    assert not fdp.evaluate_parity("jlcpcb", table).incomplete


# ---------------------------------------------------------------------------
# Issue #8940: kct-vs-fab count comparison in the parity stanza.
# ---------------------------------------------------------------------------

#: The pre-#8940 stanza shape, captured from ``origin/main`` @ 993d2d2ab
#: before any edit to ``format_parity_stanza`` (the issue's Test Plan requires
#: the baseline be captured first; AC 2 makes it the byte-identity contract).
#: #8972 bumped the table version 6 -> 7 (row attribution change) and #9034
#: 7 -> 8 (note-only row change), so only the header's digit moved -- rows,
#: order and wording are unchanged. #8840 bumps it 8 -> 9 and flips the
#: ``sharp_trace_corner`` / ``missing_plated_through_hole`` rows now that
#: both rules ship. #9037 bumps it 9 -> 10 (``sharp_trace_corner`` row
#: attribution narrows to the ``#bend`` population); again only the digit.
#: #8858 bumps it 10 -> 11 (note-only change to the
#: ``solder_mask_opening_exposing_trace`` row).
EXPECTED_DEFAULT_STANZA = (
    "\n"
    "Fab DFM parity (JLCDFM, table v11, 2026-11-12): 0 danger-capable categories not covered\n"
    "  partial     trace_spacing [danger-capable]: clearance, physical_copper_gap\n"
    "  partial     pad_spacing [danger-capable]: clearance, physical_copper_gap\n"
    "  partial     solder_mask_opening_exposing_trace [danger-capable]: mask_to_copper\n"
    "  partial     tht_to_smd [danger-capable]: tht_smd_spacing\n"
    "  partial     silkscreen_to_pad [danger-capable]: silkscreen\n"
    "  partial     silkscreen_to_hole [danger-capable]: silk_hole_overlap\n"
    "  not covered annular_ring: -\n"
    "  covered     negative_soldermask_expansion: mask_negative_expansion\n"
    "  covered     soldermask_opening_multiple_segments: mask_multi_segment\n"
    "  covered     unconnected_trace_end: dangling_copper\n"
    "  covered     sharp_trace_corner: sharp_trace_corner\n"
    "  partial     missing_plated_through_hole: missing_plated_hole\n"
    "  partial     silkscreen_line_width: silkscreen\n"
    "  covered     fiducial: fiducials"
)

#: The dynamic ``clearance_{a}_{b}`` family (rules/clearance.py builds the id
#: from the sorted element-type pair of the two sides).  Counted per pair over
#: the element types the clearance pass models.
_CLEARANCE_PAIR_IDS = frozenset(
    f"clearance_{a}_{b}" for a, b in combinations_with_replacement(("pad", "segment", "via"), 2)
)


def _implemented_count_ids() -> frozenset[str]:
    """Every id a count_rule_ids entry may legitimately name.

    Like :func:`_implemented_rule_ids` plus the dynamically-built clearance
    pair family, which appears in neither ``CHECK_CATEGORIES`` nor
    ``DRCChecker.RULE_CATEGORY``.
    """
    return _implemented_rule_ids() | _CLEARANCE_PAIR_IDS


def test_count_rule_ids_partition():
    """AC 6: no rule id counts toward two rows, so counts cannot double-count."""
    seen: dict[str, str] = {}
    for r in fdp.PARITY_TABLE:
        for rule in r.count_rule_ids:
            assert rule not in seen, (rule, seen.get(rule), r.category)
            seen[rule] = r.category


def test_count_rule_ids_are_emitted():
    """AC 6 drift guard: every count_rule_ids entry is an id kct can emit.

    Since #8972 an entry may carry a ``#band`` qualifier (``rule_id#trace`` /
    ``rule_id#pad`` -- see :func:`kicad_tools.validate.fab_dfm_parity.attribution_key`);
    the unqualified part must still be an id ``kct check`` can emit.
    """
    implemented = _implemented_count_ids()
    for r in fdp.PARITY_TABLE:
        for rule in r.count_rule_ids:
            base = rule.split("#", 1)[0]
            assert base in implemented, (r.category, rule)


def test_count_rule_ids_match_status():
    """Measured rows have an attribution key; unmeasured rows have none.

    That is what makes the stanza's ``n/a`` vs ``0`` distinction honest
    (AC 7): a ``covered``/``partial`` row is by definition measured, a
    ``not_covered`` row is not.
    """
    for r in fdp.PARITY_TABLE:
        if r.status in ("covered", "partial"):
            assert r.count_rule_ids, r.category
        else:
            assert r.count_rule_ids == (), r.category


def test_count_rule_ids_split_the_shared_spacing_rules():
    """The trace/pad split: the shared ``clearance`` family is partitioned.

    ``clearance`` and ``physical_copper_gap`` each cover two rows
    (trace_spacing + pad_spacing); the finer pair-type ids must divide
    between them so a finding counts toward exactly one.  Since #8972 the
    fab-band-graded ``physical_copper_gap`` populations divide the same way
    via the ``#band`` qualified key, while the bare id (the legacy opt-in
    pass, zone-fill ``info`` findings) stays on ``pad_spacing`` so the
    partition remains total.
    """
    rows = {r.category: r for r in fdp.PARITY_TABLE}
    trace, pad = rows["trace_spacing"].count_rule_ids, rows["pad_spacing"].count_rule_ids
    assert set(trace) | set(pad) == _CLEARANCE_PAIR_IDS | {
        "physical_copper_gap",
        "physical_copper_gap#trace",
        "physical_copper_gap#pad",
    }
    assert not (set(trace) & set(pad))
    assert any(rule.startswith("clearance_pad_") for rule in pad)
    assert any(rule.startswith("clearance_segment_") for rule in trace)
    assert "physical_copper_gap#trace" in trace
    assert "physical_copper_gap#pad" in pad
    assert "physical_copper_gap" in pad  # bandless emit sites stay attributed


def _policy_2layer_1oz():
    """The recorded ``2layer_1oz`` fab bands (manufacturers/data/jlcpcb.yaml)."""
    from kicad_tools.validate.rules.physical_gap import FabGapPolicy, GapBand

    return FabGapPolicy(trace=GapBand(0.127, None), pad=GapBand(0.15, None))


def test_trace_band_finding_counts_to_trace_spacing_row():
    """Issue #8972 regression on the committed fixture.

    The 0.080 mm same-net two-track gap on ``trace_spacing.kicad_pcb`` is
    trace-band graded (``required_value`` = the trace danger edge 0.127 mm),
    so it must count toward ``trace_spacing`` -- not ``pad_spacing`` as the
    flat rule-id key filed it, which left ``trace_spacing`` reading
    ``kct 0 err`` beside the fab's recorded 2 danger (the spurious
    ``fab_only_danger`` mark).
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap_fab_view

    findings = check_physical_copper_gap_fab_view(
        PCB.load(str(FIX / "trace_spacing.kicad_pcb")), _policy_2layer_1oz()
    ).violations
    graded = [v for v in findings if v.severity != "info"]
    assert len(graded) == 1, "fixture no longer reproduces the measured single gap"
    assert graded[0].required_value == pytest.approx(0.127)  # trace band, not pad
    rep = fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert (by_cat["trace_spacing"].kct_errors, by_cat["pad_spacing"].kct_errors) == (1, 0)
    assert by_cat["trace_spacing"].fab_only_danger is False


def test_pad_band_and_mask_bridge_findings_count_to_pad_spacing():
    """The pad populations stay on ``pad_spacing``: a pad-band-graded gap
    (measured 0.080 mm against the 0.15 mm pad edge) and a
    ``check_pad_mask_bridge`` error (2 oz) both count there."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.physical_gap import (
        check_pad_mask_bridge,
        check_physical_copper_gap_fab_view,
    )

    pcb = PCB.load(str(FIX / "pad_spacing.kicad_pcb"))
    findings = list(check_physical_copper_gap_fab_view(pcb, _policy_2layer_1oz()).violations)
    bridge = [v for v in check_pad_mask_bridge(pcb, 2.0).violations if v.severity != "info"]
    assert bridge, "fixture no longer reproduces a mask-bridge finding"
    graded = [v for v in findings if v.severity != "info"]
    assert graded and graded[0].required_value == pytest.approx(0.15)  # pad band
    rep = fdp.evaluate_parity("jlcpcb", findings=findings + bridge, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert by_cat["pad_spacing"].kct_errors == len(graded) + len(bridge)
    assert by_cat["trace_spacing"].kct_errors == 0


def test_bandless_physical_gap_findings_stay_on_pad_spacing():
    """Edge cases from the issue's Test Plan: a legacy opt-in-pass finding
    (no band concept) and a zone-fill ``info`` finding keep the bare-id
    attribution -- the former counts to ``pad_spacing``, the latter to
    neither tally (info is never an error or warning count)."""
    findings = [
        _finding("physical_copper_gap"),  # legacy pass: fab_band unset
        _finding("physical_copper_gap", "info"),  # zone-fill advisory
    ]
    rep = fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert (by_cat["pad_spacing"].kct_errors, by_cat["pad_spacing"].kct_warnings) == (1, 0)
    assert (by_cat["trace_spacing"].kct_errors, by_cat["trace_spacing"].kct_warnings) == (0, 0)


def test_pad_spacing_cases_fixture_attributes_errors_to_pad_spacing():
    """Issue #9034: the committed five-case fixture's three graded findings
    all sit on the pad band, so they count toward ``pad_spacing`` and
    ``trace_spacing`` stays clean; the zone-fill advisory counts to neither
    tally.  The issue-reported ratio (19 kct errors vs the fab's 6 danger +
    1 warning on one 4-layer board, board not committed) stays
    *reported-not-reproduced*: this fixture pins the band semantics and the
    per-pair counting granularity, not that board."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap_fab_view

    findings = list(
        check_physical_copper_gap_fab_view(
            PCB.load(str(FIX / "pad_spacing_cases.kicad_pcb")), _policy_2layer_1oz()
        ).violations
    )
    graded = [v for v in findings if v.severity != "info"]
    assert len(graded) == 3 and all(v.fab_band == "pad" for v in graded)
    rep = fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert (by_cat["pad_spacing"].kct_errors, by_cat["pad_spacing"].kct_warnings) == (3, 0)
    assert (by_cat["trace_spacing"].kct_errors, by_cat["trace_spacing"].kct_warnings) == (0, 0)


def test_physical_copper_gap_rule_id_is_band_stable():
    """AC: the qualified key is attribution-only.  ``rule_id`` -- and so
    ``DRCViolation.key`` and every existing ``.kct_waivers.json`` entry
    naming this rule -- is identical across all band populations."""
    from kicad_tools.validate.violations import DRCViolation

    trace = DRCViolation("physical_copper_gap", "error", "m", fab_band="trace")
    pad = DRCViolation("physical_copper_gap", "error", "m", fab_band="pad")
    legacy = DRCViolation("physical_copper_gap", "error", "m")
    assert {v.rule_id for v in (trace, pad, legacy)} == {"physical_copper_gap"}
    assert len({v.key for v in (trace, pad, legacy)}) == 1


def _finding(rule_id: str, severity: str = "error", *, waived: bool = False, **extra):
    from kicad_tools.validate.violations import DRCViolation

    return DRCViolation(rule_id, severity, f"synthetic {rule_id}", waived=waived, **extra)


RECORDED_8838 = RECORDED


def test_comparison_counts_attributed_by_count_rule_ids():
    """AC 6: per-row counts come from the explicit key, exact values."""
    findings = [
        _finding("clearance_segment_segment"),
        _finding("clearance_segment_segment"),
        _finding("clearance_segment_segment", "warning"),
        _finding("clearance_pad_via"),
        _finding("physical_copper_gap"),
        _finding("silk_over_copper", "warning"),
        _finding("silk_hole_overlap"),
        _finding("silkscreen_line_width", "warning"),
        _finding("silkscreen_text_thickness", "warning"),
        _finding("fiducials_missing", "warning"),
    ]
    rep = fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert (by_cat["trace_spacing"].kct_errors, by_cat["trace_spacing"].kct_warnings) == (2, 1)
    assert (by_cat["pad_spacing"].kct_errors, by_cat["pad_spacing"].kct_warnings) == (2, 0)
    assert (by_cat["silkscreen_to_pad"].kct_errors, by_cat["silkscreen_to_pad"].kct_warnings) == (
        0,
        1,
    )
    assert (by_cat["silkscreen_to_hole"].kct_errors, by_cat["silkscreen_to_hole"].kct_warnings) == (
        1,
        0,
    )
    assert (
        by_cat["silkscreen_line_width"].kct_errors,
        by_cat["silkscreen_line_width"].kct_warnings,
    ) == (0, 2)
    assert (by_cat["fiducial"].kct_errors, by_cat["fiducial"].kct_warnings) == (0, 1)


def test_sharp_trace_corner_bend_population_attributes_to_its_row():
    """Issue #9037: the ``#bend`` qualified key counts only two-segment
    bends toward the ``sharp_trace_corner`` row; junction findings are
    kct-only over-reporting and count to neither row's tallies."""
    bend = _finding("sharp_trace_corner", "warning", corner_class="bend")
    junction = _finding("sharp_trace_corner", "warning", corner_class="junction")
    assert fdp.attribution_key(bend) == "sharp_trace_corner#bend"
    assert fdp.attribution_key(junction) == "sharp_trace_corner#junction"
    rep = fdp.evaluate_parity("jlcpcb", findings=[bend, junction], recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert by_cat["sharp_trace_corner"].kct_warnings == 1
    # The qualified key is attribution-only: rule_id (and so DRCViolation.key)
    # is identical across both populations.
    assert bend.key == junction.key


def test_silk_finding_lands_in_exactly_one_silk_row():
    """AC 6 / Test Plan: the one-to-many ``silkscreen`` token must not leak.

    A silkscreen-family finding counts toward exactly one of
    ``silkscreen_to_pad`` / ``silkscreen_line_width`` / ``silkscreen_to_hole``.
    """
    findings = [
        _finding("silk_over_copper", "warning"),
        _finding("silkscreen_over_pad", "warning"),
        _finding("silkscreen_line_width", "warning"),
        _finding("silkscreen_text_thickness", "warning"),
        _finding("silk_hole_overlap"),
    ]
    rep = fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    silk_rows = ("silkscreen_to_pad", "silkscreen_line_width", "silkscreen_to_hole")
    total = sum((by_cat[c].kct_errors or 0) + (by_cat[c].kct_warnings or 0) for c in silk_rows)
    assert total == len(findings)
    assert (by_cat["silkscreen_to_pad"].kct_warnings) == 2
    assert (by_cat["silkscreen_line_width"].kct_warnings) == 2
    assert (by_cat["silkscreen_to_hole"].kct_errors) == 1


def test_comparison_uses_is_error_is_warning_predicates():
    """AC 8: waived and info findings never count (same as headline totals)."""
    findings = [
        _finding("clearance_pad_pad", waived=True),
        _finding("tht_smd_spacing", "info"),
        _finding("tht_smd_spacing", "error"),
    ]
    rep = fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    by_cat = {c.category: c for c in rep.comparisons}
    assert (by_cat["pad_spacing"].kct_errors, by_cat["pad_spacing"].kct_warnings) == (0, 0)
    assert (by_cat["tht_to_smd"].kct_errors, by_cat["tht_to_smd"].kct_warnings) == (1, 0)


def test_default_stanza_is_byte_identical():
    """AC 2: with no recorded result the stanza is byte-identical to main's."""
    assert fdp.format_parity_stanza(fdp.evaluate_parity("jlcpcb")) == EXPECTED_DEFAULT_STANZA


def test_stanza_comparison_rendering_and_marking():
    """AC 1/4/7: four count columns, the fab-danger-kct-0 mark, n/a rows."""
    findings = [_finding("silk_over_copper", "warning"), _finding("fiducials_missing", "warning")]
    stanza = fdp.format_parity_stanza(
        fdp.evaluate_parity("jlcpcb", findings=findings, recorded_counts=RECORDED_8838)
    )
    assert (
        "  partial     silkscreen_to_pad [danger-capable]: silkscreen "
        "| kct 0 err, 1 warn | fab 54 danger, 14 warn "
        "| ** fab danger, kct 0 errors **" in stanza
    )
    assert (
        "  not covered annular_ring: - | kct n/a (not measured) | fab 0 danger, 100 warn" in stanza
    )
    # A measured zero (kct covers the row, found nothing) renders as 0, not n/a.
    assert (
        "trace_spacing [danger-capable]: clearance, physical_copper_gap | kct 0 err, 0 warn"
        in stanza
    )
    # The mark is absent where kct did report errors.
    assert "tht_smd_spacing | kct 1 err" not in stanza  # no findings fed for it here
    assert stanza.count("** fab danger, kct 0 errors **") >= 1


def test_stanza_without_comparison_has_no_new_columns():
    """AC 2: no placeholder dashes or count columns without a recorded result."""
    stanza = fdp.format_parity_stanza(fdp.evaluate_parity("jlcpcb"))
    assert "| kct" not in stanza and "| fab" not in stanza and "n/a" not in stanza


def test_json_comparison_present_and_absent():
    """AC 3: comparison appears in to_dict() only when a result was supplied."""
    without = fdp.evaluate_parity("jlcpcb", findings=[_finding("fiducials_missing", "warning")])
    assert "comparison" not in without.to_dict()
    with_result = fdp.evaluate_parity(
        "jlcpcb", findings=[_finding("fiducials_missing", "warning")], recorded_counts=RECORDED_8838
    )
    comparison = with_result.to_dict()["comparison"]
    assert comparison["fiducial"] == {
        "kct_errors": 0,
        "kct_warnings": 1,
        "fab_danger": 0,
        "fab_warning": 6,
        "fab_only_danger": False,
    }
    assert comparison["silkscreen_to_hole"]["fab_only_danger"] is True
    assert comparison["annular_ring"]["kct_errors"] is None


def test_recorded_category_absent_from_table_is_surfaced():
    """Edge case: a recorded category with no parity row is named, not dropped."""
    counts = dict(RECORDED_8838)
    counts["mystery_category"] = {"danger": 3, "warning": 1}
    rep = fdp.evaluate_parity("jlcpcb", findings=[], recorded_counts=counts)
    assert rep.unmapped_recorded == ("mystery_category",)
    stanza = fdp.format_parity_stanza(rep)
    assert "recorded categories not in the parity table: mystery_category" in stanza
    assert rep.to_dict()["unmapped_recorded_categories"] == ["mystery_category"]


def test_table_category_absent_from_recorded_result():
    """Edge case: a table row the result does not mention reads 'not recorded'."""
    counts = {"fiducial": {"danger": 0, "warning": 6}}
    rep = fdp.evaluate_parity("jlcpcb", findings=[], recorded_counts=counts)
    by_cat = {c.category: c for c in rep.comparisons}
    assert by_cat["fiducial"].fab_danger == 0
    assert by_cat["trace_spacing"].fab_danger is None
    assert by_cat["trace_spacing"].fab_warning is None
    assert (
        "trace_spacing [danger-capable]: clearance, physical_copper_gap | kct 0 err, 0 warn | fab not recorded"
        in fdp.format_parity_stanza(rep)
    )


def test_zero_findings_on_both_sides():
    """Edge case: a covered row with zero findings and zero fab records."""
    counts = {"fiducial": {"danger": 0, "warning": 0}}
    rep = fdp.evaluate_parity("jlcpcb", findings=[], recorded_counts=counts)
    by_cat = {c.category: c for c in rep.comparisons}
    assert (by_cat["fiducial"].kct_errors, by_cat["fiducial"].fab_danger) == (0, 0)
    assert by_cat["fiducial"].fab_only_danger is False


def test_non_jlcpcb_mfr_gets_no_report():
    """Edge case: a non-jlcpcb --mfr never reaches the comparison at all."""
    assert fdp.evaluate_parity("pcbway", findings=[], recorded_counts=RECORDED_8838) is None


def test_load_recorded_result_validates(tmp_path):
    """AC 5: malformed or unreadable input is a clear error, never silent."""
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({"source": "x", "counts": {"fiducial": {"danger": 0, "warning": 1}}})
    )
    assert fdp.load_recorded_result(good) == {"fiducial": {"danger": 0, "warning": 1}}

    missing = tmp_path / "nope.json"
    with pytest.raises(fdp.RecordedResultError, match="cannot read"):
        fdp.load_recorded_result(missing)

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json")
    with pytest.raises(fdp.RecordedResultError, match="not valid JSON"):
        fdp.load_recorded_result(bad_json)

    not_object = tmp_path / "list.json"
    not_object.write_text("[1, 2]")
    with pytest.raises(fdp.RecordedResultError, match="expected a JSON object"):
        fdp.load_recorded_result(not_object)

    no_counts = tmp_path / "nocounts.json"
    no_counts.write_text(json.dumps({"source": "x"}))
    with pytest.raises(fdp.RecordedResultError, match="no 'counts' object"):
        fdp.load_recorded_result(no_counts)

    bad_entry = tmp_path / "badentry.json"
    bad_entry.write_text(json.dumps({"counts": {"fiducial": {"danger": "many", "warning": 1}}}))
    with pytest.raises(fdp.RecordedResultError, match="non-negative integer"):
        fdp.load_recorded_result(bad_entry)


@pytest.mark.parametrize("name", RECORDED_BOARDS)
def test_load_recorded_result_parses_both_fixtures(name):
    """The loader accepts both committed fixture shapes (#8938 adds 'stackup')."""
    counts = fdp.load_recorded_result(FIX / name)
    assert set(counts) <= set(ROWS)


def test_evaluate_check_verdict_threads_recorded_counts():
    """AC 1/3 plumbing: the shared verdict path passes findings + counts in."""
    from kicad_tools.cli.check_cmd import evaluate_check_verdict
    from kicad_tools.validate.violations import DRCResults

    results = DRCResults()
    results.violations.append(_finding("silk_over_copper", "warning"))
    verdict = evaluate_check_verdict(
        FIX / "silkscreen_to_pad.kicad_pcb",
        results,
        drc_only=True,
        manufacturer="jlcpcb",
        recorded_fab_counts=RECORDED_8838,
    )
    assert verdict.fab_dfm_parity.comparisons is not None
    by_cat = {c.category: c for c in verdict.fab_dfm_parity.comparisons}
    assert (by_cat["silkscreen_to_pad"].kct_warnings) == 1
    # The headline totals and the stanza counts see the same findings.
    assert verdict.warning_count == 1
    assert "comparison" in verdict.fab_dfm_parity.to_dict()


def test_check_parser_accepts_fab_dfm_result():
    """The flag exists on the check subcommand and defaults to off."""
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["check", "board.kicad_pcb", "--fab-dfm-result", "run.json"])
    assert args.fab_dfm_result == "run.json"
    args = parser.parse_args(["check", "board.kicad_pcb"])
    assert args.fab_dfm_result is None


def test_check_shim_forwards_fab_dfm_result():
    """The forwarding shim must not drop a supplied result (AC 5's shadow bug).

    ``run_check_command`` rebuilds argv from a flag whitelist; a flag parsed
    by the outer parser but missing from the whitelist is silently discarded
    (the #2819 failure mode), which for this flag means a user-supplied
    recorded result quietly produces the uncompared stanza.
    """
    from unittest.mock import patch

    from kicad_tools.cli.commands.validation import run_check_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["check", "board.kicad_pcb", "--mfr", "jlcpcb", "--fab-dfm-result", "run.json"]
    )
    with patch("kicad_tools.cli.check_cmd.main", return_value=0) as inner_main:
        assert run_check_command(args) == 0
    sub_argv = inner_main.call_args[0][0]
    assert sub_argv[sub_argv.index("--fab-dfm-result") + 1] == "run.json", sub_argv

    args = create_parser().parse_args(["check", "board.kicad_pcb", "--mfr", "jlcpcb"])
    with patch("kicad_tools.cli.check_cmd.main", return_value=0) as inner_main:
        assert run_check_command(args) == 0
    assert "--fab-dfm-result" not in inner_main.call_args[0][0]


def test_verdict_incomplete_and_allow_incomplete(monkeypatch):
    """The verdict flip must not depend on *which* real row is uncovered (#8902).

    It used to assert ``tht_to_smd``, which now has a rule.  A synthetic
    one-row table keeps the flip under test as rules ship.
    """
    from kicad_tools.cli.check_cmd import evaluate_check_verdict
    from kicad_tools.validate.violations import DRCResults

    table = (fdp.ParityRow("synthetic_danger", (), "not_covered", True, note="test table"),)
    real_evaluate = fdp.evaluate_parity
    monkeypatch.setattr(fdp, "evaluate_parity", lambda mfr, **kw: real_evaluate(mfr, table, **kw))

    pcb = FIX / "tht_to_smd.kicad_pcb"
    res = DRCResults()  # no findings: a clean DRC
    v = evaluate_check_verdict(pcb, res, drc_only=True, manufacturer="jlcpcb")
    assert v.verdict == "incomplete" and v.exit_code == 2
    assert "synthetic_danger" in v.fab_dfm_parity.incomplete_categories
    v2 = evaluate_check_verdict(
        pcb, res, drc_only=True, manufacturer="jlcpcb", allow_incomplete=True
    )
    assert v2.verdict == "incomplete" and v2.exit_code == 0
    v3 = evaluate_check_verdict(pcb, res, drc_only=True, manufacturer="pcbway")
    assert v3.verdict == "pass" and v3.fab_dfm_parity is None


def test_fab_category_rules_covers_every_row():
    """The mapping and the table describe the same category set."""
    assert set(FAB_CATEGORY_RULES) == set(ROWS)


def test_row_rules_are_declared_in_the_mapping():
    """A row may not name a rule the mapping does not know about."""
    for row in fdp.PARITY_TABLE:
        assert set(row.kct_rules) <= set(FAB_CATEGORY_RULES[row.category]), row.category


def test_registered_rule_forbids_not_covered():
    """A shipped rule must not leave its row saying ``not_covered`` (#8902)."""
    drifted = _drifted_rows(fdp.PARITY_TABLE, FAB_CATEGORY_RULES, _implemented_rule_ids())
    assert drifted == [], (
        f"these rows claim no rule but their mapped rule id(s) are registered: {drifted}; "
        "flip the row to 'covered'/'partial' and say what is still unchecked"
    )


def test_registered_rule_guard_bites_on_a_flipped_row():
    """The guard above is load-bearing: flipping a covered row back fails it."""
    flipped = tuple(
        fdp.ParityRow(r.category, (), "not_covered", r.danger_capable, note="pretend no rule")
        if r.category == "tht_to_smd"
        else r
        for r in fdp.PARITY_TABLE
    )
    assert _drifted_rows(flipped, FAB_CATEGORY_RULES, _implemented_rule_ids()) == ["tht_to_smd"]


def test_guard_ignores_a_row_whose_rule_has_not_shipped():
    """A ``not_covered`` row whose mapped id does not exist yet is not drift."""
    table = (fdp.ParityRow("x", (), "not_covered", True, note="in flight"),)
    assert _drifted_rows(table, {"x": ("not_a_real_rule_yet",)}, _implemented_rule_ids()) == []


def test_fab_danger_severity_fails_verdict_and_exit():
    """Issue #8938 AC: an unwaived fab-danger-level finding makes DRC FAILED,
    ``Overall:`` not PASSED, and a non-zero exit."""
    from kicad_tools.cli.check_cmd import evaluate_check_verdict
    from kicad_tools.validate.violations import DRCResults, DRCViolation

    pcb = FIX / "tht_to_smd.kicad_pcb"
    res = DRCResults()
    res.add(
        DRCViolation(
            "physical_copper_gap",
            "error",
            "Physical copper gap 0.0800mm < 0.1500mm",
            nets=("GND",),
            same_net=True,
        )
    )
    v = evaluate_check_verdict(pcb, res, drc_only=True, manufacturer="jlcpcb")
    assert v.verdict == "fail"
    assert v.exit_code != 0
