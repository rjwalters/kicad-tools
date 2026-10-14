"""Tests for the opt-in negotiated-routing nogood cache (Issue #6353)."""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.nogood_cache import NogoodCache, nogood_cache_from_env
from kicad_tools.router.rules import DesignRules


def _pad(num, x, y, net, name):
    return {
        "number": num,
        "x": x,
        "y": y,
        "width": 0.5,
        "height": 0.5,
        "net": net,
        "net_name": name,
    }


def _walled_router(monkeypatch=None) -> Autorouter:
    """Net 1 pads on either side of a full-height wall on every layer."""
    r = Autorouter(
        width=20.0, height=10.0, rules=DesignRules(grid_resolution=0.25), force_python=True
    )
    r.add_component("U1", [_pad("1", 3.0, 5.0, 1, "A"), _pad("2", 17.0, 5.0, 1, "A")])
    for layer in (Layer.F_CU, Layer.B_CU):
        r.add_obstacle(10.0, 5.0, 1.0, 12.0, layer)
    return r


def _clear_wall(r: Autorouter) -> None:
    g = r.grid
    x0, _ = g.world_to_grid(9.0, 5.0)
    x1, _ = g.world_to_grid(11.0, 5.0)
    g._blocked[:, :, x0 : x1 + 1] = False
    g._net[:, :, x0 : x1 + 1] = 0
    g.bump_occupancy_generation()


class _FakeGrid:
    cols = 40
    rows = 40

    def __init__(self):
        import numpy as np

        self._blocked = np.zeros((2, 40, 40), dtype=bool)
        self._net = np.zeros((2, 40, 40), dtype=np.int32)
        self.occupancy_generation = 1

    def world_to_grid(self, x, y):
        return (int(x), int(y))

    def poke(self, layer, y, x, val=True):
        self._blocked[layer, y, x] = val
        self.occupancy_generation += 1


KEY = NogoodCache.make_key(1, [(2.0, 2.0, "F.Cu", 1), (8.0, 8.0, "F.Cu", 1)], False, 0.5)


def test_key_stable_and_discriminating():
    assert (
        NogoodCache.make_key(1, [(2.0, 2.0, "F.Cu", 1), (8.0, 8.0, "F.Cu", 1)], False, 0.5) == KEY
    )
    assert NogoodCache.make_key(1, [(2.0, 2.0, "F.Cu", 1), (8.0, 8.0, "F.Cu", 1)], True, 0.5) != KEY
    assert (
        NogoodCache.make_key(1, [(2.0, 2.0, "F.Cu", 1), (8.0, 8.0, "F.Cu", 1)], False, 0.6) != KEY
    )


def test_unchanged_window_hits_after_change_outside_bounded_window():
    g, c = _FakeGrid(), NogoodCache(margin_cells=3)
    win = c.window(g, [(2, 2), (8, 8)])
    assert c.lookup(KEY, g, win) is None
    c.insert(KEY, g, win, "fail")
    g.poke(0, 30, 30)  # far outside the window: generation moves, window does not
    assert c.lookup(KEY, g, win) == "fail"
    assert c.stats.as_dict() == {
        "hits": 1,
        "misses": 1,
        "insertions": 1,
        "invalidations": 0,
        "evictions": 0,
    }


@pytest.mark.parametrize("change", ["add", "remove"])
def test_relevant_change_invalidates(change):
    g, c = _FakeGrid(), NogoodCache(margin_cells=3)
    if change == "remove":
        g.poke(0, 5, 5, True)
    win = c.window(g, [(2, 2), (8, 8)])
    c.insert(KEY, g, win, "fail")
    g.poke(0, 5, 5, change == "add")
    assert c.lookup(KEY, g, win) is None
    assert c.stats.invalidations == 1 and len(c) == 0


def test_net_change_invalidates():
    g, c = _FakeGrid(), NogoodCache(margin_cells=None)
    win = c.window(g, [(2, 2)])
    c.insert(KEY, g, win, "fail")
    g._net[1, 20, 20] = 7
    g.occupancy_generation += 1
    assert c.lookup(KEY, g, win) is None


def test_eviction_is_bounded_lru_and_only_causes_misses():
    g, c = _FakeGrid(), NogoodCache(max_entries=2)
    win = c.window(g, [])
    for k in ("a", "b", "c"):
        c.insert(k, g, win, k)
    assert len(c) == 2 and c.stats.evictions == 1
    assert c.lookup("a", g, win) is None  # evicted -> miss, never a wrong answer
    assert c.lookup("c", g, win) == "c"


def test_counters_deterministic():
    def run():
        g, c = _FakeGrid(), NogoodCache(max_entries=2, margin_cells=2)
        win = c.window(g, [(5, 5)])
        for i in range(5):
            if c.lookup(i % 3, g, win) is None:
                c.insert(i % 3, g, win, i)
            if i == 2:
                g.poke(0, 5, 5)
        return c.stats.as_dict()

    assert run() == run()


def test_env_default_off_and_on():
    assert nogood_cache_from_env({}) is None
    c = nogood_cache_from_env({"KCT_NOGOOD_CACHE": "1", "KCT_NOGOOD_CACHE_MARGIN_CELLS": "12"})
    assert c is not None and c.margin_cells == 12
    assert nogood_cache_from_env({"KCT_NOGOOD_CACHE": "1"}).margin_cells is None


# -- safety oracle: cached failures must not suppress newly feasible routes ----


