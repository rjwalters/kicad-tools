"""Unit test for the canonicaliser, against a checked-in kicad-cli report.

The canonicaliser is the single point where "what KiCad said" becomes "what
the harness believes". Every downstream number depends on it, and a mistake
here is silent: pair the wrong two objects and Phases 2-4 optimise against a
plausible-looking table that measures nothing.

So it is tested against **real kicad-cli 10 output**, committed at
``tests/fixtures/conformance/sample-kicad-cli-drc.json``, rather than against a
hand-written dict that could encode the same misunderstanding twice. No
kicad-cli is needed to run this test -- that is the point of committing the
sample.

The sample board deliberately contains one of each thing the canonicaliser has
to handle: overlapping foreign copper, a duplicated finding for the same pair,
a hole-to-hole violation, a one-sided board-edge violation, and three kinds of
connectivity noise that must be dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conformance.adapters import (
    BOARD_EDGE,
    KIND_CLEARANCE,
    KIND_COPPER_EDGE,
    KIND_HOLE_TO_HOLE,
    Verdict,
)
from tests.conformance.oracle import RAW_TYPE_TO_KIND, canonicalise

SAMPLE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "conformance" / "sample-kicad-cli-drc.json"
)


@pytest.fixture(scope="module")
def sample_text() -> str:
    return SAMPLE.read_text(encoding="utf-8")


def test_sample_is_real_kicad_cli_output(sample_text: str) -> None:
    """Guard against the sample being replaced by a synthetic stand-in."""
    data = json.loads(sample_text)
    assert data["$schema"].startswith("https://schemas.kicad.org/"), data.get("$schema")
    assert "violations" in data and data["violations"]
    types = {v["type"] for v in data["violations"]}
    # The sample must keep exercising both mapped and dropped families.
    assert {"clearance", "hole_to_hole", "copper_edge_clearance"} <= types
    assert {"track_dangling", "via_dangling"} & types


def test_canonicalise_extracts_exactly_the_clearance_family(sample_text: str) -> None:
    verdicts, raw_counts, dropped = canonicalise(sample_text)

    assert verdicts == {
        Verdict.pair(KIND_CLEARANCE, "T1", "V1"),
        Verdict.pair(KIND_CLEARANCE, "T1", "V2"),
        Verdict.pair(KIND_CLEARANCE, "T3", "V3"),
        Verdict.pair(KIND_CLEARANCE, "V1", "V2"),
        Verdict.pair(KIND_HOLE_TO_HOLE, "V1", "V2"),
        Verdict.pair(KIND_COPPER_EDGE, "T2", BOARD_EDGE),
    }
    assert not dropped

    # Every raw row is accounted for, including the ones deliberately ignored.
    assert raw_counts["clearance"] == 4
    assert raw_counts["track_dangling"] == 3
    assert raw_counts["via_dangling"] == 3


def test_duplicate_rows_for_one_pair_collapse(sample_text: str) -> None:
    """A pair reported twice must count once.

    This is not hypothetical: kicad-cli's row multiplicity for the *same*
    board is not stable between runs -- capturing this very sample twice
    produced five clearance rows once and four the next time, differing only
    in whether the V1/V2 pair appeared twice. Verdict identity is
    ``(kind, {nets})`` precisely so that a measurement does not depend on
    that. Counting rows instead of pairs would make every disagreement rate
    jitter with KiCad's reporting behaviour.

    The duplication is injected here rather than baked into the committed
    sample, since the sample cannot be relied on to keep exhibiting it.
    """
    data = json.loads(sample_text)
    v1_v2 = [
        v
        for v in data["violations"]
        if v["type"] == "clearance"
        and {"V1", "V2"} == {n for i in v["items"] for n in _nets(i["description"])}
    ]
    assert len(v1_v2) == 1, "sample no longer contains the V1/V2 clearance row"
    data["violations"].append(v1_v2[0])

    verdicts, raw_counts, _ = canonicalise(json.dumps(data))
    assert raw_counts["clearance"] == 5  # five rows...
    collapsed = [
        v for v in verdicts if v.kind == KIND_CLEARANCE and v.nets == frozenset({"V1", "V2"})
    ]
    assert len(collapsed) == 1  # ...four distinct pairs
    assert len([v for v in verdicts if v.kind == KIND_CLEARANCE]) == 4


def test_recorded_values_survive_canonicalisation(sample_text: str) -> None:
    """Gaps/requirements are carried for the report even though they are
    excluded from verdict identity."""
    verdicts, _, _ = canonicalise(sample_text)
    by_key = {(v.kind, v.nets): v for v in verdicts}

    hole = by_key[(KIND_HOLE_TO_HOLE, frozenset({"V1", "V2"}))]
    assert hole.gap_mm == pytest.approx(0.45, abs=1e-6)
    assert hole.required_mm == pytest.approx(0.4995, abs=1e-6)

    edge = by_key[(KIND_COPPER_EDGE, frozenset({"T2", BOARD_EDGE}))]
    assert edge.gap_mm == pytest.approx(0.075, abs=1e-6)
    assert edge.required_mm == pytest.approx(0.30, abs=1e-6)


def test_values_are_excluded_from_verdict_identity() -> None:
    """Two models reporting different gaps for one pair still agree.

    Each clearance model in the tree computes its own gap with its own
    geometry; comparing those numbers would measure arithmetic noise rather
    than the question under test.
    """
    a = Verdict.pair(KIND_CLEARANCE, "A", "B", gap_mm=0.18, required_mm=0.20)
    b = Verdict.pair(KIND_CLEARANCE, "B", "A", gap_mm=0.1801, required_mm=0.15)
    assert a == b
    assert len({a, b}) == 1


def test_unknown_kind_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="Unknown verdict kind"):
        Verdict.pair("courtyards_overlap", "A", "B")


def test_dropped_families_are_documented_by_omission() -> None:
    """The mapping is an allowlist: anything unmapped is dropped on purpose."""
    assert "track_dangling" not in RAW_TYPE_TO_KIND
    assert "via_dangling" not in RAW_TYPE_TO_KIND
    assert "unconnected_items" not in RAW_TYPE_TO_KIND
    assert "lib_footprint_issues" not in RAW_TYPE_TO_KIND
    assert "shorting_items" not in RAW_TYPE_TO_KIND
    assert set(RAW_TYPE_TO_KIND) >= {
        "clearance",
        "hole_to_hole",
        "hole_clearance",
        "copper_edge_clearance",
    }


def _nets(description: str) -> list[str]:
    import re

    return [n for n in re.findall(r"\[([^\]]+)\]", description) if n != "<no net>"]
