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


def test_verdict_incomplete_and_allow_incomplete():
    from kicad_tools.cli.check_cmd import evaluate_check_verdict
    from kicad_tools.validate.violations import DRCResults

    pcb = FIX / "tht_to_smd.kicad_pcb"
    res = DRCResults()  # no findings: a clean DRC
    v = evaluate_check_verdict(pcb, res, drc_only=True, manufacturer="jlcpcb")
    assert v.verdict == "incomplete" and v.exit_code == 2
    assert "tht_to_smd" in v.fab_dfm_parity.incomplete_categories
    v2 = evaluate_check_verdict(
        pcb, res, drc_only=True, manufacturer="jlcpcb", allow_incomplete=True
    )
    assert v2.verdict == "incomplete" and v2.exit_code == 0
    v3 = evaluate_check_verdict(pcb, res, drc_only=True, manufacturer="pcbway")
    assert v3.verdict == "pass" and v3.fab_dfm_parity is None
