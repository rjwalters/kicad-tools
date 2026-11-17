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
    BOARD_EDGE,
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
    """All nine kinds are placed, including ``pad-pad`` and the #5644 kinds.

    A kind the generator never emits is a consumer group that can only ever
    read ``not measured``, and nothing else in the suite would say so: the
    adapter would run, find no in-scope pair, and report a confident zero
    denominator.  Three are at risk for structural reasons: ``pad-pad`` costs
    both pad slots, and the two zone kinds are only drawable on the cases that
    happen to carry a pour.
    """
    kinds = {pair.kind for case in generate_corpus(range(24)) for pair in case.pairs}
    assert kinds == set(PairKind.ALL), f"missing pair kinds: {sorted(set(PairKind.ALL) - kinds)}"


def test_zone_pairs_only_exist_on_cases_that_carry_a_pour() -> None:
    """A zone pair with no pour would be an intent the board cannot realise.

    ``_choose_pair_kinds`` is told whether a pour was drawn *before* it picks,
    which is the whole reason the zone is built ahead of the pairs.  If that
    ordering is ever inverted, this is what says so.
    """
    for case in generate_corpus(range(48)):
        zone_pairs = [p for p in case.pairs if p.kind in PairKind.ZONE]
        if zone_pairs:
            assert case.zone is not None, f"seed {case.seed}: {zone_pairs} with no pour"
            for pair in zone_pairs:
                assert case.zone.net in pair.nets


def test_zone_pairs_are_drawn_above_the_requirement() -> None:
    """A sub-threshold zone pair is unreachable on a refilled board.

    KiCad backs a fresh fill off from foreign copper by the applied clearance,
    so fill copper is never closer than the requirement no matter where the
    generator puts the probe.  Drawing a sub-threshold zone gap would record an
    intent the refilled board contradicts -- the corpus would be measuring the
    filler's knockback and calling it a clearance model's answer.  The floor is
    checked with margin, because a draw *just* above the requirement would be
    realised by that same knockback rather than by the declared placement.
    """
    zone_pairs = [
        p for case in generate_corpus(range(48)) for p in case.pairs if p.kind in PairKind.ZONE
    ]
    assert zone_pairs, "no zone pair in 48 seeds -- the kinds are unreachable"
    for pair in zone_pairs:
        assert not pair.expect_violation
        assert not pair.boundary
        assert pair.target_gap_mm >= pair.required_mm + 0.01, (
            f"zone pair {sorted(pair.nets)} was drawn at {pair.target_gap_mm:.4f} mm "
            f"against a {pair.required_mm:.4f} mm requirement; a refilled pour "
            "cannot realise that gap"
        )


@pytest.mark.parametrize("seed", range(48))
def test_zone_pairs_realise_their_intended_gap_exactly(seed: int) -> None:
    """The analytic placement is exact against the pour boundary.

    The pour is an axis-aligned rectangle and the probe sits in the strip
    *above* its top edge, so the realised gap is a closed-form subtraction:
    the pour's minimum ``y`` minus the probe's copper extent towards it.  If
    that reasoning is wrong, group 18's zone cells and group 10's `pours` cell
    silently measure geometry the generator did not intend, and the
    disagreement would be a harness bug wearing a consumer's name.

    ``test_corpus_truth`` closes the same loop against kicad-cli, measuring the
    *filled* polygon rather than the declared boundary.
    """
    case = generate_case(seed)
    zone_pairs = [p for p in case.pairs if p.kind in PairKind.ZONE]
    if not zone_pairs:
        pytest.skip("no zone pair on this seed")
    assert case.zone is not None
    top = min(y for _, y in case.zone.boundary)
    segments = {s.net: s for s in case.segments}
    vias = {v.net: v for v in case.vias}

    for pair in zone_pairs:
        net = next(n for n in pair.nets if n != case.zone.net)
        if pair.kind == PairKind.SEG_ZONE:
            seg = segments[net]
            assert seg.layer == case.zone.layer, "a probe off the pour's layer cannot interact"
            assert seg.start[1] == seg.end[1], "the probe runs parallel to the pour boundary"
            realised = top - (seg.start[1] + seg.width / 2.0)
        else:
            via = vias[net]
            realised = top - (via.y + via.diameter / 2.0)
        assert realised == pytest.approx(pair.target_gap_mm, abs=1e-6), (
            f"seed {seed}: {pair.kind} pair {sorted(pair.nets)} intended a "
            f"{pair.target_gap_mm:.6f} mm gap but the placement realises {realised:.6f} mm"
        )


@pytest.mark.parametrize("seed", range(48))
def test_copper_edge_pairs_realise_their_intended_gap_exactly(seed: int) -> None:
    """Copper-to-outline placement is exact, and drawn against the edge rule.

    Boards are written ``center=False``, so the outline's top edge is ``y = 0``
    and a track whose copper reaches ``y = gap`` is exactly ``gap`` from it --
    again a closed-form statement rather than one measured by whichever
    consumer happens to notice.  The requirement is ``min_copper_to_edge``
    (the ``.kicad_pro`` board rule kicad-cli applies), **not** the netclass
    clearance every other kind is drawn against; scoring an edge pair against
    the wrong requirement would make the whole row's over/under split
    meaningless.
    """
    case = generate_case(seed)
    edge_pairs = [p for p in case.pairs if p.kind in PairKind.EDGE]
    if not edge_pairs:
        pytest.skip("no copper-edge pair on this seed")
    segments = {s.net: s for s in case.segments}

    for pair in edge_pairs:
        assert BOARD_EDGE in pair.nets, "an edge pair's partner is the board-edge pseudo-net"
        net = next(n for n in pair.nets if n != BOARD_EDGE)
        seg = segments[net]
        assert seg.start[1] == seg.end[1], "the probe runs parallel to the board edge"
        realised = seg.start[1] - seg.width / 2.0
        assert realised == pytest.approx(pair.target_gap_mm, abs=1e-6), (
            f"seed {seed}: copper-edge pair {sorted(pair.nets)} intended a "
            f"{pair.target_gap_mm:.6f} mm gap but the placement realises {realised:.6f} mm"
        )
        assert pair.required_mm == case.rules.min_copper_to_edge
        assert pair.expect_violation == (pair.target_gap_mm < case.rules.min_copper_to_edge)


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
