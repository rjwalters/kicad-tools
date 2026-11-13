"""Fab DFM parity table, verdict flip and recorded-counts bite test (Issue #8845)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.check_categories import CHECK_CATEGORIES
from kicad_tools.validate import fab_dfm_parity as fdp

FIX = Path(__file__).parent / "fixtures" / "fab_dfm_parity"
RECORDED = json.loads((FIX / "jlcdfm_8838_board.json").read_text())["counts"]
ROWS = {r.category: r for r in fdp.PARITY_TABLE}

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
    for r in fdp.PARITY_TABLE:
        if r.status in ("covered", "partial"):
            assert r.kct_rules, r.category
            for rule in r.kct_rules:
                assert rule in CHECK_CATEGORIES, (r.category, rule)


def test_table_matches_recorded_categories():
    assert set(ROWS) == set(RECORDED)


def test_danger_capable_matches_recorded_danger():
    for cat, counts in RECORDED.items():
        assert ROWS[cat].danger_capable == (counts["danger"] > 0), cat


def test_recorded_danger_not_covered_is_incomplete():
    report = fdp.evaluate_parity("jlcpcb")
    for cat, counts in RECORDED.items():
        if counts["danger"] > 0 and ROWS[cat].status == "not_covered":
            assert cat in report.incomplete_categories


def test_covered_danger_rows_have_findings_on_fixture():
    """A covered row must produce >=1 finding on its fixture board."""
    from kicad_tools.cli.check_cmd import build_check_rule_set, run_check_rule_set

    for cat, counts in RECORDED.items():
        row = ROWS[cat]
        if counts["danger"] > 0 and row.status == "covered":
            res = run_check_rule_set(build_check_rule_set(FIX / f"{cat}.kicad_pcb"))
            assert any(
                v.rule_id in row.kct_rules or v.category in row.kct_rules for v in res.violations
            ), cat


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


def test_verdict_incomplete_and_allow_incomplete(monkeypatch):
    """The verdict flip must not depend on *which* real row is uncovered (#8902).

    It used to assert ``tht_to_smd``, which now has a rule.  A synthetic
    one-row table keeps the flip under test as rules ship.
    """
    from kicad_tools.cli.check_cmd import evaluate_check_verdict
    from kicad_tools.validate.violations import DRCResults

    table = (fdp.ParityRow("synthetic_danger", (), "not_covered", True, "test table"),)
    real_evaluate = fdp.evaluate_parity
    monkeypatch.setattr(fdp, "evaluate_parity", lambda mfr: real_evaluate(mfr, table))

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
        fdp.ParityRow(r.category, (), "not_covered", r.danger_capable, "pretend there is no rule")
        if r.category == "tht_to_smd"
        else r
        for r in fdp.PARITY_TABLE
    )
    assert _drifted_rows(flipped, FAB_CATEGORY_RULES, _implemented_rule_ids()) == ["tht_to_smd"]


def test_guard_ignores_a_row_whose_rule_has_not_shipped():
    """A ``not_covered`` row whose mapped id does not exist yet is not drift."""
    table = (fdp.ParityRow("x", (), "not_covered", True, "in flight"),)
    assert _drifted_rows(table, {"x": ("not_a_real_rule_yet",)}, _implemented_rule_ids()) == []
