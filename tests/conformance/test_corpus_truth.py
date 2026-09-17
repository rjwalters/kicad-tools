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

One test item per ``(seed, fill state)``, never a loop over seeds inside one
item: the CI ``test`` job runs with ``--timeout=60`` and ``-n auto``, so a
batched item would be a timeout waiting to happen -- and would report one
failure for the whole corpus instead of naming the seed that broke.

Each item launches kicad-cli, so the smoke corpus is kept small on purpose;
the published table is measured out-of-band over a much wider seed range.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conformance.board import write_case
from tests.conformance.conftest import requires_kicad_cli
from tests.conformance.generator import BOUNDARY_BAND_MM, generate_case
from tests.conformance.oracle import run_oracle

pytestmark = requires_kicad_cli

# Kept deliberately small: this is the *smoke* corpus that runs on every PR,
# and every item here launches a kicad-cli process. The CI ``test`` job runs
# ``-n auto`` inside a 12 GiB cgroup alongside the C++ router suite, so each
# extra concurrent kicad-cli is memory the rest of the suite does not get. The
# published table is regenerated over a much wider seed range out-of-band by
# ``python -m tests.conformance.report --seeds 0-199``.
CI_SEEDS = tuple(range(6))

# Seed 0 carries a zone, so it is the seed where refilling can actually change
# something. Kept to a single seed on purpose: a refilled item costs three
# kicad-cli invocations (refill, then DRC, plus the as-is DRC it is compared
# against) and the CI ``test`` job reaps at ``--timeout=60``.
REFILL_SEEDS = (0,)


@pytest.mark.parametrize("seed", CI_SEEDS)
def test_kicad_cli_reproduces_the_intended_geometry(seed: int, tmp_path: Path) -> None:
    """The generator's intent and KiCad's measurement agree, per seed.

    Both halves of the harness self-check are asserted from a **single**
    kicad-cli run, because they are two readings of the same measurement and
    splitting them would double this suite's process count for no extra
    coverage:

    1. *Which* pairs are flagged. An equality, not an inclusion -- a missing
       flag means the placement did not realise the gap it recorded, and an
       extra flag means two objects interact that were supposed to be in
       different slots.
    2. *What gap* KiCad measured, against the gap the generator computed
       analytically, within 1 um. If this drifts, the placement maths is wrong
       and the corpus is not probing where it thinks it is.
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
