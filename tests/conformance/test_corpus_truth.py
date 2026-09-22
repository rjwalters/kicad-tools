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

from tests.conformance.adapters import KIND_CLEARANCE, KIND_COPPER_EDGE
from tests.conformance.board import write_case
from tests.conformance.conftest import requires_kicad_cli
from tests.conformance.generator import BOUNDARY_BAND_MM, PairKind, generate_case
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

# One seed per zone pair kind: seed 7 draws a ``via-zone`` pair and seed 22 a
# ``seg-zone`` one. Pinned rather than searched for, so a regeneration that
# stopped placing a kind fails loudly here instead of quietly skipping.
ZONE_PAIR_SEEDS = (7, 22)


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

    # Split by verdict kind: a ``copper-edge`` pair is reported by kicad-cli as
    # a one-sided ``copper_edge_clearance`` row against a *different* rule
    # value (``min_copper_to_edge``), so folding the two kinds together would
    # compare each intent against the wrong ground-truth family.
    edge_kinds = set(PairKind.EDGE)
    expected = {p.nets for p in case.pairs if p.expect_violation and p.kind not in edge_kinds}
    actual = {v.nets for v in result.without_zones() if v.kind == KIND_CLEARANCE}
    expected_edge = {p.nets for p in case.pairs if p.expect_violation and p.kind in edge_kinds}
    actual_edge = {v.nets for v in result.without_zones() if v.kind == KIND_COPPER_EDGE}

    assert actual == expected, (
        f"seed {seed}: kicad-cli disagrees with the generator's own geometry.\n"
        f"  intended violations: {sorted(sorted(n) for n in expected)}\n"
        f"  kicad-cli flagged:   {sorted(sorted(n) for n in actual)}\n"
        f"{result.describe()}"
    )
    assert actual_edge == expected_edge, (
        f"seed {seed}: kicad-cli disagrees with the generator's copper-to-edge "
        "geometry.\n"
        f"  intended violations: {sorted(sorted(n) for n in expected_edge)}\n"
        f"  kicad-cli flagged:   {sorted(sorted(n) for n in actual_edge)}\n"
        f"{result.describe()}"
    )
    assert not result.dropped, (
        f"seed {seed}: clearance rows that could not be mapped to a net pair "
        f"(harness gap, not a consumer disagreement): {result.dropped}"
    )

    by_pair = {
        v.nets: v for v in result.without_zones() if v.kind in (KIND_CLEARANCE, KIND_COPPER_EDGE)
    }
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


@pytest.mark.parametrize("seed", ZONE_PAIR_SEEDS)
def test_refilled_fill_realises_the_intended_zone_gap(seed: int, tmp_path: Path) -> None:
    """The zone half of the harness self-check, closed against KiCad's filler.

    ``test_generator`` asserts the probe sits at the intended gap from the
    pour's *declared boundary*. That is not the geometry the zone rules read:
    ``SegmentZoneClearanceRule`` / ``ViaZoneClearanceRule`` /
    ``physical_gap.py`` all consume committed ``filled_polygon`` copper, and
    the filler is free to put that copper somewhere else -- it knocks the pour
    back around foreign objects by the applied clearance. This is what says
    the two agree: where the probe is drawn clear of the requirement, the
    filler leaves the boundary alone and the fill realises the declared gap.

    It also pins the property that forces the one-sided draw (see
    ``generator._draw_clear_gap``): a refilled pour is *never* closer to
    foreign copper than the requirement, so kicad-cli reports no zone finding
    for a corpus zone pair, and a zone row can only ever show over-rejection.
    """
    from shapely.geometry import LineString, Point, Polygon

    from kicad_tools.schema.pcb import PCB

    case = generate_case(seed)
    zone_pairs = [p for p in case.pairs if p.kind in PairKind.ZONE]
    assert zone_pairs, f"seed {seed} no longer carries a zone pair -- re-pin ZONE_PAIR_SEEDS"
    assert case.zone is not None

    board = write_case(case, tmp_path)
    refilled = run_oracle(board.pcb_path, refill=True, work_dir=tmp_path)

    flagged = {v.nets for v in refilled.verdicts if v.zone}
    assert not (flagged & {p.nets for p in zone_pairs}), (
        f"seed {seed}: kicad-cli flagged a zone pair the generator placed "
        f"above the requirement\n{refilled.describe()}"
    )

    pcb = PCB.load(refilled.pcb_path)
    fills = [
        Polygon(points) for zone in pcb.zones for points in zone.filled_polygons if len(points) >= 3
    ]
    assert fills, f"seed {seed}: the refilled board carries no zone fill to measure against"

    segments = {s.net: s for s in case.segments}
    vias = {v.net: v for v in case.vias}
    for pair in zone_pairs:
        net = next(n for n in pair.nets if n != case.zone.net)
        if pair.kind == PairKind.SEG_ZONE:
            seg = segments[net]
            probe, half = LineString([seg.start, seg.end]), seg.width / 2.0
        else:
            via = vias[net]
            probe, half = Point(via.x, via.y), via.diameter / 2.0
        realised = min(probe.distance(fill) for fill in fills) - half
        assert realised == pytest.approx(pair.target_gap_mm, abs=BOUNDARY_BAND_MM), (
            f"seed {seed}, pair {pair.kind} {sorted(pair.nets)}: generator "
            f"intended a {pair.target_gap_mm:.4f} mm gap to the pour, the "
            f"refilled fill realises {realised:.4f} mm"
        )
