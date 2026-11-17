"""Bit-exactness of the batched 1-Steiner candidate scoring (Issue #6019).

``_iterative_one_steiner`` scores every Hanan-grid candidate with a trial
MST.  The batched numpy Prim (``_batched_trial_mst_costs``) must reproduce
the scalar ``_mst_cost`` **bit for bit**: the result feeds the FOM
``trace_length_excess`` term and the router's RSMT decomposition, and an
ulp-level difference flips near-tied ``gain > best_gain`` comparisons
(the PR #6022 review measured 44/2000 random sets diverging with a naive
``argmin`` Prim).

The reference below is the pre-#6019 loop verbatim, built on the
unchanged scalar ``_mst_cost`` / ``_hanan_grid`` / ``_build_mst_edges``.
"""

from __future__ import annotations

import random
import struct

import pytest

from kicad_tools.router.algorithms import steiner
from kicad_tools.router.algorithms.steiner import (
    _batched_trial_mst_costs,
    _build_mst_edges,
    _hanan_grid,
    _iterative_one_steiner,
    _manhattan,
    _mst_cost,
)


def _reference_iterative_one_steiner(terminals, cost_fn=None, max_iterations=50):
    """Pre-#6019 ``_iterative_one_steiner``, verbatim."""
    all_points = list(terminals)
    current_cost = _mst_cost(all_points, cost_fn)
    for _ in range(max_iterations):
        candidates = _hanan_grid(all_points)
        if not candidates:
            break
        best_gain = 0.0
        best_candidate = None
        for candidate in candidates:
            trial = all_points + [candidate]
            trial_cost = _mst_cost(trial, cost_fn)
            gain = current_cost - trial_cost
            if gain > best_gain:
                best_gain = gain
                best_candidate = candidate
        if best_candidate is None or best_gain <= 0:
            break
        all_points.append(best_candidate)
        current_cost -= best_gain
    edges = _build_mst_edges(all_points, cost_fn)
    return all_points, edges


def _bits(values):
    return [struct.pack("<d", v) for v in values]


def _point_bits(points):
    return [_bits(p) for p in points]


def _random_points(rng: random.Random, kind: str, n: int) -> list[tuple[float, float]]:
    if kind == "grid0635":  # heavy ties, inexact binary pitch
        span = rng.choice([4, 8, 20])
        return [(rng.randint(0, span) * 0.635, rng.randint(0, span) * 0.635) for _ in range(n)]
    if kind == "grid01_offset":  # 0.1 mm grid at board-like offsets
        ox = rng.uniform(50, 200)
        return [(ox + rng.randint(0, 60) * 0.1, 100 + rng.randint(0, 60) * 0.1) for _ in range(n)]
    if kind == "continuous":
        return [(rng.uniform(0, 50), rng.uniform(0, 50)) for _ in range(n)]
    if kind == "duplicates":  # stacked pads -> zero-length ties
        base = [(rng.randint(0, 5) * 0.635, rng.randint(0, 5) * 0.635) for _ in range(n // 2)]
        return [rng.choice(base) for _ in range(n)]
    raise AssertionError(kind)


KINDS = ["grid0635", "grid01_offset", "continuous", "duplicates"]


def _naive_ascending_mst_cost(points):
    """Prim with a lowest-index tie-break (what a plain ``argmin`` does)."""
    n = len(points)
    in_tree = [False] * n
    in_tree[0] = True
    key = [_manhattan(*points[0], *p) for p in points]
    total = 0.0
    for _ in range(n - 1):
        j = min((k for k in range(n) if not in_tree[k]), key=lambda k: (key[k], k))
        total += key[j]
        in_tree[j] = True
        for k in range(n):
            if not in_tree[k]:
                key[k] = min(key[k], _manhattan(*points[j], *points[k]))
    return total


class TestBatchedTrialCostsBitExact:
    @pytest.mark.parametrize("kind", KINDS)
    def test_random_point_sets(self, kind):
        rng = random.Random(6019)
        for _ in range(150):
            pts = _random_points(rng, kind, rng.randint(4, 14))
            cands = _hanan_grid(pts)
            if not cands:
                continue
            expected = [_mst_cost(pts + [c]) for c in cands]
            assert _bits(_batched_trial_mst_costs(pts, cands)) == _bits(expected), pts

    def test_set_order_tie_break(self):
        """Ties between different unconnected vertices follow ``set`` order.

        On this 5-point 0.635 mm-grid set a lowest-index tie-break (plain
        ``argmin``) sums Prim's edges in a different order and lands on a
        different float for some candidate; the batched scorer must not.
        """
        pts = [(1.905, 0.0), (1.27, 2.54), (1.905, 1.27), (2.54, 0.635), (0.0, 2.54)]
        cands = _hanan_grid(pts)
        expected = [_mst_cost(pts + [c]) for c in cands]
        naive = [_naive_ascending_mst_cost(pts + [c]) for c in cands]
        assert _bits(naive) != _bits(expected)  # the case really exercises ties
        assert _bits(_batched_trial_mst_costs(pts, cands)) == _bits(expected)

    def test_larger_net(self):
        rng = random.Random(44)
        pts = [(rng.randint(0, 40) * 0.635, rng.randint(0, 40) * 0.635) for _ in range(22)]
        cands = _hanan_grid(pts)
        expected = [_mst_cost(pts + [c]) for c in cands]
        assert _bits(_batched_trial_mst_costs(pts, cands)) == _bits(expected)


class TestIterativeOneSteinerUnchanged:
    @pytest.mark.parametrize("threshold", [0, steiner._BATCHED_MIN_POINTS])
    @pytest.mark.parametrize("kind", KINDS)
    def test_matches_reference(self, monkeypatch, kind, threshold):
        monkeypatch.setattr(steiner, "_BATCHED_MIN_POINTS", threshold)
        rng = random.Random(f"{kind}-{threshold}")
        for _ in range(30):
            n = rng.randint(4, 12)
            pts = _random_points(rng, kind, n)
            iters = 50 if n <= 9 else min(n, 30)
            exp_points, exp_edges = _reference_iterative_one_steiner(pts, None, iters)
            got_points, got_edges = _iterative_one_steiner(pts, None, iters)
            assert _point_bits(got_points) == _point_bits(exp_points), pts
            assert got_edges == exp_edges, pts

    def test_custom_cost_fn_keeps_scalar_path(self, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("batched path must not run for a custom cost_fn")

        monkeypatch.setattr(steiner, "_batched_trial_mst_costs", _boom)
        monkeypatch.setattr(steiner, "_BATCHED_MIN_POINTS", 0)

        def cost(x1, y1, x2, y2):
            return _manhattan(x1, y1, x2, y2) * (1.0 + 0.01 * x1)

        pts = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0), (5.0, 3.0)]
        assert _iterative_one_steiner(pts, cost) == _reference_iterative_one_steiner(pts, cost)

    def test_non_finite_coordinates_keep_scalar_path(self, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("batched path must not run on non-finite input")

        monkeypatch.setattr(steiner, "_batched_trial_mst_costs", _boom)
        monkeypatch.setattr(steiner, "_BATCHED_MIN_POINTS", 0)
        pts = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (float("inf"), 10.0)]
        steiner._iterative_one_steiner(pts, None)
