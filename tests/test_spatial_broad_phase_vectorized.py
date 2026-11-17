"""Vectorized broad phase must match the exhaustive expanded-bounds scan.

``candidate_pairs`` queries Shapely in blocks rather than one geometry at a
time (issue #5240).  The contract it has to keep is not "whatever Shapely
happens to return" but the documented one: every ordered pair whose bounding
boxes come within ``margin`` is yielded exactly once, in ascending ``(i, j)``
order, and malformed envelopes keep the pre-change per-element behavior.
"""

from __future__ import annotations

import math
import random
from itertools import combinations

import pytest

from kicad_tools.validate import spatial
from kicad_tools.validate.spatial import candidate_pairs

NAN = float("nan")
INF = float("inf")


def reference_pairs(bounds, margin):
    """Pure-Python exhaustive ground truth for the broad-phase contract."""
    for i, j in combinations(range(len(bounds)), 2):
        axmin, aymin, axmax, aymax = bounds[i]
        bxmin, bymin, bxmax, bymax = bounds[j]
        # Inclusive: a touching or exactly-``margin``-separated pair is a
        # candidate, because the exact predicate decides those cases.
        if (
            axmin - margin <= bxmax
            and bxmin <= axmax + margin
            and aymin - margin <= bymax
            and bymin <= aymax + margin
        ):
            yield i, j


def _layout(seed, count):
    rng = random.Random(seed)
    bounds = []
    for _ in range(count):
        x, y = rng.uniform(-30, 30), rng.uniform(-30, 30)
        # Degenerate (zero-area) copper envelopes are routine: a zero-length
        # segment or a point-sized pad both produce one.
        width = rng.choice([0.0, 0.001, rng.uniform(0, 5)])
        height = rng.choice([0.0, 0.001, rng.uniform(0, 5)])
        bounds.append((x, y, x + width, y + height))
    return bounds


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("margin", [0.0, 0.0001, 0.2001, 1.5])
def test_exhaustive_reference_parity(seed, margin):
    bounds = _layout(seed, 120)
    assert list(candidate_pairs(bounds, margin)) == list(reference_pairs(bounds, margin))


@pytest.mark.parametrize(
    "bounds,margin",
    [
        ([], 0.1),
        ([(0.0, 0.0, 1.0, 1.0)], 0.1),
        ([(0.0, 0.0, 1.0, 1.0), (1.0, 0.0, 2.0, 1.0)], 0.0),  # touching
        ([(0.0, 0.0, 1.0, 1.0), (1.05, 0.0, 2.0, 1.0)], 0.05),  # exactly margin
        ([(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)], 0.0),  # points
        ([(0.0, 0.0, 5.0, 0.0), (2.0, 0.0, 2.0, 9.0)], 0.0),  # crossing lines
        ([(1.0, 1.0, 2.0, 2.0)] * 4, 0.0),  # duplicates
        ([(0, 0, 1, 1), (1, 1, 2, 2)], 0),  # integer bounds
        ([(0.0, 0.0, 1.0, 1.0), (0.9, 0.0, 2.0, 1.0)], -0.5),  # negative margin
        ([(0.0, 0.0, 1e-12, 1e-12), (0.0, 0.0, 1e-12, 1e-12)], 0.0),
    ],
)
def test_degenerate_and_boundary_cases(bounds, margin):
    assert list(candidate_pairs(bounds, margin)) == list(reference_pairs(bounds, margin))


def test_block_boundary_pairs_are_not_dropped():
    # More elements than one query block, with every neighbor bridging the
    # block split, so a per-block query that forgot its offset would lose
    # pairs or emit them out of order.
    count = spatial._QUERY_BLOCK * 2 + 7
    bounds = [(float(i), 0.0, float(i) + 1.5, 1.0) for i in range(count)]
    actual = list(candidate_pairs(bounds, 0.1))
    assert actual == list(reference_pairs(bounds, 0.1))
    assert actual == sorted(actual)
    assert len(set(actual)) == len(actual)
    assert (spatial._QUERY_BLOCK - 1, spatial._QUERY_BLOCK) in actual


