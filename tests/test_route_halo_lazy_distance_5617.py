"""``RouteHaloGeometry.clear`` measures shapely distance only where it is read (Issue #5617).

``distance`` is consumed solely by the via-vs-via drill-pitch floor.  It used
to be measured for every neighbour that survived the bounding-box prune,
including every trace candidate, where it was dead work (~16.7 s of the ~101 s
pure-Python A* fallback in the board 06 re-route).  The verdicts are pinned
against the pre-change implementation (recorded as a digest) and the dead
``distance`` calls are shown to be gone.
"""

from __future__ import annotations

import hashlib

import pytest
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]

from kicad_tools.router.primitives import Segment, Via
from tests.test_route_halo_bounds_pruning import _candidates, _context

# sha256 over the verdict bitstring of seeds 1..5 x pairwise {False, True},
# recorded from the implementation BEFORE this change (eager ``distance``).
_PRE_CHANGE_DIGEST = "6a100e837877cd77270a6af47e5de6530e6017c4f75df4fb65194dd7e5fb0c4c"


def run_corpus(seed: int, pairwise: bool, calls: list[int]) -> list[bool]:
    """Verdicts for one corpus; ``calls[0|1]`` counts distance calls by candidate kind."""
    grid, router = _context(seed, pairwise=pairwise)
    halo = grid._route_halo
    original = BaseGeometry.distance
    state = {"kind": 0}

    def counting(self, other):
        calls[state["kind"]] += 1
        return original(self, other)

    BaseGeometry.distance = counting  # type: ignore[method-assign]
    try:
        out = []
        for index, candidate in enumerate(_candidates(seed)):
            kwargs: dict = {}
            if isinstance(candidate, Segment) and index % 3 == 0:
                kwargs = {"partner_net": 2 if candidate.net != 2 else 3, "partner_clearance": 0.1}
            if index % 5 == 0:
                kwargs["require_geometry"] = False
            state["kind"] = 0 if isinstance(candidate, Segment) else 1
            out.append(halo.clear(candidate, router, **kwargs))
    finally:
        BaseGeometry.distance = original  # type: ignore[method-assign]
    return out


def digest_all() -> tuple[str, list[int]]:
    bits: list[bool] = []
    calls = [0, 0]
    for seed in (1, 2, 3, 4, 5):
        for pairwise in (False, True):
            bits.extend(run_corpus(seed, pairwise, calls))
    assert any(bits) and not all(bits)
    return hashlib.sha256("".join("1" if b else "0" for b in bits).encode()).hexdigest(), calls


def test_verdicts_match_pre_change_implementation():
    digest, _ = digest_all()
    assert digest == _PRE_CHANGE_DIGEST


def test_trace_candidates_never_measure_shapely_distance():
    calls = [0, 0]
    for seed in (1, 2, 3):
        run_corpus(seed, False, calls)
    assert calls[0] == 0, "trace candidates must not pay for the dead distance"
    assert calls[1] > 0, "via candidates still measure it (drill-pitch floor)"


@pytest.mark.parametrize("cls", [Segment, Via])
def test_corpus_exercises_both_candidate_kinds(cls):
    assert any(isinstance(c, cls) for c in _candidates(1))
