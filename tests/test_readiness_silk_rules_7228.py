"""Readiness treats the three new silk rules as assembly-affecting (#7228)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.readiness_cmd import (
    ASSEMBLY_AFFECTING_RULES,
    FAILED,
    PASSED,
    ReadinessOptions,
    _gate_warning_review,
)
from kicad_tools.validate.checker import DRCChecker

NEW_RULES = ("silkscreen_text_height", "silkscreen_text_thickness", "silk_ref_obscured")


def _options(ack: frozenset[str] = frozenset()) -> ReadinessOptions:
    return ReadinessOptions(
        board_dir=Path("/nonexistent"),
        pcb=Path("/nonexistent/board.kicad_pcb"),
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="assembly",
        output_dir=Path("/nonexistent/manufacturing"),
        evidence_dir=Path("/nonexistent/readiness"),
        ack_warnings=ack,
    )


@pytest.mark.parametrize("rule", NEW_RULES)
def test_rule_is_advisory_in_check(rule):
    assert DRCChecker.category_for_rule(rule) == DRCChecker.CATEGORY_ADVISORY


@pytest.mark.parametrize("rule", NEW_RULES)
def test_unacknowledged_rule_fails_warning_review(rule):
    assert rule in ASSEMBLY_AFFECTING_RULES
    report = {"violations": [{"rule_id": rule, "severity": "warning"}]}

    outcome, counts = _gate_warning_review(_options(), report)
    assert outcome.status == FAILED
    assert counts == {rule: 1}
    assert rule in outcome.detail

    acked, _ = _gate_warning_review(_options(frozenset({rule})), report)
    assert acked.status == PASSED
