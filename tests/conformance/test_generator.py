"""Generator invariants -- the properties that make the corpus meaningful.

These run everywhere, kicad-cli or not: they are statements about the cases the
generator emits, not about what KiCad thinks of them.

The important one is :func:`test_corpus_probes_the_threshold_from_both_sides`.
A corpus that is entirely clean, or entirely violating, produces a
plausible-looking table of zeroes that measures nothing -- and nothing else in
the suite would go red. That failure mode is the reason the generator samples
the *target gap* around the requirement and then places geometry to realise it,
rather than sampling positions and hoping some land near the threshold.
"""

from __future__ import annotations

import math

import pytest

from tests.conformance.generator import (
    BOUNDARY_BAND_MM,
    PairKind,
    generate_case,
    generate_corpus,
)


@pytest.mark.parametrize("seed", range(24))
def test_case_shape_matches_the_phase_envelope(seed: int) -> None:
    """2-6 segments, 1-3 vias, up to 2 pads, every object on its own net."""
    case = generate_case(seed)
    assert 2 <= len(case.segments) <= 6, len(case.segments)
    assert 1 <= len(case.vias) <= 3, len(case.vias)
    assert len(case.pads) <= 2, len(case.pads)
    assert case.layers in (2, 4)

    nets = [obj.net for obj in case.copper_objects]
    if case.zone is not None:
        nets.append(case.zone.net)
    assert len(nets) == len(set(nets)), (
        "pair identity is by net name, so every object needs its own net"
    )


def test_corpus_probes_the_threshold_from_both_sides() -> None:
    """A corpus that is all-clean or all-violating measures nothing.

    The generator draws the target gap around the requirement precisely so
    that both over-rejection and under-rejection are observable. If the draw
    ever collapses to one side, every rate in the table becomes vacuous
    without any test going red -- unless this one does.
    """
    cases = generate_corpus(range(24))
    pairs = [p for case in cases for p in case.pairs]
    assert pairs

    violating = [p for p in pairs if p.expect_violation]
    clean = [p for p in pairs if not p.expect_violation]
    assert len(violating) >= len(pairs) // 4, "too few sub-threshold pairs"
    assert len(clean) >= len(pairs) // 4, "too few above-threshold pairs"

    assert not any(p.boundary for p in pairs), (
        "generated pairs must be re-drawn out of the boundary band; a pair "
        "inside it measures rounding, not model disagreement"
    )
    closest = min(abs(p.target_gap_mm - p.required_mm) for p in pairs)
    assert closest > BOUNDARY_BAND_MM


def test_every_pair_kind_appears_in_a_modest_seed_range() -> None:
    """All six kinds are placed, including ``pad-pad``.

    A kind the generator never emits is a consumer group that can only ever
    read ``not measured``, and nothing else in the suite would say so: the
    adapter would run, find no in-scope pair, and report a confident zero
    denominator.  ``pad-pad`` is the one at risk -- it costs both pad slots, so
    it is only feasible when the second pick left them free.
    """
    kinds = {pair.kind for case in generate_corpus(range(48)) for pair in case.pairs}
    assert kinds == set(PairKind.ALL), f"missing pair kinds: {sorted(set(PairKind.ALL) - kinds)}"


@pytest.mark.parametrize("seed", range(48))
def test_pad_pad_pairs_realise_their_intended_gap_exactly(seed: int) -> None:
    """The analytic placement is exact for every drawn rotation.

    ``pad-pad`` is placed along the first pad's local ``+X`` axis with the
    second pad turned to face it (``rotation + 180``), because that is the one
    direction where every catalogue shape's support is ``w / 2`` and the gap is
    therefore known in closed form regardless of angle.  If that reasoning is
    wrong -- a support that is not ``w / 2``, a rotation convention that is not
    what KiCad renders -- group 19's row silently measures geometry the
    generator did not intend, and the disagreement would be a harness bug wearing
    a consumer's name.

    Checked here in closed form (centre distance minus both supports), so a
    failure points at the placement rather than at whichever consumer happens
    to notice.  ``test_corpus_truth`` closes the same loop against kicad-cli's
    own measurement.
    """
    case = generate_case(seed)
    by_net = {pad.net: pad for pad in case.pads}
    pad_pairs = [p for p in case.pairs if p.kind == PairKind.PAD_PAD]

    for pair in pad_pairs:
        a, b = by_net[pair.net_a], by_net[pair.net_b]
        centre = math.dist((a.x, a.y), (b.x, b.y))
        realised = centre - a.shape.half_extent_x - b.shape.half_extent_x
        assert realised == pytest.approx(pair.target_gap_mm, abs=1e-6), (
            f"seed {seed}: pad-pad pair {sorted(pair.nets)} intended a "
            f"{pair.target_gap_mm:.6f} mm gap but the placement realises "
            f"{realised:.6f} mm"
        )
        # Facing each other: the second pad's local +X points back at the first.
        assert (a.rotation + 180.0) % 360.0 == pytest.approx(b.rotation, abs=1e-3)
