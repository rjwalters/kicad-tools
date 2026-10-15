"""Typed ``clear`` overloads agree bit-for-bit with the variant dispatcher (#5617).

Issue #5617 gave the C++ clearance kernel typed ``clear`` overloads for the
segment/via pair kinds, so the A* search's per-step stored-geometry checks stop
building two ``std::variant`` temporaries per call.  They must be the
dispatcher's own answer, not a close one: a single flipped verdict changes
which cells the search may enter, and with it the routed board.

The ``router_cpp.clear`` binding routes segment/via pairs through those typed
overloads, while ``router_cpp.copper_gap`` still goes through the ``KShape``
dispatcher.  So ``clear(a, b, r) == (copper_gap(a, b) >= r - EPS)`` is the
exact equivalence, and it is probed at the threshold itself (``r`` placed on
the gap, and one ulp either side of it) where any arithmetic difference would
show as a flipped verdict.
"""

from __future__ import annotations

import math
import random

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

ALL_LAYERS = -1
LAYERS = (ALL_LAYERS, 0, 1, 2, 3)


def _segment(rng: random.Random):
    from kicad_tools.router import router_cpp

    x1, y1 = rng.uniform(-5, 5), rng.uniform(-5, 5)
    if rng.random() < 0.1:  # zero-length segment
        x2, y2 = x1, y1
    else:
        x2, y2 = rng.uniform(-5, 5), rng.uniform(-5, 5)
    return router_cpp.KSegment(x1, y1, x2, y2, rng.uniform(0.05, 0.6), rng.choice(LAYERS))


def _via(rng: random.Random):
    from kicad_tools.router import router_cpp

    diameter = rng.uniform(0.3, 0.9)
    return router_cpp.KVia(rng.uniform(-5, 5), rng.uniform(-5, 5), diameter, diameter / 2)


def _requirements(gap: float, rng: random.Random) -> list[float]:
    """Requirements at, and a hair either side of, this pair's threshold."""
    from kicad_tools.router import router_cpp

    eps = router_cpp.CLEARANCE_EPSILON_MM
    reqs = [rng.uniform(0.0, 0.5)]
    if math.isfinite(gap):
        edge = gap + eps
        reqs += [edge, math.nextafter(edge, math.inf), math.nextafter(edge, -math.inf), gap]
    return reqs


@requires_cpp
@pytest.mark.parametrize("kinds", [("segment", "segment"), ("segment", "via"), ("via", "via")])
def test_typed_clear_matches_dispatcher(kinds: tuple[str, str]) -> None:
    from kicad_tools.router import router_cpp

    eps = router_cpp.CLEARANCE_EPSILON_MM
    make = {"segment": _segment, "via": _via}
    rng = random.Random(5617)
    checked = 0
    for _ in range(3000):
        a = make[kinds[0]](rng)
        b = make[kinds[1]](rng)
        # Same-kind pairs keep their argument order through the dispatcher
        # (it only reorders by rank), so the two orders may differ in the
        # last bit -- each typed call is held to its own order's gap.
        gap_ab = router_cpp.copper_gap(a, b)
        gap_ba = router_cpp.copper_gap(b, a)
        for required in _requirements(gap_ab, rng) + _requirements(gap_ba, rng):
            assert router_cpp.clear(a, b, required) is (gap_ab >= required - eps), (
                a,
                b,
                required,
                gap_ab,
            )
            assert router_cpp.clear(b, a, required) is (gap_ba >= required - eps), (
                b,
                a,
                required,
                gap_ba,
            )
            checked += 1
    assert checked > 3000


@requires_cpp
def test_segments_on_different_layers_never_interact() -> None:
    from kicad_tools.router import router_cpp

    a = router_cpp.KSegment(0.0, 0.0, 1.0, 0.0, 0.2, 0)
    b = router_cpp.KSegment(0.0, 0.0, 1.0, 0.0, 0.2, 3)  # coincident copper, other layer
    assert router_cpp.copper_gap(a, b) == math.inf
    assert router_cpp.clear(a, b, 1e9) is True
    same = router_cpp.KSegment(0.0, 0.0, 1.0, 0.0, 0.2, ALL_LAYERS)
    assert router_cpp.clear(a, same, 0.0) is False  # ALL_LAYERS meets every layer