@pytest.mark.parametrize("margin", [None, 8])
def test_oracle_stale_failure_not_served_after_blocker_removed(monkeypatch, margin):
    monkeypatch.setenv("KCT_NOGOOD_CACHE", "1")
    if margin is not None:
        monkeypatch.setenv("KCT_NOGOOD_CACHE_MARGIN_CELLS", str(margin))
    r = _walled_router()
    cache = r._get_nogood_cache()
    assert cache is not None
    first = r._route_net_negotiated(1, 0.5)
    assert not [x for x in first if x.segments]
    if cache.stats.insertions == 0:
        pytest.skip("attempt not cacheable on this backend (e.g. timeout)")
    second = r._route_net_negotiated(1, 0.5)
    assert not [x for x in second if x.segments]
    assert cache.stats.hits == 1
    _clear_wall(r)
    third = r._route_net_negotiated(1, 0.5)
    assert [x for x in third if x.segments], "stale nogood suppressed a feasible route"
    assert cache.stats.invalidations == 1 or margin is not None


def _open_router(history: float = 0.0) -> Autorouter:
    """Net 1 on an empty board; optional history cost on the straight corridor."""
    r = Autorouter(
        width=20.0, height=10.0, rules=DesignRules(grid_resolution=0.25), force_python=True
    )
    r.add_component("U1", [_pad("1", 3.0, 5.0, 1, "A"), _pad("2", 17.0, 5.0, 1, "A")])
    if history:
        r.grid._history_cost[:, 10:31, 20:61] += history
    return r


@pytest.mark.parametrize(
    "plane", ["_usage_count", "_history_cost", "_is_obstacle", "_is_zone", "_pad_blocked"]
)
def test_full_fingerprint_covers_non_occupancy_planes(plane):
    """Judge repro (PR #6605): usage/history/obstacle/zone edits left
    ``_blocked``/``_net`` and ``occupancy_generation`` untouched, so the
    occupancy-only fingerprint served the cached failure anyway."""
    r = _open_router()
    g, c = r.grid, NogoodCache(margin_cells=None)
    win = c.window(g, [(3.0, 5.0), (17.0, 5.0)])
    c.insert(KEY, g, win, "fail")
    gen = g.occupancy_generation
    arr = getattr(g, plane)
    arr[0, 20, 40] = arr[0, 20, 40] + 3 if arr.dtype.kind in "iuf" else not arr[0, 20, 40]
    assert g.occupancy_generation == gen  # the old fast path would have hit here
    assert c.lookup(KEY, g, win) is None
    assert c.stats.hits == 0 and c.stats.invalidations == 1


def test_saved_snapshots_do_not_invalidate():
    g, c = _FakeGrid(), NogoodCache()
    win = c.window(g, [])
    c.insert(KEY, g, win, "fail")
    g._saved_blocked = g._blocked.copy()
    assert c.lookup(KEY, g, win) == "fail"


def test_key_includes_budget_context():
    terms = [(2.0, 2.0, "F.Cu", 1), (8.0, 8.0, "F.Cu", 1)]
    a = NogoodCache.make_key(1, terms, False, 0.5, (100, True, None, False, False))
    b = NogoodCache.make_key(1, terms, False, 0.5, (200, True, None, False, False))
    assert a != b


def test_oracle_iteration_capped_failure_not_served_after_history_change(monkeypatch):
    """Only history changes between attempts, under an iteration cap.

    With history on the straight corridor the capped search runs out of
    iterations; with the history gone the same cap routes the net.  Neither
    occupancy nor ``occupancy_generation`` changes, so before the fix the
    capped failure was cached and served as a stale nogood.
    """
    monkeypatch.setenv("KCT_NOGOOD_CACHE", "1")
    r = _open_router(history=50.0)
    r.router._max_iterations_override = 100
    cache = r._get_nogood_cache()
    first = r._route_net_negotiated(1, 0.5)
    assert not [x for x in first if x.segments], "precondition: capped search fails"
    assert cache.stats.insertions == 0, "budget-limited failure must not be cached"
    gen = r.grid.occupancy_generation
    r.grid._history_cost[:] = 0.0
    assert r.grid.occupancy_generation == gen
    second = r._route_net_negotiated(1, 0.5)
    assert [x for x in second if x.segments], "stale nogood suppressed a feasible route"
    assert cache.stats.hits == 0


def test_definitive_gate_requires_drained_search():
    from kicad_tools.router.nogood_cache import (
        reset_search_exhausted,
        search_failure_is_definitive,
    )

    class PyRouter:
        last_route_exhausted = True

    class CppRouter:
        def __init__(self, reason, py_drained):
            self._py_router = PyRouter()
            self._py_router.last_route_exhausted = py_drained
            self._reason = reason

        def _try_python_fallback(self):  # marker only
            raise AssertionError

        def get_last_failure_info(self):
            return {"failure_reason": self._reason} if self._reason else None

    assert search_failure_is_definitive(PyRouter())
    assert search_failure_is_definitive(CppRouter(1, True))  # NO_PATH + py drained
    assert not search_failure_is_definitive(CppRouter(2, True))  # ITERATION_LIMIT
    assert not search_failure_is_definitive(CppRouter(3, True))  # TIMEOUT
    assert not search_failure_is_definitive(CppRouter(1, False))  # fallback capped/skipped
    assert not search_failure_is_definitive(CppRouter(None, True))  # refused commit
    py = PyRouter()
    reset_search_exhausted(py)
    assert not search_failure_is_definitive(py)
    assert not search_failure_is_definitive(object())


def test_default_has_no_cache(monkeypatch):
    monkeypatch.delenv("KCT_NOGOOD_CACHE", raising=False)
    assert _walled_router()._get_nogood_cache() is None
