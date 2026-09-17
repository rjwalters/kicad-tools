"""kicad-cli's verdict on the four committed named fixtures.

**Only ground truth is asserted here.** Consumer comparisons arrive with the
adapters, carry ``@pytest.mark.consumer``, and are auto-``xfail``ed by
``conftest.py`` -- this module is the hard gate that says "and KiCad really
does think this".

Two classes of assertion:

1. *Verdict* -- what kicad-cli reports for each fixture, at both fill states.
   These are the anchors every later phase measures against; if one of them
   changes, either a fixture drifted or a KiCad version changed its mind, and
   in both cases the epic's evidence needs re-reading.
2. *Rule values* -- each fixture still carries exactly the constants it was
   built to probe. A later change that silences a disagreement by relaxing the
   fixture's clearance instead of fixing the consumer must fail loudly here.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests.conformance.board import FIXTURES_DIR
from tests.conformance.conftest import requires_kicad_cli
from tests.conformance.fixtures import (
    GRID_RESOLUTION_MM,
    HOLE_TO_HOLE_MM,
    NAMED_FIXTURES,
    PROJECT_CLEARANCE_MM,
    TRACE_CLEARANCE_MM,
    VIA_CLEARANCE_MM,
    build_named_fixture,
    fixture_stem,
    roundrect_corner_support,
)
from tests.conformance.generator import pad_shape
from tests.conformance.oracle import run_oracle

pytestmark = requires_kicad_cli

# Fixtures that must be completely clean of clearance-family findings.
_CLEAN_FIXTURES = (
    "issue5410-dqs-n-halo-vs-legal-via",
    "search-vs-commit-seg-via-max",
    "roundrect-corner-gap",
)

_FILL_STATES = (False, True)


def _fixture_board(name: str) -> Path:
    case = build_named_fixture(name)
    return FIXTURES_DIR / f"{fixture_stem(case)}.kicad_pcb"


# ---------------------------------------------------------------------------
# The fixtures exist and are committed, not regenerated at test time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMED_FIXTURES)
def test_fixture_files_are_committed(name: str) -> None:
    pcb = _fixture_board(name)
    project = pcb.with_suffix(".kicad_pro")
    assert pcb.exists(), f"missing committed fixture board {pcb}"
    assert project.exists(), (
        f"missing sibling project {project} -- kicad-cli reads the applied "
        "clearance from the project's Default netclass, not from the board"
    )


# ---------------------------------------------------------------------------
# Rule-value guard
# ---------------------------------------------------------------------------


def test_fixture_rule_values_are_pinned() -> None:
    """The numbers under test have not drifted.

    These five constants *are* the disagreements: 0.15 mm is what the route
    resolver returns, 0.20 mm is what the project's Default class says, and
    0.127 mm is the grid pitch that turned #5410's via halo into a six-cell
    square. Editing a fixture's clearance instead of a consumer's arithmetic
    would make a later phase's rows go green for the wrong reason.
    """
    assert TRACE_CLEARANCE_MM == 0.15
    assert VIA_CLEARANCE_MM == 0.20
    assert PROJECT_CLEARANCE_MM == 0.20
    assert HOLE_TO_HOLE_MM == 0.50
    assert GRID_RESOLUTION_MM == 0.127

    rules_5398 = build_named_fixture("issue5398-seg-via-0p18-order").rules
    assert rules_5398.project_clearance == PROJECT_CLEARANCE_MM
    assert rules_5398.trace_clearance == TRACE_CLEARANCE_MM
    assert rules_5398.via_clearance == VIA_CLEARANCE_MM

    # The search-vs-commit fixture deliberately drops the *project* class to
    # 0.15 so kicad-cli is clean while the router's search-time predicate
    # still raises the bar to via_clearance.
    rules_svc = build_named_fixture("search-vs-commit-seg-via-max").rules
    assert rules_svc.project_clearance == TRACE_CLEARANCE_MM
    assert rules_svc.via_clearance == VIA_CLEARANCE_MM


# ---------------------------------------------------------------------------
# Geometry the fixtures claim, verified independently of KiCad
# ---------------------------------------------------------------------------


def test_issue5410_geometry_is_legal_on_both_counts() -> None:
    """0.213 mm copper and 0.513 mm drill -- above 0.20 / 0.50."""
    case = build_named_fixture("issue5410-dqs-n-halo-vs-legal-via")
    a, b = case.vias
    centre = math.dist((a.x, a.y), (b.x, b.y))
    copper_gap = centre - a.diameter
    drill_gap = centre - a.drill
    assert copper_gap == pytest.approx(0.213, abs=0.001)
    assert drill_gap == pytest.approx(0.513, abs=0.001)
    assert copper_gap > PROJECT_CLEARANCE_MM
    assert drill_gap > HOLE_TO_HOLE_MM


def test_roundrect_corner_fixture_straddles_the_two_pad_models() -> None:
    """Legal against the exact outline, illegal against the bounding rect.

    This is the fixture's whole reason to exist, so it is checked here in
    closed form rather than inferred from whichever model happens to answer.
    """
    case = build_named_fixture("roundrect-corner-gap")
    (pad,) = case.pads
    (segment,) = case.segments
    shape = pad_shape(pad.footprint)
    assert shape.roundrect_rratio is not None

    width, height = shape.size
    outline_support = roundrect_corner_support(width, height, shape.roundrect_rratio)
    rect_support = math.hypot(width / 2.0, height / 2.0)
    assert rect_support > outline_support  # rounding the corners pulls copper in

    centreline_offset = segment.start[0] - pad.x
    exact_gap = centreline_offset - outline_support - segment.width / 2.0
    rect_gap = centreline_offset - rect_support - segment.width / 2.0

    assert exact_gap == pytest.approx(0.22, abs=1e-6)
    assert exact_gap > PROJECT_CLEARANCE_MM, "kicad-cli must find this clean"
    assert rect_gap < PROJECT_CLEARANCE_MM, (
        "the rectangle-bounded router pad model must find this too close -- "
        "otherwise the fixture no longer demonstrates the disagreement"
    )


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("refill", _FILL_STATES, ids=["asis", "refilled"])
def test_issue5398_fixture_is_flagged_by_kicad_cli(refill: bool) -> None:
    """One clearance violation at 0.180 mm against a 0.20 mm requirement.

    The router accepts or rejects this same pair depending on which object was
    committed first (#5398). kicad-cli has no notion of insertion order: the
    project's Default class is 0.20 mm, so it is a violation, full stop.
    """
    case = build_named_fixture("issue5398-seg-via-0p18-order")
    result = run_oracle(_fixture_board(case.name), refill=refill)

    # One *pair*, not one row: kicad-cli's row multiplicity for a given board
    # is not stable between runs, so the verdict set is the stable unit.
    clearance = sorted(result.of_kind("clearance"), key=lambda v: sorted(v.nets))
    assert len(clearance) == 1, (
        f"expected exactly one flagged clearance pair, got {len(clearance)}:\n{result.describe()}"
    )
    verdict = clearance[0]
    assert verdict.nets == frozenset({"SENSE_TRACK", "SENSE_VIA"})
    assert verdict.required_mm == pytest.approx(0.20, abs=1e-6)
    assert verdict.gap_mm == pytest.approx(0.180, abs=0.001)
    assert not result.dropped, f"unmapped clearance rows: {result.dropped}"


@pytest.mark.parametrize("name", _CLEAN_FIXTURES)
@pytest.mark.parametrize("refill", _FILL_STATES, ids=["asis", "refilled"])
def test_clean_fixtures_have_no_clearance_findings(name: str, refill: bool) -> None:
    """Three fixtures KiCad finds legal -- and at least one consumer does not.

    Each of these is copper the router refuses (or would refuse) today. If
    kicad-cli ever starts flagging one, the fixture no longer demonstrates an
    over-rejection and the epic's evidence has to be revisited.
    """
    result = run_oracle(_fixture_board(name), refill=refill)
    assert not result.verdicts, (
        f"{name} is supposed to be clean for kicad-cli:\n{result.describe()}"
    )
    assert not result.dropped, f"unmapped clearance rows: {result.dropped}"


def test_no_named_fixture_carries_a_zone() -> None:
    """Fill state cannot change these fixtures' verdicts, by construction.

    Every verdict assertion above is parametrized over both fill states, which
    is the fill-independence check -- and it is exact rather than approximate
    precisely because none of these boards has a pour for a refill to change.
    A fixture that grows a zone later needs its own zone-aware expectations,
    so this guards that assumption instead of leaving it implicit.
    """
    for name in NAMED_FIXTURES:
        assert build_named_fixture(name).zone is None, name


def test_oracle_runs_leave_the_fixture_directory_untouched() -> None:
    """Measuring a fixture must not write anything into its directory.

    Two distinct ways kicad-cli writes to a board's directory, both of which
    would quietly rewrite the evidence under test on every CI run:

    * ``run_refill_zones`` passes ``--refill-zones --save-board``, rewriting
      the board in place;
    * *any* kicad-cli run drops a ``.kicad_prl`` local-settings file next to
      the project.

    The oracle therefore measures a scratch copy. This checks both the file
    contents and the directory listing, because the second failure mode leaves
    the board itself byte-identical.
    """
    board = _fixture_board("issue5398-seg-via-0p18-order")
    directory = board.parent
    before_bytes = board.read_bytes()
    before_listing = sorted(p.name for p in directory.iterdir())

    run_oracle(board, refill=True)

    assert board.read_bytes() == before_bytes, "an oracle run mutated the committed fixture board"
    assert sorted(p.name for p in directory.iterdir()) == before_listing, (
        "an oracle run wrote a new file into the committed fixture directory"
    )