def test_emission_is_incremental_across_blocks(monkeypatch):
    # Callers such as the coupling predicate stop as soon as their cumulative
    # evidence passes a threshold, so the first pair must not require querying
    # every block: peak temporary memory stays bounded by one block.
    import shapely

    queries = []
    real_tree = shapely.STRtree

    class CountingTree(real_tree):  # type: ignore[misc, valid-type]
        def query(self, geometry, *args, **kwargs):
            queries.append(getattr(geometry, "size", 1))
            return super().query(geometry, *args, **kwargs)

    monkeypatch.setattr(shapely, "STRtree", CountingTree)
    count = spatial._QUERY_BLOCK * 3
    bounds = [(float(i), 0.0, float(i) + 1.5, 1.0) for i in range(count)]
    pairs = candidate_pairs(bounds, 0.1)
    assert next(pairs) == (0, 1)
    assert queries == [spatial._QUERY_BLOCK]
    assert len(list(pairs)) == count - 2  # remaining consecutive neighbors
    assert len(queries) == 3


@pytest.mark.parametrize(
    "bounds,margin",
    [
        ([(NAN, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)], 0.1),
        ([(-INF, -INF, INF, INF), (0.0, 0.0, 1.0, 1.0)], 0.1),
        ([(0.0, 0.0, 1.0, 1.0), (50.0, 50.0, 51.0, 51.0)], INF),
        ([(NAN, NAN, NAN, NAN), (NAN, NAN, NAN, NAN)], 0.1),
        ([(0.0, 0.0, 1.0, 1.0), (0.5, 0.5, 1.5, 1.5)], NAN),
    ],
)
def test_non_finite_input_uses_scalar_path(monkeypatch, bounds, margin):
    calls = []
    original = spatial._scalar_candidate_pairs

    def recorded(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(spatial, "_scalar_candidate_pairs", recorded)
    # A fully non-finite envelope makes GEOS raise; a partially non-finite one
    # yields pairs.  Either way the outcome must come from the scalar path,
    # never from a vectorized query that would silently report "no candidates"
    # and skip the exact check the caller asked for.
    try:
        result = list(candidate_pairs(bounds, margin))
    except Exception:  # noqa: BLE001 - GEOS error type is not part of the contract
        result = None
    assert len(calls) == 1
    if result is not None:
        assert result == list(original(bounds, margin))


def test_finite_input_skips_scalar_path(monkeypatch):
    def forbidden(*args):
        raise AssertionError("finite envelopes must use the vectorized query")

    monkeypatch.setattr(spatial, "_scalar_candidate_pairs", forbidden)
    bounds = _layout(9, 40)
    assert list(candidate_pairs(bounds, 0.2)) == list(reference_pairs(bounds, 0.2))


def test_no_shapely_fallback_is_still_exhaustive(monkeypatch):
    monkeypatch.setattr(spatial, "has_shapely", lambda: False)
    bounds = [(0.0, 0.0, 1.0, 1.0), (100.0, 100.0, 101.0, 101.0), (0.5, 0.5, 0.6, 0.6)]
    assert list(candidate_pairs(bounds, 0.0)) == [(0, 1), (0, 2), (1, 2)]


def test_scalar_and_vectorized_agree_on_dense_copper():
    # Dense overlapping copper: candidate count is quadratic here, so the
    # block-wise path must still deduplicate and order correctly.
    bounds = [(i * 0.01, 0.0, i * 0.01 + 1.0, 1.0) for i in range(300)]
    vectorized = list(candidate_pairs(bounds, 0.05))
    assert vectorized == list(spatial._scalar_candidate_pairs(bounds, 0.05))
    assert vectorized == list(reference_pairs(bounds, 0.05))
    assert math.isclose(len(vectorized), len(set(vectorized)))
