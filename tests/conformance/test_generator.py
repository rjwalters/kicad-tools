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

import pytest

from tests.conformance.generator import (
    BOUNDARY_BAND_MM,
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
