"""The corpus itself is measured against kicad-cli -- before any adapter is.

A conformance oracle is only worth anything if its generator actually produces
the geometry it claims to. If the analytic placement were wrong, every rate in
``docs/clearance-conformance.md`` would be measuring a harness bug and reading
as a consumer disagreement.

So these tests close that loop: for each seed, the gap the generator *intended*
is compared against what KiCad *measured*, and the set of pairs the generator
expects to be violations is compared against the set KiCad flagged. They assert
ground truth only -- no consumer is involved, nothing is ``xfail``ed.

They also protect the property that makes the corpus readable at all: pairs
live in well-separated slots, so the only near-neighbour relationship on a
board is the intended one. A stray cross-slot finding would silently inflate
every disagreement rate.

One test item per ``(seed, refill)``: the CI ``test`` job runs with
``--timeout=60`` and ``-n auto``, so a loop over many seeds inside a single
item would be a timeout waiting to happen (and would report one failure for
the whole corpus instead of naming the seed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conformance.board import write_case
from tests.conformance.conftest import requires_kicad_cli
from tests.conformance.generator import BOUNDARY_BAND_MM, generate_case
from tests.conformance.oracle import run_oracle

pytestmark = requires_kicad_cli

# Kept deliberately small: this is the *smoke* corpus that runs on every PR.
# The published table is regenerated over a much wider seed range by
# ``python -m tests.conformance.report --seeds 0-199``.
CI_SEEDS = tuple(range(8))

# Seed 0 carries a zone, so it is the seed where refilling can actually change
# something. Kept to a single seed on purpose: a refilled item costs three
# kicad-cli invocations (refill, then DRC, plus the as-is DRC it is compared
# against) and the CI ``test`` job reaps at ``--timeout=60``.
REFILL_SEEDS = (0,)


@pytest.mark.parametrize("seed", CI_SEEDS)
def test_kicad_cli_reproduces_the_intended_gaps(seed: int, tmp_path: Path) -> None:
    """Every pair the generator placed at ``gap < required`` is flagged, and
    no pair placed above it is.

    This is the harness's self-check. It is an equality, not an inclusion:
    a missing flag means the placement did not realise the gap it recorded,
    and an extra flag means two objects interact that were supposed to be in
    different slots.
    """
    case = generate_case(seed)
    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)

    expected = {p.nets for p in case.pairs if p.expect_violation}
    actual = {v.nets for v in result.without_zones() if v.kind == "clearance"}

    assert actual == expected, (
        f"seed {seed}: kicad-cli disagrees with the generator's own geometry.\n"
        f"  intended violations: {sorted(sorted(n) for n in expected)}\n"
        f"  kicad-cli flagged:   {sorted(sorted(n) for n in actual)}\n"
        f"{result.describe()}"
    )
    assert not result.dropped, (
        f"seed {seed}: clearance rows that could not be mapped to a net pair "
        f"(harness gap, not a consumer disagreement): {result.dropped}"
    )


@pytest.mark.parametrize("seed", CI_SEEDS)
def test_measured_gap_matches_the_analytic_placement(seed: int, tmp_path: Path) -> None:
    """KiCad's reported ``actual`` equals the gap the generator computed.

    Within 1 um -- the same band the generator treats as "too close to the
    threshold to mean anything". If this drifts, the placement maths is wrong
    and the corpus is not probing where it thinks it is.
    """
    case = generate_case(seed)
    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)

    by_pair = {v.nets: v for v in result.without_zones() if v.kind == "clearance"}
    checked = 0
    for pair in case.pairs:
        verdict = by_pair.get(pair.nets)
        if verdict is None or verdict.gap_mm is None:
            continue
        checked += 1
        assert verdict.gap_mm == pytest.approx(pair.target_gap_mm, abs=BOUNDARY_BAND_MM), (
            f"seed {seed}, pair {pair.kind} {sorted(pair.nets)}: generator "
            f"intended a {pair.target_gap_mm:.4f} mm gap, kicad-cli measured "
            f"{verdict.gap_mm:.4f} mm"
        )
    if by_pair:
        assert checked, "no flagged pair could be matched back to its intent"


@pytest.mark.parametrize("seed", REFILL_SEEDS)
def test_non_zone_verdicts_are_fill_state_independent(seed: int, tmp_path: Path) -> None:
    """Refilling zones changes zone rows only.

    Zones are written unfilled, so an unrefilled run sees no zone copper at
    all. Non-zone verdicts must be identical either way -- that is what makes
    the cheaper unrefilled run a valid basis for the non-zone columns of the
    table.
    """
    case = generate_case(seed)
    board = write_case(case, tmp_path)
    as_is = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)
    refilled = run_oracle(board.pcb_path, refill=True, work_dir=tmp_path)

    assert as_is.without_zones() == refilled.without_zones(), (
        f"seed {seed}: refilling changed a non-zone verdict\n"
        f"as-is:    {sorted(v.describe() for v in as_is.without_zones())}\n"
        f"refilled: {sorted(v.describe() for v in refilled.without_zones())}"
    )
    assert as_is.refill is False and refilled.refill is True
