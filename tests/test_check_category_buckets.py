"""Two-bucket manufacturing-vs-advisory rendering in ``kct check`` (Issue #3803).

``output_table`` (the ``kct check`` table reporter) historically folded
routing-intent / quality findings (``connectivity``, diff-pair
skew/continuity, ``copper_sliver``, ``ampacity``) into the same
undifferentiated "DRC violations" count as fabrication-blocking
copper/clearance defects.  Because ``connectivity`` and the diff-pair
rules emit ``severity="error"``, "N DRC violations" could read as N
fab-blocking copper defects even when native ``kicad-cli`` reports 0.

This slice adds a CATEGORY SUMMARY block that splits the findings into two
labelled buckets with a per-bucket headline count.  It is
PRESENTATION-ONLY: the per-severity ``Results:`` counts and the
PASS/FAIL/WARNING verdict line are unchanged.

These tests capture the reporter output (``capsys``) and assert the
bucket rendering, and separately assert the verdict line is byte-identical
to what the severity counts alone dictate (no gate/exit-code drift).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import output_table
from kicad_tools.validate import DRCResults, DRCViolation

_PCB = Path("board.kicad_pcb")


def _render(violations: list[DRCViolation]) -> str:
    results = DRCResults()
    for v in violations:
        results.add(v)
    results.rules_checked = max(1, len({v.rule_id for v in violations}))
    output_table(violations, results, _PCB, "jlcpcb", 4, False)
    return ""


def _render_capsys(violations: list[DRCViolation], capsys) -> str:
    _render(violations)
    return capsys.readouterr().out


def test_two_labeled_buckets_present(capsys) -> None:
    """A mixed board renders both the manufacturing and advisory headers."""
    violations = [
        DRCViolation(rule_id="clearance", severity="error", message="copper short"),
        DRCViolation(rule_id="connectivity", severity="error", message="GND incomplete"),
    ]
    out = _render_capsys(violations, capsys)
    assert "CATEGORY SUMMARY" in out
    assert "Manufacturing DRC:" in out
    assert "Advisory/quality:" in out


def test_all_advisory_board_reads_zero_blocking(capsys) -> None:
    """On a board whose only findings are connectivity / diff-pair /
    copper_sliver, the manufacturing headline reads 0 blocking while the
    advisory bucket lists them (the exact headline complaint)."""
    violations = [
        DRCViolation(rule_id="connectivity", severity="error", message="GND incomplete"),
        DRCViolation(rule_id="diffpair_length_skew", severity="error", message="skew"),
        DRCViolation(rule_id="copper_sliver", severity="warning", message="sliver"),
    ]
    out = _render_capsys(violations, capsys)
    assert "Manufacturing DRC: 0 blocking" in out
    # The advisory bucket surfaces the three findings and names the rules.
    assert "Advisory/quality:  3 advisory" in out
    assert "connectivity" in out
    assert "copper_sliver" in out
    assert "diffpair_length_skew" in out


def test_manufacturing_violation_counted_in_manufacturing_bucket(capsys) -> None:
    """A real clearance/edge violation lands in the manufacturing bucket
    and the blocking headline reflects it."""
    violations = [
        DRCViolation(rule_id="clearance", severity="error", message="copper short"),
        DRCViolation(rule_id="edge_clearance", severity="error", message="near edge"),
    ]
    out = _render_capsys(violations, capsys)
    assert "Manufacturing DRC: 2 blocking" in out


def test_manufacturing_only_board_omits_empty_advisory_header(capsys) -> None:
    """Edge case: a manufacturing-only board renders no empty advisory
    header (but still shows the manufacturing headline)."""
    violations = [
        DRCViolation(rule_id="clearance", severity="error", message="copper short"),
    ]
    out = _render_capsys(violations, capsys)
    assert "Manufacturing DRC: 1 blocking" in out
    assert "Advisory/quality:" not in out


def test_dynamic_clearance_subtype_is_manufacturing(capsys) -> None:
    """A dynamically-suffixed ``clearance_segment_via`` subtype (not a
    literal in the category map) is bucketed as manufacturing via the
    prefix fallback."""
    violations = [
        DRCViolation(
            rule_id="clearance_segment_via",
            severity="error",
            message="trace/via short",
            nets=("NET_A", "NET_B"),
        ),
    ]
    out = _render_capsys(violations, capsys)
    assert "Manufacturing DRC: 1 blocking" in out
    assert "Advisory/quality:" not in out


@pytest.mark.parametrize(
    ("violations", "expected_verdict"),
    [
        # Advisory-only ERROR must still drive DRC FAILED (unchanged gate).
        (
            [DRCViolation(rule_id="connectivity", severity="error", message="x")],
            "DRC FAILED - Fix errors before manufacturing",
        ),
        # Advisory-only WARNING -> DRC WARNING (unchanged).
        (
            [DRCViolation(rule_id="copper_sliver", severity="warning", message="x")],
            "DRC WARNING - Review warnings",
        ),
        # Manufacturing error -> DRC FAILED (unchanged).
        (
            [DRCViolation(rule_id="clearance", severity="error", message="x")],
            "DRC FAILED - Fix errors before manufacturing",
        ),
    ],
)
def test_verdict_line_unchanged_by_category_split(
    violations: list[DRCViolation], expected_verdict: str, capsys
) -> None:
    """Presentation-only guarantee: the PASS/FAIL/WARNING verdict line is
    determined solely by severity counts and is NOT altered by re-bucketing
    findings into manufacturing vs advisory.

    In particular an advisory-only *error* still prints DRC FAILED -- the
    reporting-category split does NOT demote connectivity/diff-pair errors
    off the blocking verdict (that would be a gate-semantics change, which
    is explicitly out of scope for this slice).
    """
    out = _render_capsys(violations, capsys)
    assert expected_verdict in out


def test_category_summary_does_not_alter_results_counts(capsys) -> None:
    """The per-severity ``Results:`` block is unchanged -- the CATEGORY
    SUMMARY is purely additive output."""
    violations = [
        DRCViolation(rule_id="clearance", severity="error", message="x"),
        DRCViolation(rule_id="connectivity", severity="error", message="y"),
        DRCViolation(rule_id="copper_sliver", severity="warning", message="z"),
    ]
    out = _render_capsys(violations, capsys)
    # Two errors + one warning, exactly as before this slice.
    assert "Errors:     2" in out
    assert "Warnings:   1" in out
